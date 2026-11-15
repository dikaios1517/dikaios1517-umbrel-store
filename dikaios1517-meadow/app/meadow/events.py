"""NIP-01 event id + signature validation."""
import hashlib
import json

from . import schnorr


def serialize(ev: dict) -> bytes:
    return json.dumps(
        [0, ev['pubkey'], ev['created_at'], ev['kind'], ev['tags'],
         ev['content']],
        separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def event_id(ev: dict) -> str:
    return hashlib.sha256(serialize(ev)).hexdigest()


def valid(ev: dict, expected_author: str | None = None) -> bool:
    """True when id and signature check out (and author matches if given)."""
    try:
        if event_id(ev) != ev['id']:
            return False
        if expected_author is not None and ev['pubkey'] != expected_author:
            return False
        return schnorr.verify(bytes.fromhex(ev['pubkey']),
                              bytes.fromhex(ev['id']),
                              bytes.fromhex(ev['sig']))
    except (KeyError, ValueError, TypeError):
        return False


def parse_taglist(ev: dict, name: str) -> list[str]:
    return [t[1] for t in ev.get('tags', [])
            if isinstance(t, list) and len(t) >= 2 and t[0] == name
            and isinstance(t[1], str)]
