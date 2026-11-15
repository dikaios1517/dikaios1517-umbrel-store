"""Upstream relay client: fetch events from other relays.

Adapted from Lodestone (clean-room, same author). One WebSocket per request
batch; bounded timeouts everywhere so a dead relay never stalls the walk.
"""
import asyncio
import json
import os
import ssl as _ssl
import time

import websockets

# Never route upstream connections through proxy env vars: umbrelOS
# containers connect directly, and a stale/broken proxy var would
# black-hole every outbound websocket.
for _var in ('WSS_PROXY', 'wss_proxy', 'WS_PROXY', 'ws_proxy',
             'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
             'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_var, None)

_ctx = None


def _ssl_ctx():
    global _ctx
    if _ctx is None:
        _ctx = _ssl.create_default_context()
    return _ctx


async def fetch_events(relay_url: str, filters: list[dict],
                       timeout_s: float = 8.0, max_events: int = 5000,
                       diag: list | None = None):
    """Yield event dicts matching filters from one relay until EOSE/timeout."""
    sub = f'md{int(time.time() * 1000)}'
    kwargs = {'open_timeout': timeout_s, 'close_timeout': 2,
              'max_size': 4 * 1024 * 1024}
    if relay_url.startswith('wss://'):
        kwargs['ssl'] = _ssl_ctx()
    try:
        async with websockets.connect(relay_url, **kwargs) as ws:
            await ws.send(json.dumps(['REQ', sub] + filters))
            n = 0
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
                except (asyncio.TimeoutError, OSError,
                        websockets.ConnectionClosed):
                    return
                try:
                    arr = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not (isinstance(arr, list) and arr):
                    continue
                if arr[0] == 'EVENT' and len(arr) >= 3 \
                        and isinstance(arr[2], dict):
                    yield arr[2]
                    n += 1
                    if n >= max_events:
                        return
                elif arr[0] == 'EOSE':
                    return
                elif arr[0] == 'CLOSED':
                    if diag is not None and len(arr) > 2:
                        diag.append('closed: ' + str(arr[2])[:100])
                    return
                elif arr[0] == 'NOTICE' and diag is not None:
                    diag.append('notice: ' + str(arr[1])[:100]
                                if len(arr) > 1 else 'notice')
                elif arr[0] == 'AUTH' and diag is not None:
                    diag.append('relay requires NIP-42 auth for this query')
    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake,
            websockets.ConnectionClosed, asyncio.TimeoutError):
        return


async def fetch_latest(relay_url: str, filters: list[dict],
                       timeout_s: float = 8.0, diag: list | None = None) \
        -> dict | None:
    """First (latest) event matching the filters."""
    async for ev in fetch_events(relay_url, filters, timeout_s,
                                 max_events=1, diag=diag):
        return ev
    return None
