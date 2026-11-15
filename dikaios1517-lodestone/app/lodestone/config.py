"""Lodestone configuration: defaults, TOML load/save.

The GUI edits the same TOML file the relay reads. Unknown keys are preserved
on save (we round-trip the parsed dict, adding/changing only what the GUI
sends).
"""
import os
import tomllib

DEFAULTS = {
    'relay': {
        'name': 'Lodestone',
        'description': 'A personal Nostr relay',
        'bind': '0.0.0.0',
        'port': 7070,
        'gui_port': 3001,
    },
    'owner': {
        # 64-char hex (GUI accepts npub and stores hex)
        'pubkey': '',
    },
    'graph': {
        'depth': 3,                 # hops: follows, their follows, their follows
        'refresh_hours': 24,        # social graph refresh interval
        'indexer_relays': [
            'wss://purplepag.es',
            'wss://indexer.coracle.social',
            'wss://user.kindpag.es',
            'wss://profiles.nostr1.com',
        ],
    },
    'aggregation': {
        'interval_minutes': 5,      # poll graph members' write relays
        'pow_threshold': 16,        # PoW bits required from strangers' tagged replies/zaps/poll responses
        'upstream_timeout_s': 8,
        'max_graph_pubkeys': 5000,  # hard cap on graph size
        'fallback_relays': [        # used for members with no kind 10002
            'wss://relay.damus.io',
            'wss://nos.lol',
            'wss://relay.primal.net',
        ],
    },
    'retention': {
        'months': 12,
        'owner_kept_indefinitely': True,
    },
    'kinds': {
        # Everything the relay stores, per the spec: profile/relay/list metadata
        # plus common feed kinds. Owner-editable in the GUI.
        'allowed': [
            0,      # profile metadata
            1,      # text note
            2,      # relay recommendation (legacy)
            3,      # follow list
            5,      # event deletion request (NIP-09)
            6,      # repost
            7,      # reaction
            20,     # picture
            21,     # video
            22,     # short video
            1068,   # poll (NIP-C7)
            1018, 1019,  # poll responses
            1984,   # report
            9734,   # zap request
            9735,   # zap receipt
            10002,  # relay list metadata
            10003,  # bookmarks
            10015,  # interests
            10030,  # emoji set
            1063,   # file metadata
            22242,  # client auth
            30023,  # long-form content
        ],
    },
    'limits': {
        'max_message_length': 262144,
        'max_content_length': 65536,
        'max_subscriptions': 20,
        'max_filters': 10,
        'max_limit': 500,
        'max_subid_length': 64,
        'max_event_tags': 100,
        'min_pow_difficulty': 0,     # relay-wide gate; aggregation PoW is separate
    },
    'rate_limits': {
        'events_per_minute_per_ip': 300,
        'connections_per_ip': 20,
    },
    'timeouts': {
        'client_idle_s': 900,
        'upstream_connect_s': 10,
        'upstream_read_s': 5,
    },
    'security': {
        'allow_public_reads': True,
        'require_auth_writes': False,   # NIP-42 gate when True
    },
    'negentropy': {
        'enabled': False,  # NIP-77 planned for v2
    },
    'filters': {
        'words': [],        # reject notes containing these (case-insensitive)
        'hashtags': [],     # reject notes tagged with these (with or without #)
    },
}

CONFIG_PATH = os.environ.get('LODESTONE_CONFIG', '/data/lodestone.toml')
LOADED_PATH = None   # set by load(); save() prefers it


def _deep_merge(base, extra):
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list)
               else v) for k, v in base.items()}
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str = None) -> dict:
    global LOADED_PATH
    path = path or CONFIG_PATH
    LOADED_PATH = path
    with open(path, 'rb') as f:
        user = tomllib.load(f)
    return _deep_merge(DEFAULTS, user)


def to_toml(cfg: dict) -> str:
    """Render config as TOML. Strings are quoted; lists of scalars inline."""
    def fmt(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, int) or isinstance(v, float):
            return str(v)
        if isinstance(v, list):
            return '[' + ', '.join(fmt(x) for x in v) + ']'
        s = str(v).replace('\\', '\\\\').replace('"', '\\"')
        s = s.replace('\n', '\\n').replace('\r', '')
        return f'"{s}"'

    lines = []
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        lines.append(f'[{section}]')
        for k, v in values.items():
            lines.append(f'{k} = {fmt(v)}')
        lines.append('')
    return '\n'.join(lines)


def save(cfg: dict, path: str = None) -> None:
    path = path or LOADED_PATH or CONFIG_PATH
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(to_toml(cfg))
    os.replace(tmp, path)
