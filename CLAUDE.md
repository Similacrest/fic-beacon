# CLAUDE.md — Fic-Beacon

Guidance for working in this repo. See `Architecture.md` for the full concept and C4 diagrams,
`README.md` for setup/run, and `CHANGELOG.md` for what has actually landed vs. what is planned.

## What this is

Fic-Beacon is a single batched, weighted reading queue for **both** a completed backlog
(**Calibre** EPUBs) **and** the user's real **ongoing web serials** (their RSS feeds). It
re-serializes everything into synthetic *ongoing* RSS/Atom feeds so the backlog arrives with the
same drip-fed hook as ongoing fiction — and so ongoing serials stop getting implicit priority
over the backlog. A web admin page groups sources into **channels** (TV-style), sets a per-cycle
reading budget per channel, and each drop embeds feedback links to steer the rotation.

## Non-negotiable constraints

- **Reader-agnostic.** Feeds must be standards-compliant **RSS 2.0 + Atom** and work in **any**
  RSS reader. InoReader is a *reference client only* — no InoReader-specific extensions. All
  feedback is plain `<a href>` GET hyperlinks inside item HTML. (WebSub is a W3C standard and is
  fine — it degrades gracefully to polling.)
- **Calibre is read-only.** Mount the library folder RO; read `metadata.db` (SQLite) and parse
  EPUBs in place. Never write to the Calibre library. All app state lives in Fic-Beacon's own
  SQLite DB. (Any FanFicFare-fetched ongoing EPUBs, if ever added, live in Fic-Beacon's own
  writable work dir — never in the Calibre library.)
- **Never split a chapter / unit.** A drop packs *whole* chapters (EPUB) or *whole* entries
  (ongoing). An oversized unit is posted whole.

## Stack

- **Python + FastAPI** (web/API + feeds + feedback + reader pages + WebSub hub)
- **APScheduler** (in-process: drop cycle on `cadence_cron`; hourly ongoing-feed poll)
- **SQLAlchemy + SQLite** (app state; schema built via `create_all`, recreate volume on
  schema-changing upgrades)
- **Jinja + HTMX** (server-rendered single-user admin UI)
- **ebooklib + BeautifulSoup** (EPUB chapterizing + word counts)
- **feedparser** (poll ongoing serial feeds), **httpx** (WebSub verification + push)
- **feedgen** (RSS 2.0 / Atom generation)
- One **Docker** container; Calibre library mounted read-only; a volume for the app SQLite DB.
- **RSSHub is not used** for output — we generate our feeds ourselves. (RSSHub may *optionally*
  be used later as an *input* to produce RSS for serial sites that lack it.)

## Repo layout

```
fic-beacon/
  app/
    main.py            # FastAPI app wiring
    config.py          # pydantic-settings (BEACON_* env, incl. BEACON_TZ)
    routers/           # feed, feedback, reader, admin, ongoing, websub
    calibre/           # Calibre Adapter (metadata.db RO, identifiers, tags, EPUB paths)
    epub/              # Chapterizer (spine -> chapters + word counts, cached by book+mtime)
    planner/           # Drop Planner (per-channel stochastic budget; unit abstraction)
    ongoing/           # OPML parse + serial feed poller (buffers entries)
    feed/              # Feed Builder (feedgen)
    websub/            # WebSub publisher (push to subscribers)
    scheduler.py       # APScheduler wiring (timezone-aware)
    models.py          # SQLAlchemy models
    templates/         # Jinja + HTMX
  Dockerfile
  docker-compose.yml
  CLAUDE.md  Architecture.md  README.md  CHANGELOG.md
```

## Key concepts & rules

### Channels & slots (TV-channels model)
- A **channel** groups sources by a Calibre **genre prefix** (`genre_match`) against the
  custom column **`#genre_manual`** (hierarchical, `.`-separated, e.g. `Fantasy.Rational`).
  On import each book is auto-routed to the first channel whose `genre_match` prefix-matches
  one of its genres; with `#genre_manual` blank, a genre is derived by keyword-grepping the
  raw **`#genre`** column into one of five buckets (Fanfiction, Sci-Fi, Fantasy, Classical,
  Non-fiction) — see `app/calibre/genre.py`. Unmatched books fall back to **General**.
- **Every source belongs to exactly one channel** (`book.channel_id` is NOT NULL). There is no
  global/default group: budget and slots live only on channels. A **"General" channel** is
  auto-created on first run so imports always have a home; books can be moved between channels
  (without dropping) and channels renamed (slug/feed URLs stay stable) from the admin UI.
- A channel has its own **budget** and **parallel_slots**; the **cadence is global** (one cron),
  as is reading speed (`config.wpm`) and the 👎 drop threshold.
- **One feed per slot:** `GET /feed/{channel_slug}/{feed_key}` where `feed_key` is `"1".."N"`.
  Both EPUB backlog books and ongoing serials occupy numbered slots — there is no separate
  "ongoing" feed. There is **no all-channels union feed** — subscribe to each channel/slot feed.
- A slot shows one book at a time; when it completes (EPUB) or is dropped, the next queued book
  inherits the slot and that feed keeps broadcasting.

### Sources & units (EPUB and ongoing unified)
- A **source** is a `book` row. `kind=epub` (Calibre-backed) or `kind=ongoing` (RSS-backed,
  carries `feed_url`). Both hold `quota_weight`, votes, `status`, and live in a channel.
- A **unit** is one drop-able chunk: an EPUB chapter (`chapterize(epub)[cursor]`) or an unreleased
  `ongoing_entry` (oldest first). The poller buffers ongoing entries hourly; they are *released*
  only at drop time. Unit shape: `{title, html, word_count, source_url}`.

### Drop Planner — per-channel stochastic budget
- Runs per channel each cycle. Effective budget `B = channel.budget + channel.budget_credit`
  (signed carry-over so the long-run mean tracks the budget).
- Candidates = each active source's next unit, ordered by `quota_weight` (weighted-random).
  Include a unit of size `w` with probability `p = clamp((B − used)/w, 0, 1)`, biased up by
  weight/votes. Included → emit + advance cursor; excluded → **roll over** whole to a later cycle.
- **Pure stochastic:** no guaranteed first chapter — over budget, even a source's first unit can
  defer; a low-weight source may get nothing some cycles. **Never split a unit.**
- After the pass: `budget_credit += channel.budget − used` (clamped to ±budget).
- Budget can be words or reading-time minutes (per-channel `budget_mode`; `config.wpm` is global).

### Permalinks (source-aware, per-chapter) — EPUB
FanFicFare writes a **per-chapter** canonical URL into each chapter's `<head>`:
`<meta name="chapterurl" content="...">`. The chapterizer reads it from the **raw zip** (ebooklib
strips `<head>`) keyed by file basename — see `app/epub/chapterizer.py:_chapter_url_map`.
Item link precedence (`app/feed/builder.py:_permalink`): (1) `drop.source_url` (per-chapter),
(2) `book.source_url` (whole-work `url:` identifier), (3) `/read/{slug}` reader page. For ongoing
drops the link is the entry's original chapter URL.
**GUID ≠ link.** The item `guid`/`id` is always `urn:fic-beacon:drop:{reader_slug}` (per-drop
uuid4) so multiple drops never collide on a shared work URL.

### Feedback contract (plain hyperlinks, any reader)
Four ordered actions per drop: **🪝 extra · 👍 up · 👎 down · ❌ drop**.
- `up` → `thumbs_up++`, `quota_weight ×= 1.25`. **Instant bare GET** `GET /fb/{token}?action=up`.
- `down` → `thumbs_down++`, `quota_weight ×= 0.8`; at `>= thumbs_down_drop_threshold` the book is
  `dropped`. **Instant bare GET.**
- `extra` (super-up) → `thumbs_up += 3`, strong weight boost, **and** inject an out-of-cycle drop.
  **Confirm page** (`/fb/confirm/{token}`).
- `drop` (super-down) → set book `dropped` immediately. **Confirm page.**
- **Idempotent per `(drop_id, action)`** so reader/proxy prefetch and double-clicks count once.
- The **🪝 extra link renders only when a next unit exists** (`extra_available`): a buffered
  ongoing chapter, or a non-last EPUB chapter.
- Tokens are per-drop and unguessable; a click binds to exactly one book/drop.

### WebSub (realtime push)
Each feed declares `<link rel="hub" href="{base}/websub/hub">` + a correct `<link rel="self">`.
The self-hosted hub (`app/routers/websub.py`) handles subscribe/verify; `app/websub/publisher.py`
pushes the Atom body to verified subscribers after each cycle/extra. Works on InoReader's free
plan; degrades to polling for readers without WebSub.

### Calibre access
Open `metadata.db` read-only. Books, authors, identifiers (`url:` source), and **tags** come from
there; EPUB paths derive from the library folder structure. Do not require a running Calibre.

## Data model (summary)

`channel` (`name`, `slug`, `genre_match`, `parallel_slots`, `budget_*`, `budget_mode`,
`budget_credit`, `queue_order`, `is_inbox`) · `book` (`kind` epub|ongoing, `feed_url?`, `status`
queued|active|completed|dropped, `channel_id` **NOT NULL**, `slot_index`, `queue_position`,
`quota_weight`, `cursor_chapter_index`, thumbs) · `ongoing_entry` (buffer: `guid`,
`content_html`, `word_count`, `published_at`, `released`, `drop_id?`) · `drop`
(`feedback_token`, `reader_slug`, `channel_id`, `feed_key`, `chapter_start/end`, `word_count`,
`source_url?`) · `feedback_event` · `websub_subscription` (`topic_url`, `callback_url`,
`secret?`, `lease_expires_at`, `verified`) · `config` (single-row globals: `wpm`, `cadence_cron`,
`thumbs_down_drop_threshold`, `feed_secret`). See `Architecture.md §5`.

## Timezone

Drop/poll times use `BEACON_TZ` (e.g. `Europe/Tallinn`), passed to APScheduler and
`CronTrigger`. With no `TZ`/`BEACON_TZ` set, a stock container resolves to **UTC**.

## Verification

- Generated feeds pass the **W3C Feed Validator** and render in **≥2 readers** (FreshRSS + InoReader).
- Feedback links work as plain GET hyperlinks from within a reader; up/down are instant + idempotent.
- Calibre volume is never written to; all state is in the app SQLite DB.
- Batching never splits a unit; oversized units post whole; stochastic mean tracks the budget.
- Ongoing entries are buffered hourly and released batched at drop time, weighted against EPUBs.
