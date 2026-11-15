"""Social-graph crawler.

Owner npub -> indexer relays -> kind 10002 relay list -> kind 3 follow list
-> BFS expansion to `graph.depth` hops (default 3), re-crawled on a schedule
(default 24 h). Every discovered pubkey's kind 10002 is cached in the store
so the aggregator knows where each user writes/reads.
"""
import asyncio
import time

from .upstream import fetch_events, fetch_latest
from .schnorr import verify

MAX_RELAYS_PER_KEY = 16      # cap fan-out per pubkey
CONCURRENT_FETCHES = 6


class GraphCrawler:
    def __init__(self, store, config, log=None):
        self.store = store
        self.cfg = config
        self.log = log or (lambda *a: None)

    def _owner(self) -> bytes | None:
        hex_pk = self.cfg['owner']['pubkey']
        if hex_pk and len(hex_pk) == 64:
            try:
                return bytes.fromhex(hex_pk)
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------- fetchers
    async def fetch_relay_list(self, pubkey: bytes) -> list[tuple[str, bool, bool]]:
        """Kind 10002 via the indexer relays; caches in the store."""
        cached = self.store.relay_list(pubkey)
        filters = [{'kinds': [10002], 'authors': [pubkey.hex()], 'limit': 1}]
        indexers = self.cfg['graph']['indexer_relays']
        for relay_url in indexers:
            ev = await fetch_latest(relay_url, filters,
                                    self.cfg['aggregation']['upstream_timeout_s'])
            if not ev:
                continue
            if not self._valid(ev, pubkey):
                continue
            entries = self._parse_10002(ev)
            self.store.set_relay_list(pubkey, entries)
            # the relay-list event itself belongs in the local store too
            self.store.store_event(ev)
            return entries
        return [(u, True, True) for u in cached]  # stale cache fallback

    @staticmethod
    def _valid(ev: dict, expected_author: bytes) -> bool:
        from .store import event_id
        try:
            if bytes.fromhex(ev['pubkey']) != expected_author:
                return False
            if event_id(ev).hex() != ev['id']:
                return False
            return verify(bytes.fromhex(ev['pubkey']),
                          bytes.fromhex(ev['id']),
                          bytes.fromhex(ev['sig']))
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _parse_10002(ev: dict) -> list[tuple[str, bool, bool]]:
        out = {}
        for tag in ev['tags']:
            if not (isinstance(tag, list) and len(tag) >= 2
                    and tag[0] == 'r' and isinstance(tag[1], str)):
                continue
            url = tag[1].strip()
            if not url.startswith(('ws://', 'wss://')):
                continue
            marker = tag[2] if len(tag) >= 3 and isinstance(tag[2], str) else ''
            rd = marker != 'write'
            wr = marker != 'read'
            if url in out:
                rd = rd or out[url][0]
                wr = wr or out[url][1]
            out[url] = (rd, wr)
        return [(u, rd, wr) for u, (rd, wr) in list(out.items())[:MAX_RELAYS_PER_KEY]]

    async def fetch_follows(self, pubkey: bytes,
                            relays: list[str]) -> set[bytes]:
        """Kind 3 p-tags from the pubkey's own relays (indexer fallback)."""
        filters = [{'kinds': [3], 'authors': [pubkey.hex()], 'limit': 1}]
        seen = set()
        for relay_url in relays + self.cfg['graph']['indexer_relays']:
            if relay_url in seen:
                continue
            seen.add(relay_url)
            ev = await fetch_latest(relay_url, filters,
                                    self.cfg['aggregation']['upstream_timeout_s'])
            if not ev or not self._valid(ev, pubkey):
                continue
            self.store.store_event(ev)  # keep the follow list locally
            follows = set()
            for tag in ev['tags']:
                if (isinstance(tag, list) and len(tag) >= 2
                        and tag[0] == 'p' and isinstance(tag[1], str)
                        and len(tag[1]) == 64):
                    try:
                        follows.add(bytes.fromhex(tag[1]))
                    except ValueError:
                        continue
            return follows
        return set()

    # ---------------------------------------------------------------- crawl
    async def crawl(self) -> dict:
        owner = self._owner()
        if not owner:
            self.log('no owner pubkey configured; skipping graph crawl')
            return {'ok': False, 'reason': 'no owner pubkey'}

        max_depth = int(self.cfg['graph']['depth'])
        max_pubkeys = int(self.cfg['aggregation'].get('max_graph_pubkeys', 5000))
        sem = asyncio.Semaphore(CONCURRENT_FETCHES)
        graph = {owner: 0}
        frontier = [owner]

        for depth in range(1, max_depth + 1):
            if not frontier or len(graph) >= max_pubkeys:
                break

            async def expand(pk):
                async with sem:
                    entries = await self.fetch_relay_list(pk)
                    read_relays = [u for u, rd, wr in entries if rd]
                    return pk, await self.fetch_follows(pk, read_relays)

            results = await asyncio.gather(
                *[expand(pk) for pk in frontier], return_exceptions=True)
            next_frontier = []
            for res in results:
                if isinstance(res, Exception):
                    continue
                pk, follows = res
                for f in follows:
                    if f not in graph and len(graph) < max_pubkeys:
                        graph[f] = depth
                        next_frontier.append(f)
            self.log(f'graph depth {depth}: +{len(next_frontier)} pubkeys '
                     f'(total {len(graph)})')
            frontier = next_frontier

        # pubkeys discovered at max depth never went through expand();
        # fetch their relay lists so the aggregator knows where they write
        leaf = [pk for pk, d in graph.items()
                if d > 0 and not self.store.relay_list(pk)]
        sem2 = asyncio.Semaphore(CONCURRENT_FETCHES)

        async def list_only(pk):
            async with sem2:
                await self.fetch_relay_list(pk)

        if leaf:
            await asyncio.gather(*[list_only(pk) for pk in leaf])
            self.log(f'fetched relay lists for {len(leaf)} leaf pubkeys')

        self.store.set_graph([(pk, d) for pk, d in graph.items()])
        self.store.set_relay_sync('__graph__', int(time.time()))
        return {'ok': True, 'pubkeys': len(graph)}

    def graph_age(self) -> float | None:
        """Seconds since the last COMPLETED crawl, or None if never."""
        ts = self.store.relay_sync('__graph__')
        return None if ts is None else time.time() - ts

    async def run_forever(self):
        # Startup policy (spec): if no social graph exists, or the existing
        # one is older than the refresh window, build it first. If it is
        # still fresh from a previous run, skip straight ahead so the
        # aggregator can begin immediately.
        interval = max(60, int(self.cfg['graph']['refresh_hours']) * 3600)
        while True:
            age = self.graph_age()
            if age is not None and age < interval:
                await asyncio.sleep(interval - age)
                continue
            started = time.time()
            try:
                await self.crawl()
            except Exception as e:
                self.log(f'graph crawl error: {e!r}')
            await asyncio.sleep(max(60, interval - (time.time() - started)))
