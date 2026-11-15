"""Lodestone relay server: NIP-01 WebSocket protocol on its own port.

Handled here (v1): EVENT/REQ/CLOSE/COUNT, NIP-09 deletions, NIP-11 relay
info document, NIP-13 PoW checks, NIP-20 OK message prefixes, NIP-42 auth
(optional gate), NIP-70 protected-event handling. Subscription lifecycle
follows the asyncio `websockets` library (>= 13).
"""
import asyncio
import hashlib
import ipaddress
import json
import time
from collections import defaultdict, deque

from websockets.asyncio.server import serve, Response
from websockets.datastructures import Headers
from websockets.http11 import Request

from . import schnorr
from .store import EventStore, canonical_event_json, event_id, is_ephemeral


def validate_pow(eid_hex: str, difficulty: int) -> bool:
    """NIP-13: count leading zero bits of the event id."""
    if difficulty <= 0:
        return True
    bits = 0
    for ch in eid_hex:
        v = int(ch, 16)
        if v == 0:
            bits += 4
        else:
            bits += (4 - v.bit_length())
            break
    else:
        bits = 256
    return bits >= difficulty


def leading_zero_bits(eid_hex: str) -> int:
    bits = 0
    for ch in eid_hex:
        v = int(ch, 16)
        if v == 0:
            bits += 4
        else:
            bits += (4 - v.bit_length())
            break
    else:
        bits = 256
    return bits


class Relay:
    def __init__(self, store: EventStore, config: dict, onion_host: str = ''):
        self.store = store
        self.cfg = config
        self.onion_host = onion_host  # e.g. 'xxxx.onion' for NIP-11/uptime info
        self.server = None
        self._subs: dict[str, list[dict]] = {}      # conn -> active filters
        self._events_per_ip: dict[str, deque] = defaultdict(deque)
        self._conns_per_ip: dict[str, int] = defaultdict(int)
        self._challenge_for = {}                    # conn -> challenge string

    # ------------------------------------------------------------- admission
    def admit(self, ev: dict, ip: str,
              authed_pk: str | None = None) -> tuple[bool, str]:
        """Full relay-side admission. Returns (ok, message) per NIP-20.

        authed_pk: the NIP-42-authenticated pubkey of this connection, if it
        has completed an AUTH flow (required for NIP-70 protected events).
        """
        limits = self.cfg['limits']
        if not isinstance(ev, dict):
            return (False, 'invalid:event must be an object')
        for k in ('id', 'pubkey', 'created_at', 'kind', 'tags', 'content', 'sig'):
            if k not in ev:
                return (False, f'invalid:missing {k}')
        if not (isinstance(ev['id'], str) and len(ev['id']) == 64
                and isinstance(ev['pubkey'], str) and len(ev['pubkey']) == 64
                and isinstance(ev['created_at'], int)
                and isinstance(ev['kind'], int)
                and isinstance(ev['tags'], list)
                and isinstance(ev['content'], str)
                and isinstance(ev['sig'], str) and len(ev['sig']) == 128):
            return (False, 'invalid:malformed event fields')
        if ev['kind'] not in self.cfg['kinds']['allowed'] \
                and not is_ephemeral(ev['kind']):
            return (False, f'blocked:kind {ev["kind"]} not allowed on this relay')
        if len(ev['content']) > limits['max_content_length']:
            return (False, 'invalid:content too long')
        if len(ev['tags']) > limits['max_event_tags']:
            return (False, 'invalid:too many tags')

        # per-IP event rate limit
        now = time.time()
        bucket = self._events_per_ip[ip]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= self.cfg['rate_limits']['events_per_minute_per_ip']:
            return (False, 'blocked:rate limited')

        # relay-wide PoW
        if not validate_pow(ev['id'], limits['min_pow_difficulty']):
            return (False, 'pow:insufficient difficulty')

        # signature and id integrity
        try:
            computed = event_id(ev).hex()
        except (TypeError, ValueError, KeyError):
            return (False, 'invalid:malformed event')
        if computed != ev['id']:
            return (False, 'invalid:id does not match event')
        try:
            ok = schnorr.verify(bytes.fromhex(ev['pubkey']),
                                bytes.fromhex(ev['id']),
                                bytes.fromhex(ev['sig']))
        except ValueError:
            return (False, 'invalid:malformed signature')
        if not ok:
            return (False, 'invalid:signature verification failed')

        # NIP-70 protected events: the ["-"] tag means only the author may
        # publish, and only over an authenticated connection
        if any(isinstance(t, list) and t[:1] == ['-'] and len(t) == 1
               for t in ev['tags']):
            if authed_pk != ev['pubkey']:
                return (False, 'auth-required: this event may only be '
                        'published by its author')

        # membership policy
        author = bytes.fromhex(ev['pubkey'])
        owner_hex = self.cfg['owner']['pubkey']
        owner = bytes.fromhex(owner_hex) if owner_hex and len(owner_hex) == 64 else None
        if author == owner:
            pass
        elif author in self.store.members('blacklist'):
            return (False, 'blocked:you are blacklisted from this relay')
        elif author in self.store.members('whitelist') \
                or self.store.in_graph(author):
            pass
        else:
            return (False, 'restricted:only social-graph members may publish here')

        # word/hashtag filters
        content_low = ev['content'].lower()
        for w in self.cfg['filters']['words']:
            if w and w.lower() in content_low:
                return (False, 'blocked:contains a filtered word')
        for h in self.cfg['filters']['hashtags']:
            if h:
                h = h.lstrip('#').lower()
                # match a t-tag OR the hashtag appearing in the content text
                if ('#' + h) in content_low:
                    return (False, 'blocked:contains a filtered hashtag')
                for tag in ev['tags']:
                    if (isinstance(tag, list) and len(tag) >= 2
                            and tag[0] == 't'
                            and isinstance(tag[1], str)
                            and tag[1].lstrip('#').lower() == h):
                        return (False, 'blocked:contains a filtered hashtag')

        return (True, '')

    # ------------------------------------------------------------- broadcast
    def broadcast(self, ev: dict):
        """Deliver a stored event to every live subscription that matches."""
        raw = json.dumps(ev, separators=(',', ':'), ensure_ascii=False)
        for ws, subs in list(self._subs.items()):
            for sub in subs:
                if _match_filter(ev, sub['filter']):
                    try:
                        asyncio.get_running_loop().create_task(ws.send(
                            f'["EVENT",{json.dumps(sub["subid"])},{raw}]'))
                    except Exception:
                        pass
                    break  # one delivery per subscription

    # ---------------------------------------------------------------- server
    async def handle(self, ws):
        ip = ''
        peer = ws.remote_address
        if peer:
            ip = peer[0]
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            ip = 'unknown'
        self._conns_per_ip[ip] += 1
        challenge = hashlib.sha256(
            f'{time.time_ns()}-{id(ws)}'.encode()).hexdigest()
        self._challenge_for[ws] = challenge
        self._subs[ws] = []
        ws._subids = {}
        try:
            if self._conns_per_ip[ip] > self.cfg['rate_limits']['connections_per_ip']:
                await ws.close(code=1013, reason='too many connections')
                return
            await ws.send(json.dumps(['AUTH', challenge]))  # NIP-42 challenge
            async for message in ws:
                replies = await self._handle_message(ws, message, ip)
                for reply in replies:
                    await ws.send(reply)
        except Exception:
            pass
        finally:
            self._subs.pop(ws, None)
            self._challenge_for.pop(ws, None)
            self._conns_per_ip[ip] -= 1

    async def _handle_message(self, ws, message: str, ip: str) -> list[str]:
        try:
            arr = json.loads(message)
            if not isinstance(arr, list) or not arr:
                return []
            mtype = arr[0]
        except (json.JSONDecodeError, UnicodeDecodeError):
            return [json.dumps(['NOTICE', 'invalid:malformed JSON'])]

        if mtype == 'EVENT':
            ev = arr[-1] if len(arr) in (2, 3) else None
            if not isinstance(ev, dict):
                return [json.dumps(['OK', '', False, 'invalid:bad EVENT message'])]
            ok, msg = self.admit(ev, ip, getattr(ws, '_authed', None))
            if ok:
                if is_ephemeral(ev['kind']):
                    # NIP-16: broadcast but never store
                    self._events_per_ip[ip].append(time.time())
                    self.broadcast(ev)
                    return [json.dumps(['OK', ev['id'], True, ''])]
                stored, smsg = self.store.store_event(ev)
                if stored:
                    self._events_per_ip[ip].append(time.time())
                    self.broadcast(ev)
                    return [json.dumps(['OK', ev['id'], True, ''])]
                return [json.dumps(['OK', ev['id'], False, smsg])]
            return [json.dumps(['OK', ev.get('id', ''), False, msg])]

        if mtype == 'AUTH':
            # NIP-42: verify kind 22242 event carrying our challenge
            try:
                ev = arr[1]
                challenges = [t[1] for t in ev.get('tags', []) if t[0] == 'challenge']
                if (ev.get('kind') == 22242 and challenges
                        and challenges[0] == self._challenge_for.get(ws)):
                    computed = event_id(ev).hex()
                    if (computed == ev.get('id') and schnorr.verify(
                            bytes.fromhex(ev['pubkey']),
                            bytes.fromhex(ev['id']),
                            bytes.fromhex(ev['sig']))):
                        ws._authed = ev['pubkey']
                return []
            except Exception:
                return []

        if mtype == 'REQ':
            if len(arr) < 3:
                return [json.dumps(['NOTICE', 'invalid:bad REQ'])]
            subid = arr[1]
            filters = arr[2:]
            limits = self.cfg['limits']
            if not isinstance(subid, str) or not subid \
                    or len(subid) > limits['max_subid_length']:
                return [json.dumps(['NOTICE', 'invalid:bad subscription id'])]
            if not isinstance(filters, list) or len(filters) > limits['max_filters']:
                return [json.dumps(['NOTICE', 'invalid:too many filters'])]
            for f in filters:
                if not isinstance(f, dict):
                    return [json.dumps(['NOTICE', 'invalid:filter must be object'])]
                if 'limit' in f and isinstance(f['limit'], int) \
                        and f['limit'] > limits['max_limit']:
                    return [json.dumps(['CLOSED', subid, 'error:limit too large'])]
            # register for live broadcasts
            self._subs[ws] = [{'subid': subid, 'filter': dict(f)}
                              for f in filters]
            # stream stored results, deduped across filters
            results = []
            seen_ids = set()
            try:
                for raw in self.store.query(filters):
                    eid = json.loads(raw)['id']
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        results.append(raw)
            except Exception as e:
                self._subs.pop(ws, None)
                return [json.dumps(['NOTICE', f'invalid:{e}'])]
            out = [f'["EVENT",{json.dumps(subid)},{raw}]' for raw in results]
            out.append(f'["EOSE",{json.dumps(subid)}]')
            return out

        if mtype == 'CLOSE':
            if len(arr) >= 2:
                subid = arr[1]
                self._subs[ws] = [s for s in self._subs.get(ws, [])
                                  if s['subid'] != subid]
            return []

        if mtype == 'COUNT':
            # NIP-45 COUNT (v1)
            subid = arr[1]
            filters = arr[2:]
            try:
                n = self.store.count(filters)
                return [json.dumps(['COUNT', subid, {'count': n}])]
            except Exception as e:
                return [json.dumps(['NOTICE', f'invalid:{e}'])]

        return [json.dumps(['NOTICE', 'unknown:unrecognized message type'])]

    # ---------------------------------------------------------------- NIP-11
    def nip11_document(self, request_host: str) -> dict:
        cfg = self.cfg
        return {
            'name': cfg['relay']['name'],
            'description': cfg['relay']['description'],
            'pubkey': cfg['owner']['pubkey'],
            'contact': '',
            'supported_nips': [1, 2, 9, 11, 13, 16, 33, 40, 42, 45, 50, 51,
                               65, 70, 86],
            'software': 'https://github.com/Dikaios1517/lodestone',
            'version': '0.1.6',
            'onion': self.onion_host,
        }

    async def process_request(self, connection, request: Request):
        """Serve the NIP-11 relay info document over HTTP on the relay port.

        NIP-86 management RPC is served by the GUI server instead: the
        websockets handshake parser never reads POST bodies, so the relay
        port cannot host an HTTP RPC.
        """
        accept = request.headers.get('Accept', '')
        if 'application/nostr+json' not in accept:
            return None  # not a NIP-11 request: close as a bad WS handshake
        doc = self.nip11_document(request.headers.get('Host', ''))
        body = json.dumps(doc).encode()
        return Response(200, 'OK', Headers([
            ('Content-Type', 'application/nostr+json'),
            ('Content-Length', str(len(body))),
        ]), body)

    async def start(self, host: str = None, port: int = None):
        host = host or self.cfg['relay']['bind']
        port = port or self.cfg['relay']['port']
        self.server = await serve(
            self.handle, host, port,
            process_request=self.process_request,
            max_size=self.cfg['limits']['max_message_length'],
            max_queue=32,
        )

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


def _limit0(f: dict) -> dict:
    return {k: v for k, v in f.items() if not k.startswith('_')}


def _match_filter(ev: dict, f: dict) -> bool:
    """In-memory NIP-01 filter match, used for live broadcast."""
    if 'ids' in f and ev['id'] not in f['ids']:
        return False
    if 'authors' in f and ev['pubkey'] not in f['authors']:
        return False
    if 'kinds' in f and ev['kind'] not in f['kinds']:
        return False
    if 'since' in f and ev['created_at'] < f['since']:
        return False
    if 'until' in f and ev['created_at'] > f['until']:
        return False
    for key, vals in f.items():
        if key.startswith('#') and isinstance(vals, list):
            name = key[1:]
            got = [t[1] for t in ev['tags']
                   if isinstance(t, list) and t and t[0] == name
                   and len(t) >= 2]
            if not any(v in got for v in vals):
                return False
    return True
