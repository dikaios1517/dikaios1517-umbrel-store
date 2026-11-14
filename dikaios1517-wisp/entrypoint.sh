#!/bin/sh
# Seed the persisted config on first run, then run two things in one container:
#   1. the relay, supervised by a restart loop (the GUI's "Restart relay"
#      button kills the relay; this loop brings it back up with the new config)
#   2. the management GUI + transparent relay proxy on port 3000, which is what
#      umbrelOS routes the app icon / Tor hidden service / LAN port to.
# The container entrypoint runs as root (umbrelOS requirement); both services
# run as the unprivileged "wisp" user via gosu. Config edits made through the
# umbrelOS Files app persist and are never overwritten.
set -e

CONFIG="/data/wisp.toml"

if [ ! -f "${CONFIG}" ]; then
    cp /app/wisp.toml.default "${CONFIG}"
fi

chown -R wisp:wisp /data

SHUTDOWN_FLAG="/tmp/.shutdown"

term() {
    touch "${SHUTDOWN_FLAG}" 2>/dev/null || true
    [ -n "${WISP_PID}" ] && kill "${WISP_PID}" 2>/dev/null || true
    [ -n "${GUI_PID}" ] && kill "${GUI_PID}" 2>/dev/null || true
}
trap term TERM INT

rm -f "${SHUTDOWN_FLAG}"

gosu wisp python3 /app/gui/server.py &
GUI_PID=$!

while true; do
    gosu wisp wisp relay "${CONFIG}" &
    WISP_PID=$!
    echo "${WISP_PID}" > /tmp/wisp.pid
    chown wisp:wisp /tmp/wisp.pid

    # Wait for the relay to exit; wait() also returns early if we get TERM'd.
    wait "${WISP_PID}" || true

    if [ -f "${SHUTDOWN_FLAG}" ]; then
        exit 0
    fi

    # Relay died on its own: brief pause so a crash loop doesn't spin hot,
    # then bring it back with the current config.
    sleep 3
done
