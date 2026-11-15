"""Meadow web server: gallery UI + JSON/SSE API + media proxy.

- GET  /                          static UI
- GET  /api/browse?npub=..[&force=1]   SSE: progress then done
- GET  /api/feed?root=<hex>&before=<ts>&limit=N[&kinds=all]
- GET  /api/profiles?pubkeys=a,b  cached kind-0 profiles (+ background refresh)
- GET  /media?server=<url>&sha256=<hex>   proxied blob (Range passthrough)
"""
import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from . import blossom, config, npub
from .graph import MeadowGraph
from .store import Store

STATIC_DIR = None  # set by main


def _log(msg: str):
    print(f'[meadow] {time.strftime("%H:%M:%S")} {msg}', flush=True)


class MeadowApp:
    def __init__(self, data_dir: str):
        self.cfg = config.load(data_dir)
        self.store = Store(data_dir)
        self.graph = MeadowGraph(self.store, self.cfg, log=_log)
        self.sem = asyncio.Semaphore(int(self.cfg['concurrent_fetches']))
        self._profile_task = None

    # ------------------------------------------------------------ helpers
    def allowed_servers(self) -> set[str]:
        servers = set(self.cfg['default_servers'])
        for (server,) in self.store.db.execute(
                'SELECT DISTINCT server FROM server_authors'):
            servers.add(server)
        return servers

    async def _collect_blobs(self, root: str,
                             servers: dict[str, list[str]],
                             progress) -> dict:
        """Query each server for each pubkey that listed it."""
        sem = self.sem
        timeout = float(self.cfg['server_timeout_s'])
        stats = {'servers_ok': 0, 'servers_err': 0, 'blobs': 0, 'detail': {}}

        async def one(session, server, pubkeys):
            total = 0
            reasons = {}
            async with sem:
                for pk in pubkeys:
                    desc, result = await blossom.list_blobs(
                        session, server, pk, timeout)
                    if desc:
                        self.store.upsert_blobs(server, desc, pk)
                        total += len(desc)
                    reasons[result] = reasons.get(result, 0) + 1
                    await asyncio.sleep(0.05)  # be polite to servers
            return server, total, reasons

        connector = aiohttp.TCPConnector(limit=int(
            self.cfg['concurrent_fetches']) * 2, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [one(session, server, pks)
                     for server, pks in servers.items()]
            done = 0
            for fut in asyncio.as_completed(tasks):
                try:
                    server, total, reasons = await fut
                except Exception as e:  # never let one server kill the walk
                    stats['servers_err'] += 1
                    _log(f'server task failed: {e!r}')
                    continue
                stats['detail'][server] = {'blobs': total, 'results': reasons}
                if total:
                    stats['servers_ok'] += 1
                else:
                    stats['servers_err'] += 1
                stats['blobs'] += total
                done += 1
                await progress('blobs', done=done,
                               total=len(tasks), blobs=stats['blobs'])
        self.store.meta_set('collect_at_' + root, int(time.time()))
        return stats

    # ------------------------------------------------------------- browse
    async def browse(self, request: web.Request):
        npub_in = request.query.get('npub', '')
        force = request.query.get('force') == '1'
        root = npub.npub_decode(npub_in)
        resp = web.StreamResponse(status=200, headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'})
        await resp.prepare(request)

        async def send(obj):
            await resp.write(f'data: {json.dumps(obj)}\n\n'.encode())

        if not root:
            await send({'type': 'error',
                        'message': 'Not a valid npub or pubkey'})
            await resp.write_eof()
            return resp

        async def progress(stage, **kw):
            await send({'type': 'progress', 'stage': stage, **kw})

        await send({'type': 'progress', 'stage': 'start', 'root': root})
        try:
            age = None if force else self.store.graph_age(root)
            cached = self.store.graph_servers(root)
            cache_hours = float(self.cfg['graph_cache_hours'])
            if age is not None and age < cache_hours * 3600 and cached:
                await send({'type': 'progress', 'stage': 'cache',
                            'age_hours': round(age / 3600, 1)})
                graph = {'root': root,
                         'follows': self.store.graph_follows(root),
                         'servers': dict(cached)}
            else:
                graph = await self.graph.walk(root, progress=progress)
                self.store.save_graph(graph['root'], graph['follows'],
                                      graph['servers'])
            await send({'type': 'progress', 'stage': 'collect',
                        'servers': len(graph['servers'])})
            stats = await self._collect_blobs(root, graph['servers'],
                                              progress)
            await send({'type': 'done', 'root': npub.npub_encode(root),
                        'root_hex': root,
                        'follows': len(graph['follows']),
                        'servers': len(graph['servers']),
                        'stats': {k: v for k, v in stats.items()
                                  if k != 'detail'}})
        except Exception as e:
            _log(f'browse error: {e!r}')
            await send({'type': 'error', 'message': repr(e)[:200]})
        await resp.write_eof()
        return resp

    # --------------------------------------------------------------- feed
    async def feed(self, request: web.Request):
        try:
            before = request.query.get('before')
            before = float(before) if before else None
            limit = min(100, max(1, int(
                request.query.get('limit', self.cfg['page_size']))))
        except ValueError:
            return web.json_response({'error': 'bad cursor'}, status=400)
        kinds = request.query.get('kinds', 'media')
        if kinds not in ('media', 'image', 'video', 'all'):
            kinds = 'media'
        items = self.store.feed(before, limit + 1, kinds)
        has_more = len(items) > limit
        items = items[:limit]
        pks = sorted({it['pubkey'] for it in items} |
                     ({request.query['root']} if 'root' in request.query
                      and len(request.query['root']) == 64 else set()))
        profiles = self.store.profiles(pks)
        self._maybe_refresh_profiles()
        return web.json_response({'items': items, 'has_more': has_more,
                                  'profiles': profiles})

    # ------------------------------------------------------------ profiles
    def _maybe_refresh_profiles(self):
        if self._profile_task and not self._profile_task.done():
            return
        stale = self.store.stale_profiles(
            int(self.cfg['profile_refresh_hours']) * 3600)[:200]
        if not stale:
            return
        self._profile_task = asyncio.get_running_loop().create_task(
            self._refresh_profiles(stale))

    async def _refresh_profiles(self, pubkeys: list[str]):
        seen = set()
        for relay_url in self.cfg['index_relays'][:3]:
            missing = [pk for pk in pubkeys
                       if pk not in seen and not
                       self.store.profiles([pk]).get(pk)]
            if not missing:
                return
            chunked = [{'kinds': [0], 'authors': missing[i:i + 100],
                        'limit': 1}
                       for i in range(0, len(missing), 100)]
            async for ev in fetch_events(
                    relay_url, chunked,
                    float(self.cfg['relay_timeout_s']),
                    max_events=len(missing)):
                if not events_ok(ev):
                    continue
                seen.add(ev['pubkey'])
                name, pic = None, None
                try:
                    d = json.loads(ev.get('content') or '{}')
                    if isinstance(d, dict):
                        name = (d.get('name') or d.get('display_name')
                                or '')[:80] or None
                        pic = (d.get('picture') or '')[:500] or None
                except (json.JSONDecodeError, TypeError):
                    pass
                self.store.set_profile(ev['pubkey'], name, pic)

    # --------------------------------------------------------------- media
    async def media(self, request: web.Request):
        server = blossom.normalize_server(request.query.get('server', ''))
        sha = request.query.get('sha256', '')
        if not server or len(sha) != 64:
            return web.json_response({'error': 'bad params'}, status=400)
        try:
            int(sha, 16)
        except ValueError:
            return web.json_response({'error': 'bad hash'}, status=400)
        host = urlparse(server).hostname or ''
        if not _host_is_public(host):
            return web.json_response({'error': 'blocked host'}, status=403)

        max_bytes = int(self.cfg['media_proxy_max_bytes'])
        timeout = aiohttp.ClientTimeout(total=120, connect=15)
        headers = {}
        if 'Range' in request.headers:
            headers['Range'] = request.headers['Range']
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                        URL_JOIN(server, sha), headers=headers) as up:
                    if up.status >= 400:
                        return web.json_response(
                            {'error': f'upstream {up.status}'},
                            status=up.status)
                    out = web.StreamResponse(status=up.status)
                    out.headers['Content-Type'] = \
                        up.headers.get('Content-Type',
                                       'application/octet-stream')
                    if 'Content-Length' in up.headers:
                        try:
                            if int(up.headers['Content-Length']) > max_bytes:
                                return web.json_response(
                                    {'error': 'too large'}, status=413)
                            out.headers['Content-Length'] = \
                                up.headers['Content-Length']
                        except ValueError:
                            pass
                    if 'Accept-Ranges' in up.headers:
                        out.headers['Accept-Ranges'] = \
                            up.headers['Accept-Ranges']
                    if 'Content-Range' in up.headers:
                        out.headers['Content-Range'] = \
                            up.headers['Content-Range']
                    if 'Content-Disposition' in up.headers:
                        out.headers['Content-Disposition'] = \
                            up.headers['Content-Disposition']
                    out.headers['Cache-Control'] = \
                        'public, max-age=86400, immutable'
                    await out.prepare(request)
                    sent = 0
                    async for chunk in up.content.iter_chunked(64 * 1024):
                        sent += len(chunk)
                        if sent > max_bytes:
                            await out.write_eof()
                            return out
                        await out.write(chunk)
                    await out.write_eof()
                    return out
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            _log(f'media proxy error for {host}: {e!r}')
            return web.json_response({'error': 'upstream failed'},
                                     status=502)


def URL_JOIN(server: str, sha: str) -> str:
    from yarl import URL
    return str(URL(server) / sha)


def events_ok(ev) -> bool:
    from . import events as _ev
    return _ev.valid(ev)


def _host_is_public(host: str) -> bool:
    """Reject hosts that resolve to loopback/private/link-local space."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def build_app(data_dir: str, static_dir: str) -> web.Application:
    STATIC_DIR = static_dir
    app = web.Application()
    meadow = MeadowApp(data_dir)

    app.router.add_get('/', _index(static_dir))
    app.router.add_get('/api/browse', meadow.browse)
    app.router.add_get('/api/feed', meadow.feed)
    app.router.add_get('/api/stats', _stats(meadow))
    app.router.add_get('/media', meadow.media)
    if static_dir:
        app.router.add_static('/static/', static_dir)
    return app


def _index(static_dir):
    async def handler(request):
        return web.FileResponse(f'{static_dir}/index.html')
    return handler


def _stats(meadow: MeadowApp):
    async def handler(request):
        return web.json_response({
            'blobs': meadow.store.blob_count(),
            'backend': __import__('meadow.schnorr', fromlist=['x']).backend(),
            'version': __import__('meadow', fromlist=['x']).__version__,
        })
    return handler
