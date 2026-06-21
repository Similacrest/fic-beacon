# CLAUDE.md — Fic-Beacon

Guidance for working in this repo. See `Architecture.md` for the full concept and C4 diagrams.

## What this is

Fic-Beacon re-serializes **complete** books from a **Calibre library** into a synthetic
*ongoing* RSS feed, so the user reads their backlog the same drip-fed way they follow ongoing
web serials. A web admin page configures the queue, parallelism, and a per-cycle reading
budget; each chapter drop embeds feedback links to steer the rotation.

## Non-negotiable constraints

- **Reader-agnostic.** The feed must be standards-compliant **RSS 2.0 + Atom** and work in
  **any** RSS reader. InoReader is a *reference client only* — no InoReader-specific
  extensions. All feedback is plain `<a href>` GET hyperlinks inside item HTML.
- **Calibre is read-only.** Mount the library folder RO; read `metadata.db` (SQLite) and parse
  EPUBs in place. Never write to the Calibre library. All app state lives in Fic-Beacon's own
  SQLite DB.
- **Never split a chapter.** Batching packs *whole* chapters up to the budget, allowing
  overshoot within `overshoot_tolerance`. An oversized chapter is posted whole.

## Stack

- **Python + FastAPI** (web/API + feed + feedback + reader pages)
- **APScheduler** (in-process, fires the drop cycle on `cadence_cron`)
- **SQLAlchemy + SQLite** (app state)
- **Jinja + HTMX** (server-rendered single-user admin UI)
- **ebooklib + BeautifulSoup** (EPUB chapterizing + word counts)
- **feedgen** (RSS 2.0 / Atom generation)
- One **Docker** container; Calibre library mounted read-only; a volume for the app SQLite DB.
- **RSSHub is not used** — we generate the feed ourselves.

## Repo layout

```
fic-beacon/
  app/
    main.py            # FastAPI app + routes (feed, feedback, reader, admin)
    calibre/           # Calibre Adapter (metadata.db RO, identifiers, EPUB paths)
    epub/              # Chapterizer (spine -> chapters + word counts, cached by book+mtime)
    planner/           # Budget / Drop Planner
    feed/              # Feed Builder (feedgen)
    scheduler.py       # APScheduler wiring
    models.py          # SQLAlchemy models
    templates/         # Jinja + HTMX
  Dockerfile
  docker-compose.yml   # app + RO mount of Calibre library + volume for app SQLite
  CLAUDE.md
  Architecture.md
```

## Key conventions & rules

### Batching (Drop Planner)
- One **global budget per cycle**, split across the N active books weighted by `quota_weight`.
- For each book, start at `cursor_chapter_index`, pack whole chapters until the book's share is
  reached (overshoot allowed up to `overshoot_tolerance`); **never split**. Advance the cursor.
- When a book's cursor passes its last chapter → mark `completed`, promote the next `queued`
  book into the freed slot.
- Budget can be expressed in words or reading-time minutes (`budget_mode`, `wpm`).

### Permalinks (source-aware, per-chapter)
FanFicFare writes a **per-chapter** canonical URL into each chapter's `<head>`:
`<meta name="chapterurl" content="...">` (exact AO3 chapter, FFN chapter number, forum
post, Wattpad part, etc.). The chapterizer reads it from the **raw zip** (ebooklib strips
`<head>` on parse) keyed by file basename — see `app/epub/chapterizer.py:_chapter_url_map`.

Item link precedence (`app/feed/builder.py:_permalink`):
1. `drop.source_url` — the exact per-chapter URL of the drop's first chapter (FFF books).
2. `book.source_url` — the whole-work URL (`url:` identifier in `metadata.db`) as fallback.
3. `/read/{slug}` — self-hosted reader page (non-FanFicFare books).

**GUID ≠ link.** The item `guid`/`id` is always `urn:fic-beacon:drop:{reader_slug}` (a
per-drop uuid4) so multiple drops of one FanFicFare book never collide on the shared work
URL. The reader page always exists as a fallback for readers that clip long items.

For FFF books, "a chapter is real iff it has a `chapterurl`" — this is how front-matter
(Title Page / metadata) is excluded. Non-FFF books fall back to a word-count floor.

### Feedback contract (plain hyperlinks, any reader)
- Each drop embeds three tokenized links: `GET /fb/{token}?action=up|down|extra`.
- `up` → `thumbs_up++`, raise `quota_weight`.
- `down` → `thumbs_down++`; at `>= thumbs_down_drop_threshold` the book is `dropped` and the
  next queued book is promoted.
- `extra` → inject an immediate out-of-cycle drop for that book.
- Tokens are per-drop and unguessable; a click binds to exactly one book/drop.

### Calibre access
- Open `metadata.db` read-only. Books, authors, and identifiers come from there; EPUB file
  paths are derived from the library folder structure. Do not require a running Calibre.

## Data model (summary)

`book` (status `queued|active|completed|dropped`, `queue_position`, `quota_weight`,
`cursor_chapter_index`, `source_url`, thumbs counts) · `drop` (`feedback_token`,
`reader_slug`, `chapter_start/end`, `word_count`) · `feedback_event` · `config`
(`global_budget_words`, `budget_mode`, `wpm`, `overshoot_tolerance`, `parallel_slots`,
`cadence_cron`, `thumbs_down_drop_threshold`, `feed_secret`). See `Architecture.md §5`.

## v2 (designed-for, not built)

Ongoing-feed balancing: import OPML of the user's real ongoing fics, poll them, count recent
words, and set the synthetic global budget to `target_total − recent_ongoing_volume`. Only the
`config` target and the Planner's budget computation are affected — don't bake assumptions that
block this.

## Verification

- Generated feed passes the **W3C Feed Validator** and renders in **≥2 readers**
  (e.g. FreshRSS + InoReader).
- Feedback links work as plain GET hyperlinks from within a reader.
- Calibre volume is never written to; all state is in the app SQLite DB.
- Batching never splits a chapter; oversized chapters post whole.
