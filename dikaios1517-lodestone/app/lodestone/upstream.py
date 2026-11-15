"""Upstream relay client: fetch events from other relays (crawler/aggregator).

One WebSocket per request batch; bounded timeouts everywhere so a dead relay
never stalls the workers.
"""
import asyncio
import json
import ssl as _ssl
import time

import websockets

_ctx = None


def _ssl_ctx():
    global _ctx
    if _ctx is None:
        _ctx = _ssl.create_default_context()
    return _ctx


async def fetch_events(relay_url: str, filters: list[dict],
                       timeout_s: float = 8.0, max_events: int = 5000):
    """Yield event dicts matching filters from one relay until EOSE/timeout."""
    sub = f'ls{int(time.time() * 1000)}'
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
                    return
    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake,
            websockets.ConnectionClosed, asyncio.TimeoutError):
        return


async def fetch_latest(relay_url: str, filters: list[dict],
                       timeout_s: float = 8.0) -> dict | None:
    """First (latest, by limit-1 ordering) event matching the filters."""
    async for ev in fetch_events(relay_url, filters, timeout_s, max_events=1):
        return ev
    return None
