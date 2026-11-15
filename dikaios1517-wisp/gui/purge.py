#!/usr/bin/env python3
"""Retention purge worker: deletes events older than a configured age directly
from wisp's LMDB store (the relay itself has no age-based deletion).

Schema mirrored from wisp v0.9.3 (src/store.zig + libnostr-z src/index_keys.zig):

  events          key = id(32)                    value = event JSON
  idx:created     key = created_at_be(8) + id(32)
  idx:pubkey      key = pubkey(32) + created_at_be(8) + id(32)
  idx:kind        key = kind_be(4) + created_at_be(8) + id(32)
  idx:tag         key = letter(1) + value + created_at_be(8) + id(32)
                      binary tag: letter in e/p/E/P with 64-char hex value
                      (value = 32 decoded bytes); otherwise string (1..256 bytes)
  idx:expiration  key = expiration_be(8) + id(32)      (only if expiration tag)
  replaceable     key = pubkey(32) + kind_be(4) [+ d value for 30000-39999]
                      value = id(32)  (kinds 0, 3, 10000-19999, 30000-39999)
  deleted         key = id(32)   value = timestamp as 8 bytes native-endian

Env flags mirror wisp's Lmdb.init: NOSUBDIR | WRITEMAP | NORDAHEAD
(+ NOSYNC/NOMETASYNC to match the configured sync mode) — LMDB serializes
writers across processes, so running alongside the relay is safe.
"""

import ctypes
import ctypes.util
import json
import struct
import threading
import time

# lmdb.h flag values
MDB_NOSUBDIR = 0x4000
MDB_NOTLS = 0x200000
MDB_NOSYNC = 0x10000
MDB_RDONLY = 0x20000
MDB_NOMETASYNC = 0x40000
MDB_WRITEMAP = 0x80000
MDB_NORDAHEAD = 0x800000

MDB_NOTFOUND = -30798
MDB_FIRST = 0
MDB_NEXT = 8        # MDB_cursor_op enum position (not the #define list)
MDB_SET_RANGE = 17

REPLACEABLE_KINDS = (0, 3)  # plus 10000-19999; addressable = 30000-39999
BINARY_LETTERS = set(b'ePEp')
BATCH_SIZE = 500


class LmdbError(Exception):
    pass


class Lmdb:
    """Minimal ctypes binding to liblmdb scoped to what the purge needs."""

    def __init__(self, path, map_size_mb=10240, sync_mode='meta'):
        for candidate in ('liblmdb.so.0', 'liblmdb.so'):
            try:
                self.c = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        else:
            found = ctypes.util.find_library('lmdb')
            if not found:
                raise LmdbError('liblmdb not found in the container image')
            self.c = ctypes.CDLL(found)

        c = self.c
        c.mdb_env_create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        c.mdb_env_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                   ctypes.c_uint, ctypes.c_uint]
        c.mdb_env_set_mapsize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        c.mdb_env_set_maxdbs.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        c.mdb_txn_begin.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        c.mdb_dbi_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                   ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        # mdb_get/mdb_put/mdb_del/mdb_cursor_get take MDB_val* args — leave
        # argtypes unset for those and pass byref(MDB_val) at call sites.
        c.mdb_cursor_open.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_void_p)]
        for f in ('mdb_env_close', 'mdb_txn_abort', 'mdb_cursor_close'):
            getattr(c, f).argtypes = [ctypes.c_void_p]
        c.mdb_txn_commit.argtypes = [ctypes.c_void_p]
        c.mdb_dbi_close.argtypes = [ctypes.c_void_p, ctypes.c_uint]

        flags = (MDB_NOSUBDIR | MDB_WRITEMAP | MDB_NORDAHEAD | MDB_NOTLS)
        if sync_mode == 'none':
            flags |= MDB_NOSYNC | MDB_NOMETASYNC
        elif sync_mode == 'meta':
            flags |= MDB_NOMETASYNC

        self.env = ctypes.c_void_p()
        if c.mdb_env_create(ctypes.byref(self.env)) != 0:
            raise LmdbError('mdb_env_create failed')
        c.mdb_env_set_mapsize(self.env, map_size_mb * 1024 * 1024)
        c.mdb_env_set_maxdbs(self.env, 32)
        rc = c.mdb_env_open(self.env, path.encode(), flags, 0o600)
        if rc != 0:
            c.mdb_env_close(self.env)
            raise LmdbError(f'mdb_env_open failed: rc={rc} for {path}')
        self.path = path

    def close(self):
        if self.env:
            self.c.mdb_env_close(self.env)
            self.env = None

    class _Val(ctypes.Structure):
        _fields_ = [('mv_size', ctypes.c_size_t), ('mv_data', ctypes.c_void_p)]

    def _val(self, data):
        v = self._Val()
        v.mv_size = len(data)
        v.mv_data = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p)
        return v

    def txn_begin(self, readonly=False):
        txn = ctypes.c_void_p()
        rc = self.c.mdb_txn_begin(self.env, None, 1 if readonly else 0,
                                  ctypes.byref(txn))
        if rc != 0:
            raise LmdbError(f'mdb_txn_begin failed: rc={rc}')
        return txn

    def dbi_open(self, txn, name):
        dbi = ctypes.c_uint()
        rc = self.c.mdb_dbi_open(txn, name.encode(), 0, ctypes.byref(dbi))
        if rc != 0:
            raise LmdbError(f'mdb_dbi_open({name}) failed: rc={rc}')
        return dbi

    def get(self, txn, dbi, key):
        v = self._Val()
        rc = self.c.mdb_get(txn, dbi, self.byrefval(key), ctypes.byref(v))
        if rc != 0:
            return None
        return ctypes.string_at(v.mv_data, v.mv_size)

    def put(self, txn, dbi, key, value):
        rc = self.c.mdb_put(txn, dbi, self.byrefval(key), self.byrefval(value), 0)
        if rc != 0:
            raise LmdbError(f'mdb_put failed: rc={rc}')

    def delete(self, txn, dbi, key):
        rc = self.c.mdb_del(txn, dbi, self.byrefval(key), None)
        if rc not in (0, MDB_NOTFOUND):
            raise LmdbError(f'mdb_del failed: rc={rc}')

    def byrefval(self, data):
        return ctypes.byref(self._val(data))

    def cursor_iter(self, txn, dbi, start_key=None):
        """Yields (key, value) from start_key (inclusive) or from the start."""
        cur = ctypes.c_void_p()
        if self.c.mdb_cursor_open(txn, dbi, ctypes.byref(cur)) != 0:
            raise LmdbError('mdb_cursor_open failed')
        try:
            k, v = self._Val(), self._Val()
            op = MDB_FIRST
            if start_key is not None:
                # The search key must live in the KEY MDB_val itself —
                # LMDB overwrites it with the found key on success.
                k.mv_size = len(start_key)
                k.mv_data = ctypes.cast(ctypes.c_char_p(start_key),
                                        ctypes.c_void_p)
                op = MDB_SET_RANGE
            while True:
                rc = self.c.mdb_cursor_get(cur, ctypes.byref(k),
                                           ctypes.byref(v), op)
                if rc != 0:
                    return
                op = MDB_NEXT
                key = ctypes.string_at(k.mv_data, k.mv_size)
                if start_key is not None and key == start_key:
                    start_key = None  # resume marker: already processed
                    continue
                yield (key, ctypes.string_at(v.mv_data, v.mv_size))
        finally:
            self.c.mdb_cursor_close(cur)


def _be64(n):
    return struct.pack('>Q', n)


def _be32(n):
    return struct.pack('>I', n)


def expiration_ts(event_json):
    for tag in event_json.get('tags', []):
        if (isinstance(tag, list) and len(tag) >= 2 and tag[0] == 'expiration'
                and isinstance(tag[1], str)):
            try:
                val = int(tag[1])
                return val if val >= 0 else None
            except ValueError:
                return None
    return None


def tag_keys(event_json, ts_be, event_id):
    """All idx:tag keys for an event, exactly as wisp's indexEvent built them."""
    keys = []
    for tag in event_json.get('tags', []):
        if not isinstance(tag, list) or len(tag) < 2:
            continue
        name, value = tag[0], tag[1]
        if not isinstance(name, str) or len(name) != 1:
            continue
        letter = name.encode('utf-8', 'surrogatepass')[0]
        if not isinstance(value, str):
            continue
        raw = value.encode('utf-8', 'surrogatepass')
        if letter in BINARY_LETTERS and len(value) == 64:
            try:
                decoded = bytes.fromhex(value)
            except ValueError:
                continue
            keys.append(letter.to_bytes(1, 'big') + decoded + ts_be + event_id)
        elif 0 < len(raw) <= 256:
            keys.append(letter.to_bytes(1, 'big') + raw + ts_be + event_id)
    return keys


def replaceable_key(event_json):
    """(key, matched) for the replaceable pointer, or (None, False)."""
    kind = event_json.get('kind')
    pubkey = bytes.fromhex(event_json['pubkey'])
    if kind in REPLACEABLE_KINDS or 10000 <= kind < 20000:
        return pubkey + _be32(kind), False
    if 30000 <= kind < 40000:
        d = ''
        for tag in event_json.get('tags', []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == 'd':
                d = tag[1]
                break
        return pubkey + _be32(kind) + str(d or '').encode(), True
    return None, False


def purge_once(env_path, map_size_mb, sync_mode, cutoff_ts, keep_admin=True,
               admin_pubkeys=(), max_events=None, progress=None):
    """One purge pass. Returns dict with deleted/skippedAdmin/errors counts."""
    lmdb = Lmdb(env_path, map_size_mb, sync_mode)
    deleted = skipped_admin = errors = 0
    resume_after = None
    reached_end = False
    try:
        while True:
            # --- read phase: collect a batch of candidates oldest-first ---
            txn = lmdb.txn_begin(readonly=True)
            candidates = []
            try:
                idx_created = lmdb.dbi_open(txn, 'idx:created')
                events = lmdb.dbi_open(txn, 'events')
                for key, _ in lmdb.cursor_iter(txn, idx_created, resume_after):
                    if len(key) != 40:
                        errors += 1
                        continue
                    ts, event_id = struct.unpack('>Q', key[:8])[0], key[8:]
                    if ts > cutoff_ts:
                        reached_end = True
                        break
                    if len(candidates) >= BATCH_SIZE:
                        break  # txn must end; resume after the last candidate
                    js = lmdb.get(txn, events, event_id)
                    if js is None:
                        # already gone but index entry remains: just clean the index
                        candidates.append((key, event_id, None))
                        continue
                    try:
                        ej = json.loads(js)
                    except (ValueError, UnicodeDecodeError):
                        errors += 1
                        continue
                    # sanity: the JSON must match the index entry, or we abort
                    if (not isinstance(ej, dict)
                            or ej.get('id') != event_id.hex()
                            or ej.get('created_at') != ts
                            or not isinstance(ej.get('kind'), int)
                            or not isinstance(ej.get('pubkey'), str)):
                        raise LmdbError(
                            f'index/json mismatch for {event_id.hex()}; aborting')
                    candidates.append((key, event_id, ej))
            finally:
                lmdb.c.mdb_txn_abort(txn)

            if candidates:
                # Resume from the last PROCESSED key: cursor_iter skips the
                # resume key itself (it was handled), so the next pass picks
                # up exactly where this one ended. Using the first *unprocessed*
                # key here silently dropped one event per batch boundary.
                resume_after = candidates[-1][0]
            if not candidates:
                return {'deleted': deleted, 'skippedAdmin': skipped_admin,
                        'errors': errors, 'done': True}
            if progress:
                progress(len(candidates))

            # --- write phase: remove each candidate completely ---
            txn = lmdb.txn_begin()
            try:
                idx_created = lmdb.dbi_open(txn, 'idx:created')
                idx_pubkey = lmdb.dbi_open(txn, 'idx:pubkey')
                idx_kind = lmdb.dbi_open(txn, 'idx:kind')
                idx_tag = lmdb.dbi_open(txn, 'idx:tag')
                idx_expiration = lmdb.dbi_open(txn, 'idx:expiration')
                events = lmdb.dbi_open(txn, 'events')
                replaceable = lmdb.dbi_open(txn, 'replaceable')
                deleted_dbi = lmdb.dbi_open(txn, 'deleted')

                for created_key, event_id, ej in candidates:
                    if ej is None:
                        lmdb.delete(txn, idx_created, created_key)
                        continue
                    if keep_admin and ej.get('pubkey').lower() in admin_pubkeys:
                        skipped_admin += 1
                        continue
                    ts = ej['created_at']
                    pubkey = bytes.fromhex(ej['pubkey'])
                    kind = ej['kind']
                    ts_be = _be64(ts)

                    lmdb.delete(txn, idx_created, created_key)
                    lmdb.delete(txn, idx_pubkey, pubkey + ts_be + event_id)
                    lmdb.delete(txn, idx_kind, _be32(kind) + ts_be + event_id)
                    for tk in tag_keys(ej, ts_be, event_id):
                        lmdb.delete(txn, idx_tag, tk)
                    exp = expiration_ts(ej)
                    if exp is not None:
                        lmdb.delete(txn, idx_expiration, _be64(exp) + event_id)
                    rkey, _addr = replaceable_key(ej)
                    if rkey is not None and lmdb.get(txn, replaceable, rkey) == event_id:
                        lmdb.delete(txn, replaceable, rkey)
                    lmdb.delete(txn, events, event_id)
                    # tombstone so sync/spider/negentropy cannot re-add the event
                    lmdb.put(txn, deleted_dbi, event_id,
                             struct.pack('<q', int(time.time())))
                    deleted += 1
                lmdb.c.mdb_txn_commit(txn)
            except Exception:
                lmdb.c.mdb_txn_abort(txn)
                raise

            if max_events is not None and deleted >= max_events:
                return {'deleted': deleted, 'skippedAdmin': skipped_admin,
                        'errors': errors, 'done': False}
            if reached_end:
                return {'deleted': deleted, 'skippedAdmin': skipped_admin,
                        'errors': errors, 'done': True}
    finally:
        lmdb.close()


def admin_pubkeys_from(config):
    admins = set()
    if (config.get('retention', {}) or {}).get('keep_admin_notes', True):
        for key, field in (('spider', 'admin'), ('management', 'admin_pubkeys')):
            section = config.get(key, {}) or {}
            raw = section.get(field)
            if isinstance(raw, str):
                admins |= {p.strip().lower() for p in raw.split(',')
                           if len(p.strip()) == 64}
    return admins


def run_retention(config):
    """Interpret [retention] (+[storage]/[spider]) from the parsed wisp.toml."""
    retention = config.get('retention', {}) or {}
    if not retention.get('enabled'):
        return {'enabled': False, 'deleted': 0, 'skippedAdmin': 0, 'errors': 0,
                'done': True}
    months = int(retention.get('purge_after_months', 12) or 12)
    months = max(1, months)
    cutoff = int(time.time()) - months * 30 * 86400  # 30-day months
    storage = config.get('storage', {}) or {}
    return purge_once(
        env_path=storage.get('path', '/data/wisp'),
        map_size_mb=int(storage.get('map_size_mb', 10240) or 10240),
        sync_mode=storage.get('sync', 'meta'),
        cutoff_ts=cutoff,
        keep_admin=bool(retention.get('keep_admin_notes', True)),
        admin_pubkeys=admin_pubkeys_from(config),
    )


class PurgeScheduler:
    """Daily purge runs inside the GUI server process."""

    def __init__(self, read_config_fn):
        self.read_config_fn = read_config_fn
        self.lock = threading.Lock()
        self.status = {'lastRun': None, 'lastResult': None, 'running': False}

    def run_now(self):
        with self.lock:
            if self.status['running']:
                return {'ok': False, 'error': 'purge already running'}
            self.status['running'] = True
        try:
            result = run_retention(self.read_config_fn())
            result['ok'] = True
            self.status['lastRun'] = int(time.time())
            self.status['lastResult'] = result
            return result
        except Exception as e:  # noqa: BLE001 - surfaced via /api/purge
            result = {'ok': False, 'error': str(e)}
            self.status['lastRun'] = int(time.time())
            self.status['lastResult'] = result
            return result
        finally:
            self.status['running'] = False

    def loop(self):
        time.sleep(20)  # let the relay settle after container start
        while True:
            try:
                cfg = self.read_config_fn()
                interval_hours = int((cfg.get('retention', {}) or {})
                                     .get('interval_hours', 24) or 24)
                interval_hours = max(1, interval_hours)
                if (cfg.get('retention', {}) or {}).get('enabled'):
                    self.run_now()
            except Exception:
                pass
            slept = 0
            while slept < interval_hours * 3600:  # short slices: interval edits apply
                time.sleep(30)
                slept += 30

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
