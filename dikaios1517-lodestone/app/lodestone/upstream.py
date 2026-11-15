"""Upstream relay client: fetch events from other relays (crawler/aggregator).

One WebSocket per request batch; bounded timeouts everywhere so a dead relay
never stalls the workers.
"""
import asyncio
import json
import os
import ssl as _ssl
import time

import websockets

# Never route upstream connections through HTTPS_PROXY-style env vars:
# umbrelOS containers connect directly, and a stale/broken proxy var would
# black-hole every outbound websocket while wisp-style clients that ignore
# the env keep working. websockets 15 reads these implicitly and has no
# `proxy=False` sentinel, so we clear them at import.
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
    """Yield event dicts matching filters from one relay until EOSE/timeout.

    Non-EVENT relay frames (CLOSED/NOTICE/AUTH) are normally swallowed; pass
    a `diag` list to capture them as short strings for diagnostics.
    """
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
    """First (latest, by limit-1 ordering) event matching the filters."""
    async for ev in fetch_events(relay_url, filters, timeout_s,
                                 max_events=1, diag=diag):
        return ev
    return None


async def probe_relay(relay_url: str, timeout_s: float = 8.0) -> str:
    """Reachability check: 'ok' or a short classified failure reason.

    Distinguishes dns / refused / timeout / tls / http-status / network so
    the GUI can show WHY a relay is unreachable (e.g. everything timing out
    after DNS succeeds suggests an MTU/handshake blackhole).
    """
    import socket as _socket
    kwargs = {'open_timeout': timeout_s, 'close_timeout': 2}
    if relay_url.startswith('wss://'):
        kwargs['ssl'] = _ssl_ctx()
    try:
        async with websockets.connect(relay_url, **kwargs):
            return 'ok'
    except _socket.gaierror:
        return 'dns lookup failed'
    except (ConnectionRefusedError, ConnectionResetError):
        return 'connection refused'
    except asyncio.TimeoutError:
        return 'connect timed out'
    except websockets.exceptions.InvalidURI:
        return 'not a ws:// or wss:// URL'
    except websockets.exceptions.InvalidStatus as e:
        return f'http {e.response.status_code}'
    except websockets.exceptions.InvalidHandshake as e:
        return f'handshake failed: {e!r}'[:80]
    except OSError as e:
        return f'network error: {e}'[:80]
