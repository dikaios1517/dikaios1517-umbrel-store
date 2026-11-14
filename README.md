# dikaios1517's App Store

A personal **umbrelOS community app store**. It can hold any number of apps —
each app is one folder at the repo root, and the build workflow discovers,
builds, and publishes every one of them automatically.

Currently included:

| App | Description | Upstream |
|---|---|---|
| `dikaios1517-wisp` | Wisp — a fast, lightweight private Nostr relay (v0.9.3), with a built-in management dashboard (client addresses, settings editor, one-click relay restart), ported from [Start9-Community/wisp-startos](https://github.com/Start9-Community/wisp-startos) | [privkeyio/wisp](https://github.com/privkeyio/wisp) |

## Wisp: management dashboard and ports

Clicking the Wisp icon on the umbrelOS dashboard opens a built-in web GUI. It
shows the relay's LAN and Tor websocket addresses, renders every `wisp.toml`
setting as a form, can save and restart the relay, and manages optional
retention. It is also served directly at **http://umbrel:3000**.

**Ports** (as of 0.9.5):

- `ws://umbrel:7777` — the relay itself, published directly on the host
  network. Use this for LAN clients, Tailscale, and tunnels (point cloudflared
  at `http://<umbrel-ip>:7777`).
- `http://umbrel:3000` — the management GUI, also published directly.
- Dashboard icon and Tor onion address — routed through Umbrel's app proxy to
  the GUI, which transparently proxies websocket upgrades, NIP-11 info
  requests and `/metrics` to the relay, so Tor clients keep working.

The GUI proxy injects the real client IP into `X-Forwarded-For`, and the
seeded config sets `trust_proxy = true` with `trusted_proxies = "127.0.0.1"`
so per-IP rate limits keep working. If you installed 0.9.3 first, flip those
two `[security]` lines in the GUI to keep per-IP limiting.

**Retention** (`[retention]` in the settings form; ignored by the relay
itself): a daily worker in the GUI deletes events older than
`purge_after_months` and tombstones them so sync cannot re-add them. With
`keep_admin_notes = true`, every event authored by the `[spider]` admin (or
`[management]` `admin_pubkeys`) is kept indefinitely. Check the last run and
trigger one manually from the GUI's Retention card. The worker deletes
directly from LMDB using Wisp 0.9.3's exact index layout — re-verify after a
major Wisp upgrade.

The config file can still be edited directly (Files -> Apps -> Wisp -> data ->
wisp.toml); values containing double quotes or newlines are rejected on save
because Wisp's own TOML parser cannot represent them.

## One-time setup

1. **Push this repo to GitHub** under your account, e.g.
   `github.com/<you>/dikaios1517-umbrel-store`.
2. Images publish to `ghcr.io/dikaios1517/<app-id>:<version>` automatically
   (the compose files already point there).
3. **The `build-apps` workflow runs on every push** (or via manual dispatch)
   and builds/pushes multi-arch images for any app folder that changed. If a
   GHCR package ends up private by default, set its visibility to public on
   GitHub so umbrelOS can pull it.
4. Point each app's `submission:` field in `umbrel-app.yml` at this repo.

## Install on Umbrel

In umbrelOS: **App Store → (top-right menu) → Community App Stores →
Add store**, and enter your repo URL:

```
github.com/<you>/dikaios1517-umbrel-store
```

Apps appear under "dikaios1517's App Store".

## Adding a new app

1. Copy `templates/app-skeleton/` to a new top-level folder named after the
   app id. **App ids must start with the store id**: store id is `dikaios1517`, so
   the app id is `dikaios1517-<name>` and the folder matches (`dikaios1517-<name>/`).
2. Fill in `umbrel-app.yml` (name, tagline, description, `port`, `version`).
3. Write the `Dockerfile`, or point `image:` at an existing published image
   and delete the Dockerfile + let the workflow skip it (it only builds
   folders containing a Dockerfile).
4. In `docker-compose.yml`, follow the conventions: an `app_proxy` service
   with `APP_HOST: dikaios1517-<name>_<service>_1` and `APP_PORT` set to the app's
   port, and the app image at `ghcr.io/<you>/<app-id>:<version>`. If the app
   ships a default config, use an entrypoint that copies it into
   `${APP_DATA_DIR}` on first run only (`[ ! -f ]` guard) so user edits
   survive restarts and updates, then drop privileges with gosu.
5. Push. The workflow builds the image; then install/update the app from
   your store in umbrelOS.

## Conventions used by every app here

- Images: `ghcr.io/<repo-owner>/<app-id>:<app-version>` (version taken from
  `umbrel-app.yml`; bump it there and in the compose `image:` tag together).
- Data: `${APP_DATA_DIR}/data` bind mount; config seeded, never overwritten.
- The arm64 stage of compiled-language builds runs under QEMU and can take
  20-40 minutes the first time; the GHA cache keeps later rebuilds fast.

---

## Wisp-specific notes

### Connect Nostr clients

- LAN / Tailscale: `ws://umbrel:7777`
- Tor: the onion address on the Wisp app page (`ws://<onion>`)
- Remote (no Tor): port-forward 7777 and use `ws://<address>:7777`

### Configuration

On first start the container copies the default config to
`data/wisp.toml` in the app's persistent storage. Edit it via
**Files → Apps → Wisp → data → wisp.toml**, then restart the app
(right-click / long-press the app icon → Restart).

`trust_proxy` stays `false` for direct client connections (LAN, Tailscale,
Tor), which is the normal Umbrel setup. Only enable it behind a reverse
proxy you control that sets `X-Forwarded-For`.

### Updating Wisp

Bump `WISP_VERSION` / `WISP_COMMIT` in `dikaios1517-wisp/Dockerfile`, bump
`version:` in `dikaios1517-wisp/umbrel-app.yml` and the image tag in its
`docker-compose.yml`, push, then hit **Update** on the app in umbrelOS.

### What was translated from StartOS

- `manifest.yaml` volumes/ports → `umbrel-app.yml` + compose volume + `port: 7777`
- StartOS generated config + Actions → seeded `/data/wisp.toml` you edit by hand
- StartOS reverse-proxy websocket interface → Umbrel `app_proxy` passthrough
  (Umbrel's proxy supports websocket upgrades, same pattern as Umbrel's own
  Nostr Relay app)
- StartOS root `chown` oneshot → entrypoint seeds/chowns `/data` then drops
  privileges with gosu
- `trust_proxy` is no longer forced true (StartOS forces it because all
  clients arrive via its proxy; on Umbrel clients connect directly)
