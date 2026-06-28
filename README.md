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
  column into one of Fanfiction / Sci-Fi / Fantasy / Classical / Non-fiction. Each channel has its
  own reading budget and parallel **slots**; one global cron sets the cadence. Every source belongs
  to a channel — a **"General"** channel is created automatically and catches anything unmatched,
  and you can move books between channels or rename a channel anytime (feed URLs stay stable).
- **One feed per slot** — `GET /feed/{channel}/{slot}` — a finite backlog book streams in a slot
  and rolls to the next when it finishes, while **tracked** (auto-updating) stories ride alongside
  it in the same slot feeds. There's no separate ongoing feed.
- **Tracked stories** are added by URL. The fetcher container runs FanFicFare to download them into
  Calibre; if the URL has an RSS feed it's used only to *notice* updates fast (checked pre-drop),
  otherwise a daily sweep refreshes it. From there they're ordinary EPUBs.
- **Stochastic budget** packs whole chapters up to a per-channel word (or reading-minute) budget;
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
logins in `fetcher/config/personal.ini` (copy `fetcher/personal.ini.example`).

Then open the admin UI at `http://localhost:8000/admin/`, create channels (or use the auto-created
**General** one), import books from Calibre into a channel, add tracked stories by URL on the
**Tracked Stories** page, and copy each per-slot feed URL (with its `?token=`) into your RSS reader.
There is no single "all" feed — subscribe to each channel/slot feed you want.

### Configuration (`.env` / `BEACON_*` env)

| Variable | Purpose | Default |
|---|---|---|
| `CALIBRE_LIBRARY_PATH` | Host path to your Calibre library (RO in app, RW in fetcher) | `.` |
| `FETCHER_CONFIG_PATH` | Host path holding the fetcher's `personal.ini` | `./fetcher/config` |
| `BEACON_BASE_URL` | Public base URL used in feed/links | `http://localhost:8000` |
| `BEACON_PORT` | Host port to expose | `8000` |
| `BEACON_FEED_SECRET` | Secret token gating the feeds (auto-generated if unset) | random |
| `BEACON_TZ` | Timezone for drop/sweep schedules (e.g. `Europe/Tallinn`) | system / UTC |
| `BEACON_FETCHER_URL` | URL of the fetcher service | `http://fetcher:8080` |
| `BEACON_FETCHER_POLL_INTERVAL` | Seconds between polls of a running fetch job | `30` |
| `BEACON_FETCHER_JOB_TIMEOUT` | Give up on a fetch job after this many seconds | `1200` |

Generate a feed secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Development

Uses [uv](https://docs.astral.sh/uv/) for packaging.

```bash
uv sync                       # install (incl. dev extras)
uv run uvicorn app.main:app --reload
uv run pytest                 # run the test suite
```

The `beacon` app **never writes** the Calibre library — it reads `metadata.db` (SQLite) and parses
EPUBs in place, with all app state in its own SQLite DB. Only the isolated `fetcher` container
writes the library (via FanFicFare + `calibredb`).

## License

See repository.
