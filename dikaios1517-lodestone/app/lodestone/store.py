"""Lodestone event storage: SQLite in WAL mode.

Design notes:
- One `events` table storing the raw canonical JSON plus indexed columns.
- A `tags` table for #e/#p/#a/#d-style REQ filters.
- A `replaceable` table tracking the current winner for replaceable kinds
  (NIP-16) and parameterized replaceable kinds (NIP-33), keyed by
  (pubkey, kind, d-tag).
- A `deleted` table for NIP-09 deletions so deleted ids stay rejected.
- Graph/whitelist/blacklist membership tables (used by admission and crawlers).
"""
import json
import sqlite3
import threading
import time


class Duplicate(Exception):
    pass


class Rejected(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         BLOB PRIMARY KEY,
    pubkey     BLOB NOT NULL,
    kind       INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    content    TEXT NOT NULL,
    sig        BLOB NOT NULL,
    raw        TEXT NOT NULL,
    expiration INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_events_pubkey ON events(pubkey, created_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS tags (
    name     TEXT NOT NULL,
    value    TEXT NOT NULL,
    event_id BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_name_value ON tags(name, value);
CREATE INDEX IF NOT EXISTS idx_tags_event ON tags(event_id);

CREATE TABLE IF NOT EXISTS replaceable (
    pubkey     BLOB NOT NULL,
    kind       INTEGER NOT NULL,
    d          TEXT NOT NULL DEFAULT '',
    event_id   BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (pubkey, kind, d)
);

CREATE TABLE IF NOT EXISTS deleted (
    event_id BLOB PRIMARY KEY,
    time     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS graph (
    pubkey BLOB PRIMARY KEY,
    depth  INTEGER NOT NULL,
    added  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS list_members (
    list   TEXT NOT NULL,
    pubkey BLOB NOT NULL,
    added  INTEGER NOT NULL,
    PRIMARY KEY (list, pubkey)
);

CREATE TABLE IF NOT EXISTS relay_lists (
    pubkey BLOB NOT NULL,
    url    TEXT NOT NULL,
    read   INTEGER NOT NULL DEFAULT 1,
    write  INTEGER NOT NULL DEFAULT 1,
    updated INTEGER NOT NULL,
    PRIMARY KEY (pubkey, url)
);

CREATE TABLE IF NOT EXISTS relay_sync (
    url      TEXT PRIMARY KEY,
    last_ts  INTEGER NOT NULL
);
"""


def canonical_event_json(ev: dict) -> str:
    """NIP-01 serialization for id hashing."""
    return json.dumps(
        [0, ev['pubkey'], ev['created_at'], ev['kind'], ev['tags'], ev['content']],
        separators=(',', ':'), ensure_ascii=False)


def event_id(ev: dict) -> bytes:
    import hashlib
    return hashlib.sha256(canonical_event_json(ev).encode()).digest()


def is_replaceable(kind: int) -> bool:
    return kind in (0, 2, 3, 5) or 10000 <= kind < 20000


def is_ephemeral(kind: int) -> bool:
    """NIP-16 ephemeral kinds: broadcast but never stored."""
    return 20000 <= kind < 30000


def is_parameterized(kind: int) -> bool:
    return 30000 <= kind < 40000


def d_tag(ev: dict) -> str:
    if not is_parameterized(ev['kind']):
        return ''
    for tag in ev['tags']:
        if isinstance(tag, list) and tag and tag[0] == 'd' and len(tag) >= 2:
            return tag[1] if isinstance(tag[1], str) else str(tag[1])
    return ''


class EventStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------ write
    def store_event(self, ev: dict) -> tuple[bool, str]:
        """Validate-light admission (signature already verified by caller).

        Returns (stored, message). Raises Rejected for kind-5 semantics only
        when the referenced events cannot be deleted? (deletion always
        succeeds silently for the author's own events).
        """
        eid = bytes.fromhex(ev['id'])
        pubkey = bytes.fromhex(ev['pubkey'])
        kind = ev['kind']
        with self.lock:
            cur = self.db.cursor()
            try:
                # NIP-09 deletion: author deletes own events by e-tag reference
                if kind == 5:
                    n = 0
                    for tag in ev['tags']:
                        if (isinstance(tag, list) and tag and tag[0] == 'e'
                                and len(tag) >= 2 and isinstance(tag[1], str)):
                            try:
                                target = bytes.fromhex(tag[1])
                            except ValueError:
                                continue
                            row = cur.execute(
                                'SELECT pubkey FROM events WHERE id = ?',
                                (target,)).fetchone()
                            if row and row[0] == pubkey:
                                cur.execute('INSERT OR IGNORE INTO deleted '
                                            '(event_id, time) VALUES (?, ?)',
                                            (target, int(time.time())))
                                cur.execute('DELETE FROM events WHERE id = ?',
                                            (target,))
                                n += 1
                    self.db.commit()
                    return (True, f'deleted:{n}' if n else 'noop:nothing deleted')

                if self._is_deleted(cur, eid):
                    return (False, 'deleted:event has been deleted')

                # NIP-40: already-expired events are not stored
                for tag in ev['tags']:
                    if (isinstance(tag, list) and len(tag) >= 2
                            and tag[0] == 'expiration'
                            and isinstance(tag[1], str)):
                        try:
                            if int(tag[1]) <= int(time.time()):
                                return (False,
                                        'expired:event has expired')
                        except ValueError:
                            pass

                # duplicate?
                if cur.execute('SELECT 1 FROM events WHERE id = ?',
                               (eid,)).fetchone():
                    return (False, 'duplicate:event already stored')

                # replaceable / parameterized replaceable rules
                d = d_tag(ev)
                if is_parameterized(kind) or is_replaceable(kind):
                    row = cur.execute(
                        'SELECT event_id, created_at FROM replaceable '
                        'WHERE pubkey = ? AND kind = ? AND d = ?',
                        (pubkey, kind, d)).fetchone()
                    if row:
                        old_id, old_ts = bytes(row[0]), row[1]
                        # NIP-16/33: newer wins; tie broken by lowest id.
                        wins = (ev['created_at'] > old_ts or
                                (ev['created_at'] == old_ts and eid < old_id))
                        if not wins:
                            return (False, 'duplicate:superseded by newer event')
                        cur.execute('DELETE FROM events WHERE id = ?', (old_id,))
                        cur.execute('DELETE FROM tags WHERE event_id = ?',
                                    (old_id,))

                raw = json.dumps(ev, separators=(',', ':'), ensure_ascii=False)
                # NIP-40: record the expiration timestamp, if any
                expiration = None
                for tag in ev['tags']:
                    if (isinstance(tag, list) and len(tag) >= 2
                            and tag[0] == 'expiration'
                            and isinstance(tag[1], str)):
                        try:
                            expiration = int(tag[1])
                        except ValueError:
                            pass
                cur.execute(
                    'INSERT INTO events (id, pubkey, kind, created_at, content,'
                    ' sig, raw, expiration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (eid, pubkey, kind, ev['created_at'], ev['content'],
                     bytes.fromhex(ev['sig']), raw, expiration))
                for tag in ev['tags']:
                    if (isinstance(tag, list) and len(tag) >= 2
                            and isinstance(tag[0], str) and len(tag[0]) == 1
                            and isinstance(tag[1], str)):
                        cur.execute(
                            'INSERT INTO tags (name, value, event_id) '
                            'VALUES (?, ?, ?)',
                            (tag[0], tag[1][:512], eid))
                if is_parameterized(kind) or is_replaceable(kind):
                    cur.execute(
                        'INSERT OR REPLACE INTO replaceable (pubkey, kind, d,'
                        ' event_id, created_at) VALUES (?, ?, ?, ?, ?)',
                        (pubkey, kind, d, eid, ev['created_at']))
                self.db.commit()
                return (True, '')
            except sqlite3.IntegrityError:
                self.db.rollback()
                return (False, 'duplicate:event already stored')
            finally:
                cur.close()

    @staticmethod
    def _is_deleted(cur, eid: bytes) -> bool:
        return bool(cur.execute('SELECT 1 FROM deleted WHERE event_id = ?',
                                (eid,)).fetchone())

    # ------------------------------------------------------------------- read
    def query(self, filters: list[dict], chunk: int = 500):
        """Yields raw event JSON strings matching any of the filters."""
        with self.lock:
            seen = set()
            for f in filters:
                where, params = [], []
                unmatchable = False
                if 'ids' in f and f['ids']:
                    pks_ids = []
                    for i in f['ids']:
                        if isinstance(i, str) and len(i) == 64:
                            try:
                                pks_ids.append(bytes.fromhex(i))
                            except ValueError:
                                pass
                    if not pks_ids:
                        continue  # cannot match
                    for batch in _chunks(pks_ids, chunk):
                        where.append(
                            f"id IN ({','.join('?' * len(batch))})")
                        params.extend(batch)
                if 'authors' in f and f['authors']:
                    pks = []
                    for a in f['authors']:
                        if isinstance(a, str) and len(a) == 64:
                            try:
                                pks.append(bytes.fromhex(a))
                            except ValueError:
                                pass
                    if not pks:
                        continue  # filter cannot match
                    for batch in _chunks(pks, chunk):
                        where.append(
                            f"pubkey IN ({','.join('?' * len(batch))})")
                        params.extend(batch)
                if 'kinds' in f and f['kinds']:
                    where.append(
                        f"kind IN ({','.join('?' * len(f['kinds']))})")
                    params.extend(int(k) for k in f['kinds'])
                if 'since' in f and f['since'] is not None:
                    where.append('created_at >= ?')
                    params.append(int(f['since']))
                if 'until' in f and f['until'] is not None:
                    where.append('created_at <= ?')
                    params.append(int(f['until']))
                for key, val in f.items():
                    if key.startswith('#') and isinstance(val, list) and val:
                        name = key[1:]
                        sub = ('id IN (SELECT event_id FROM tags '
                               "WHERE name = ? AND value IN (")
                        sub += ','.join('?' * len(val)) + '))'
                        where.append(sub)
                        params.append(name)
                        params.extend(str(v)[:512] for v in val)
                # NIP-50 search: case-insensitive substring on content
                if isinstance(f.get('search'), str) and f['search'].strip():
                    term = f['search'].strip()[:200]
                    like = '%' + term.replace('\\', '\\\\') \
                        .replace('%', r'\%').replace('_', r'\_') + '%'
                    where.append(
                        "lower(content) LIKE ? ESCAPE '\\' ")
                    params.append(like.lower())
                # NIP-40: expired events are never served
                where.append(
                    '(expiration IS NULL OR expiration > ?)')
                params.append(int(time.time()))
                # NIP-86 banevent tombstones are never served
                where.append(
                    'id NOT IN (SELECT event_id FROM deleted)')
                if not where:
                    where = ['1']  # match-all filter (bounded by limit)
                limit = min(int(f.get('limit', 0)) or self.max_query_rows,
                            self.max_query_rows)
                order = ('ORDER BY created_at DESC' if 'limit' in f
                         else 'ORDER BY created_at ASC')
                sql = ('SELECT id, raw FROM events WHERE '
                       + ' AND '.join(f'({w})' for w in where)
                       + f' {order} LIMIT ?')
                try:
                    rows = self.db.execute(sql, (*params, limit)).fetchall()
                except sqlite3.OperationalError as e:
                    raise Rejected(f'invalid:bad filter ({e})')
                for eid, raw in rows:
                    if eid not in seen:
                        seen.add(eid)
                        yield raw

    max_query_rows = 5000

    def count(self, filters: list[dict]) -> int:
        n = 0
        for _raw in self.query(filters):
            n += 1
        return n

    # ------------------------------------------------------------ membership
    def add_member(self, list_name: str, pubkey_hex: str):
        with self.lock:
            self.db.execute(
                'INSERT OR IGNORE INTO list_members (list, pubkey, added) '
                'VALUES (?, ?, ?)',
                (list_name, bytes.fromhex(pubkey_hex), int(time.time())))
            self.db.commit()

    def remove_member(self, list_name: str, pubkey_hex: str):
        with self.lock:
            self.db.execute(
                'DELETE FROM list_members WHERE list = ? AND pubkey = ?',
                (list_name, bytes.fromhex(pubkey_hex)))
            self.db.commit()

    def members(self, list_name: str) -> set[bytes]:
        with self.lock:
            rows = self.db.execute(
                'SELECT pubkey FROM list_members WHERE list = ?',
                (list_name,)).fetchall()
        return {bytes(r[0]) for r in rows}

    def in_graph(self, pubkey: bytes) -> bool:
        with self.lock:
            return bool(self.db.execute(
                'SELECT 1 FROM graph WHERE pubkey = ?',
                (pubkey,)).fetchone())

    def set_graph(self, members: list[tuple[bytes, int]]):
        with self.lock:
            self.db.execute('DELETE FROM graph')
            self.db.executemany(
                'INSERT OR REPLACE INTO graph (pubkey, depth, added) '
                'VALUES (?, ?, ?)',
                [(pk, depth, int(time.time())) for pk, depth in members])
            self.db.commit()

    def graph_members(self) -> dict[bytes, int]:
        with self.lock:
            rows = self.db.execute(
                'SELECT pubkey, depth FROM graph').fetchall()
        return {bytes(r[0]): r[1] for r in rows}

    # ------------------------------------------------------------ relay lists
    def set_relay_list(self, pubkey: bytes,
                       entries: list[tuple[str, bool, bool]]):
        """entries: [(url, read, write)] — replaces the pubkey's list."""
        now = int(time.time())
        with self.lock:
            self.db.execute('DELETE FROM relay_lists WHERE pubkey = ?',
                            (pubkey,))
            self.db.executemany(
                'INSERT OR REPLACE INTO relay_lists (pubkey, url, read,'
                ' write, updated) VALUES (?, ?, ?, ?, ?)',
                [(pubkey, url, 1 if rd else 0, 1 if wr else 0, now)
                 for url, rd, wr in entries])
            self.db.commit()

    def relay_list(self, pubkey: bytes, mode: str = None) -> list[str]:
        """URLs for a pubkey; mode 'read' or 'write' filters by capability."""
        q = ('SELECT url, read, write FROM relay_lists WHERE pubkey = ?'
             + (' AND ' + mode + ' = 1' if mode else ''))
        with self.lock:
            rows = self.db.execute(q, (pubkey,)).fetchall()
        return [r[0] for r in rows]

    def set_relay_sync(self, url: str, ts: int):
        with self.lock:
            self.db.execute(
                'INSERT OR REPLACE INTO relay_sync (url, last_ts) '
                'VALUES (?, ?)', (url, ts))
            self.db.commit()

    def relay_sync(self, url: str) -> int | None:
        with self.lock:
            row = self.db.execute('SELECT last_ts FROM relay_sync '
                                  'WHERE url = ?', (url,)).fetchone()
        return row[0] if row else None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
