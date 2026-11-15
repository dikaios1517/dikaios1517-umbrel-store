#!/usr/bin/env python3
"""Feed engine for the Wisp management GUI (stdlib only).

Lists notes stored on this relay with sorting (newest, oldest, most zapped
in the last X days), and per-note detail views that pull in replies and zap
receipts which were never stored here.

Crawling model, for a note by author OP:
  1. OP's kind 10002 relay list (read relays) and kind 3 contact list are
     looked up locally first (relay LMDB), then in the crawl cache, then
     fetched from the bootstrap relays.
  2. OP's read relays are queried for replies (kind 1 e-tagged to the note)
     and zap receipts (kind 9735 e-tagged to the note).
  3. Replies from authors outside OP's web of trust (OP's follows and their
     follows' follows, built from kind 3 contact lists) are dropped unless
     the reply carries NIP-13 proof-of-work >= the configured threshold.
     Zaps are always included - they have a monetary cost.

Crawled events are cached in SQLite (feed-cache.db) next to wisp.toml; the
relay's own LMDB store is never written by this module.
"""

import base64
import ctypes
import hashlib
import json
import os
import queue
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.parse

import purge

# MDB_cursor_op enum positions (see purge.py lesson: NOT the #define list)
MDB_FIRST = 0
MDB_NEXT = 8
MDB_LAST = 6
MDB_PREV = 12
MDB_SET_RANGE = 17

DEFAULT_BOOTSTRAP = 'wss://relay.damus.io,wss://nos.lol,wss://relay.primal.net'
KIND_NOTE = 1
KIND_ZAP = 9735

REPLIES_PER_RELAY = 100
ZAPS_PER_RELAY = 200
RELAYS_PER_NOTE = 4
AUTHORS_PER_REQ = 100
MAX_PROFILE_RELAYS = 8   # author-relay connections per profile enrichment run
MAX_CANDIDATES = 5000


def leading_zero_bits(hexid):
    """NIP-13 difficulty: leading zero bits of the event id hex string."""
    total = 0
    for ch in hexid:
        n = int(ch, 16)
        if n == 0:
            total += 4
        else:
            total += 4 - n.bit_length()
            break
    return total


def zap_amount_msat(tags):
    """NIP-57 zap receipt amount in millisats (0 when absent).

    Receipts normally carry an 'amount' tag; older ones embed the zap
    request in a 'description' tag with the amount inside it."""
    for t in tags:
        if t and t[0] == 'amount':
            try:
                return int(t[1])
            except (ValueError, IndexError):
                pass
    for t in tags:
        if t and t[0] == 'description':
            try:
                req = json.loads(t[1])
                for rt in req.get('tags', []):
                    if rt[0] == 'amount':
                        return int(rt[1])
            except (ValueError, TypeError, IndexError, KeyError):
                pass
    return 0


def read_relays_from_10002(event_json):
    """Read relay URLs from a kind 10002 event."""
    out = []
    for t in event_json.get('tags', []):
        if t and t[0] == 'r' and len(t) >= 2 and \
                str(t[1]).startswith(('ws://', 'wss://')):
            marker = t[2] if len(t) > 2 else ''
            if marker in ('', 'read'):
                out.append(t[1])
    return out


def follows_from_kind3(event_json):
    """Hex pubkeys from a kind 3 contact list's p-tags."""
    out = []
    for t in event_json.get('tags', []):
        if t and t[0] == 'p' and len(t) >= 2:
            v = str(t[1]).lower()
            if len(v) == 64 and all(c in '0123456789abcdef' for c in v):
                out.append(v)
    return out


def profile_from_kind0(event_json):
    name = pic = None
    if event_json is None:
        return None, None
    try:
        meta = json.loads(event_json.get('content') or '{}')
        name = meta.get('name') or meta.get('display_name')
        pic = meta.get('picture')
    except ValueError:
        pass
    return name, pic


def event_row(ej):
    """Flatten an event for the cache table."""
    return (ej['id'], ej['pubkey'], ej['kind'], ej.get('created_at', 0),
            ej.get('content', ''),
            json.dumps(ej.get('tags', []), separators=(',', ':')))


class NostrRelay:
    """Minimal websocket Nostr client (stdlib only). Client frames are
    masked; server frames are parsed including continuation and ping."""

    def __init__(self, url, timeout=8):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self.buf = b''

    def connect(self):
        u = urllib.parse.urlparse(self.url)
        port = u.port or (443 if u.scheme == 'wss' else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
        if u.scheme == 'wss':
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
        self.sock = raw
        path = u.path or '/'
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f'GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\n'
               'Upgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(req.encode())
        self.sock.settimeout(self.timeout)
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError(f'{self.url}: handshake failed')
            resp += chunk
        first_line = resp.split(b'\r\n', 1)[0]
        if b' 101 ' not in first_line:
            raise ConnectionError(f'{self.url}: no websocket upgrade')
        self.buf = resp.split(b'\r\n\r\n', 1)[1]

    def _send_frame(self, payload, opcode=1):
        mask = os.urandom(4)
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack('>H', n)
        else:
            head.append(0x80 | 127)
            head += struct.pack('>Q', n)
        head += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + body)

    def _read_exact(self, n, deadline):
        while len(self.buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f'{self.url}: read timed out')
            self.sock.settimeout(min(remaining, self.timeout))
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError(f'{self.url}: connection closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_message(self, deadline):
        """One complete text message (handles fragmentation, pings, close)."""
        payload = b''
        while True:
            b0, b1 = self._read_exact(2, deadline)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._read_exact(2, deadline))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._read_exact(8, deadline))[0]
            if ln > 10 * 1024 * 1024:
                raise ConnectionError(f'{self.url}: frame too large')
            mask = self._read_exact(4, deadline) if masked else None
            data = self._read_exact(ln, deadline)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 9:  # ping -> pong
                self._send_frame(data, opcode=10)
                continue
            if opcode == 8:  # close
                raise ConnectionError(f'{self.url}: closed by relay')
            payload += data  # opcode 1 (text) or 0 (continuation)
            if b0 & 0x80:  # FIN
                return payload

    def query(self, filters, max_events=5000):
        """Send REQ, yield EVENT objects until EOSE or deadline."""
        subid = 'f' + os.urandom(4).hex()
        deadline = time.time() + self.timeout
        self._send_frame(json.dumps(['REQ', subid, *filters]).encode())
        got = 0
        try:
            while got < max_events:
                if time.time() >= deadline:
                    break
                try:
                    msg = self._read_message(deadline)
                except TimeoutError:
                    break
                try:
                    arr = json.loads(msg)
                except ValueError:
                    continue
                typ = arr[0] if arr else ''
                if typ == 'EOSE' and len(arr) > 1 and arr[1] == subid:
                    break
                if typ == 'EVENT' and len(arr) > 2 and arr[1] == subid:
                    got += 1
                    yield arr[2]
        finally:
            try:
                self._send_frame(json.dumps(['CLOSE', subid]).encode())
            except OSError:
                pass

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ---- LMDB read helpers (wisp v0.9.3 schema; see purge.py) ----

def _cursor_scan(env, txn, dbi, start_key=None, reverse=False):
    """Yield (key, value) forward from start_key (inclusive) or from the
    start; or backward from start_key (inclusive, via PREV) or from LAST."""
    cur = ctypes.c_void_p()
    if env.c.mdb_cursor_open(txn, dbi, ctypes.byref(cur)) != 0:
        raise purge.LmdbError('mdb_cursor_open failed')
    # kv is the KEY MDB_val (in: search key for SET_RANGE; out: found key),
    # v the DATA MDB_val. Keep kv referenced for the generator's life —
    # LMDB writes the found-key pointer back into it on every call.
    kv, v = env._Val(), env._Val()
    if start_key is not None:
        kv.mv_size = len(start_key)
        kv.mv_data = ctypes.cast(ctypes.c_char_p(start_key),
                                 ctypes.c_void_p)
    try:
        if reverse:
            if start_key is not None:
                rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                          ctypes.byref(v), MDB_SET_RANGE)
                if rc != 0:
                    rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                              ctypes.byref(v), MDB_LAST)
                else:
                    # landed on the first key >= start; step back to the
                    # largest key < start
                    rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                              ctypes.byref(v), MDB_PREV)
            else:
                rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                          ctypes.byref(v), MDB_LAST)
            if rc != 0:
                return
            op = MDB_PREV
        else:
            if start_key is not None:
                rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                          ctypes.byref(v), MDB_SET_RANGE)
            else:
                rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv),
                                          ctypes.byref(v), MDB_FIRST)
            if rc != 0:
                return
            op = MDB_NEXT
        while True:
            key = ctypes.string_at(kv.mv_data, kv.mv_size)
            val = ctypes.string_at(v.mv_data, v.mv_size)
            yield key, val
            rc = env.c.mdb_cursor_get(cur, ctypes.byref(kv), ctypes.byref(v),
                                      op)
            if rc != 0:
                return
    finally:
        env.c.mdb_cursor_close(cur)


def _idx_scan(env, txn, dbi, since=None, until=None, order='desc', limit=None,
              offset=0):
    """Walk idx:created (ts_be8 + id32) within the ts window."""
    skipped = 0
    if order == 'desc':
        start = struct.pack('>Q', until + 1) + b'\xff' * 32 \
            if until is not None else None
        keys = _cursor_scan(env, txn, dbi, start_key=start, reverse=True)
    else:
        start = struct.pack('>Q', since) + b'\x00' * 32 \
            if since is not None else None
        keys = _cursor_scan(env, txn, dbi, start_key=start)
    taken = 0
    for key, _ in keys:
        if len(key) != 40:
            continue
        ts = struct.unpack('>Q', key[:8])[0]
        if since is not None and ts < since:
            if order == 'asc':
                break
            continue  # descending: window start not reached yet
        if until is not None and ts > until:
            if order == 'desc':
                break
            continue
        if skipped < offset:
            skipped += 1
            continue
        yield key[8:]
        taken += 1
        if limit is not None and taken >= limit:
            return


class FeedEngine:
    """Feed listing, per-note detail with crawled replies/zaps, WoT filter."""

    def __init__(self, read_config_fn, data_dir):
        self.read_config = read_config_fn
        self.cache_path = os.path.join(data_dir, 'feed-cache.db')
        self._status = {'crawling': False, 'lastCrawl': None,
                        'lastError': None, 'queue': 0}
        self._status_lock = threading.Lock()
        self._env_lock = threading.Lock()
        self._env_handle = None
        self._profile_lock = threading.Lock()
        self._profile_busy = False
        self._refresh_queue = queue.Queue()
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- config ----
    def _cfg(self):
        c = self.read_config().get('feed', {})
        return {
            'enabled': bool(c.get('enabled', True)),
            'bootstrap': [r.strip() for r in str(
                c.get('bootstrap_relays', DEFAULT_BOOTSTRAP)).split(',')
                if r.strip()],
            'pow_threshold': int(c.get('pow_threshold', 16)),
            'max_depth1': int(c.get('max_depth1', 200)),
            'ttl': float(c.get('cache_ttl_hours', 24)) * 3600,
            'timeout': float(c.get('crawl_timeout_s', 8)),
        }

    # ---- sqlite cache ----
    def _db(self):
        db = sqlite3.connect(self.cache_path, timeout=10)
        db.execute('''CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, pubkey TEXT, kind INTEGER, created_at INTEGER,
            content TEXT, tags TEXT, relay TEXT, fetched_at INTEGER)''')
        db.execute('''CREATE TABLE IF NOT EXISTS follows (
            pk TEXT, follows TEXT, fetched_at INTEGER,
            PRIMARY KEY (pk, follows))''')
        db.execute('''CREATE TABLE IF NOT EXISTS targets (
            event_id TEXT, note_id TEXT, relation TEXT,
            PRIMARY KEY (event_id, note_id, relation))''')
        db.execute('''CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY, v TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS ev_pk_kind ON events(pubkey, kind)')
        db.execute('CREATE INDEX IF NOT EXISTS tgt_note ON targets(note_id, relation)')
        return db

    def _cache_put(self, db, ej, relay):
        db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)',
                   (*event_row(ej), relay, int(time.time())))

    # ---- local LMDB lookups ----
    def _env(self):
        # One persistent env for the process lifetime: repeated
        # mdb_env_open/env_close on the same file from concurrent threads
        # breaks LMDB's reader table (EINVAL txn_begin / MDB_NOTFOUND
        # dbi_open). Read txns are still begun/aborted per call.
        with self._env_lock:
            if self._env_handle is None:
                cfg = self.read_config()
                path = cfg['storage']['path']
                sync = cfg.get('storage', {}).get('sync', 'meta')
                self._env_handle = purge.Lmdb(path, sync_mode=sync)
            return self._env_handle

    def _local_event(self, env, event_id_hex):
        txn = env.txn_begin(readonly=True)
        try:
            events = env.dbi_open(txn, 'events')
            js = env.get(txn, events, bytes.fromhex(event_id_hex))
            return json.loads(js) if js else None
        finally:
            env.c.mdb_txn_abort(txn)

    def _local_events_by_kind(self, env, kinds, since=None, until=None,
                              limit=100, offset=0, order='desc'):
        txn = env.txn_begin(readonly=True)
        out = []
        try:
            idx_created = env.dbi_open(txn, 'idx:created')
            events = env.dbi_open(txn, 'events')
            kinds = set(kinds)
            for eid in _idx_scan(env, txn, idx_created, since, until,
                                 order, limit, offset):
                js = env.get(txn, events, eid)
                if js is None:
                    continue
                try:
                    ej = json.loads(js)
                except (ValueError, UnicodeDecodeError):
                    continue
                if ej.get('kind') in kinds:
                    out.append(ej)
        finally:
            env.c.mdb_txn_abort(txn)
        return out

    # ---- crawling: profiles, relay lists, follows ----
    def _crawl_op_meta(self, db, op, cfg):
        """Fetch OP's kind 0/3/10002 from the bootstrap relays into cache."""
        got = False
        for url in cfg['bootstrap']:
            try:
                rc = NostrRelay(url, timeout=cfg['timeout'])
                rc.connect()
                for ej in rc.query([{'kinds': [0, 3, 10002], 'authors': [op],
                                     'limit': 3}]):
                    self._cache_put(db, ej, url)
                    if ej.get('kind') == 3:
                        for f in follows_from_kind3(ej):
                            db.execute(
                                'INSERT OR REPLACE INTO follows VALUES (?,?,?)',
                                (op, f, int(time.time())))
                    got = True
                rc.close()
            except (OSError, ConnectionError, TimeoutError, ValueError):
                continue
        db.commit()
        return got

    def _crawl_kind3(self, db, pubkeys, cfg):
        """Fetch kind 3 for a batch of pubkeys from the bootstrap relays."""
        found = []
        todo = [p for p in pubkeys if len(p) == 64]
        for url in cfg['bootstrap']:
            if not todo:
                break
            try:
                rc = NostrRelay(url, timeout=cfg['timeout'])
                rc.connect()
                flt = {'kinds': [3], 'authors': todo, 'limit': len(todo)}
                for ej in rc.query([flt]):
                    self._cache_put(db, ej, url)
                    for f in follows_from_kind3(ej):
                        db.execute(
                            'INSERT OR REPLACE INTO follows VALUES (?,?,?)',
                            (ej['pubkey'], f, int(time.time())))
                    todo = [p for p in todo if p != ej['pubkey']]
                    found.append(ej)
                rc.close()
            except (OSError, ConnectionError, TimeoutError, ValueError):
                continue
        db.commit()
        return found

    def _crawl_kind0(self, db, pubkeys, relays, timeout):
        """Fetch kind 0 for a batch of pubkeys, trying each relay once.
        Returns the pubkeys whose profile was found and cached."""
        found = set()
        todo = [p for p in pubkeys if isinstance(p, str) and len(p) == 64]
        for url in relays:
            if not todo:
                break
            try:
                rc = NostrRelay(url, timeout=timeout)
                rc.connect()
                for ej in rc.query([{'kinds': [0], 'authors': todo,
                                     'limit': len(todo)}]):
                    pk = ej.get('pubkey')
                    if ej.get('kind') == 0 and pk in todo:
                        self._cache_put(db, ej, url)
                        todo.remove(pk)
                        found.add(pk)
                rc.close()
            except (OSError, ConnectionError, TimeoutError, ValueError):
                continue
        db.commit()
        return found

    def _author_read_relays(self, db, pk, cfg, crawl_missing):
        """Cached kind 10002 read relays for an author; optionally crawled."""
        row = db.execute('SELECT tags FROM events WHERE pubkey=? AND '
                         'kind=10002 ORDER BY created_at DESC LIMIT 1',
                         (pk,)).fetchone()
        if row is None and crawl_missing:
            for url in cfg['bootstrap'][:2]:
                try:
                    rc = NostrRelay(url, timeout=cfg['timeout'])
                    rc.connect()
                    for ej in rc.query([{'kinds': [10002], 'authors': [pk],
                                         'limit': 1}]):
                        if ej.get('kind') == 10002:
                            self._cache_put(db, ej, url)
                    rc.close()
                except (OSError, ConnectionError, TimeoutError, ValueError):
                    continue
            row = db.execute('SELECT tags FROM events WHERE pubkey=? AND '
                             'kind=10002 ORDER BY created_at DESC LIMIT 1',
                             (pk,)).fetchone()
        if row is None:
            return []
        try:
            tags = json.loads(row[0])
        except ValueError:
            return []
        relays = read_relays_from_10002({'tags': tags})
        return [r for r in relays
                if r not in ('ws://umbrel:7777', 'ws://127.0.0.1:7777',
                             'ws://localhost:7777')][:4]

    def _enrich_profiles(self, env, db, pubkeys, cfg):
        """Fetch kind 0 for pubkeys with no profile yet. Order per spec:
        each author's kind 10002 read relays first, then the [feed]
        bootstrap fallbacks. Bounded so a page of strangers can't pin the
        GUI: at most MAX_PROFILE_RELAYS author-relay connections plus the
        bootstrap sweep, each with the configured timeout."""
        todo = []
        for pk in dict.fromkeys(pubkeys):
            if isinstance(pk, str) and len(pk) == 64 and \
                    self._profile(env, db, pk)['source'] is None:
                todo.append(pk)
        if not todo:
            return 0
        # author relay lists (crawled where absent), grouped per relay
        relays_by_author = {pk: self._author_read_relays(db, pk, cfg,
                                                         crawl_missing=True)
                            for pk in todo}
        author_relays = {}   # relay -> authors to ask there
        for pk, rels in relays_by_author.items():
            for r in rels:
                author_relays.setdefault(r, []).append(pk)
        found = set()
        # their own relays first: most-connected relay asks for all its
        # authors in one subscription
        for r in sorted(author_relays, key=lambda u: -len(author_relays[u])) \
                [:MAX_PROFILE_RELAYS]:
            authors = [p for p in author_relays[r] if p not in found]
            found |= self._crawl_kind0(db, authors, [r], cfg['timeout'])
        # fallback relays for everyone still missing
        still = [p for p in todo if p not in found]
        if still:
            found |= self._crawl_kind0(db, still, cfg['bootstrap'][:3],
                                       min(cfg['timeout'], 5.0))
        return len(found)

    def _enrich_profiles_bg(self, env, pubkeys):
        """Bound background profile fetch; deduplicates concurrent calls."""
        with self._profile_lock:
            if self._profile_busy:
                return
            self._profile_busy = True
        def run():
            db = self._db()
            try:
                self._enrich_profiles(env, db, pubkeys, self._cfg())
            except Exception:  # noqa: BLE001 - background best-effort
                pass
            finally:
                db.close()
                with self._profile_lock:
                    self._profile_busy = False
        threading.Thread(target=run, daemon=True).start()

    def _follows_of(self, env, db, pk, cfg):
        """Follow set for one pubkey: local store, cache, then crawl."""
        ej = self._local_replaceable(env, pk, 3)
        if ej is not None:
            return set(follows_from_kind3(ej))
        row = db.execute(
            'SELECT MAX(fetched_at) FROM follows WHERE pk=?', (pk,)).fetchone()
        if row and row[0] and time.time() - row[0] < cfg['ttl']:
            return {r[0] for r in db.execute(
                'SELECT follows FROM follows WHERE pk=?', (pk,))}
        crawled = self._crawl_kind3(db, [pk], cfg)
        if crawled:
            return set(follows_from_kind3(crawled[0]))
        return set()

    def build_wot(self, env, db, op, cfg):
        """OP's follows plus their follows (depth 2)."""
        depth1 = self._follows_of(env, db, op, cfg)
        wot = set(depth1) | {op}
        missing = []
        for pk in sorted(depth1)[:cfg['max_depth1']]:
            ej = self._local_replaceable(env, pk, 3)
            if ej is not None:
                wot |= set(follows_from_kind3(ej))
                continue
            row = db.execute('SELECT MAX(fetched_at) FROM follows WHERE pk=?',
                             (pk,)).fetchone()
            if row and row[0] and time.time() - row[0] < cfg['ttl']:
                wot |= {r[0] for r in db.execute(
                    'SELECT follows FROM follows WHERE pk=?', (pk,))}
            else:
                missing.append(pk)
        for i in range(0, len(missing), AUTHORS_PER_REQ):
            self._crawl_kind3(db, missing[i:i + AUTHORS_PER_REQ], cfg)
        if missing:
            q = ','.join('?' * len(missing))
            for row in db.execute(
                    f'SELECT follows FROM follows WHERE pk IN ({q})', missing):
                wot |= {row[0]}
        return wot

    def _ensure_op_data(self, env, db, op, cfg):
        """(profile, read_relays) for OP; crawls when missing or stale."""
        profile = self._local_replaceable(env, op, 0)
        rel10002 = self._local_replaceable(env, op, 10002)
        if rel10002 is None:
            row = db.execute('SELECT tags, fetched_at FROM events WHERE '
                             'pubkey=? AND kind=10002 ORDER BY created_at '
                             'DESC LIMIT 1', (op,)).fetchone()
            if row:
                if time.time() - row[1] >= cfg['ttl']:
                    self._crawl_op_meta(db, op, cfg)
                    row = db.execute('SELECT tags, fetched_at FROM events '
                                     'WHERE pubkey=? AND kind=10002 ORDER BY '
                                     'created_at DESC LIMIT 1', (op,)).fetchone()
                if row:
                    rel10002 = {'tags': json.loads(row[0])}
            else:
                self._crawl_op_meta(db, op, cfg)
        read_relays = read_relays_from_10002(rel10002) if rel10002 else []
        read_relays = [r for r in read_relays
                       if r not in ('ws://umbrel:7777', 'ws://127.0.0.1:7777',
                                    'ws://localhost:7777')]
        if not profile:
            row = db.execute('SELECT content, tags FROM events WHERE pubkey=? '
                             'AND kind=0 ORDER BY created_at DESC LIMIT 1',
                             (op,)).fetchone()
            if row:
                profile = {'content': row[0]}
        return profile, read_relays or list(cfg['bootstrap'])

    # ---- note refresh (crawl replies + zaps from OP's read relays) ----
    def refresh_note(self, event_id):
        """Queue a background crawl; returns immediately."""
        if not self._cfg()['enabled']:
            return {'queued': False}
        with self._status_lock:
            self._status['queue'] += 1
        self._refresh_queue.put(event_id)
        return {'queued': True}

    def _worker(self):
        while not self._stop.is_set():
            try:
                event_id = self._refresh_queue.get(timeout=2)
            except queue.Empty:
                continue
            with self._status_lock:
                self._status['crawling'] = True
            try:
                self._refresh_note_sync(event_id)
                with self._status_lock:
                    self._status['lastCrawl'] = int(time.time())
                    self._status['lastError'] = None
            except Exception as e:  # noqa: BLE001
                with self._status_lock:
                    self._status['lastError'] = str(e)
            finally:
                with self._status_lock:
                    self._status['crawling'] = False
                    self._status['queue'] = max(0, self._status['queue'] - 1)

    def _refresh_note_sync(self, event_id):
        cfg = self._cfg()
        env = self._env()
        db = self._db()
        try:
            note = self._local_event(env, event_id)
            if note is None:
                raise ValueError('note not found in relay store')
            op = note['pubkey']
            _, read_relays = self._ensure_op_data(env, db, op, cfg)
            since = note.get('created_at', 0)
            for url in read_relays[:RELAYS_PER_NOTE]:
                try:
                    rc = NostrRelay(url, timeout=cfg['timeout'])
                    rc.connect()
                    for ej in rc.query([
                        {'kinds': [KIND_NOTE], '#e': [event_id],
                         'since': since, 'limit': REPLIES_PER_RELAY},
                        {'kinds': [KIND_ZAP], '#e': [event_id],
                         'since': since, 'limit': ZAPS_PER_RELAY},
                    ]):
                        self._cache_put(db, ej, url)
                        rel = 'zap' if ej.get('kind') == KIND_ZAP else 'reply'
                        db.execute('INSERT OR REPLACE INTO targets VALUES '
                                   '(?,?,?)', (ej['id'], event_id, rel))
                    rc.close()
                except (OSError, ConnectionError, TimeoutError, ValueError):
                    continue
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                       (f'refresh:{event_id}', str(int(time.time()))))
            db.commit()
        finally:
            db.close()

    def _local_replaceable(self, env, pubkey_hex, kind):
        """Latest replaceable event (0/3/10002/...) for a pubkey, or None."""
        txn = env.txn_begin(readonly=True)
        try:
            rep = env.dbi_open(txn, 'replaceable')
            events = env.dbi_open(txn, 'events')
            key = bytes.fromhex(pubkey_hex) + struct.pack('>I', kind)
            eid = env.get(txn, rep, key)
            if eid is None:
                return None
            js = env.get(txn, events, eid)
            if js is None:
                return None
            return json.loads(js)
        finally:
            env.c.mdb_txn_abort(txn)

    def _local_tagged(self, env, letter, value_hex, kinds):
        """Local events tagged letter=value_hex (binary tag index layout)."""
        txn = env.txn_begin(readonly=True)
        out = []
        try:
            idx_tag = env.dbi_open(txn, 'idx:tag')
            events = env.dbi_open(txn, 'events')
            prefix = letter.encode() + bytes.fromhex(value_hex)
            for key, _ in _cursor_scan(env, txn, idx_tag, start_key=prefix):
                if not key.startswith(prefix):
                    break
                js = env.get(txn, events, key[-32:])
                if not js:
                    continue
                try:
                    ej = json.loads(js)
                except (ValueError, UnicodeDecodeError):
                    continue
                if ej.get('kind') in kinds:
                    out.append(ej)
        finally:
            env.c.mdb_txn_abort(txn)
        return out

    # ---- profiles ----
    def _profile(self, env, db, pk):
        ej = self._local_replaceable(env, pk, 0)
        src = 'relay'
        if ej is None:
            row = db.execute('SELECT content FROM events WHERE pubkey=? AND '
                             'kind=0 ORDER BY created_at DESC LIMIT 1',
                             (pk,)).fetchone()
            if row:
                ej = {'content': row[0]}
                src = 'cache'
        name, pic = profile_from_kind0(ej)
        return {'name': name, 'picture': pic, 'source': src if ej else None}

    # ---- feed listing ----
    def _local_zap_totals(self, env, db, note_ids, window_since):
        """msat zap totals per note id (local LMDB + crawl cache)."""
        totals = {nid: 0 for nid in note_ids}
        txn = env.txn_begin(readonly=True)
        try:
            idx_tag = env.dbi_open(txn, 'idx:tag')
            events = env.dbi_open(txn, 'events')
            for nid in note_ids:
                prefix = b'e' + bytes.fromhex(nid)
                for key, _ in _cursor_scan(env, txn, idx_tag,
                                           start_key=prefix):
                    if not key.startswith(prefix):
                        break
                    ts = struct.unpack('>Q', key[33:41])[0]
                    if ts < window_since:
                        continue
                    js = env.get(txn, events, key[-32:])
                    if not js:
                        continue
                    try:
                        ej = json.loads(js)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if ej.get('kind') == KIND_ZAP:
                        totals[nid] += zap_amount_msat(ej.get('tags', []))
        finally:
            env.c.mdb_txn_abort(txn)
        for nid in note_ids:
            for row in db.execute(
                    "SELECT e.tags FROM targets t JOIN events e ON "
                    "e.id=t.event_id WHERE t.note_id=? AND t.relation='zap' "
                    "AND e.created_at>=?", (nid, window_since)):
                totals[nid] += zap_amount_msat(json.loads(row[0]))
        return totals

    # ---- admin pubkeys ----
    def _admins(self):
        """Admin pubkeys ([spider] admin + [management] admin_pubkeys)."""
        cfg = self.read_config()
        admins = set()
        for section, key in (('spider', 'admin'),
                             ('management', 'admin_pubkeys')):
            v = cfg.get(section, {}).get(key)
            if not v:
                continue
            if isinstance(v, str):
                v = [v]
            for a in v:
                a = str(a).strip().lower()
                if len(a) == 64 and all(c in '0123456789abcdef' for c in a):
                    admins.add(a)
        return admins

    def feed(self, sort='newest', days=None, until=None, limit=50,
             offset=0, hide_admin=False):
        """kind-1 notes on this relay, sorted. Zapped sort ranks by zap
        msat total within the window (local store + crawl cache)."""
        env = self._env()
        db = self._db()
        try:
            since = int(time.time()) - int(days) * 86400 if days else None
            if until is not None:
                until = int(until)
            admins = self._admins() if hide_admin else set()
            if sort == 'zapped':
                window_since = since if since is not None else \
                    int(time.time()) - 7 * 86400
                cands = self._local_events_by_kind(
                    env, [KIND_NOTE], since=window_since, until=until,
                    limit=MAX_CANDIDATES, order='desc')
                if admins:
                    cands = [c for c in cands if c['pubkey'] not in admins]
                totals = self._local_zap_totals(env, db,
                                                [c['id'] for c in cands],
                                                window_since)
                cands.sort(key=lambda e: totals.get(e['id'], 0), reverse=True)
                page = cands[offset:offset + limit]
            elif admins:
                # filter admin posts before paging so offsets stay consistent
                page = self._local_events_by_kind(
                    env, [KIND_NOTE], since=since, until=until,
                    limit=MAX_CANDIDATES,
                    order='desc' if sort == 'newest' else 'asc')
                page = [c for c in page if c['pubkey'] not in admins]
                page = page[offset:offset + limit]
                totals = self._local_zap_totals(env, db, [c['id'] for c in page],
                                                since or 0)
            else:
                page = self._local_events_by_kind(
                    env, [KIND_NOTE], since=since, until=until, limit=limit,
                    offset=offset, order='desc' if sort == 'newest' else 'asc')
                totals = self._local_zap_totals(env, db, [c['id'] for c in page],
                                                since or 0)
            out = []
            for c in page:
                replies = len(self._local_tagged(env, 'e', c['id'], [KIND_NOTE]))
                replies += db.execute(
                    "SELECT COUNT(*) FROM targets WHERE note_id=? AND "
                    "relation='reply'", (c['id'],)).fetchone()[0]
                out.append({
                    'id': c['id'], 'pubkey': c['pubkey'],
                    'createdAt': c.get('created_at'),
                    'content': c.get('content', ''),
                    'zapMsat': totals.get(c['id'], 0),
                    'replies': replies,
                    'profile': self._profile(env, db, c['pubkey']),
                })
            if self._cfg()['enabled']:
                missing = [c['pubkey'] for c in out
                           if c['profile']['source'] is None]
                if missing:
                    # Names appear on the next page view/refetch once the
                    # background crawl caches the authors' kind 0s.
                    self._enrich_profiles_bg(env, missing)
            return out
        finally:
            db.close()

    # ---- note detail ----
    def status(self):
        with self._status_lock:
            st = dict(self._status)
        st['enabled'] = self._cfg()['enabled']
        return st

    def note_detail(self, event_id):
        """Note + replies (WoT/PoW filtered) + all zaps; kicks a crawl when
        the cached interaction data is older than the TTL."""
        cfg = self._cfg()
        env = self._env()
        db = self._db()
        try:
            note = self._local_event(env, event_id)
            if note is None:
                return {'error': 'note not found in relay store'}
            op = note['pubkey']
            profile, _ = self._ensure_op_data(env, db, op, cfg)
            op_name, _ = profile_from_kind0(profile)
            row = db.execute('SELECT v FROM meta WHERE k=?',
                             (f'refresh:{event_id}',)).fetchone()
            stale = (not row or time.time() - int(row[0]) > cfg['ttl'])
            if stale and cfg['enabled']:
                self.refresh_note(event_id)
            wot = self.build_wot(env, db, op, cfg) if cfg['enabled'] else set()

            replies, zaps, seen = [], [], set()
            for ej in self._local_tagged(env, 'e', event_id, [KIND_NOTE]):
                seen.add(ej['id'])
                replies.append(ej)
            for row in db.execute(
                    "SELECT e.id, e.pubkey, e.kind, e.created_at, e.content, "
                    "e.tags FROM targets t JOIN events e ON e.id=t.event_id "
                    "WHERE t.note_id=? AND t.relation='reply'", (event_id,)):
                if row[0] in seen:
                    continue
                seen.add(row[0])
                replies.append({'id': row[0], 'pubkey': row[1],
                                'kind': row[2], 'created_at': row[3],
                                'content': row[4], 'tags': json.loads(row[5])})
            replies.sort(key=lambda e: e.get('created_at', 0), reverse=True)

            kept, filtered = [], 0
            for ej in replies:
                if ej['pubkey'] in wot:
                    kept.append(ej)
                elif leading_zero_bits(ej['id']) >= cfg['pow_threshold']:
                    kept.append(ej)
                else:
                    filtered += 1

            for ej in self._local_tagged(env, 'e', event_id, [KIND_ZAP]):
                zaps.append(ej)
            for row in db.execute(
                    "SELECT e.id, e.pubkey, e.created_at, e.tags FROM targets "
                    "t JOIN events e ON e.id=t.event_id WHERE t.note_id=? AND "
                    "t.relation='zap'", (event_id,)):
                if row[0] not in seen:
                    zaps.append({'id': row[0], 'pubkey': row[1],
                                 'created_at': row[2],
                                 'tags': json.loads(row[3])})
            zaps.sort(key=lambda e: e.get('created_at', 0), reverse=True)
            rrow = db.execute('SELECT v FROM meta WHERE k=?',
                              (f'refresh:{event_id}',)).fetchone()

            if cfg['enabled']:
                reply_pks = [e['pubkey'] for e in kept
                             if self._profile(env, db, e['pubkey'])['source']
                             is None]
                if reply_pks:
                    self._enrich_profiles_bg(env, reply_pks)
            return {
                'id': event_id, 'pubkey': op, 'opName': op_name,
                'content': note.get('content', ''),
                'createdAt': note.get('created_at'),
                'replies': [{'id': e['id'], 'pubkey': e['pubkey'],
                             'createdAt': e.get('created_at'),
                             'content': e.get('content', ''),
                             'profile': self._profile(env, db, e['pubkey'])}
                            for e in kept],
                'zaps': [{'pubkey': z['pubkey'],
                          'createdAt': z.get('created_at'),
                          'msat': zap_amount_msat(z.get('tags', []))}
                         for z in zaps],
                'zapMsat': sum(zap_amount_msat(z.get('tags', [])) for z in zaps),
                'filteredReplies': filtered, 'wotSize': len(wot),
                'crawling': self.status()['crawling'],
                'lastRefresh': int(rrow[0]) if rrow else None,
            }
        finally:
            db.close()
