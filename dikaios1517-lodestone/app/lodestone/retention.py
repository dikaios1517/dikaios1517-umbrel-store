"""Retention: delete events older than the configured cutoff, in batches,
with an owner-exemption toggle (owner's own notes kept indefinitely by
default). Designed to run on a schedule from the GUI, or manually from the
Manage tab — a single call fully drains everything past the cutoff (the
wisp purge bug — resume cursors losing their place — is why this walks a
simple indexed scan in batches until nothing is left).
"""
import asyncio
import time
from datetime import datetime, timezone

BATCH = 1000


def cutoff_ts(months: int) -> int:
    """Calendar-months back from now (not a fixed 30-day approximation)."""
    now = datetime.now(timezone.utc)
    month = now.month - months
    year = now.year
    while month <= 0:
        month += 12
        year -= 1
    # clamp day (Jan 31 - 1 month = Dec 31, not Jan 31 -> invalid Feb 31)
    import calendar
    day = min(now.day, calendar.monthrange(year, month)[1])
    return int(now.replace(year=year, month=month, day=day).timestamp())


class Retention:
    def __init__(self, store, config, log=None):
        self.store = store
        self.cfg = config
        self.log = log or (lambda *a: None)

    def purge(self, months: int = None, keep_owner: bool = None) -> dict:
        months = int(self.cfg['retention']['months'] if months is None
                     else months)
        keep_owner = (self.cfg['retention']['owner_kept_indefinitely']
                      if keep_owner is None else keep_owner)
        cutoff = cutoff_ts(months)
        with self.store.lock:
            deleted = 0
            while True:
                if keep_owner:
                    owner = self._owner()
                    if owner is None:
                        rows = self.store.db.execute(
                            'SELECT id FROM events WHERE created_at < ? '
                            'ORDER BY created_at LIMIT ?',
                            (cutoff, BATCH)).fetchall()
                    else:
                        rows = self.store.db.execute(
                            'SELECT id FROM events WHERE created_at < ? '
                            'AND pubkey != ? ORDER BY created_at LIMIT ?',
                            (cutoff, owner, BATCH)).fetchall()
                else:
                    rows = self.store.db.execute(
                        'SELECT id FROM events WHERE created_at < ? '
                        'ORDER BY created_at LIMIT ?',
                        (cutoff, BATCH)).fetchall()
                if not rows:
                    break
                ids = [(r[0],) for r in rows]
                self.store.db.executemany(
                    'DELETE FROM tags WHERE event_id = ?', ids)
                self.store.db.executemany(
                    'DELETE FROM replaceable WHERE event_id = ?', ids)
                self.store.db.executemany(
                    'DELETE FROM events WHERE id = ?', ids)
                deleted += len(rows)
            self.store.db.commit()
        self.log(f'retention purge: {deleted} events older than '
                 f'{months} months removed')
        return {'deleted': deleted, 'cutoff': cutoff}

    def _owner(self) -> bytes | None:
        hex_pk = self.cfg['owner']['pubkey']
        if hex_pk and len(hex_pk) == 64:
            try:
                return bytes.fromhex(hex_pk)
            except ValueError:
                pass
        return None

    async def run_forever(self):
        # check hourly; the daily purge fires when the UTC day rolls over
        last_day = None
        while True:
            try:
                today = int(time.time() // 86400)
                if last_day is not None and today != last_day:
                    self.purge()
                last_day = today
            except Exception as e:
                self.log(f'retention error: {e!r}')
            await asyncio.sleep(3600)
