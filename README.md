# Fic-Beacon 📡

Turn your **completed** Calibre backlog *and* your **real ongoing web serials** into a single,
paced, weighted set of RSS/Atom feeds — so you read everything the same drip-fed way you already
follow ongoing fiction, and your backlog stops losing to whatever updated today.

Fic-Beacon re-serializes complete EPUBs into chapter drops. Ongoing web serials become real
Calibre books too: a separate **fetcher** container downloads new chapters with **FanFicFare**
(RSS is used only to *notice* updates), and they're served from the same channels — batched to
scheduled drop times instead of arriving all day. You steer the rotation from inside any RSS
reader with plain feedback links.

> See [CHANGELOG.md](CHANGELOG.md) for what has landed.

## Why

- **Backlog gets a fair shake.** Ongoing serials normally feel more urgent than finished books;
  here they share one weighted budget, so completed works actually get read.
- **No mid-day distractions.** New chapters are fetched and released only at your drop
  times (e.g. morning/evening), batched with the backlog.
- **Steer from your reader.** Every drop has 🪝 extra · 👍 up · 👎 down · ❌ drop links that adjust
  a source's weight or remove it — plain GET hyperlinks that work in any reader.
- **Reader-agnostic.** Standards-compliant RSS 2.0 + Atom; InoReader is just a reference client.

## How it works

- **Channels** group sources by a Calibre **genre prefix** — the `#genre_manual` custom column
  (e.g. `Fantasy`, `Non-fiction.Self-improvement`). On import each book is auto-routed to the
  matching channel; if `#genre_manual` is blank, a genre is derived by grepping the raw `#genre`
  column into one of Fanfiction / Sci-Fi / Fantasy / Classical / Non-fiction. Each channel has a
  **weight** (its share of each release's budget) and parallel **slots**; **schedules** set when releases
  fire and how much each may release (e.g. 5000 words weekday mornings, 20000 at weekends). Every source belongs
  to a channel — a **"General"** channel is created automatically and catches anything unmatched,
  and you can move books between channels or rename a channel anytime (the slug — and thus the
  feed URL — is editable too, but changing it means re-subscribing in your reader).
- **One feed per slot** — `GET /feed/{channel}/{slot}` — a finite backlog book streams in a slot
  and rolls to the next when it finishes, while **tracked** (auto-updating) stories ride alongside
  it in the same slot feeds. There's no separate ongoing feed.
- **Adding** is one page: paste story URLs and/or pick books already in Calibre, then press **Add**. Each Calibre book is routed by its
  `#status`: ongoing serials (In-Progress / Incomplete / Hiatus) become **tracked** auto-updating
  sources; finished/blank ones join the **backlog** queue. A tracked book you've marked `#read=Yes`
  starts at the current end (only *new* chapters drop); an unread one starts at chapter 1.
- **Tracked stories** can also be added by URL. The fetcher container runs FanFicFare to download
  them into Calibre; if the URL has an RSS feed it's used only to *notice* updates fast (checked
  pre-drop), otherwise a daily sweep refreshes it. From there they're ordinary EPUBs. Stories the
  source site marks done (`#status` Completed / Abandoned / Published) are skipped on fetch.
- **Stochastic budget** packs whole chapters up to each release's word (or reading-minute) budget, split across channels by weight;
  the further over budget, the more likely a unit rolls to the next cycle. Votes bias the draw; the
  long-run mean tracks your budget. Chapters are never split.
- **WebSub** push gives realtime updates on InoReader's free plan; readers without it just poll.

See [Architecture.md](Architecture.md) for C4 diagrams and the data model, and
[CLAUDE.md](CLAUDE.md) for working conventions.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: set CALIBRE_LIBRARY_PATH to your Calibre library folder, BEACON_BASE_URL, etc.
docker compose up -d --build
```

This starts two containers: `beacon` (the app, with the Calibre library mounted **read-only**) and
`fetcher` (FanFicFare + `calibredb`, library **read-write** — the only writer). Put your site
logins in `fetcher/config/personal.ini` (copy `fetcher/personal.ini.example`). If you track XenForo
serials (SpaceBattles / SufficientVelocity / QuestionableQuesting), keep the `[base_xenforoforum]`
block from the example: it drops non-story threadmark categories and date-orders the rest so new
chapters append at the EPUB end — without it, category-grouped threadmarks make stale chapters leak
to the feed (Fic-Beacon's cursor is positional/append-only).

**Admin access.** Admin routes are deny-by-default. Put beacon behind a reverse proxy that sends
`X-Beacon-Proxy-Token: $BEACON_PROXY_TOKEN` (see *Deploying behind a reverse proxy* below), or — on a
network you trust, never the public internet — set `BEACON_ADMIN_OPEN=true` in `.env` to skip the gate.
Without either, `/admin` answers `503`.

Then open the admin UI at `http://localhost:8000/admin/`, create channels (or use the auto-created
**General** one), import books from Calibre into a channel, add tracked stories by URL on the
**Tracked Stories** page, and copy each per-slot feed URL (with its `?token=`) into your RSS reader.
There is no single "all" feed — subscribe to each channel/slot feed you want.

### Configuration (`.env` / `BEACON_*` env)

| Variable | Purpose | Default |
|---|---|---|
| `CALIBRE_LIBRARY_PATH` | Host path to your Calibre library (RO in app, RW in fetcher) | `.` |
| `FETCHER_CONFIG_PATH` | Host path holding the fetcher's `personal.ini` | `./fetcher/config` |
| `FETCHER_UID` / `FETCHER_GID` | Run the fetcher as this uid/gid so new library files match your Calibre owner instead of `root:root` (`stat -c '%u:%g' "$CALIBRE_LIBRARY_PATH"`) | `0` / `0` |
| `BEACON_BASE_URL` | Public base URL used in feed/links | `http://localhost:8000` |
| `BEACON_FEEDBACK_BASE_URL` | Optional separate origin for the 🪝 👍 👎 ⏸ ❌ ✓ feedback links in feed items (`/fb/*` must be reachable there). Default `BEACON_BASE_URL` |
| `BEACON_READER_BASE_URL` | Optional origin for feed items' `/read/` links. Default `BEACON_FEEDBACK_BASE_URL`. Must serve `/read`, `/img` and `/fb` (the reader page uses relative URLs). Feeds, WebSub and images inside feed items stay on `BEACON_BASE_URL` | = `BEACON_BASE_URL` |
| `BEACON_LEGACY_BASE_URLS` | Comma-separated origins older drops have baked into their image URLs (e.g. after a base-URL change); `/img/` URLs on them are rewritten to `BEACON_BASE_URL` and signed at render time. Other hosts are never touched | none |
| `BEACON_PORT` | Host port to expose | `8000` |
| `BEACON_FEED_SECRET` | Secret token gating the feeds. **Seeds the DB value on first run only — the database copy wins afterwards** (rotate on the Channels page); a warning is logged if they differ | random |
| `BEACON_PROXY_TOKEN` | Shared secret the reverse proxy sends as `X-Beacon-Proxy-Token`. Required on every route except `/feed` `/read` `/fb` `/img` `/static`; `X-Remote-User` is trusted (audit only) when it matches | unset |
| `BEACON_BIND_HOST` | Address uvicorn binds to (used only for the fail-closed check). With no token and a non-loopback value, admin is refused; set `127.0.0.1` for local dev | `0.0.0.0` |
| `BEACON_ADMIN_OPEN` | Explicit opt-out of the gate when no token is set: `/admin` is open to anyone who can reach the port. Trusted networks only | `false` |
| `BEACON_FETCHER_TOKEN` / `FETCHER_TOKEN` | Shared secret beacon sends (`X-Fetcher-Token`) and the fetcher requires; set both to the same value | unset |
| `BEACON_UID` / `BEACON_GID` | Optional: run the beacon container as this non-root user (chown the `/data` volume first) | `0` / `0` |
| `BEACON_PUBLIC_WEBSUB` | Treat `/websub/*` as public (no proxy token) — set it together with the proxy publishing `/websub/` (the bundled `docker-compose.yml` does not pass it through — add it to the container environment). Feeds always advertise `rel=hub`; while the hub isn't reachable, readers just poll | `false` |
| `BEACON_WEBSUB_CALLBACK_HOSTS` | Optional comma-separated allowlist of WebSub callback hosts (e.g. `incoming.inoreader.com`). Callbacks are always https and public-address only | any |
| `BEACON_TZ` | Timezone for drop/sweep schedules (e.g. `Europe/Tallinn`) | system / UTC |
| `BEACON_FETCHER_URL` | URL of the fetcher service | `http://fetcher:8080` |
| `BEACON_FETCHER_POLL_INTERVAL` | Seconds between polls of a running fetch job | `30` |
| `BEACON_FETCHER_JOB_TIMEOUT` | Give up on a fetch job after this many seconds | `1200` |
| `FETCHER_FANFICFARE_TIMEOUT` | Per-FanFicFare-call wall-clock cap, seconds (set in the **fetcher** container) | `1200` |
| `FETCHER_CALIBREDB_TIMEOUT` | Per-`calibredb`-call wall-clock cap, seconds (set in the **fetcher** container) | `600` |
| `BEACON_LOG_LEVEL` | App log level; set `DEBUG` to trace the WebSub flow | `INFO` |

Generate a feed secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Deploying behind a reverse proxy

Public paths (no token needed): `/feed/*`, `/read/*`, `/fb/*`, `/img/*`, `/static/*`, `/robots.txt`
(and `/websub/*` only with `BEACON_PUBLIC_WEBSUB=true`). Everything else needs the shared token:

```nginx
# Authenticate the user (SSO, basic auth, …) for /admin, then inject the token:
location / {
    auth_request /auth;                               # your auth
    proxy_set_header X-Beacon-Proxy-Token $beacon_proxy_token;   # == BEACON_PROXY_TOKEN
    proxy_set_header X-Remote-User        $remote_user;          # audit log only
    proxy_pass http://beacon:8000;
}
# Public feed/reader/feedback/image paths: forward WITHOUT auth, and always overwrite the headers
# so a client can't inject its own:
location ~ ^/(feed|read|fb|img|static)/ {
    proxy_set_header X-Beacon-Proxy-Token "";
    proxy_set_header X-Remote-User "";
    proxy_pass http://beacon:8000;
}
```

Admin POSTs are also checked for cross-site origin (`Sec-Fetch-Site` / `Origin`); the proxy must
pass `Host` (or `X-Forwarded-Host`) through unchanged, or set `BEACON_BASE_URL` to the public origin.

### Maintenance scripts

If you update EPUBs in the Calibre library *outside* the fetcher (e.g. a manual bulk transfer),
run `scripts/refresh_cursors.py` inside the `beacon` container to recompute each source's max
chapter count and clamp any cursor left pointing past the EPUB's end (reading positions still in
range are untouched, so unread new chapters still drop):

```bash
docker exec fic-beacon-beacon-1 /app/.venv/bin/python /app/scripts/refresh_cursors.py          # dry-run
docker exec fic-beacon-beacon-1 /app/.venv/bin/python /app/scripts/refresh_cursors.py --apply   # write
```

## Development

Uses [uv](https://docs.astral.sh/uv/) for packaging.

```bash
uv sync                       # install (incl. dev extras)
BEACON_BIND_HOST=127.0.0.1 uv run uvicorn app.main:app --reload   # loopback bind ⇒ admin open for dev
uv run pytest                 # run the test suite
```

The `beacon` app **never writes** the Calibre library — it reads `metadata.db` (SQLite) and parses
EPUBs in place, with all app state in its own SQLite DB. Only the isolated `fetcher` container
writes the library (via FanFicFare + `calibredb`).

## License

See repository.
