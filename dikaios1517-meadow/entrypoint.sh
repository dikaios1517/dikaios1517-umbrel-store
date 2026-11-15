#!/bin/sh
# Meadow runs as ONE process (gallery UI + API + media proxy + graph
# crawler, all asyncio). Bootstrap the data volume, then drop privileges
# and exec. Config is seeded by meadow itself on first run
# ($MEADOW_DATA/meadow.json if missing), so user edits persist across
# restarts and updates. If the process dies, docker's restart: on-failure
# brings it back.
set -e

chown -R meadow:meadow /data

set +e
exec gosu meadow python3 -m meadow.main
