#!/bin/sh
# Lodestone runs as ONE process (relay + management GUI + graph crawler +
# aggregator + retention worker, all asyncio), so the entrypoint is simple:
# bootstrap the data volume, then drop privileges and exec. Config is seeded
# by lodestone itself on first run (DEFAULTS written to
# $LODESTONE_CONFIG if missing), so user edits persist across restarts and
# updates. If the process dies, docker's restart: on-failure brings it back.
set -e

chown -R lodestone:lodestone /data

set +e
exec gosu lodestone python3 -m lodestone.main
