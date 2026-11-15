FROM docker.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# coincurve ships manylinux wheels for amd64 and arm64, so no build
# toolchain is needed; schnorr.py falls back to its pure-Python BIP-340
# implementation if the wheel is ever unavailable.
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    pip install --no-cache-dir websockets==15.0.1 aiohttp==3.12.15 coincurve==21.0.0

RUN useradd --system --no-create-home --shell /usr/sbin/nologin meadow \
    && mkdir -p /data

# Starts as root to chown /data, then drops to the meadow user in
# entrypoint.sh. On Umbrel the ${APP_DATA_DIR} bind mount is root-owned, so
# the container itself must bootstrap permissions.
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    MEADOW_DATA=/data
ENTRYPOINT ["/entrypoint.sh"]
EXPOSE 3010
