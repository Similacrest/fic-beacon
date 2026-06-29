# CLAUDE.md — Fic-Beacon

Guidance for working in this repo. See `Architecture.md` for the full concept and C4 diagrams,
`README.md` for setup/run, and `CHANGELOG.md` for what has actually landed vs. what is planned.

## What this is

Fic-Beacon is a single batched, weighted reading queue for **both** a completed backlog
(**Calibre** EPUBs) **and** the user's real **ongoing web serials**. Everything is a Calibre
EPUB: the backlog is imported; ongoing serials are downloaded into Calibre by **FanFicFare**
(in a separate container) and kept up to date, with their **RSS feed used only as a trigger**
that signals "new chapters exist". Fic-Beacon re-serializes all of it into synthetic *ongoing*
RSS/Atom feeds so the backlog arrives with the same drip-fed hook as ongoing fiction — and so
ongoing serials stop getting implicit priority over the backlog. A web admin page groups sources
into **channels** (TV-style), sets a per-cycle reading budget per channel, and each drop embeds
feedback links to steer the rotation.

## Non-negotiable constraints

- **Reader-agnostic.** Feeds must be standards-compliant **RSS 2.0 + Atom** and work in **any**
  RSS reader. InoReader is a *reference client only* — no InoReader-specific extensions. All
  feedback is plain `<a href>` GET hyperlinks inside item HTML. (WebSub is a W3C standard and is
  fine — it degrades gracefully to polling.)
- **Calibre is read-only *from Fic-Beacon*.** The app mounts the library folder **RO**, reads
  `metadata.db` (SQLite), and parses EPUBs in place — it never writes the library. Writes happen
  **only** in the isolated **fetcher container** (FanFicFare + `calibredb`), which has the library
  mounted RW and is the sole writer. All app state lives in Fic-Beacon's own SQLite DB. (The setup
  coexists with an external calibre-web pointed at the same library.)
- **RSS is a trigger, not content.** Feed bodies are never read for chapter text (sites only
  syndicate previews). A feed only tells us a story updated; FanFicFare fetches the real chapters.
- **Never split a chapter / unit.** A drop packs *whole* EPUB chapters. An oversized unit is
  posted whole.

## Stack

- **Python + FastAPI** (web/API + feeds + feedback + reader pages + WebSub hub)
- **APScheduler** (in-process: drop cycle on `cadence_cron`, which polls feeds first; a daily
  feedless sweep). There is **no hourly poll** — feeds are checked pre-drop.
- **SQLAlchemy + SQLite** (app state; schema is **Alembic-migration-owned** — `init_db()` runs
  `alembic upgrade head` on startup. **Never `create_all` in production and never hand-edit a
  deployed schema; add a migration** (`alembic revision --autogenerate -m "…"`, review it, ship
  it). A legacy `create_all` DB with no `alembic_version` is auto-stamped at baseline then
  upgraded — volumes are **not** recreated on schema changes anymore. Tests still build the schema
  with `create_all` from the models, which is fine — the rule is about deployed DBs.)
- **Jinja + HTMX** (server-rendered single-user admin UI)
- **ebooklib + BeautifulSoup** (EPUB chapterizing + word counts)
- **feedparser** (read newest GUID from trigger feeds), **httpx** (WebSub push + calling the fetcher)
- **feedgen** (RSS 2.0 / Atom generation)
- **Two Docker containers**: `beacon` (this app, library RO) and `fetcher` (FanFicFare + calibredb,
  library RW; see `./fetcher`). A volume holds the app SQLite DB.
- **RSSHub is not used** for output — we generate our feeds ourselves.

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
    ongoing/           # RSS update-detection poller + feed-URL inference (no content)
    fetch/             # async HTTP client to the fetcher (submit_fetch / poll_fetch / apply_result)
    feed/              # Feed Builder (feedgen)
    websub/            # WebSub publisher (push to subscribers)
    scheduler.py       # APScheduler wiring (timezone-aware)
    models.py          # SQLAlchemy models
    templates/         # Jinja + HTMX
  fetcher/             # SEPARATE container: FanFicFare + calibredb HTTP service (POST /fetch)
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
  (without dropping) and channels renamed from the admin UI. The **slug is editable** (kept stable
  on a plain rename); changing it rewrites that channel's `/feed/{slug}/{key}` URLs, so readers
  must re-subscribe.
- A channel has its own **budget** and **parallel_slots**; the **cadence is global** (one cron),
  as is reading speed (`config.wpm`) and the 👎 drop threshold.
- **One feed per slot:** `GET /feed/{channel_slug}/{feed_key}` where `feed_key` is `"1".."N"`.
  There is **no all-channels union feed** — subscribe to each channel/slot feed.
- **A slot is a feed *bucket*, not a single-book reservation.** Slot N's feed carries the *one*
  backlog book currently streaming in that slot **plus** all tracked stories pinned to that slot,
  interleaved. Picture each slot as a TV channel: one "main show" (a finite backlog book) running
  alongside several serial shorts (tracked, auto-updating stories).
- **Backlog (untracked) books stream one-at-a-time per slot.** At most **N are active per channel**
  (one per slot); extras stay `queued`. A slot may legitimately hold **zero** backlog books. When
  one completes or is dropped its slot frees and the next queued book rebalances in (lowest free slot).
- **Tracked stories are never capped, never queued, and never "complete".** Every active tracked
  story is eligible each broadcast (it self-gates on whether a chapter sits past its cursor) and is
  pinned to a slot by load-balancing: the slot with the fewest pinned works, tie-broken by the
  fewest chapters ever dropped into that slot. Pinning is **sticky** — and can be **overridden
  manually** by dragging a source between slot cards on the dashboard (`POST /admin/books/{id}/set-slot`;
  backlog books swap to keep one-per-slot, tracked just move). A valid manual pin survives the next
  broadcast (the assignment step only (re)places sources lacking a valid slot). `book.tracked` is the
  single flag that distinguishes the two behaviours (there is no `kind`).
- **Selection is slot-agnostic** (see Drop Planner): the per-channel weighted budget pass decides
  *which* chapters drop across the whole channel; slot assignment is a separate, sticky step that
  only decides *which feed* each chosen chapter lands in.
- Terminology: a scheduled **broadcast** is one drop cycle (it emits `drop` rows); **dropping**
  (❌) means *cancelling a source*. Don't conflate the two.

### Sources & units (one unified, EPUB-backed model)
- A **source** is a `book` row — always a Calibre EPUB (`calibre_id`). `tracked=True` marks one that
  auto-updates; it carries an optional `feed_url` (RSS trigger) and reuses `source_url` as the
  FanFicFare fetch URL. All sources hold `quota_weight`, votes, `status`, and live in a channel.
- A **unit** is one drop-able chunk: a whole EPUB chapter (`chapterize(epub)[cursor]`). Unit shape:
  `{title, html, word_count, source_url}`. There is no separate ongoing-entry path.
- **Library import (`POST /admin/library/add`) is routed by Calibre `#status`** (see
  `app/calibre/status.py`): an *updating* status (In-Progress / Incomplete / Hiatus) → a **tracked**
  source; a *done* status (Completed / Abandoned / Published) or blank → a **backlog** queue entry.
  A tracked book marked **`#read=Yes`** starts its `cursor_chapter_index` at the current EPUB end
  (caught up → only new chapters drop); otherwise it starts at chapter 1. One batched **Add** button
  in the Library UI — there is no separate per-row track action.
- **Updates:** pre-drop, the poller reads each trigger feed's newest GUID; changed feeds are batched
  into one **async** fetch job (`scheduler.submit_and_track`) that downloads the new chapters into
  Calibre in the background. The triggering broadcast does **not** wait — new chapters land in the
  *next* one. Feed-less tracked stories are refreshed by a daily sweep. Both the poller and sweep
  **skip** stories whose `#status` is done (Completed / Abandoned / Published) — their EPUBs are
  already complete, so re-fetching is wasted.
- **Stub handling (chapter labels & cursor floor):** if the site removed chapters, the fetcher
  archives the old EPUB as a separate Calibre entry, overwrites the book, and returns
  `stub {old,new}`. Fic-Beacon then bumps `book.chapter_label_offset` by `old−new` (so the next
  chapter still labels continuously — `absolute_chapter_number(book, physical_index)`), sets the
  cursor to `new`, and raises `book.cursor_floor` to `new` (the admin UI can't rewind below it).
  `cursor_chapter_index` is always a **physical** index into the current EPUB.

### Drop Planner — per-channel stochastic budget
- Runs per channel each broadcast. **First, assign slots** (`_assign_slots`): promote queued backlog
  books into free slots up to `parallel_slots`, and pin every active tracked story to a balanced
  slot. *Then* select content — selection is slot-agnostic.
- Effective budget `B = channel.budget + channel.budget_credit` (signed carry-over so the long-run
  mean tracks the budget).
- Candidates = the next unit of **every active source in the channel**: each active backlog book's
  next chapter (≤ N) **plus** every tracked story with a chapter past its cursor (uncapped). Ordered
  by `quota_weight` (weighted-random). Include a unit of size `w` with probability
  `p = clamp((B − used)/w, 0, 1)`, biased up by weight/votes. Included → emit + advance cursor;
  excluded → **roll over** whole to a later broadcast.
- **Pure stochastic:** no guaranteed first chapter — over budget, even a source's first unit can
  defer; a low-weight source may get nothing some cycles. **Never split a unit.**
- After the pass: `budget_credit += channel.budget − used` (clamped to ±budget). Sources whose
  units rolled over are written to a per-broadcast **skip log** (`app_state[last_broadcast_skips]`)
  surfaced on the dashboard.
- Each emitted `drop`'s `feed_key` is its source's pinned `slot_index`, so the chapter lands in
  that slot's feed regardless of which other sources also dropped this broadcast.
- Budget can be words or reading-time minutes (per-channel `budget_mode`; `config.wpm` is global).

### Permalinks (source-aware, per-chapter) — EPUB
FanFicFare writes a **per-chapter** canonical URL into each chapter's `<head>`:
`<meta name="chapterurl" content="...">`. The chapterizer reads it from the **raw zip** (ebooklib
strips `<head>`) keyed by file basename — see `app/epub/chapterizer.py:_chapter_url_map`.
Item link precedence (`app/feed/builder.py:_permalink`): (1) `drop.source_url` (per-chapter),
(2) `book.source_url` (whole-work `url:` identifier / fetch URL), (3) `/read/{slug}` reader page.
This applies uniformly — tracked stories are FanFicFare EPUBs and carry per-chapter `chapterurl`s too.
**GUID ≠ link.** The item `guid`/`id` is always `urn:fic-beacon:drop:{reader_slug}` (per-drop
uuid4) so multiple drops never collide on a shared work URL.

### In-EPUB images (served read-only)
Chapter HTML references images that live *inside* the EPUB zip (`<img src="images/…">`); nothing
else serves them, so readers 404 against the beacon origin. The chapterizer rewrites every relative
`<img>`/SVG `<image>`/`srcset` URL — resolved against the chapter's OPF-relative directory — to a
sentinel (`app/epub/chapterizer.py`); `materialize_image_urls()` swaps it for
`{base_url}/img/{calibre_id}/{path}` when a drop's `content_html` is materialised, so stored content
is self-contained and byte-stable (WebSub-safe). `GET /img/{calibre_id}/{path}`
(`app/routers/media.py`) streams the entry from the EPUB zip RO (re-anchored to the EPUB's OPF dir);
external (`http(s)`/`data:`/root-absolute) URLs pass through, traversal is rejected.

### Endnotes/footnotes (inlined per chapter)
Notes usually live in a back-matter file separate from the chapter that cites them, so a
single-chapter drop would dangle every note marker. `_build_note_index` (a two-pass scan: collect
referenced ids, then resolve them) maps each cited note's id → inline-ready HTML, and
`_inline_footnotes` replaces each marker **in place** with a collapsible
`<details class="beacon-note">` disclosure (the marker number becomes the `<summary>`; the note
expands right where it's cited). This is deliberate: a relative `#anchor` resolves against the
item's permalink in a feed reader and navigates the reader *away* from the feed, whereas
`<details>` stays put — so the **same HTML works in the feed and on the reader page**, in any
reader (no scroll-to-bottom, no anchor). Two marker conventions are detected (`_is_noteref`): EPUB3 semantic
(`epub:type="noteref"`/`rearnote`/`footnote`) and the plain superscript-anchor form
(`<a href="…#id"><sup>…</sup></a>`, no epub:type — the `<sup>` is the discriminator, so bare-text
Part/chapter nav links aren't misread as notes). **Only cross-file notes are inlined** — same-file
footnotes already resolve inside the dropped item. The note's own number/back-link anchors are
unwrapped (links dropped, authored number kept); note images use the image route above.

### Feedback contract (plain hyperlinks, any reader)
Four ordered actions per drop: **🪝 extra · 👍 up · 👎 down · ❌ drop**.
- `up` → `thumbs_up++`, `quota_weight ×= 1.25`. **Instant bare GET** `GET /fb/{token}?action=up`.
- `down` → `thumbs_down++`, `quota_weight ×= 0.8`; at `>= thumbs_down_drop_threshold` the book is
  `dropped`. **Instant bare GET.**
- `extra` (super-up) → `thumbs_up += 3`, `quota_weight ×= config.extra_boost_multiplier`
  (admin-configurable, default **1.5**; was a hard-coded `1.25**3 ≈ 1.95`), **and** inject an
  out-of-cycle drop.
  **Confirm page** (`/fb/confirm/{token}`).
- `drop` (super-down) → set book `dropped` immediately. **Confirm page.**
- **Idempotent per `(drop_id, action)`** so reader/proxy prefetch and double-clicks count once.
- The **🪝 extra link renders only when a next unit exists** (`extra_available`): a chapter past
  the cursor in the current EPUB (same check for backlog and tracked sources).
- Tokens are per-drop and unguessable; a click binds to exactly one book/drop.

### WebSub (realtime push)
Each feed declares `<link rel="hub" href="{base}/websub/hub">` + a correct `<link rel="self">`.
The self-hosted hub (`app/routers/websub.py`) handles subscribe/verify; `app/websub/publisher.py`
pushes the Atom body to verified subscribers after each cycle/extra. Works on InoReader's free
plan; degrades to polling for readers without WebSub.
**Verification honours `hub.verify` (PuSH 0.3 sync / WebSub async).** `POST /websub/hub` validates
the request (bad mode / foreign topic → 4xx) then picks the subscriber's preferred verification mode:
- **sync** (what Inoreader/Superfeedr request): verify **inline** — call the subscriber's callback
  while its subscribe request is still open, then return `204`. A sync subscriber arms its callback
  only *during* that request, so a deferred (post-response) callback arrives too late and the callback
  returns an empty `200` (verification silently fails). Inline verification is mandatory here.
- **async** (WebSub 0.4, or `hub.verify` absent): return **`202` immediately**, then verify + persist
  in a background task (own DB session) with **backoff retries** (`_VERIFY_DELAYS`) to absorb the
  arming race.

The verification GET **echoes back the subscriber's `hub.verify_token`** when present: PuSH 0.3
subscribers match a pending subscription on *both* `hub.topic` and `hub.verify_token` before echoing
the challenge. The whole subscribe→verify→store→push path is **debug-logged** (including the full
inbound hub form, key+value); set `BEACON_LOG_LEVEL=DEBUG` to trace it.
The advertised `rel=self` / topic is **tokened** (`{base}/feed/{slug}/{key}?token=…`) so the topic
URL is actually fetchable — WebSub requires the topic to return the *same* bytes the hub pushes, and
the feed route gates on `token`. The token is the single global `feed_secret` already embedded in
the feed URL the reader holds, so advertising it inside the (already token-gated) feed body leaks
nothing new. The publisher still matches subscriptions registered both with and without the
`?token=…` (some readers subscribe with their poll URL, others with the bare `rel=self`), so push is
never silently dropped. `hub._is_own_topic` accepts any `{base}/feed…` topic regardless of query.
The admin dashboard lists current subscribers + last/next cron runs for diagnosing "feed not
updating".

### Calibre access
Open `metadata.db` read-only. Books, authors, identifiers (`url:` source), **tags**, and the custom
columns **`#genre_manual`**/**`#genre`** (channel routing), **`#status`** (publication state →
import routing + fetch-skip), and **`#read`** (caught-up → cursor placement) come from there; EPUB
paths derive from the library folder structure. Missing custom columns degrade gracefully (empty).
Do not require a running Calibre. The fetcher container is what *writes* (via `calibredb`); the app
only ever reads.

### Fetcher contract (`app/fetch/client.py` ↔ `fetcher/app.py`) — batched & async
FanFicFare runs can take **~15 min**, so fetches are batched and asynchronous; they never block a
broadcast or an admin request.
- `POST {BEACON_FETCHER_URL}/fetch {"urls":[...]}` → **`202 {job_id}`** immediately. The fetcher
  works in a background `ThreadPoolExecutor(max_workers=1)` (one worker ⇒ `calibredb` writes never
  overlap, preserving the single-writer invariant). **NEW** stories (no matching `url:` identifier)
  download together in one warm `fanficfare -i` pass; **EXISTING** ones update per-story (`-u`,
  archive-on-stub, `add_format`). It borrows two ideas from AutomatedFanfic, trimmed: broad
  **force-detection** (`force_update_epub_always` guidance → force-redownload; a chapter *shrink* is
  the stub case) and a **3-try exponential backoff** on transient site/network errors.
  **Every `subprocess.run` has a wall-clock `timeout=`** (`FETCHER_FANFICFARE_TIMEOUT`, default
  1200s; `FETCHER_CALIBREDB_TIMEOUT`, default 600s): a hung site socket would otherwise block the
  lone worker forever and stall every queued job behind it (the "stuck at `fetching…`" failure). On
  timeout the child is killed and surfaced as a transient error so the worker always frees.
- `GET {BEACON_FETCHER_URL}/fetch/{job_id}` → `{status: running|done|unknown, results:[{url,
  calibre_id, chapter_count, stub:{old,new}|null, phase, error}]|null}`.
- App side: `submit_fetch(urls)` posts the batch and returns the `job_id`; `scheduler.submit_and_track`
  marks the books `fetching…` and persists the job→book map in `app_state` (so a restart resumes).
  A transient `fetch_poll_{id}` interval job polls until `done`, reflecting each book's live `phase`
  on the dashboard, then `apply_result(book, raw)` folds calibre_id / chapter_count / stub into the
  row. **Freshly fetched chapters land in the *next* broadcast**, not the one that triggered them.

## Data model (summary)

`channel` (`name`, `slug`, `genre_match`, `parallel_slots`, `budget_*`, `budget_mode`,
`budget_credit`, `queue_order`) · `book` (`calibre_id`, `tracked`, `feed_url?`,
`last_seen_guid?`, `last_fetch_at?`, `last_fetch_status?`, `source_url?`, `status`
queued|active|completed|dropped, `channel_id` **NOT NULL**, `slot_index`, `queue_position`,
`quota_weight`, `cursor_chapter_index`, `chapter_label_offset`, `cursor_floor`, thumbs) · `drop`
(`feedback_token`, `reader_slug`, `channel_id`, `feed_key`, `chapter_start/end`, `word_count`,
`source_url?`) · `feedback_event` · `websub_subscription` (`topic_url`, `callback_url`,
`secret?`, `lease_expires_at`, `verified`) · `config` (single-row globals: `wpm`, `cadence_cron`,
`thumbs_down_drop_threshold`, `extra_boost_multiplier`, `feed_secret`) · `app_state` (key/value runtime store, e.g.
`last_drop_run_at` / `last_poll_run_at`). See `Architecture.md §5`.

The app **version** has a single source of truth — `[project].version` in `pyproject.toml`,
read at runtime by `app/version.py` (no baked env var). Bump it there on release.

## Timezone

Drop/sweep times use `BEACON_TZ` (e.g. `Europe/Tallinn`), passed to APScheduler and
`CronTrigger`. With no `TZ`/`BEACON_TZ` set, a stock container resolves to **UTC**.

## Verification

- Generated feeds pass the **W3C Feed Validator** and render in **≥2 readers** (FreshRSS + InoReader).
- Feedback links work as plain GET hyperlinks from within a reader; up/down are instant + idempotent.
- The `beacon` container never writes the Calibre library (mount is `:ro`); the `fetcher` does.
- Batching never splits a unit; oversized units post whole; stochastic mean tracks the budget.
- A trigger feed's new GUID drives a FanFicFare fetch into Calibre; the chapters then drop via the
  normal cursor path. A stub keeps labels continuous (`chapter_label_offset`) and floors the cursor.
