"""Lodestone entrypoint: one process, two ports.

    python -m lodestone.main

Relay websocket on cfg.relay.port (default 7070), GUI on cfg.relay.gui_port
(default 3001). Background workers: graph crawler, aggregator, retention.
"""
import asyncio
import os
import sys

from . import config as lconfig
from .aggregator import Aggregator
from .graph import GraphCrawler
from .gui_server import Gui
from .relay import Relay
from .retention import Retention
from .store import EventStore

DATA_DIR = os.environ.get('LODESTONE_DATA', '/data')


def main() -> int:
    cfg_path = os.environ.get('LODESTONE_CONFIG',
                              os.path.join(DATA_DIR, 'lodestone.toml'))
    if not os.path.exists(cfg_path):
        os.makedirs(os.path.dirname(cfg_path) or '.', exist_ok=True)
        lconfig.save(lconfig.DEFAULTS, cfg_path)
        print(f'wrote initial config to {cfg_path}', file=sys.stderr)
    cfg = lconfig.load(cfg_path)

    store = EventStore(os.path.join(DATA_DIR, 'lodestone.db'))

    def log(*a):
        print(f'[{int(asyncio.get_event_loop().time())}]', *a, flush=True)

    crawler = GraphCrawler(store, cfg, log=log)
    aggregator = Aggregator(store, cfg, log=log)
    retention = Retention(store, cfg, log=log)
    onion = os.environ.get('LODESTONE_ONION',
                           os.environ.get('TOR_HOSTNAME', '')).strip()
    relay = Relay(store, cfg, onion_host=onion)
    gui = Gui(store, cfg, retention, crawler, aggregator, log=log)

    async def run():
        await relay.start(cfg['relay']['bind'], cfg['relay']['port'])
        await gui.start(cfg['relay']['bind'], cfg['relay']['gui_port'])
        await asyncio.gather(
            crawler.run_forever(),
            aggregator.run_forever(),
            retention.run_forever(),
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
