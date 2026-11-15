"""Defaults + seeded JSON config (user edits survive restarts/updates)."""
import json
import os

DEFAULTS = {
    # Index relays used to find follow lists (kind 3) and media-server
    # lists (kind 10063) in bulk.
    'index_relays': [
        'wss://nos.lol/',
        'wss://relay.damus.io/',
        'wss://relay.primal.net/',
        'wss://relay.nostr.band/',
    ],
    # Queried for authors who publish no kind 10063 list (most users).
    'default_servers': [
        'https://blossom.primal.net/',
        'https://blossom.band/',
        'https://blossom.nostr.build/',
        'https://blssm.us/',
    ],
    'relay_timeout_s': 8,
    'server_timeout_s': 10,
    'concurrent_fetches': 6,
    'max_follows': 1000,
    'graph_cache_hours': 12,
    'page_size': 30,
    'media_proxy_max_bytes': 209715200,
    'preview_max_bytes': 26214400,
    'profile_refresh_hours': 24,
}


def config_path(data_dir: str) -> str:
    return os.path.join(data_dir, 'meadow.json')


def load(data_dir: str) -> dict:
    cfg = dict(DEFAULTS)
    path = config_path(data_dir)
    if os.path.exists(path):
        try:
            with open(path) as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass
    else:
        try:
            save(data_dir, cfg)
        except OSError:
            pass
    return cfg


def save(data_dir: str, cfg: dict):
    os.makedirs(data_dir, exist_ok=True)
    with open(config_path(data_dir), 'w') as f:
        json.dump(cfg, f, indent=2)
