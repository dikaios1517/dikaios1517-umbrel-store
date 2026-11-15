"""Social-graph + media-server discovery.

Root npub -> kind 10002 relay list -> kind 3 follow list (root's write
relays, indexer fallback) -> batched kind 10063 media-server lists for the
root and every follow -> mapping of server URL -> declaring pubkeys.
"""
import asyncio
import re

from . import events
from .upstream import fetch_events, fetch_latest

CONCURRENT_FETCHES = 6
AUTHOR_CHUNK = 100


def parse_10002(ev: dict) -> list[str]:
    urls = []
    for t in ev.get('tags', []):
        if (isinstance(t, list) and len(t) >= 2 and t[0] == 'r'
                and isinstance(t[1], str)
                and t[1].startswith(('ws://', 'wss://'))):
            marker = t[2] if len(t) >= 3 and isinstance(t[2], str) else ''
            if marker != 'write':  # include read and unmarked
                urls.append(t[1])
    return list(dict.fromkeys(urls))


def parse_10063(ev: dict) -> list[str]:
    from .blossom import normalize_server
    out = []
    for url in events.parse_taglist(ev, 'server'):
        n = normalize_server(url)
        if n:
            out.append(n)
    return list(dict.fromkeys(out))


def parse_imeta(ev: dict) -> list[dict]:
    """NIP-92/NIP-68 imeta tags -> blob descriptors.

    Each imeta value is space-separated key/value pairs:
    `url https://srv/<sha256>.png sha256 <hex> m image/jpeg dim 800x600`.
    The blossom origin (scheme://host) becomes the 'server'.
    """
    from .blossom import normalize_server
    out = []
    for value in events.parse_taglist(ev, 'imeta'):
        parts = value.split()
        kv = {}
        for i in range(0, len(parts) - 1, 2):
            kv[parts[i]] = parts[i + 1]
        url = kv.get('url', '')
        sha = (kv.get('sha256') or '').lower()
        if not sha or len(sha) != 64:
            # blossom-style URL: /<64-hex>.<ext>
            import re
            m = re.search(r'/([0-9a-fA-F]{64})(?:\.[a-zA-Z0-9]+)?'
                          r'(?![0-9a-fA-F])', url)
            if not m:
                continue
            sha = m.group(1).lower()
        server = normalize_server(url)
        if not server:
            continue
        typ = kv.get('m')
        if not typ:
            typ = _mime_from_url(url)
        out.append({'sha256': sha, 'size': None, 'type': typ,
                    'name': kv.get('alt') or kv.get('name'),
                    'created_at': int(ev.get('created_at') or 0),
                    'server': server})
    return out


_MIME_BY_EXT = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'avif': 'image/avif',
    'bmp': 'image/bmp', 'svg': 'image/svg+xml',
    'mp4': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime',
    'mkv': 'video/x-matroska', 'm4v': 'video/x-m4v',
    'mp3': 'audio/mpeg', 'ogg': 'audio/ogg', 'wav': 'audio/wav',
    'm4a': 'audio/mp4', 'flac': 'audio/flac', 'pdf': 'application/pdf',
}


def _mime_from_url(url: str) -> str | None:
    ext = url.rsplit('.', 1)[-1].lower() if '.' in url.rsplit('/', 1)[-1] \
        else ''
    return _MIME_BY_EXT.get(ext)


_BLOB_URL = re.compile(
    r'https?://[^\s<>"\']+/' + r'([0-9a-fA-F]{64})(?:\.[a-zA-Z0-9]+)?'
    r'(?![0-9a-fA-F])')


def parse_content_urls(ev: dict) -> list[dict]:
    """Blossom-style URLs pasted in note content (no imeta tag).

    Only URLs with a 64-hex path segment qualify — that's Blossom's
    content addressing, so random web links are never picked up.
    """
    out = []
    seen = set()
    for match in _BLOB_URL.finditer(ev.get('content') or ''):
        url, sha = match.group(0), match.group(1).lower()
        if sha in seen:
            continue
        server = _origin(url)
        if not server:
            continue
        seen.add(sha)
        out.append({'sha256': sha, 'size': None, 'type': _mime_from_url(url),
                    'name': None, 'created_at': int(ev.get('created_at') or 0),
                    'server': server})
    return out


def _origin(url: str) -> str | None:
    from .blossom import normalize_server
    return normalize_server(url)


class MeadowGraph:
    def __init__(self, store, cfg, log=None):
        self.store = store
        self.cfg = cfg
        self.log = log or (lambda *a: None)

    async def _follows_of(self, root_hex: str,
                          relay_lists: list[str]) -> set[str]:
        """Kind 3 p-tags: root's read relays first, then indexers."""
        filters = [{'kinds': [3], 'authors': [root_hex], 'limit': 1}]
        seen = set()
        diag = []
        for relay_url in relay_lists + self.cfg['index_relays']:
            if relay_url in seen:
                continue
            seen.add(relay_url)
            ev = await fetch_latest(relay_url, filters,
                                    self.cfg['relay_timeout_s'], diag=diag)
            if not ev:
                continue
            if not events.valid(ev, root_hex):
                diag.append(f'{relay_url}: invalid kind 3 event')
                continue
            follows = {t for t in events.parse_taglist(ev, 'p')
                       if len(t) == 64}
            self.log(f'follow list found on {relay_url}: {len(follows)} '
                     'follows')
            return follows
        self.log('no valid follow list found; diag: ' + '; '.join(diag[:4]))
        return set()

    async def _relay_lists_of(self, root_hex: str) -> list[str]:
        filters = [{'kinds': [10002], 'authors': [root_hex], 'limit': 1}]
        for relay_url in self.cfg['index_relays']:
            ev = await fetch_latest(relay_url, filters,
                                    self.cfg['relay_timeout_s'])
            if ev and events.valid(ev, root_hex):
                relays = parse_10002(ev)
                self.log(f'root relay list: {len(relays)} relays')
                return relays
        return []

    async def _media_servers(self, authors: list[str], progress=None) \
            -> dict[str, list[str]]:
        """Batched kind 10063 fetch for all authors from the index relays."""
        latest: dict[str, dict] = {}   # author -> newest valid 10063
        filters = [{'kinds': [10063],
                    'authors': authors[i:i + AUTHOR_CHUNK],
                    'limit': 500}
                   for i in range(0, len(authors), AUTHOR_CHUNK)]
        seen_relays = set()
        for relay_url in self.cfg['index_relays']:
            if relay_url in seen_relays:
                continue
            seen_relays.add(relay_url)
            got = 0
            async for ev in fetch_events(
                    relay_url, filters, self.cfg['relay_timeout_s'],
                    max_events=len(authors) * 2):
                if not events.valid(ev):
                    continue
                got += 1
                prev = latest.get(ev['pubkey'])
                if prev is None or ev.get('created_at', 0) >= \
                        prev.get('created_at', 0):
                    latest[ev['pubkey']] = ev
            self.log(f'{relay_url}: {got} media-server lists')
            if progress:
                await progress('servers', got=got)
            # enough indexers answered for a good sample
            if len(latest) >= max(1, len(authors) // 2):
                break
        mapping: dict[str, list[str]] = {}
        for author, ev in latest.items():
            for server in parse_10063(ev):
                mapping.setdefault(server, []).append(author)
        return mapping

    async def walk(self, root_hex: str, progress=None) -> dict:
        async def report(stage, **kw):
            if progress:
                await progress(stage, **kw)

        await report('relaylist')
        relay_lists = await self._relay_lists_of(root_hex)

        await report('follows')
        follows = await self._follows_of(root_hex, relay_lists)
        max_follows = int(self.cfg['max_follows'])
        if len(follows) > max_follows:
            follows = set(sorted(follows)[:max_follows])
        await report('follows-found', count=len(follows))

        authors = [root_hex] + sorted(follows)
        server_map = await self._media_servers(authors, progress=progress)

        # Primary blob source: the graph's own media events (kinds 20/21/22
        # picture/video + NIP-92 imeta attachments in notes), fetched in
        # bulk from the index relays. Server /list is a secondary source —
        # most public servers refuse anonymous listing.
        await report('media-events')
        n = await self._media_events(authors, progress=progress)

        # Fallback: authors with no kind 10063 use the default server set.
        with_list = set().union(*server_map.values()) if server_map else set()
        plain = [pk for pk in authors if pk not in with_list]
        for server in self.cfg['default_servers']:
            server_map.setdefault(server, []).extend(plain)

        return {'root': root_hex, 'follows': sorted(follows),
                'media_events': n,
                'servers': {s: pks for s, pks in server_map.items() if pks}}

    async def _media_events(self, authors: list[str], progress=None) -> int:
        """Fetch media-carrying events for all authors, store blobs."""
        total = 0
        chunked = [{'kinds': [1, 20, 21, 22],
                    'authors': authors[i:i + AUTHOR_CHUNK],
                    'limit': 400}
                   for i in range(0, len(authors), AUTHOR_CHUNK)]
        for relay_url in self.cfg['index_relays']:
            got = 0
            async for ev in fetch_events(
                    relay_url, chunked, self.cfg['relay_timeout_s'],
                    max_events=3000):
                if not events.valid(ev):
                    continue
                ev_type = ev.get('kind')
                descs = parse_imeta(ev)
                if ev_type == 1:
                    # plain notes: blossom URLs pasted in the text itself
                    descs = descs + parse_content_urls(ev)
                if descs:
                    self.store.upsert_owned(descs, ev['pubkey'])
                    total += len(descs)
                    got += len(descs)
            self.log(f'{relay_url}: {got} blobs from media events')
            if progress:
                await progress('media-events', blobs=total)
            if total >= 1000:
                break
        return total
