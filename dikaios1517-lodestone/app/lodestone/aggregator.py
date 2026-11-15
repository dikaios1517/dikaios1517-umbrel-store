"""Aggregation worker: pulls notes for the owner's social graph from the
relays those users actually use.

Two request families per upstream relay, every poll (default 5 min):
1. {authors: [graph batch], kinds: allowed, since: last-sync}  — everything
   the graph members publish.
2. {#p: [graph batch], kinds: [1, 9735, 1018, 1019], since}  — replies,
   zap receipts and poll responses that tag graph members. Stored from
   graph authors freely; from strangers only above the PoW threshold.
"""
import asyncio
import time

from . import schnorr
from .relay import leading_zero_bits
from .store import event_id
from .upstream import fetch_events

AUTHORS_PER_REQ = 100
STRANGER_TAG_KINDS = [1, 9735, 1018, 1019]   # replies, zap receipts, poll responses
SYNC_OVERLAP_S = 600                          # re-fetch window to absorb clock skew


class Aggregator:
    def __init__(self, store, config, log=None):
        self.store = store
        self.cfg = config
        self.log = log or (lambda *a: None)
        self.last_poll = None   # {'stored': n, 'relays': n, 'at': ts} after each sweep

    def _members(self) -> list[bytes]:
        """Owner + every graph pubkey, owner first."""
        owner_hex = self.cfg['owner']['pubkey']
        members = []
        if owner_hex and len(owner_hex) == 64:
            try:
                members.append(bytes.fromhex(owner_hex))
            except ValueError:
                pass
        for pk in sorted(self.store.graph_members()):
            if pk not in members:
                members.append(pk)
        return members

    def _relays_for(self, pubkey: bytes) -> list[str]:
        urls = self.store.relay_list(pubkey, 'write')
        if not urls:
            urls = self.store.relay_list(pubkey, 'read')
        if not urls:
            urls = list(self.cfg['aggregation'].get('fallback_relays', []))
        return urls[:8]

    def _valid_event(self, ev: dict) -> bool:
        try:
            if event_id(ev).hex() != ev['id']:
                return False
            if not schnorr.verify(bytes.fromhex(ev['pubkey']),
                                  bytes.fromhex(ev['id']),
                                  bytes.fromhex(ev['sig'])):
                return False
        except (KeyError, ValueError):
            return False
        limits = self.cfg['limits']
        if not isinstance(ev.get('content'), str) \
                or len(ev['content']) > limits['max_content_length']:
            return False
        if not isinstance(ev.get('tags'), list) \
                or len(ev['tags']) > limits['max_event_tags']:
            return False
        if ev['kind'] not in self.cfg['kinds']['allowed']:
            return False
        return True

    def _filtered_out(self, ev: dict) -> bool:
        author = bytes.fromhex(ev['pubkey'])
        if author in self.store.members('blacklist'):
            return True
        content_low = ev['content'].lower()
        for w in self.cfg['filters']['words']:
            if w and w.lower() in content_low:
                return True
        for h in self.cfg['filters']['hashtags']:
            if h:
                h = h.lstrip('#').lower()
                if ('#' + h) in content_low:
                    return True
                for tag in ev['tags']:
                    if (isinstance(tag, list) and len(tag) >= 2
                            and tag[0] == 't' and isinstance(tag[1], str)
                            and tag[1].lstrip('#').lower() == h):
                        return True
        return False

    def _ingest(self, ev: dict, graph_pks: set[bytes]) -> bool:
        """Aggregator admission: verified event from the graph, or a
        stranger's tagged reply/zap/poll-response above the PoW threshold."""
        if not self._valid_event(ev):
            return False
        if self._filtered_out(ev):
            return False
        author = bytes.fromhex(ev['pubkey'])
        is_graph = author in graph_pks
        if not is_graph:
            kinds = self.cfg['aggregation']
            # non-graph content is accepted only when it tags a member AND
            # carries the configured PoW
            if leading_zero_bits(ev['id']) < int(kinds['pow_threshold']):
                return False
        stored, _ = self.store.store_event(ev)
        return stored

    def _tagged_is_relevant(self, ev: dict, graph_pks: set[bytes]) -> bool:
        for tag in ev['tags']:
            if (isinstance(tag, list) and len(tag) >= 2 and tag[0] == 'p'
                    and isinstance(tag[1], str) and len(tag[1]) == 64):
                try:
                    if bytes.fromhex(tag[1]) in graph_pks:
                        return True
                except ValueError:
                    continue
        return False

    async def poll(self) -> dict:
        members = self._members()
        graph_pks = set(members)
        # First-ever sync of a relay gets the retention window, not the
        # regular overlap window — otherwise a fresh install would only
        # pull the last few minutes and the feed would stay empty until
        # graph members posted something new.
        backfill_s = min(int(self.cfg['retention']['months']), 24) * 30 * 86400
        stats = {'stored': 0, 'relays': 0}

        # relay -> set of members that use it
        by_relay: dict[str, set[bytes]] = {}
        for pk in members:
            for url in self._relays_for(pk):
                by_relay.setdefault(url, set()).add(pk)

        for url, pks in by_relay.items():
            last = self.store.relay_sync(url)
            since = (last - SYNC_OVERLAP_S) if last \
                else int(time.time()) - backfill_s
            stats['relays'] += 1

            async def pull(filters, tagged=False):
                n = 0
                async for ev in fetch_events(
                        url, filters,
                        self.cfg['aggregation']['upstream_timeout_s']):
                    if tagged and not self._tagged_is_relevant(ev, graph_pks):
                        continue
                    if self._ingest(ev, graph_pks):
                        n += 1
                return n

            # 1. graph members' own events, batched
            pk_list = sorted(pks)
            for i in range(0, len(pk_list), AUTHORS_PER_REQ):
                batch = [pk.hex() for pk in pk_list[i:i + AUTHORS_PER_REQ]]
                stats['stored'] += await pull(
                    [{'authors': batch, 'kinds': self.cfg['kinds']['allowed'],
                      'since': since}])

            # 2. tagged replies/zaps/poll-responses: these live on the
            # responder's relays, so scan EVERY relay for anything that tags
            # any graph member (batched)
            for i in range(0, len(members), AUTHORS_PER_REQ):
                batch = [pk.hex() for pk in members[i:i + AUTHORS_PER_REQ]]
                stats['stored'] += await pull(
                    [{'kinds': STRANGER_TAG_KINDS, '#p': batch, 'since': since}],
                    tagged=True)

            self.store.set_relay_sync(url, int(time.time()))
        return stats

    def _graph_fresh(self) -> bool:
        ttl = max(60, int(self.cfg['graph']['refresh_hours']) * 3600)
        ts = self.store.relay_sync('__graph__')
        return ts is not None and time.time() - ts < ttl

    async def run_forever(self):
        # Startup policy (spec): a note aggregation cycle begins only once a
        # fresh social graph exists. If the graph is missing or stale, the
        # crawler builds it first; wait for it (up to 10 minutes, so a
        # failing crawl can't stall aggregation forever) before sweeping.
        deadline = time.time() + 600
        while not self._graph_fresh() and time.time() < deadline:
            await asyncio.sleep(5)
        while True:
            started = time.time()
            try:
                stats = await self.poll()
                self.last_poll = {**stats, 'at': int(time.time())}
                if stats['stored']:
                    self.log(f"aggregated {stats['stored']} events "
                             f"from {stats['relays']} relays")
            except Exception as e:
                self.last_poll = {'stored': 0, 'relays': 0,
                                  'at': int(time.time()), 'error': repr(e)}
                self.log(f'aggregation error: {e!r}')
            interval = int(self.cfg['aggregation']['interval_minutes']) * 60
            await asyncio.sleep(max(30, interval - (time.time() - started)))
