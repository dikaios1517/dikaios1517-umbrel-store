"""SQLite cache: graph, server-author map, blob descriptors, profiles.

Lives in the persisted data volume so repeat visits and restarts are fast.
"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS follows (
    root TEXT NOT NULL, pubkey TEXT NOT NULL, added_at INTEGER,
    PRIMARY KEY (root, pubkey));
CREATE TABLE IF NOT EXISTS server_authors (
    root TEXT NOT NULL, server TEXT NOT NULL, pubkey TEXT NOT NULL,
    added_at INTEGER, PRIMARY KEY (root, server, pubkey));
CREATE TABLE IF NOT EXISTS blobs (
    server TEXT NOT NULL, sha256 TEXT NOT NULL, pubkey TEXT NOT NULL,
    created_at INTEGER, size INTEGER, type TEXT, name TEXT,
    PRIMARY KEY (server, sha256));
CREATE INDEX IF NOT EXISTS blobs_feed ON blobs (created_at DESC);
CREATE TABLE IF NOT EXISTS profiles (
    pubkey TEXT PRIMARY KEY, name TEXT, picture TEXT, fetched_at INTEGER);
"""


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, 'meadow.db')
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- meta
    def meta_get(self, k: str, default=None):
        row = self.db.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        return row[0] if row else default

    def meta_set(self, k: str, v):
        self.db.execute('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)',
                        (k, json.dumps(v)))
        self.db.commit()

    # ---------------------------------------------------------------- graph
    def save_graph(self, root: str, follows: list[str],
                   server_authors: dict[str, list[str]]):
        now = int(time.time())
        cur = self.db.cursor()
        cur.execute('DELETE FROM follows WHERE root=?', (root,))
        cur.execute('DELETE FROM server_authors WHERE root=?', (root,))
        cur.executemany(
            'INSERT OR REPLACE INTO follows VALUES (?,?,?)',
            [(root, pk, now) for pk in follows])
        cur.executemany(
            'INSERT OR REPLACE INTO server_authors VALUES (?,?,?,?)',
            [(root, server, pk, now)
             for server, pks in server_authors.items() for pk in pks])
        cur.execute('INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)',
                    ('graph_at_' + root, json.dumps(now)))
        self.db.commit()

    def graph_age(self, root: str) -> float | None:
        raw = self.meta_get('graph_at_' + root)
        if raw is None:
            return None
        try:
            return max(0.0, time.time() - json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return None

    def graph_servers(self, root: str) -> list[tuple[str, list[str]]]:
        rows = self.db.execute(
            'SELECT server, pubkey FROM server_authors WHERE root=?',
            (root,)).fetchall()
        out: dict[str, list[str]] = {}
        for server, pk in rows:
            out.setdefault(server, []).append(pk)
        return list(out.items())

    def graph_follows(self, root: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            'SELECT pubkey FROM follows WHERE root=?', (root,))]

    # ---------------------------------------------------------------- blobs
    def upsert_blobs(self, server: str, descriptors: list[dict],
                     pubkey: str):
        self.db.executemany(
            'INSERT OR IGNORE INTO blobs VALUES (?,?,?,?,?,?,?)',
            [(server, d['sha256'], pubkey, d['created_at'], d['size'],
              d['type'], d['name']) for d in descriptors])
        self.db.commit()

    def upsert_owned(self, descriptors: list[dict], pubkey: str):
        """Descriptors that already carry their own 'server' key."""
        self.db.executemany(
            'INSERT OR IGNORE INTO blobs VALUES (?,?,?,?,?,?,?)',
            [(d['server'], d['sha256'], pubkey, d['created_at'], d['size'],
              d['type'], d['name']) for d in descriptors])
        self.db.commit()

    def blob_count(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0]

    def feed(self, before: float | None, limit: int, kinds: str = 'media'):
        """Deduped media items newest-first, before = created_at cursor.

        kinds: 'media' = image/* + video/*, 'image' or 'video' = one type,
        'all' = everything with a known type.
        Same blob on several servers is shown once (first server wins).
        """
        where, args = [], []
        if before is not None:
            where.append('created_at < ?')
            args.append(before)
        clause = {
            'media': "(type LIKE 'image/%' OR type LIKE 'video/%')",
            'image': "type LIKE 'image/%'",
            'video': "type LIKE 'video/%'",
            'all': "type IS NOT NULL",
        }.get(kinds, "type LIKE 'image/%' OR type LIKE 'video/%'")
        where.append(clause)
        sql = ('SELECT sha256, MIN(server), pubkey, MAX(created_at),'
               ' MAX(size), MAX(type), MAX(name) FROM blobs'
               + (' WHERE ' + ' AND '.join(where) if where else '')
               + ' GROUP BY sha256 ORDER BY created_at DESC LIMIT ?')
        args.append(limit)
        return [{'sha256': r[0], 'server': r[1], 'pubkey': r[2],
                 'created_at': r[3], 'size': r[4], 'type': r[5],
                 'name': r[6]} for r in self.db.execute(sql, args).fetchall()]

    # ------------------------------------------------------------- profiles
    def profiles(self, pubkeys: list[str]) -> dict[str, dict]:
        out = {}
        for pk in pubkeys:
            row = self.db.execute(
                'SELECT name, picture, fetched_at FROM profiles '
                'WHERE pubkey=?', (pk,)).fetchone()
            if row:
                out[pk] = {'name': row[0], 'picture': row[1],
                           'fetched_at': row[2]}
        return out

    def set_profile(self, pubkey: str, name, picture):
        self.db.execute(
            'INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)',
            (pubkey, name, picture, int(time.time())))
        self.db.commit()

    def stale_profiles(self, max_age_s: int) -> list[str]:
        cut = int(time.time()) - max_age_s
        known = {r[0] for r in self.db.execute(
            "SELECT DISTINCT pubkey FROM blobs").fetchall()}
        cached = {r[0] for r in self.db.execute(
            'SELECT pubkey FROM profiles WHERE fetched_at > ?',
            (cut,)).fetchall()}
        return sorted(known - cached)
