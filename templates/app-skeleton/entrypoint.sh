#!/bin/sh
# Generic Umbrel entrypoint pattern for apps that need a config file and/or
# non-root execution. umbrelOS binds ${APP_DATA_DIR} root-owned and offers no
# root init hook, so the container starts as root, seeds/chowns /data, then
# drops privileges via gosu.
set -e

CONFIG="/data/<app>.conf"          # whatever the app reads

# Seed the config on first run only, so user edits survive restarts/updates.
if [ ! -f "${CONFIG}" ]; then
    cp /app/<app>.conf.default "${CONFIG}"
fi

chown -R <user>:<user> /data

exec gosu <user> <command-to-run>
