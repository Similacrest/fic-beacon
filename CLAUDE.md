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
into **channels** (TV-style), sets a reading budget per release **schedule** (split across channels
by weight), and each release embeds
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
- **Never split a chapter / unit.** A release packs *whole* EPUB chapters. An oversized unit is
  posted whole.

## Stack

- **Python + FastAPI** (web/API + feeds + feedback + reader pages + WebSub hub)
- **APScheduler** (in-process: one release-cycle job per enabled **schedule**, each polling feeds first; a daily
  feedless sweep). There is **no hourly poll** — feeds are checked just before each release. The
  dashboard's **Run release / Check feeds** buttons run **off the request path** (a one-shot scheduler
  job, so closing the tab is safe) under an **in-process lock** (`scheduler.cycle_status()`,
  single-worker) that refuses a second run while one is in flight.
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
    routers/           # feed, feedback, reader, media (/img), admin, websub
    calibre/           # Calibre Adapter (metadata.db RO, identifiers, tags, EPUB paths)
    epub/              # Chapterizer (spine -> chapters + word counts, cached by book+mtime)
    planner/           # Drop Planner (per-release budget split by channel weight; slot round-robin)
    ongoing/           # RSS update-detection poller, feed-URL inference, add-by-URL (no content)
    fetch/             # async HTTP client to the fetcher (submit_fetch / poll_fetch / apply_result)
    feed/              # Feed Builder (feedgen)
    websub/            # WebSub publisher (push to subscribers)
    scheduler.py       # APScheduler wiring (timezone-aware); records every run's outcome
    health.py          # dashboard health: attention items, 14-day volume
    progress.py        # live stage of the running release cycle (banner)
    timefmt.py         # relative/absolute times, compact numbers for the admin UI
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
- A channel has its own **weight** (its relative share of a release's budget) and **parallel_slots**.
  Timing and budget belong to **schedules** (below); reading speed (`config.wpm`) and the vote/fade
  tuning are global.

### Schedules (when a release fires, and how much it may release)
A `schedule` row is `{name, cron, budget, budget_mode (words|minutes), enabled, sort_order}`; every
enabled row is its own APScheduler cron job (`release_cycle:{id}`, in `BEACON_TZ`), so e.g. 5000 words
on weekday mornings, 10000 on weekday evenings and 20000 at weekends are three rows. The firing
schedule's `budget` is the **whole release's** allowance. `run_release_cycle` runs in three phases: (1)
per channel assign slots, tick cooldowns and load each active source's next units; (2) **split the
budget across the channels that have content, proportionally to `channel.weight`** (`split_budget` — a
channel with nothing to release is left out of the denominator, so its share goes to the others instead of
being lost); (3) plan + materialise per channel under its share. Carried `budget_credit` is
**re-clamped at the start of each release to that release's scale** (else a 20k weekend release's
leftover would turn the next 5k release into 25k). The dashboard's **▶ Run release** takes a schedule
dropdown (a manual run must say whose budget to use; a disabled schedule can still be run by hand). The
last schedule can't be deleted, and a cron is validated on save (`scheduler.validate_cron`). The
migration turned the old `cadence_cron` into a `Default` schedule whose budget is the sum of the old
channel budgets (converted to words), and each channel's old budget into a weight relative to the
smallest — so the first release after upgrading is the same size and split as before.
- **One feed per slot:** `GET /feed/{channel_slug}/{feed_key}` where `feed_key` is `"1".."N"`.
  There is **no all-channels union feed** — subscribe to each channel/slot feed. Each slot feed
  serves at most **`channel.feed_item_limit`** items (newest first, per-channel, default 50, seeded
  from `settings.feed_item_limit`, editable on the Channels page). Older drops are **kept in the DB**
  (their `/read/` permalinks keep working) — they just fall off the tail of the feed.
- **A slot is a feed *bucket*, not a single-book reservation.** Slot N's feed carries the *one*
  backlog book currently streaming in that slot **plus** all tracked stories pinned to that slot,
  interleaved. Picture each slot as a TV channel: one "main show" (a finite backlog book) running
  alongside several serial shorts (tracked, auto-updating stories).
- **Backlog (untracked) books stream one-at-a-time per slot.** At most **N are active per channel**
  (one per slot); extras stay `queued`. A slot may legitimately hold **zero** backlog books. When
  one completes or is dropped its slot frees and the next queued book rebalances in (lowest free slot).
- **Tracked stories are never capped, never queued, and never "complete".** Every active tracked
  story is eligible each broadcast (it self-gates on whether a chapter sits past its cursor) and is
  pinned to a slot by load-balancing: the slot with the lowest **summed `quota_weight`** of pinned
  works (backlog book included — a raw count treats a heavyweight serial like a dormant one), tie-broken
  by the fewest chapters ever dropped into that slot. A story is **placed at add/import time**
  (`planner.assign_channel_slots`, tracked only — backlog promotion stays with the release cycle),
  not lumped until the next broadcast. Pinning is **sticky** — and can be **overridden
  manually** by dragging a source between slot cards on the dashboard, or with the slot picker on its
  story page (touch-friendly) (`POST /admin/books/{id}/set-slot`;
  backlog books swap to keep one-per-slot, tracked just move). A valid manual pin survives the next
  broadcast (the assignment step only (re)places sources lacking a valid slot). `book.tracked` is the
  single flag that distinguishes the two behaviours (there is no `kind`).
- **Selection is slot-diverse** (see Drop Planner): slot assignment is a separate, sticky step
  that decides *which feed* each source lands in; the per-channel budget pass then **round-robins
  across the occupied slots** so releases spread over the slot feeds instead of one slot hogging the
  budget. `quota_weight` throttles a source's share *within* its slot; an idle slot's budget spills
  to the others.
- Terminology: a scheduled **release** (a.k.a. broadcast, `run_release_cycle`) emits `drop` rows;
  **dropping** (❌) means *cancelling a source*. Don't conflate the two. In the **UI, say "release"**
  for the act and for what it emits ("last release", "recent releases", "unread releases", "next
  chapter to release") and keep "drop" only for cancelling a source (❌ Drop, Dropped, Bring back).
  The `drop` **table/model** (`Drop`) and the feedback action `drop` keep the word internally.

### Sources & units (one unified, EPUB-backed model)
- A **source** is a `book` row — always a Calibre EPUB (`calibre_id`). `tracked=True` marks one that
  auto-updates; it carries an optional `feed_url` (RSS trigger) and reuses `source_url` as the
  FanFicFare fetch URL. All sources hold `quota_weight`, votes, `status`, and live in a channel.
- A **unit** is one releasable chunk: a whole EPUB chapter (`chapterize(epub)[cursor]`). Unit shape:
  `{title, html, word_count, source_url}`. There is no separate ongoing-entry path.
- **Library import (`import_library_books`, via `POST /admin/add`) is routed by Calibre `#status`** (see
  `app/calibre/status.py`): an *updating* status (In-Progress / Incomplete / Hiatus) → a **tracked**
  source; a *done* status (Completed / Abandoned / Published) or blank → a **backlog** queue entry.
  A tracked book marked **`#read=Yes`** starts its `cursor_chapter_index` at the current EPUB end
  (caught up → only new chapters are released); otherwise it starts at chapter 1. **One Add page**
  (`/admin/add`): a textarea of story URLs (one per line → tracked, fetched in the background) and the
  Calibre library table share one channel picker and one **Add** button; the redirect lists what
  happened to each item (added / already here / invalid / gone from Calibre, with its live reason). A
  Calibre book whose story is already a source (same `story_key`, e.g. pasted as a URL but not yet
  downloaded) counts as "already here" and is hidden from the table — never a second row.
  `/admin/library` redirects there.
  - **Tracked sources import at a higher default `quota_weight`** (`config.tracked_default_weight`,
    default **2.0**, tunable on the admin Settings page) vs **1.0** for backlog, so real ongoing
    serials get priority over the finite archive in the stochastic budget pass. This is a
    *default-weight* nudge, not a planner hardcode — weights stay per-source tunable/votable.
- **Updates:** just before each release, the poller reads each trigger feed's newest GUID; changed feeds are batched
  into one **async** fetch job (`scheduler.submit_and_track`) that downloads the new chapters into
  Calibre in the background. The triggering broadcast does **not** wait — new chapters land in the
  *next* one. **A trigger GUID is marked seen (`last_seen_guid`) only when its fetch lands chapters**
  (`scheduler._settle_trigger`; the GUID rides in the `fetch_job` mapping): a failed fetch, or one that
  finds nothing new, leaves it unseen so the next cycle retries — capped at `TRIGGER_MAX_ATTEMPTS` (3)
  fetches per GUID (`app_state[trigger_retry:{book_id}]`) so a GUID that never yields a chapter (a
  skipped threadmark category, RSS running ahead of the site) can't refetch forever. Giving up writes
  `app_state[fetch_giveup:{book_id}]` (dashboard attention item; cleared when any fetch lands chapters).
  **Drift check:** with an unchanged GUID the poller still refetches if the feed's newest link is
  missing from the EPUB's chapter URLs while older entries are present (`poller._feed_behind_epub`),
  unless that GUID was given up on. A fetcher update that fails is an `error`, never `ok`. A book with a
  fetch in flight is skipped by the poller. A `304` from a trigger feed means "no change".
  Feed-less tracked stories are refreshed by a daily sweep. Both the poller and sweep
  **skip** stories whose `#status` is done (Completed / Abandoned / Published) — their EPUBs are
  already complete, so re-fetching is wasted.
- **Initial-download self-heal:** a story added by URL has no Calibre EPUB yet; its first download is
  a one-shot triggered at add time (`scheduler.trigger_fetch_pending`). Because that trigger can be
  lost (restart/race) and `poll_all_feeds` only *seeds* a feed's first-sight GUID without downloading,
  `poller.fetch_pending` also runs as a **backstop at the start of every release cycle** — it (re)submits
  any tracked book still missing its `calibre_id`, skipping ones already `fetching…`, so a story can't
  strand at `pending` forever.
- **Stub handling (chapter labels & cursor) — identity-based:** when the site removes chapters the
  fetcher archives the old EPUB as a separate Calibre entry, overwrites the book, and returns
  `stub {old, new, old_urls, new_urls}` — `old_urls`/`new_urls` are the **ordered per-chapter
  canonical URLs** (`<meta name="chapterurl">`) of the pre-/post-stub EPUBs, in the same spine order
  the chapterizer uses for physical indices. `apply_result` matches chapters by URL **identity**, so
  a removal *anywhere* (front, middle, tail), a mid-work gap where labels jump (4 → 73), and
  **repeated** stubs are all handled:
  - **Labels — piecewise `book.label_map`:** a JSON list of `[physical_index, cumulative_offset]`
    breakpoints from the URL diff, so `absolute_chapter_number` keeps each surviving chapter's
    original author label and **composes** across successive stubs. `chapter_label_offset` is the
    scalar fallback when the map is empty (old rows, or the count-only path below).
  - **Cursor — remapped, not reset:** `cursor_chapter_index` (and `cursor_floor`) move to the new
    physical index of the **first surviving chapter at/after the old position**, so a behind reader
    resumes on the same chapter with every unread one intact. (The old code set cursor = `new`,
    silently dropping unread chapters after a non-tail removal.) Cursor is always a physical index.
  - **Count-only fallback:** no `chapterurl`s (URL lists absent, or their lengths disagree with
    `old`/`new`) → the legacy linear path: bump `chapter_label_offset` by `old−new` and set cursor
    and `cursor_floor` to `new`. Only degrades non-FanFicFare EPUBs (no per-chapter identity anyway).
  - **`total_chapters` is always beacon's own chapterizer count of the EPUB it reads** — never the
    fetcher's `chapter_count`. `apply_result` chapterizes `book.calibre_id`'s EPUB (RO, mtime-cached) and
    uses that for `total_chapters` *and* the `fresh_from_index` comparison; the fetcher's spine count
    (front matter included, and of whichever Calibre entry it touched) is only a fallback when the EPUB
    can't be read here. Mixing the two produced a phantom "N waiting" that never released.
  - **`total_chapters` is kept honest every broadcast**, not only when a source is selected:
    `planner._get_chapters` writes `book.total_chapters = len(chapterize(epub))` for *every* active
    source it inspects. A caught-up source is never selected, so if its EPUB later shrinks (e.g. an
    author unpublishes chapters — common on RoyalRoad) a stale `total_chapters` would otherwise show
    a phantom "N waiting" on the dashboard forever.

### Drop Planner — per-channel slot round-robin
- Runs per channel each broadcast. **First, assign slots** (`_assign_slots`): promote queued backlog
  books into free slots up to `parallel_slots`, and pin every active tracked story to a balanced
  slot. *Then* select content — the selection is **slot-diverse** (below).
- Effective budget `B = channel_share + budget_credit`, where `channel_share` is the channel's slice of
  the firing schedule's budget (see Schedules) and `budget_credit` is a signed carry-over so the
  long-run mean tracks the share.
- Candidates = the next unit of **every active source in the channel**: each active backlog book's
  next chapter (≤ N) **plus** every tracked story with a chapter past its cursor (uncapped).
- **Slot round-robin (`_plan_drops` pass 2).** Rotate through the channel's **occupied slots**; on
  each slot's turn a **weight-proportional** random source pinned to that slot (`_weighted_choice`
  by `quota_weight`) drops its next unit of size `w` if a stochastic roll `p = clamp((B − used)/w,
  0, 1)` passes. Included → emit + advance cursor; excluded → **roll over** whole to a later
  broadcast. This spreads releases across the slot feeds (**diversity**), while `quota_weight` governs
  each source's share *within* a slot — so down-voting a source shrinks its slice relative to its
  slot-mates. A slot whose sources are all caught-up (or capped) **passes its turn, spilling its
  budget to slots that still have content** — no wasted budget, but when only one slot has content
  it still fills up.
- **Fresh chapters jump the roll (`_plan_drops` pass 1.5).** A chapter that arrives by an upstream
  update must be released in the **next available batch unless there is no budget for it**. When a
  fetch lands with more chapters than the EPUB had at the last broadcast, `apply_result` sets
  `book.fresh_from_index` (= that old `total_chapters`; **never** on a story's first download or an
  import backfill — `total_chapters` was `None` — so a freshly added 200-chapter serial doesn't claim
  priority on all of it; cleared on a stub and when the cursor catches up). A tracked story whose
  cursor has reached that index is *fresh*, and the fresh pass releases its next whole unit
  **deterministically — no stochastic roll, no unread penalty, no weight fade** — while budget
  lasts. Guard rails: **one unit per fresh source per cycle** (round-robin, oldest arrival first by
  `last_fetch_at`, so a 10-chapter dump can't eat the batch; leftovers stay fresh and go first next
  cycle), a unit that doesn't fit the remaining budget stays fresh (never split; oversized units keep
  their accumulation pass), and a weight-0 source never posts. A reader still *behind* on older
  chapters isn't fresh — those release in order through the normal pass. The fetcher's chapter count
  is a rough spine count, so the "more chapters" test can over-trigger; that's harmless because
  `fresh_from_index` is always a valid lower bound (a stale flag with no chapter behind it matches
  nothing).
- **Otherwise pure stochastic:** no guaranteed first chapter — over budget, even a non-fresh source's
  first unit can defer; a low-share source may get nothing some cycles. **Never split a unit.**
- **Oversized units accumulate, they don't force-post.** A unit larger than the channel's *base*
  per-cycle budget can't fit in one cycle, so it is **not** released every cycle. Instead an
  accumulation pass (runs before the stochastic pass) posts it whole only once `B` (base +
  saved-up credit) can afford it — a 9k chapter on a 3k budget posts once every ~3 cycles, so its
  long-run rate still tracks the budget. It's posted whole (never split), at most one oversized
  unit per source per cycle.
- After the pass: `budget_credit += channel_share − used`. Negative is clamped to one base
  budget; **positive is clamped to the largest pending unit** (so an oversized chapter can be
  saved up for) or to one base budget when nothing oversized is pending (so idle channels don't
  run away). Sources whose units rolled over are written to a per-broadcast **skip log**
  (`app_state[last_broadcast_skips]`) with *why*, as the planner saw it (`book_id`, next unit size,
  `oversized`, `fresh`, unread `penalty`, weight `fade`, the channel's `budget`/`base`/`used`; a source
  whose Calibre entry is gone gets `missing`). **The dashboard's per-source reason chip**
  (`app/planner/explain.py:explain`, pure) combines that record (why it was held *last* release —
  never recomputed from current state, which would lie once the user reads a release) with live state
  (paused, queued #n, cooling down, caught up, downloading, fetch error, weight 0, live unread
  streak — the forecast for the *next* one). Old entries without `book_id` match on title.
- Each emitted `drop`'s `feed_key` is its source's pinned `slot_index`, so the chapter lands in
  that slot's feed regardless of which other sources were also released that cycle.
- **Soft read-gating (transient unread ramp).** A source's stochastic acceptance is scaled by how
  many of its **most-recent drops in a row are unacknowledged** (`drop.acknowledged_at is None`):
  **0 or 1 unread → no penalty at all** (one unread drop is just "not read yet"), then **0.8× / 0.6× /
  0.4× …**, floored at `config.unacked_penalty_floor` (0.2). It's an *absolute* back-off (unlike
  `quota_weight`, which is relative within a slot), a *nudge, not a hard gate* (the source still
  trickles), and it **never touches `quota_weight`** — the streak is derived from the drop rows and
  evaporates the moment any drop is read, so a week away costs nothing permanent (nothing is
  auto-dropped for being unread). A source with no drops yet is never penalised. A **🪝-injected
  drop is created already acknowledged**: asking for an extra chapter is engagement, not falling
  behind (it used to sit unread and penalise the source next cycle). A drop is acknowledged on opening `/read/{slug}`, clicking **any**
  `/fb/` link, or the explicit **✓ Mark read** action. This is best-effort and reader-agnostic:
  passive read-detection is unreliable (readers don't report reads, and many show the full item
  content without ever opening the `/read/` permalink), so we deliberately chose an explicit/opt-in signal over a tracking
  pixel — image proxies (e.g. Inoreader) prefetch on poll and would mark everything read on ingest.
  See `planner.py:_unread_penalties`.
- **Weight fade.** Below `config.weight_skip_floor` (default 1.0) a source's acceptance is scaled by
  `weight / floor` (`planner._weight_fade`), so a low-rated source trickles ever more slowly rather than
  being skipped outright; weight `0` never posts. The weight is capped at **100.0** (`WEIGHT_CAP`).
- A schedule's budget can be words or reading-time minutes (per-schedule `budget_mode`; `config.wpm`
  is global).

### EPUBs that chapterize to nothing
The chapterizer resolves the TOC by exact spine name first (the only strategy a book that already
chapterizes uses, so its chapter list and every cursor into it never change); only if that yields
nothing does it retry by unambiguous basename, and only if *that* yields nothing does it fall back to
the spine (also when there is no TOC at all). If an
*active backlog* book still has an EPUB with 0 chapters, `_set_aside_empty_backlog` pauses it (so it
stops holding its slot and starving the queue), records why in `app_state[book_note:{id}]` (dashboard ⚠
badge; cleared by resume) and promotes the next queued book in the same cycle. A *missing* EPUB file or
Calibre entry is a different, possibly transient, problem: warned about, never paused. Tracked stories
with 0 chapters are just still downloading and are left alone.

### Permalinks — always the mirror
Every feed item links to **our own reader page**, `{base_url}/read/{slug}`
(`app/feed/builder.py:_permalink`) — **never the story site**, for backlog and tracked sources alike.
Reading happens on the mirror, and opening it acknowledges the drop (read-gating). FanFicFare's
per-chapter canonical URL (`<meta name="chapterurl">`, read from the raw zip by
`app/epub/chapterizer.py:_chapter_url_map`) is still recorded on `drop.source_url` and drives stub
identity, but it is not linked from the feed.
The reader page has **← Previous / Next →** links between the same story's releases in *release*
order (`reader._neighbours`; not chapter index — a stub renumbers indices), so Next stops at the
latest released chapter.
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
external (`http(s)`/`data:`/root-absolute) URLs pass through, traversal is rejected. Drops stored under
an earlier base URL carry that origin in their baked-in `/img/` URLs; `sign_image_urls` rewrites
`/img/` URLs on **our own** alias origins (`settings.image_alias_bases` = the feedback origin +
`BEACON_LEGACY_BASE_URLS`) onto `base_url` and signs them at render time. Other hosts are never
touched — chapters embed third-party images whose paths can look like `/img/123/x.jpg`.

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

### Admin UI
Pages: **Dashboard**, **Add**, **Channels**, **Schedules**, **Settings**, plus one **story page** per
source. Built for a large touch screen as well as a desktop: reasons and errors are written out, not
only in tooltips. Times are relative (`|ago`) with the full timestamp in `title=` (`app/timefmt.py`).
- **Dashboard** (`/admin/`):
  - *Health card* (`app/health.py`): "All healthy" or "N need attention" (failed run, no enabled
    schedule, set-aside / fetch-failed / missing-from-Calibre / stuck-download stories, stale push
    subscriptions — each links to the story), last and next release, a 14-day words-per-day sparkline
    (days cut in `BEACON_TZ`), the unread count, and the run controls.
  - *Runs:* ▶ Run release (schedule picker) and 🔄 Check feeds. While one is in flight the banner shows
    its stage (`app/progress.py`, set by the scheduler job and by the planner per channel) and elapsed
    time. Every run — scheduled or manual — records its outcome or error in
    `app_state[last_run_result:release|feedcheck]` (`scheduler._record_run`).
  - *Holds:* **⏸ Pause releases** (`app_state[releases_paused]`) holds every *scheduled* release until
    ▶ Resume; **⏭ Skip next release** (`app_state[skip_next_release]` = `{schedule_id, at}`) holds just
    the earliest upcoming firing, used up when that firing comes (`scheduler._release_hold`; a target
    over an hour past without firing is void). A skipped firing still checks feeds; while paused
    nothing is fetched (the daily sweep is skipped too). A held firing releases nothing (no drops, no credit carried, no cooldown tick) and records `held` in its run
    result; the manual ▶ Run release is never held.
  - *Find & act:* a search box + chips (attention / unread / paused / tracked / backlog / queued)
    filter rows client-side (`data-search`/`data-flags`, set in `admin._build_slot_view`; the state
    survives HTMX swaps). **☑ Select** reveals row checkboxes and a sticky bar →
    `POST /admin/books/batch` (`pause|resume|fetch|move|read|drop`; stories an action doesn't apply to are
    skipped and counted in the `?done=&n=&skipped=` flash).
  - *On air:* per channel, slot cards with one row per story and its reason chip
    (`planner/explain.py`), the channel's queue ("Up next", ↑↓ within the channel), then fetches in
    progress, system status (in time order), recently completed and dropped. Push state shows per slot feed.
- **Add** (`/admin/add`): see Sources & units.
- **Story page** (`GET /admin/books/{id}`, `admin/story.html`) is the one place for a story's
  controls: pause/resume, fetch now, drop, cursor (⏮/⏭/exact), weight, track toggle, channel, slot
  picker, queue position, source and fetch facts, recent releases (read/unread) and your feedback.
  Recovery lives there: **✓ Mark N read** (acknowledges every unread release → lifts the unread
  slowdown, weight untouched), **↩ Undo** of the last manual cursor move
  (`app_state[cursor_undo:{id}]`; offered only while the cursor is still where the move left it, so
  it can't re-send chapters a release has since delivered), **↺ Bring back** for a dropped source
  (`POST …/restore`: same chapter, weight and votes; a weight of 0 resets to 1) vs **⏮ Start over** /
  **↺ Read again** (`…/requeue`, resets). **🗑 Remove permanently** (`POST …/delete`, dropped or
  finished stories only, so a live one needs a ❌ Drop first) deletes the source and its release
  history from Fic-Beacon — never from Calibre — and sends you to the dashboard.
- Book action routes take an optional `next` form field (`admin._back`: only `/admin/…` paths, no
  `//`) and redirect there, else to the dashboard; inline auto-saves answer HTMX with 204.

### Feedback contract (plain hyperlinks, any reader)
Five ordered actions per drop: **🪝 extra · 👍 up · 👎 down · ⏸ pause · ❌ drop**.
Links render as **bare emoji** (no text label); the wording lives in each link's `title=`/`aria-label=`
(hover tooltip + screen readers). Same markup in the feed and on `/read/{slug}` (`feedback_block`).
- Weights are **additive**: `quota_weight` lives on its natural ~0–3 scale, hard-capped at **100.0**.
- `up` → `thumbs_up++`, `quota_weight += config.vote_step` (default **0.25**). **Instant bare GET**
  `GET /fb/{token}?action=up`.
- `down` → `thumbs_down++`, `quota_weight −= config.vote_step`, **and** `cooldown_remaining = max(2, …)`.
  `_tick_cooldowns` runs at the *start* of each channel's turn (before selection, and before the
  empty-channel exit — a lone cooling source must not freeze its channel), so a 2 is 1 when selection
  runs: the source sits out **exactly one** broadcast. Weight reaching **0 via a 👎** auto-drops the
  source (`dropped`); the old `thumbs_down_drop_threshold` count is retired, and typing 0 into the
  admin weight box does *not* drop it (it just never posts). **Instant bare GET.**
- `extra` (super-up) → `thumbs_up += 3`, `quota_weight += config.extra_boost_step` (admin-configurable,
  default **0.5**; additive — the old multiplicative boost compounded too fast), **and** inject an
  out-of-cycle drop (born acknowledged, see read-gating). **Rate-limited per channel per release
  cycle** (`config.extra_per_channel_per_cycle`, default **1**, `0` disables): the count lives in
  `app_state[extra_used:{channel_id}]` and is wiped at the start of every release cycle. Over the limit
  the confirm page (and the POST) return an explanatory page naming the next release and change
  **nothing** — the refusal returns *before* `apply_feedback` records the `FeedbackEvent`, else the
  `(drop, extra)` idempotency guard would block a legitimate retry on that drop after the next
  release. The 🪝 link stays in the feed regardless (bodies stay byte-stable for WebSub); it's
  enforced at click time. A repeat click on an already-granted drop is a no-op, not a refusal. An extra
  on a paused/dropped/completed source injects nothing, and `_extra_available` hides the link for them.
  **Confirm page** (`/fb/confirm/{token}`).
- `pause` → set `book.paused` — the source broadcasts nothing until resumed. **Instant bare GET**
  (reversible, so no confirm page). A **backlog** book frees its slot (re-enters the queue so the
  next queued book streams in); a **tracked** story just stops (keeps its sticky slot). Paused
  sources are excluded from candidate selection and slot assignment (`_active_books_in`,
  `_assign_slots` filter `paused`). **Resume is admin-UI-only** — a paused source emits no feed
  items, so the feed can't carry a resume link. Pause/resume is unified on `pause_book`/`resume_book`
  (`planner.py`): the feed ⏸ link, the story page's toggle (`POST /admin/books/{id}/pause|resume`), and
  and the dashboard's batch bar (`POST /admin/books/batch`) all call it.
- `drop` (super-down) → set book `dropped` immediately. **Confirm page.**
- `read` → neutral **✓ Mark read** acknowledgement: sets `drop.acknowledged_at`, no weight change,
  no side effects. **Instant bare GET.** Feeds the soft read-gate below.
- **Idempotent per `(drop_id, action)`** so reader/proxy prefetch and double-clicks count once.
- **Any feedback action also acknowledges its drop** (`drop.acknowledged_at`), as does opening
  `/read/{slug}` — see read-gating below.
- The **🪝 extra link renders only when a next unit exists** (`extra_available`): a chapter past
  the cursor in the current EPUB (same check for backlog and tracked sources).
- Tokens are per-drop and unguessable; a click binds to exactly one book/drop.
- **Feedback links may live on their own origin** (`BEACON_FEEDBACK_BASE_URL`, default `base_url`):
  `settings.feedback_base` is used only by `feed/builder.py:_feedback_html`. **`/read/` item links**
  use `settings.reader_base` (`BEACON_READER_BASE_URL`, default the feedback origin), so everything
  the user clicks can live on a host the feed reader's servers can't reach. Feeds, the hub and `/img`
  URLs *inside feed items* stay on `base_url` (the reader's fetcher/image proxy needs them). The
  `/read` page itself emits **root-relative** `/img/…?sig=` and `/fb/…` URLs (`sign_image_urls(…,
  out_base="")`, `feedback_block(drop, base="")`), so it works on whichever origin serves it. The confirm form posts to a *relative* `/fb/confirm/{token}` (CSP
  `form-action 'self'`), and `/fb` is a public prefix so it isn't origin-checked — the origin that
  served the page receives the POST.

### WebSub (realtime push)
Each feed declares `<link rel="hub" href="{base}/websub/hub">` + a correct `<link rel="self">`.
The self-hosted hub (`app/routers/websub.py`) handles subscribe/verify; `app/websub/publisher.py`
pushes the Atom body to verified subscribers after each cycle/extra. Works on InoReader's free
plan; degrades to polling for readers without WebSub.
**The push body is byte-budgeted, not the full feed.** A slot feed carries up to
`channel.feed_item_limit` *full-content* chapters (~1.6 MB for 50 items), and **Inoreader silently
drops oversized fat pings** — it 200-acks the POST but never ingests, so realtime dies and the feed
falls back to slow polling (the classic "dashboard says Realtime but updates arrive hours late").
So the publisher trims the push to the **newest drops that fit `config.websub_max_push_bytes`**
(default 100 KB; `0` disables trimming), always keeping ≥1 item and never splitting a chapter — see
`publisher._trim_to_budget`. This means a push is **no longer byte-identical** to a GET of the topic
(readers merge the pushed newest items into the polled feed by GUID, which WebSub permits); the
polled `/feed/…` route still serves the full `feed_item_limit`.
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
URL is actually fetchable (WebSub wants the topic to resolve; the pushed body is a byte-budgeted
*subset* of it, merged by GUID — see above), and the feed route gates on `token`. The token is the single global `feed_secret` already embedded in
the feed URL the reader holds, so advertising it inside the (already token-gated) feed body leaks
nothing new. The hub **rejects** any topic lacking a valid token (`safety.topic_status`: foreign → 404, missing/wrong
token → 403) and the publisher only pushes to subscriptions whose stored topic carries the *current* token
(extra query params are fine; token-free rows are pruned at startup — `safety.prune_subscriptions`).
The dashboard shows push state per slot feed (⚡ n subscribers / ⚠ stale / polling) for diagnosing
"feed not updating".

### Public-exposure security (see CHANGELOG "Security")
Everything is **deny-by-default** behind `app/security.py:ProxyTokenMiddleware`: only `/feed` `/read` `/fb` `/img` `/static` (+`/robots.txt`; `/websub` iff `BEACON_PUBLIC_WEBSUB`) are reachable without a valid `X-Beacon-Proxy-Token` (`BEACON_PROXY_TOKEN`); the rest 401, or 503 when the token is unset and `BEACON_BIND_HOST` isn't loopback. `X-Remote-User` is audit-only and honoured only with a valid token. New public routes must be added to `_PUBLIC_PREFIXES`. Non-GET admin requests are CSRF-checked (`Sec-Fetch-Site`/`Origin`). `BEACON_ADMIN_OPEN` is the explicit no-proxy opt-out. Chapter HTML is sanitised (`app/sanitize.py`) and `/read`+`/fb` pages carry a CSP and escape every story-derived string — keep it that way. Story URLs must pass `is_fetchable_url` (they reach FanFicFare argv); the fetcher is gated by `FETCHER_TOKEN`. URL secrets (`?token=`, `?sig=`, `/fb/{token}`) are masked in the uvicorn access log (`main._AccessLogSecretFilter`) — logs leave the host. The WebSub hub requires the **tokened** topic and only https callbacks resolving to public IPs, connected by pinned IP with no
redirects (`app/websub/safety.py`) — never loosen these. `/img` serves only `image/*` entries with a valid
`?sig=` (HMAC keyed by the feed secret), signed at render time (`sign_image_urls`) not stored. The DB
`config.feed_secret` is authoritative over `BEACON_FEED_SECRET`.

### Calibre access
Open `metadata.db` read-only. Books, authors, identifiers (`url:` source), **tags**, and the custom
columns **`#genre_manual`**/**`#genre`** (channel routing), **`#status`** (publication state →
import routing + fetch-skip), and **`#read`** (caught-up → cursor placement) come from there; EPUB
paths derive from the library folder structure. Missing custom columns degrade gracefully (empty).
Do not require a running Calibre. The fetcher container is what *writes* (via `calibredb`); the app
only ever reads.

### Ongoing vs completed (`book.story_status`)
`calibredb add` sets no custom columns, so a story added by URL has a blank Calibre `#status` forever
(→ `classify_status` "unknown" → never skipped, re-fetched every cycle for the rest of time). The
fetcher therefore reports each story's status and `apply_result` records it on **`book.story_status`**
(Beacon DB only — Calibre is never written by the app). The poller/sweep skip a story if **either**
`story_status` or the Calibre `#status` is done (`_drop_done`); a completed story stays **tracked**
(keeps its slot, keeps delivering its remaining chapters) and just stops being re-fetched. The manual
**Fetch now** button bypasses the skip, so a story that resumes can refresh its status. On a story's
*first* download `apply_result` also adopts the canonical `story_url` as `source_url` (unless another
source owns it) — the Calibre `url:` identifier the fetcher later searches by is the canonical one, so
keeping the pasted URL made every later update miss the library entry and re-download a duplicate.
The story page shows the status; the Add page reports a duplicate or invalid add instead of silently
ignoring it.

### Fetcher contract (`app/fetch/client.py` ↔ `fetcher/app.py`) — batched & async
FanFicFare runs can take **~15 min**, so fetches are batched and asynchronous; they never block a
broadcast or an admin request.
- `POST {BEACON_FETCHER_URL}/fetch {"urls":[...], "calibre_ids": {url: id}?}` → **`202 {job_id}`**
  immediately. **`calibre_ids` pins a URL to the Calibre entry beacon already reads** (`book.calibre_id`,
  sent by `submit_and_track` when every book for that URL agrees): the fetcher updates *exactly* that
  entry with no URL search, and `apply_result` ignores a result whose `calibre_id` differs from the
  book's (its count/stub describe another EPUB; it only follows a new id if the old entry was deleted
  from Calibre). Without a pin (a brand-new story, an older app) the entry is found by **story identity**
  — `_story_key` (host + numeric story id), never the exact `url:` string: calibredb's `=url:X` is a
  *contains* match on the value, so the exact string misses a differently-spelled identifier and a short
  id can hit a longer one; the search is a pre-filter, each hit's identifier is then key-checked, lowest
  id wins. `app/ongoing/story_key.py` mirrors `fetcher/app.py:_story_key` (tests assert they agree) and
  also drives duplicate detection and `normalize_story_url` on add. The fetcher
  works in a background `ThreadPoolExecutor(max_workers=1)` (one worker ⇒ `calibredb` writes never
  overlap, preserving the single-writer invariant). **NEW** stories (no matching `url:` identifier)
  download together in one warm `fanficfare -i` pass; **EXISTING** ones update per-story (`-u`,
  archive-on-stub, `add_format`). The NEW path is hardened the same way as EXISTING: each produced
  EPUB is matched back to its submitted URL by **story identity** (`_story_key`: host + numeric story
  id — FanFicFare canonicalises URLs, e.g. FFN `/s/123` → `/s/123/1/Title`, so an exact-string match
  used to report a *successful* download as `fanficfare produced no epub` and silently discard it);
  empty results are retried with backoff while the output looks transient; and a genuine failure
  carries **FanFicFare's own output** (tail) in `error`, attributed per URL (a multi-URL batch's
  failures are re-run alone so each gets its own reason). `calibredb add` exits 0 but adds nothing for
  a title+author duplicate, so a NEW download with no reported id refreshes the matching entry
  (`add_format`) — only if its `url:` identifier was actually read and key-matches (`strict` lookup) —
  and otherwise fails loudly; it never force-adds a duplicate and never reports "done" without an
  entry. **The stub archive** is added with `add -d` (the live entry has the same
  title/author, so a plain add silently kept no archive) and its URL is moved to a **`url-archive:`**
  identifier, so a `url:` lookup can never resolve to the archive instead of the live book. It borrows two ideas from AutomatedFanfic, trimmed: broad
  **force-detection** (`force_update_epub_always` guidance → force-redownload; a chapter *shrink* is
  the stub case) and a **3-try exponential backoff** on transient site/network errors.
  **Every `-u` backdates the exported EPUB and passes `-o always_overwrite=true`**
  (`fetcher/app.py:_fanficfare_update`): FanFicFare's writer skips a file whose mtime date is later
  than the story's last update, and `calibredb export` writes mtime=now, so updates silently landed
  only on same-day posts. Its "more recently than Story … - Skipping" warning is a fetch error, not ok.
  **Every `subprocess.run` has a wall-clock `timeout=`** (`FETCHER_FANFICFARE_TIMEOUT`, default
  1200s; `FETCHER_CALIBREDB_TIMEOUT`, default 600s): a hung site socket would otherwise block the
  lone worker forever and stall every queued job behind it (the "stuck at `fetching…`" failure). On
  timeout the child is killed and surfaced as a transient error so the worker always frees.
- `GET {BEACON_FETCHER_URL}/fetch/{job_id}` → `{status: running|done|unknown, results:[{url,
  calibre_id, chapter_count, stub:{old,new}|null, story_url, story_status, phase, error}]|null}`.
  `url` is always the **submitted** URL (the app maps results back by it); `story_url` is the
  canonical URL FanFicFare wrote into the EPUB, and `story_status` its publication status
  (`In-Progress`/`Completed`/`Hiatus`/…), read from the title page's `Status:` line or the OPF
  `dc:subject`.
- App side: `submit_fetch(urls)` posts the batch and returns the `job_id`; `scheduler.submit_and_track`
  marks the books `fetching…` and persists the job→book map in `app_state` (so a restart resumes).
  The poll is registered only **after the submitting transaction commits** (a SQLAlchemy
  `after_commit` hook), the release cycle commits its feed poll/submits *before* planning drops, and
  `scheduler.ensure_fetch_polls` (startup + every cycle) re-polls any persisted job lacking a poll —
  else a slow cycle let the poll give up before its mapping existed.
  A transient `fetch_poll_{id}` interval job polls until `done`, reflecting each book's live `phase`
  on the dashboard, then `apply_result(book, raw)` folds calibre_id / chapter_count / stub into the
  row. **Freshly fetched chapters land in the *next* broadcast**, not the one that triggered them —
  and there they take the deterministic *fresh pass* (see Drop Planner), not the stochastic roll.
- **XenForo threadmark ordering is a fetcher-config concern.** SB/SV/QQ threads group threadmarks by
  *category* (Story/Threadmarks, Sidestory, Apocrypha, Omake, Media, Informational, Staff Post) in a
  fixed, non-chronological order, so a new Story chapter inserts *mid-EPUB* and shifts the trailing
  categories forward. The reading cursor (`cursor_chapter_index`) is a **positional index that
  assumes chapters only ever append at the end** (`planner.py` slices `all_chapters[cursor:]`), so a
  mid-EPUB insert makes stale tail chapters leak to the feed instead of the new one. This is **not**
  the stub path (that only fires on a chapter-count *shrink*). The fix lives entirely in the
  fetcher's `personal.ini` `[base_xenforoforum]` block (see `fetcher/personal.ini.example`):
  `skip_threadmarks_categories` drops the categories you don't read and
  `order_threadmarks_by_date_categories` date-orders the rest so new chapters append at the EPUB end,
  restoring the append-only invariant. No app code is involved.

## Data model (summary)

`channel` (`name`, `slug`, `genre_match`, `parallel_slots`, `weight`,
`budget_credit`, `queue_order`, `feed_item_limit`) · `schedule` (`name`, `cron`, `budget`,
`budget_mode`, `enabled`, `sort_order`) · `book` (`calibre_id`, `tracked`, `fresh_from_index?`, `story_status?`, `feed_url?`,
`last_seen_guid?`, `last_fetch_at?`, `last_fetch_status?`, `source_url?`, `status`
queued|active|completed|dropped, `paused`, `cooldown_remaining`, `channel_id` **NOT NULL**,
`slot_index`, `queue_position`,
`quota_weight`, `cursor_chapter_index`, `chapter_label_offset` (legacy scalar), `label_map?`
(piecewise stub offsets, JSON), `cursor_floor`, thumbs) · `drop`
(`feedback_token`, `reader_slug`, `channel_id`, `feed_key`, `chapter_start/end`, `word_count`,
`source_url?`, `acknowledged_at?`) · `feedback_event` · `websub_subscription` (`topic_url`, `callback_url`,
`secret?`, `lease_expires_at`, `verified`) · `config` (single-row globals: `wpm`,
`vote_step`, `extra_boost_step`, `extra_per_channel_per_cycle`, `weight_skip_floor`,
`unacked_penalty_floor`, `tracked_default_weight`, `websub_max_push_bytes`, `feed_secret`) ·
`app_state` (key/value runtime store, e.g.
`last_release_run_at` / `last_poll_run_at`). See `Architecture.md §5`.

The app **version** has a single source of truth — `[project].version` in `pyproject.toml`,
read at runtime by `app/version.py` (no baked env var). Bump it there on release.

## Logging

`main._configure_logging` sets the `app` logger from `BEACON_LOG_LEVEL`. **Nothing may reconfigure
logging afterwards**: `init_db()` runs Alembic embedded with `cfg.attributes["configure_logger"] =
False`, which `alembic/env.py` honours (its `fileConfig` used to reset root to WARNING and disable every
existing logger, silencing the app and uvicorn after startup). Even the CLI path passes
`disable_existing_loggers=False`.

## Timezone

Drop/sweep times use `BEACON_TZ` (e.g. `Europe/Tallinn`), passed to APScheduler and
`CronTrigger`. With no `TZ`/`BEACON_TZ` set, a stock container resolves to **UTC**.
`docker-compose.yml` also sets the container `TZ` from `BEACON_TZ` so the wall clock and the
scheduler agree.

**Never hand APScheduler a naive `datetime.now()`** for `next_run_time`/`run_date`. APScheduler
localises naive datetimes to the scheduler tz, so a UTC-clock container with a non-UTC `BEACON_TZ`
would place the run hours in the past — firing interval jobs immediately and dropping date jobs as
misfires. Use `app/scheduler.py:_now()` (tz-aware). This bit the async fetch polls once: they fired
before the submitting cycle committed the `fetch_job` mapping and unscheduled themselves, stranding
stories at `fetching…` (see CHANGELOG).

## Verification

- Generated feeds pass the **W3C Feed Validator** and render in **≥2 readers** (FreshRSS + InoReader).
- Feedback links work as plain GET hyperlinks from within a reader; up/down are instant + idempotent.
- The `beacon` container never writes the Calibre library (mount is `:ro`); the `fetcher` does.
- Batching never splits a unit; oversized units post whole; stochastic mean tracks the budget.
- A trigger feed's new GUID drives a FanFicFare fetch into Calibre; the chapters are then released via the
  normal cursor path. A stub keeps labels continuous (`label_map`, per-chapter URL identity) and
  remaps the cursor to the first surviving chapter — no unread chapter is skipped, even mid-work.
