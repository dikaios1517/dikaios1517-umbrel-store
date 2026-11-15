"""Meadow entrypoint: one asyncio process serving UI + API + proxy."""
import argparse
import os

from aiohttp import web

from . import server as meadow_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=os.environ.get(
        'MEADOW_DATA', '/data'))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('MEADOW_PORT', '3010')))
    args = parser.parse_args()

    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    app = meadow_server.build_app(args.data, static_dir)
    print(f'[meadow] listening on {args.host}:{args.port}, '
          f'data in {args.data}', flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == '__main__':
    main()
