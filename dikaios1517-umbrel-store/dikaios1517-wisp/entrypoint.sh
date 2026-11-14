#!/bin/sh
# Seed the persisted config on first run, fix permissions, drop to the wisp
# user, and start the relay. Umbrel runs this image as root, so config edits
# made through the umbrelOS Files app persist and are never overwritten.
set -e

CONFIG="/data/wisp.toml"

if [ ! -f "${CONFIG}" ]; then
    cp /app/wisp.toml.default "${CONFIG}"
fi

chown -R wisp:wisp /data

exec gosu wisp wisp relay "${CONFIG}"
