"""Lodestone GUI: JSON API + static UI on its own port (default 3001).

Runs in the same asyncio process as the relay. Never serves the GUI on the
relay port; the relay port is websocket-only.
"""
import asyncio
import json
import os
import time

from aiohttp import web

from . import config as lconfig
from . import schnorr
from .npub import npub_decode, npub_encode
from .store import event_id

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
FEED_KINDS = [1, 6, 20, 21, 22, 1068, 30023]
RANGES = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400,
          '90d': 90 * 86400, '1y': 365 * 86400, '2y': 2 * 365 * 86400,
          'all': None}

# GUI-editable fields, as (section, key, type). Anything not listed here
# cannot be changed over HTTP.
EDITABLE = {
    'relay': ['name', 'description'],
    'graph': ['depth', 'refresh_hours', 'indexer_relays'],
    'aggregation': ['interval_minutes', 'pow_threshold', 'fallback_relays'],
    'limits': list(lconfig.DEFAULTS['limits'].keys()),
    'rate_limits': list(lconfig.DEFAULTS['rate_limits'].keys()),
    'timeouts': list(lconfig.DEFAULTS['timeouts'].keys()),
    'security': list(lconfig.DEFAULTS['security'].keys()),
    'retention': ['months', 'owner_kept_indefinitely'],
}


def _onion_address() -> str:
    for env in ('TOR_HOSTNAME', 'LODESTONE_ONION'):
        v = os.environ.get(env)
        if v:
            return v.strip()
    for d in ('/var/lib/tor/hidden_service', '/var/lib/tor',
              '/etc/tor/hidden_service'):
        try:
            for name in sorted(os.listdir(d)):
                if name.endswith('.onion'):
                    continue
                path = os.path.join(d, 'hostname')
                if os.path.isfile(path):
                    with open(path) as f:
                        v = f.read().strip()
                    if v:
                        return v
        except OSError:
            continue
    return ''


def _local_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


class Gui:
    def __init__(self, store, cfg, retention, crawler=None, aggregator=None,
                 log=None):
        self.store = store
        self.cfg = cfg
        self.retention = retention
        self.crawler = crawler
        self.aggregator = aggregator
        self.log = log or (lambda *a: None)
        self.started = time.time()
        self.last_purge = None
        self.runner = None

    # ------------------------------------------------------------------ NIP-86
    def _nip86_methods(self) -> list[str]:
        return ['supportedmethods', 'listbannedpubkeys', 'banpubkey',
                'unbanpubkey', 'listallowedpubkeys', 'allowpubkey',
                'unallowpubkey', 'changerelayname', 'changerelaydescription',
                'allowkind', 'disallowkind', 'listallowedkinds',
                'listdisallowedkinds', 'listeventsneedingmoderation',
                'banevent']

    def _verify_nip98(self, request) -> str | None:
        """NIP-98 auth: kind 27235 event in the Authorization header.

        Returns the authenticated pubkey hex, or None.
        """
        import base64
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Nostr '):
            return None
        try:
            ev = json.loads(base64.b64decode(auth[6:]))
        except Exception:
            return None
        if ev.get('kind') != 27235:
            return None
        tags = ev.get('tags', [])
        u = next((t[1] for t in tags if t[0] == 'u'), None)
        m = next((t[1] for t in tags if t[0] == 'method'), None)
        if not u or m != 'POST':
            return None
        try:
            if event_id(ev).hex() != ev.get('id') or not schnorr.verify(
                    bytes.fromhex(ev['pubkey']), bytes.fromhex(ev['id']),
                    bytes.fromhex(ev['sig'])):
                return None
        except (ValueError, KeyError, TypeError):
            return None
        return ev['pubkey']

    async def api_nip86(self, request):
        """NIP-86 management RPC. Owner-only via NIP-98."""
        if 'application/nostr+json+rpc' not in \
                request.headers.get('Content-Type', ''):
            return web.json_response(
                {'error': 'Content-Type must be application/nostr+json+rpc'},
                status=400)
        try:
            body = await request.json()
            method = body.get('method', '')
            params = body.get('params', [])
        except (json.JSONDecodeError, ValueError):
            return web.json_response({'error': 'invalid JSON'}, status=400)

        caller = self._verify_nip98(request)
        if not caller:
            return web.json_response(
                {'error': 'restricted: valid NIP-98 auth required'},
                status=401)
        if caller != self.cfg['owner']['pubkey']:
            return web.json_response(
                {'error': 'restricted: owner only'}, status=403)

        store = self.store
        try:
            if method == 'supportedmethods':
                return web.json_response(
                    {'result': self._nip86_methods()})
            if method in ('listbannedpubkeys', 'listallowedpubkeys'):
                lst = 'blacklist' if method.startswith('listbanned') \
                    else 'whitelist'
                out = [{'pubkey': pk.hex()} for pk in store.members(lst)]
                return web.json_response({'result': out})
            if method in ('banpubkey', 'allowpubkey'):
                lst = 'blacklist' if method == 'banpubkey' else 'whitelist'
                other = 'whitelist' if lst == 'blacklist' else 'blacklist'
                store.remove_member(other, params[0])
                store.add_member(lst, params[0])
                return web.json_response({'result': True})
            if method in ('unbanpubkey', 'unallowpubkey'):
                lst = 'blacklist' if method == 'unbanpubkey' else 'whitelist'
                store.remove_member(lst, params[0])
                return web.json_response({'result': True})
            if method == 'changerelayname':
                self.cfg['relay']['name'] = str(params[0])
                lconfig.save(self.cfg)
                return web.json_response({'result': True})
            if method == 'changerelaydescription':
                self.cfg['relay']['description'] = str(params[0])
                lconfig.save(self.cfg)
                return web.json_response({'result': True})
            if method == 'allowkind':
                k = int(params[0])
                if k not in self.cfg['kinds']['allowed']:
                    self.cfg['kinds']['allowed'].append(k)
                    lconfig.save(self.cfg)
                return web.json_response({'result': True})
            if method == 'disallowkind':
                k = int(params[0])
                self.cfg['kinds']['allowed'] = [
                    x for x in self.cfg['kinds']['allowed'] if x != k]
                lconfig.save(self.cfg)
                return web.json_response({'result': True})
            if method == 'listallowedkinds':
                return web.json_response(
                    {'result': list(self.cfg['kinds']['allowed'])})
            if method == 'listdisallowedkinds':
                return web.json_response({'result': []})
            if method == 'listeventsneedingmoderation':
                raws = list(store.query([{'kinds': [1984], 'limit': 200}]))
                out = []
                for r in raws:
                    ev = json.loads(r)
                    out.append({'id': ev['id'],
                                'reason': ev.get('content', '')})
                return web.json_response({'result': out})
            if method == 'banevent':
                eid = bytes.fromhex(params[0])
                with store.lock:
                    store.db.execute('DELETE FROM tags WHERE event_id = ?',
                                     (eid,))
                    store.db.execute('DELETE FROM replaceable '
                                     'WHERE event_id = ?', (eid,))
                    store.db.execute('DELETE FROM events WHERE id = ?', (eid,))
                    store.db.execute('INSERT OR IGNORE INTO deleted '
                                     '(event_id, time) VALUES (?, ?)',
                                     (eid, int(time.time())))
                    store.db.commit()
                return web.json_response({'result': True})
        except (IndexError, ValueError, TypeError):
            return web.json_response({'error': 'invalid parameters'},
                                     status=400)
        return web.json_response({'error': f'unknown method {method}'},
                                 status=400)

    # ------------------------------------------------------------- lifecycle
    async def start(self, host: str, port: int):
        app = web.Application()
        app.router.add_post('/nip86', self.api_nip86)
        app.router.add_get('/', self.index)
        app.router.add_get('/api/status', self.api_status)
        app.router.add_get('/api/config', self.api_get_config)
        app.router.add_post('/api/config', self.api_set_config)
        app.router.add_get('/api/feed', self.api_feed)
        app.router.add_get('/api/reports', self.api_reports)
        app.router.add_post('/api/reports/action', self.api_report_action)
        app.router.add_post('/api/blacklist', self.api_list_add)
        app.router.add_delete('/api/blacklist', self.api_list_remove)
        app.router.add_post('/api/whitelist', self.api_list_add)
        app.router.add_delete('/api/whitelist', self.api_list_remove)
        app.router.add_get('/api/filters', self.api_get_filters)
        app.router.add_post('/api/filters', self.api_set_filters)
        app.router.add_post('/api/purge', self.api_purge)
        app.router.add_post('/api/crawl', self.api_crawl)
        app.router.add_post('/api/restart', self.api_restart)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        self.log(f'GUI listening on http://{host}:{port}')

    # ------------------------------------------------------------------ html
    async def index(self, request):
        with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
            body = f.read()
        return web.Response(body=body, content_type='text/html')

    # ---------------------------------------------------------------- status
    async def api_status(self, request):
        relay_port = self.cfg['relay']['port']
        ip = _local_ip()
        with self.store.lock:
            n_events = self.store.db.execute(
                'SELECT COUNT(*) FROM events').fetchone()[0]
            n_graph = self.store.db.execute(
                'SELECT COUNT(*) FROM graph').fetchone()[0]
            last_crawl = self.store.db.execute(
                "SELECT last_ts FROM relay_sync WHERE url = '__graph__'"
            ).fetchone()
            last_crawl = last_crawl[0] if last_crawl else None
        return web.json_response({
            'relay_endpoint': f"ws://{ip}:{relay_port}",
            'onion_address': _onion_address(),
            'events': n_events,
            'graph_size': n_graph,
            'last_crawl': last_crawl,
            'uptime_s': int(time.time() - self.started),
            'last_purge': self.last_purge,
            'relay_name': self.cfg['relay']['name'],
            'owner_set': bool(self.cfg['owner']['pubkey']),
            'last_poll': getattr(self.aggregator, 'last_poll', None),
        })

    # ---------------------------------------------------------------- config
    async def api_get_config(self, request):
        out = {s: dict(self.cfg[s]) for s in EDITABLE}
        out['filters'] = {k: list(v)
                          for k, v in self.cfg['filters'].items()}
        out['kinds'] = list(self.cfg['kinds']['allowed'])
        out['blacklist_npubs'] = [npub_encode(pk.hex()) for pk in
                                  sorted(self.store.members('blacklist'))]
        out['whitelist_npubs'] = [npub_encode(pk.hex()) for pk in
                                  sorted(self.store.members('whitelist'))]
        if self.cfg['owner']['pubkey']:
            out['owner_npub'] = npub_encode(self.cfg['owner']['pubkey'])
        else:
            out['owner_npub'] = ''
        return web.json_response(out)

    async def api_set_config(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)
        changed = []
        for section, keys in EDITABLE.items():
            if section not in body:
                continue
            for key in keys:
                if key in body[section]:
                    val = body[section][key]
                    if isinstance(val, dict) and key in self.cfg[section]:
                        val = {**self.cfg[section][key], **val}
                    self.cfg[section][key] = val
                    changed.append(f'{section}.{key}')
        # owner npub: accept npub or hex, store hex
        if 'owner' in body and 'npub' in body['owner']:
            hex_pk = npub_decode(body['owner']['npub'])
            if not hex_pk:
                return web.json_response({'error': 'invalid npub'},
                                         status=400)
            self.cfg['owner']['pubkey'] = hex_pk
            changed.append('owner.pubkey')
        # kinds list: replace wholesale
        if 'kinds' in body and isinstance(body['kinds'], list):
            try:
                self.cfg['kinds']['allowed'] = sorted(
                    {int(k) for k in body['kinds']})
                changed.append('kinds')
            except (TypeError, ValueError):
                return web.json_response({'error': 'bad kind list'},
                                         status=400)
        if not changed:
            return web.json_response({'error': 'nothing to change'},
                                     status=400)
        lconfig.save(self.cfg)
        return web.json_response({'ok': True, 'changed': changed})

    # ------------------------------------------------------------------ feed
    async def api_feed(self, request):
        qs = request.rel_url.query
        range_key = qs.get('range', 'all')
        since = RANGES.get(range_key, RANGES['all'])
        if since is not None:
            since = int(time.time()) - since
        sort = qs.get('sort', 'newest')
        show_owner = qs.get('owner', '1') == '1'
        limit = min(max(int(qs.get('limit', 50)), 1), 200)
        offset = max(int(qs.get('offset', 0)), 0)
        owner = self._owner_bytes()

        q = ('SELECT id, pubkey, kind, created_at, content FROM events '
             'WHERE kind IN (%s)' % ','.join('?' * len(FEED_KINDS)))
        params: list = list(FEED_KINDS)
        if since is not None:
            q += ' AND created_at >= ?'
            params.append(since)
        if not show_owner and owner:
            q += ' AND pubkey != ?'
            params.append(owner)
        q += ' ORDER BY created_at DESC LIMIT 2000'
        with self.store.lock:
            rows = self.store.db.execute(q, params).fetchall()
        if sort in ('oldest', 'newest'):
            rows.sort(key=lambda r: r[3], reverse=(sort == 'newest'))
        else:
            counts = self._interaction_counts([r[0].hex() for r in rows])
            def total(r):
                c = counts.get(r[0].hex(), {})
                return (c.get('zaps', 0) + c.get('replies', 0)
                        + c.get('reactions', 0))
            if sort == 'zapped':
                key = lambda r: counts.get(r[0].hex(), {}).get('zaps', 0)
            elif sort == 'replies':
                key = lambda r: counts.get(r[0].hex(), {}).get('replies', 0)
            elif sort == 'reactions':
                key = lambda r: (counts.get(r[0].hex(), {})
                                 .get('reactions', 0))
            else:   # interactions
                key = total
            rows.sort(key=lambda r: (key(r), r[3]), reverse=True)
        page = rows[offset:offset + limit]

        counts = self._interaction_counts([r[0].hex() for r in page])
        notes = []
        for rid, pk, kind, created_at, content in page:
            c = counts.get(rid.hex(), {})
            notes.append({
                'id': rid.hex(),
                'pubkey': pk.hex(),
                'kind': kind,
                'created_at': created_at,
                'content': content or '',
                'replies_count': c.get('replies', 0),
                'reactions_count': c.get('reactions', 0),
                'zaps_count': c.get('zaps', 0),
                'interactions_count': (c.get('replies', 0)
                                       + c.get('reactions', 0)
                                       + c.get('zaps', 0)),
                **{k: v for k, v in (self._profile(pk) or {}).items()
                   if k in ('name', 'display_name', 'picture')},
            })
        return web.json_response({'notes': notes, 'total': len(rows)})

    def _interaction_counts(self, hex_ids):
        counts: dict[str, dict] = {}
        if not hex_ids:
            return counts
        from .store import _chunks
        with self.store.lock:
            for chunk in _chunks(hex_ids, 400):
                marks = ','.join('?' * len(chunk))
                rows = self.store.db.execute(
                    'SELECT t.value, e.kind, COUNT(*) FROM tags t '
                    'JOIN events e ON e.id = t.event_id '
                    f'WHERE t.name = "e" AND e.kind IN (1, 7, 9735) '
                    f'AND t.value IN ({marks}) GROUP BY t.value, e.kind',
                    chunk).fetchall()
                for value, kind, n in rows:
                    c = counts.setdefault(value, {})
                    if kind == 1:
                        c['replies'] = c.get('replies', 0) + n
                    elif kind == 7:
                        c['reactions'] = c.get('reactions', 0) + n
                    else:
                        c['zaps'] = c.get('zaps', 0) + n
        return counts

    def _profile(self, pk: bytes) -> dict | None:
        with self.store.lock:
            row = self.store.db.execute(
                'SELECT content FROM events WHERE kind = 0 AND pubkey = ? '
                'ORDER BY created_at DESC LIMIT 1', (pk,)).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    # --------------------------------------------------------------- reports
    async def api_reports(self, request):
        with self.store.lock:
            rows = self.store.db.execute(
                'SELECT id, pubkey, created_at, content FROM events '
                'WHERE kind = 1984 ORDER BY created_at DESC LIMIT 200'
            ).fetchall()
        reports = []
        for rid, pk, created_at, content in rows:
            with self.store.lock:
                targets = self.store.db.execute(
                    'SELECT value FROM tags WHERE event_id = ? AND name = "e"',
                    (rid,)).fetchall()
            reports.append({
                'id': rid.hex(), 'pubkey': pk.hex(),
                'created_at': created_at, 'content': content or '',
                'targets': [t[0] for t in targets],
            })
        return web.json_response({'reports': reports})

    async def api_report_action(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)
        action = body.get('action')
        report_id = body.get('id', '')
        try:
            rid = bytes.fromhex(report_id)
        except ValueError:
            return web.json_response({'error': 'bad id'}, status=400)
        if action == 'dismiss':
            # stop surfacing: add the report id to deleted (keeps NIP-09
            # semantics — id is filtered everywhere)
            with self.store.lock:
                self.store.db.execute(
                    'INSERT OR IGNORE INTO deleted (event_id, time) '
                    'VALUES (?, ?)', (rid, int(time.time())))
                self.store.db.commit()
            return web.json_response({'ok': True})
        if action == 'delete_target':
            with self.store.lock:
                rows = self.store.db.execute(
                    'SELECT value FROM tags WHERE event_id = ? '
                    'AND name = "e"', (rid,)).fetchall()
                for (target_hex,) in rows:
                    self.store.db.execute(
                        'DELETE FROM events WHERE id = ?',
                        (bytes.fromhex(target_hex),))
                    self.store.db.execute(
                        'INSERT OR IGNORE INTO deleted (event_id, time) '
                        'VALUES (?, ?)',
                        (bytes.fromhex(target_hex), int(time.time())))
                self.store.db.execute(
                    'INSERT OR IGNORE INTO deleted (event_id, time) '
                    'VALUES (?, ?)', (rid, int(time.time())))
                self.store.db.commit()
            return web.json_response({'ok': True})
        return web.json_response({'error': 'unknown action'}, status=400)

    # ----------------------------------------------------------------- lists
    async def api_list_add(self, request):
        return await self._list_edit(request, add=True)

    async def api_list_remove(self, request):
        return await self._list_edit(request, add=False)

    async def _list_edit(self, request, add: bool):
        lst = 'blacklist' if request.path.endswith('blacklist') \
            else 'whitelist'
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)
        hex_pk = npub_decode(body.get('npub', ''))
        if not hex_pk:
            return web.json_response({'error': 'invalid npub'}, status=400)
        if add:
            self.store.add_member(lst, hex_pk)
        else:
            self.store.remove_member(lst, hex_pk)
        return web.json_response({'ok': True})

    # --------------------------------------------------------------- filters
    async def api_get_filters(self, request):
        return web.json_response({k: list(v)
                                  for k, v in self.cfg['filters'].items()})

    async def api_set_filters(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)
        if 'words' in body and isinstance(body['words'], list):
            self.cfg['filters']['words'] = [str(w) for w in body['words']]
        if 'hashtags' in body and isinstance(body['hashtags'], list):
            self.cfg['filters']['hashtags'] = [str(h)
                                               for h in body['hashtags']]
        lconfig.save(self.cfg)
        return web.json_response({'ok': True})

    # ------------------------------------------------------------------ purge
    async def api_purge(self, request):
        stats = await asyncio.to_thread(self.retention.purge)
        self.last_purge = {**stats, 'at': int(time.time())}
        return web.json_response(self.last_purge)

    async def api_restart(self, request):
        """Exit the process so the container supervisor restarts it.

        umbrelOS runs the app with docker `restart: on-failure`, so a
        non-zero exit brings the whole relay (and every worker) back up
        reading the saved config. The short delay lets the HTTP response
        reach the browser first.
        """
        async def _exit():
            await asyncio.sleep(0.8)
            print('restart requested via GUI: exiting (supervisor will '
                  'restart the container)', flush=True)
            os._exit(1)
        asyncio.get_event_loop().create_task(_exit())
        return web.json_response({'restarting': True})

    async def api_crawl(self, request):
        if not self.crawler:
            return web.json_response({'error': 'no crawler'}, status=503)
        stats = await self.crawler.crawl()
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR REPLACE INTO relay_sync (url, last_ts) "
                "VALUES ('__graph__', ?)", (int(time.time()),))
            self.store.db.commit()
        return web.json_response(stats)

    def _owner_bytes(self) -> bytes | None:
        hex_pk = self.cfg['owner']['pubkey']
        if hex_pk and len(hex_pk) == 64:
            try:
                return bytes.fromhex(hex_pk)
            except ValueError:
                pass
        return None
