# dikaios1517's App Store

A personal **umbrelOS community app store**. It can hold any number of apps —
each app is one folder at the repo root, and the build workflow discovers,
builds, and publishes every one of them automatically.

Currently included:

| App | Description | Upstream |
|---|---|---|
| `dikaios1517-wisp` | Wisp — a fast, lightweight private Nostr relay (v0.9.3), with a built-in management dashboard (client addresses, settings editor, one-click relay restart), ported from [Start9-Community/wisp-startos](https://github.com/Start9-Community/wisp-startos) | [privkeyio/wisp](https://github.com/privkeyio/wisp) |
| `dikaios1517-lodestone` | Lodestone — a clean-room personal Nostr relay in Python that follows your social graph (3 levels deep), aggregates your community's notes from the relays they write to, and serves a slate/orange management GUI with a feed, live settings, and moderation tools | Built in this repo (`dikaios1517-lodestone/app/`) |
| `dikaios1517-meadow` | Meadow — a read-only media gallery: enter any npub and browse the photos and videos its whole social graph posted, gathered from Nostr media events and public Blossom servers | Built in this repo (`dikaios1517-meadow/app/`) |
| `dikaios1517-jethro` | Jethro PMM — church membership, attendance, rosters, services and pastoral-care notes (visits and diaconal needs tracked as assignable notes), packaged for umbrelOS with a bundled MariaDB | [tbar0970/jethro-pmm](https://github.com/tbar0970/jethro-pmm) |

## Lodestone: management GUI and ports

Lodestone is an **inbox relay**: the owner plus the people they follow (and
their follows, and theirs — graph depth configurable, default 3) may publish
to it. The graph is discovered automatically from kind 3 contact lists and
kind 10002 relay lists, and a background aggregator pulls everyone's notes,
replies, reactions, zaps, long-form posts, polls, pictures, and videos from
the public relays they actually write to (default: every 5 minutes).
Strangers' tagged replies/zaps are only accepted above a configurable
NIP-13 proof-of-work threshold. Retention (default 12 months, owner exempt)
runs automatically; everything is editable in the GUI.

**Ports:**

- `ws://<umbrel-ip>:7070` — the relay itself, published directly on the host
  network via Docker. Use this for Nostr clients on LAN/Tailscale and as a
  tunnel target. The management GUI is **not** reachable on this port (the
  relay port is websocket + NIP-11 only).
- `http://umbrel:3001` — the management GUI (manifest port, dashboard icon,
  and Tor onion all route through Umbrel's app proxy to it).

**GUI tabs:** Feed (newest/oldest/most zapped/most replies/most reactions/
most interactions, time filters 24h→all-time, display names, inline images,
copy event ID), Settings (relay info, owner npub, graph depth/refresh,
aggregation interval, stranger PoW, retention, allowed kinds, limits /
rate-limits / timeouts / security), Manage (manual purge, kind-1984 reports,
blacklist/whitelist by npub, rejected words/hashtags).


**NIPs:** 1, 2, 9, 11, 13, 16, 33, 40, 42, 45 (COUNT), 50 (search), 51, 65,
70 (protected events), 86 (management API — owner-only via NIP-98, served on
the GUI port at `/nip86`), 98 (auth). NIP-77 negentropy is planned for v2.

Config lives at `data/lodestone.toml` in the app's persistent storage (seeded
with defaults on first start; the GUI writes it). The relay's Tor address
appears in the GUI once remote Tor access is enabled in umbrelOS.

## Meadow: social-graph media gallery

Meadow is **read-only**: enter any npub and it walks that user's social
graph one level (kind 3 follows via kind 10002 relay lists), collects each
author's kind 10063 media-server declarations, and gathers the media the
whole graph actually posted — NIP-68 picture/video events (kinds 20, 21,
22) and NIP-92 `imeta` attachments in notes, with Blossom BUD-02 blob
listings as a secondary source for authors who declared a server that
allows public listing.

**Ports:** `http://umbrel:3010` — the gallery (manifest port, dashboard
icon, and Tor onion all route through Umbrel's app proxy). No other
service, no exposed raw port; the app makes only outbound requests.

**Notes:** media renders inline (JPG/PNG/GIF/WebP/AVIF images, MP4/WebM
video) through a size-capped proxy; the feed pages 30 items at a time with
infinite scroll and a Load-more button; display names/avatars come from
kind 0 profiles fetched in the background. The graph cache (12 h) and all
limits live in `data/meadow.json` (Files -> Apps -> Meadow -> data).

## Wisp: management dashboard and ports

Clicking the Wisp icon on the umbrelOS dashboard opens a built-in web GUI. It
shows the relay's LAN and Tor websocket addresses, renders every `wisp.toml`
setting as a form, can save and restart the relay, and manages optional
retention. It is also served directly at **http://umbrel:3000**.

**Ports** (as of 0.9.6):

- `ws://<umbrel-ip>:7777` — the relay itself, published directly on the host
  network via Docker. Use this for LAN clients, Tailscale, and tunnels (point
  cloudflared at `http://<umbrel-ip>:7777`). The management GUI is **not**
  reachable on this port.
- `http://umbrel:3000` — the management GUI, served by Umbrel's app gateway
  (the manifest port) and by the dashboard icon / Tor onion address — all
  three route through the app proxy to the GUI.
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

**Feed** (`[feed]`; as of 0.9.7): a tab in the GUI listing the notes on
*this* relay, sortable by newest, oldest, or most zapped within a chosen
time window (24 hours, 7/30/90 days, 1 or 2 years, or all time). Image
URLs in note text render as images and other URLs become links. A toggle
hides the admin's own notes (useful since those may be retained far longer
than everything else). Clicking a note opens a detail
view with zap totals (from local receipts plus the author's relays) and
replies. Interaction data is crawled from each original poster's kind 10002
read relays (falling back to the configured `bootstrap_relays`), cached in
`data/feed-cache.db` (SQLite, TTL `cache_ttl_hours`), and never written back
to the relay's LMDB. Replies from outside the original poster's web of
trust — their follows and their follows' follows, built from kind 3 contact
lists — are hidden unless the reply carries NIP-13 proof of work of
`pow_threshold` (default 16) or higher; zaps are always included because
they cost real sats. The crawler is best-effort: unreachable relays time
out (`crawl_timeout_s`) and the feed falls back to whatever is stored
locally.

The config file can still be edited directly (Files -> Apps -> Wisp -> data ->
wisp.toml); values containing double quotes or newlines are rejected on save
because Wisp's own TOML parser cannot represent them.

## Jethro PMM: church membership and pastoral care

Jethro PMM is a GPL web app for churches: a searchable directory of people and
families, week-by-week attendance recording with reports, groups, rosters and
service planning, and notes on any person or family that can be **assigned to
another user for follow-up** — the mechanism for tracking pastoral/elder visits
and diaconal needs (who hasn't been visited, what follow-up is outstanding).

**Port:** `http://umbrel:3011` — the Jethro UI (manifest port, dashboard icon,
and Tor onion all route through Umbrel's app proxy). MariaDB and the file
store are internal; nothing else is exposed.

**First run:** open the app — Jethro's own installer starts automatically.
Accept the pre-filled database settings (already wired to the bundled
MariaDB), create the initial admin account, and log in. Import members from
CSV via Admin → Import persons.

**Data locations** (Files → Apps → Hermes Agent's sibling app data):
- `data/conf.php` — Jethro config, seeded on first start, never overwritten;
  edit and restart the app to apply.
- `data/files/` — uploaded documents and photos (symlinked into the install).
- `data/mysql/` — the database itself. **Back this folder up** (it is only
  consistent while the app is stopped) or use Jethro's Admin → Export
  database.

**Updating Jethro:** bump `JETHRO_VERSION` in `dikaios1517-jethro/Dockerfile`,
bump `version:` in its `umbrel-app.yml` and the compose `image:` tag, push.
Jethro applies its own database upgrades on first request after an upgrade.
Read the upstream release notes before major version jumps.

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
