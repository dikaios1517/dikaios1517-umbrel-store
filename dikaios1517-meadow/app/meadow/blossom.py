"""Blossom HTTP client (BUD-02 blob descriptor listing).

BUD-02: GET <server>/list?pubkey=<hex>[&since=..&until=..] returns a JSON
array of kind 1063 blob descriptor events authored by that pubkey, newest
first. Public reads need no auth; servers that do answer with 401/403 are
reported as unavailable-for-browsing, not errors.
"""
import asyncio
import json

import aiohttp
from yarl import URL

from . import events

# Cap per page: descriptor lists can be huge; the feed pages server-side
# from the store, so a bounded slice per (server, pubkey) is plenty.
MAX_DESCRIPTORS_PER_REQUEST = 1000

RESULT_OK = 'ok'
RESULT_EMPTY = 'empty'
RESULT_AUTH = 'auth required'
RESULT_BAD_RESPONSE = 'bad response'
RESULT_ERROR = 'error'


def normalize_server(url: str) -> str | None:
    """Keep only public http(s) URLs, scheme + host + path, trailing slash."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        return None
    try:
        u = URL(url)
    except ValueError:
        return None
    if not u.host or '@' in (u.raw_authority or ''):
        return None
    path = u.path or '/'
    return u.scheme + '://' + u.host + \
        ('' if u.port in (None, 80, 443) else f':{u.port}') + \
        ('/' if not path.endswith('/') else path)


def parse_descriptor(ev: dict) -> dict | None:
    """Kind 1063 -> {sha256, size, type, name, created_at} or None."""
    if not isinstance(ev, dict) or ev.get('kind') != 1063:
        return None
    tags = ev.get('tags') or []
    sha = size = typ = name = None
    for t in tags:
        if isinstance(t, list) and len(t) >= 2 and isinstance(t[1], str):
            if t[0] == 'x' and sha is None:
                sha = t[1].lower()
            elif t[0] == 'size' and size is None:
                try:
                    size = int(t[1])
                except ValueError:
                    pass
            elif t[0] == 'type' and typ is None:
                typ = t[1].split(';')[0].strip().split()[0] if t[1] else None
            elif t[0] == 'name' and name is None:
                name = t[1][:200]
    # content sometimes carries a JSON descriptor as well
    if (sha is None or size is None or typ is None) and ev.get('content'):
        try:
            d = json.loads(ev['content'])
            if isinstance(d, dict):
                sha = sha or d.get('sha256') or d.get('x')
                if size is None:
                    try:
                        size = int(d.get('size'))
                    except (TypeError, ValueError):
                        pass
                typ = typ or d.get('type')
                name = name or d.get('name')
        except (json.JSONDecodeError, TypeError):
            pass
    if not sha or len(sha) != 64:
        return None
    try:
        int(sha, 16)
    except ValueError:
        return None
    try:
        created = int(ev.get('created_at') or 0)
    except (TypeError, ValueError):
        created = 0
    return {'sha256': sha, 'size': size, 'type': typ or None,
            'name': name, 'created_at': created}


async def list_blobs(session: aiohttp.ClientSession, server_url: str,
                     pubkey_hex: str, timeout_s: float = 10.0,
                     verify: bool = True) -> tuple[list[dict], str]:
    """(descriptors, result) for one (server, pubkey)."""
    url = URL(server_url) / 'list'
    try:
        async with session.get(
                url, params={'pubkey': pubkey_hex},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                headers={'Accept': 'application/json'}) as resp:
            if resp.status in (401, 403):
                return [], RESULT_AUTH
            if resp.status == 404:
                return [], RESULT_EMPTY
            if resp.status != 200:
                return [], RESULT_BAD_RESPONSE
            try:
                data = await resp.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ClientError):
                return [], RESULT_BAD_RESPONSE
            if not isinstance(data, list):
                return [], RESULT_BAD_RESPONSE
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return [], RESULT_ERROR
    out = []
    for ev in data[:MAX_DESCRIPTORS_PER_REQUEST]:
        if verify and not events.valid(ev, pubkey_hex):
            continue
        d = parse_descriptor(ev)
        if d:
            out.append(d)
    return out, (RESULT_OK if out else RESULT_EMPTY)
