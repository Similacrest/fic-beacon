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
- **APScheduler** (in-process: release cycle on `cadence_cron`, which polls feeds first; a daily
  feedless sweep). There is **no hourly poll** — feeds are checked pre-drop. The admin
  **"Run release cycle now" / "Check feeds now"** buttons run **off the request path** (a one-shot
  scheduler job), so the POST returns immediately and the tab is safe to close/refresh — the work
  runs in the scheduler thread. An **in-process lock** (`scheduler.cycle_status()`, single-worker)
  refuses a second run while one is in flight; the dashboard disables the buttons and shows a live
  progress banner (HTMX-polls `#dash-main` every 3s until the run clears).
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
    planner/           # Drop Planner (per-channel slot round-robin budget; unit abstraction)
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
  pinned to a slot by load-balancing: the slot with the fewest pinned works, tie-broken by the
  fewest chapters ever dropped into that slot. Pinning is **sticky** — and can be **overridden
  manually** by dragging a source between slot cards on the dashboard (`POST /admin/books/{id}/set-slot`;
  backlog books swap to keep one-per-slot, tracked just move). A valid manual pin survives the next
  broadcast (the assignment step only (re)places sources lacking a valid slot). `book.tracked` is the
  single flag that distinguishes the two behaviours (there is no `kind`).
- **Selection is slot-diverse** (see Drop Planner): slot assignment is a separate, sticky step
  that decides *which feed* each source lands in; the per-channel budget pass then **round-robins
  across the occupied slots** so drops spread over the slot feeds instead of one slot hogging the
  budget. `quota_weight` throttles a source's share *within* its slot; an idle slot's budget spills
  to the others.
- Terminology: a scheduled **broadcast** is one **release cycle** (it emits `drop` rows);
  **dropping** (❌) means *cancelling a source*. Don't conflate the two — the user-facing name is
  deliberately "release cycle" (button, dashboard, `run_release_cycle`, the `release_cycle` cron
  job, `last_release_run_at`) precisely to avoid the "run a drop cycle" vs "drop a source"
  ambiguity. The `drop` **row/table** and the ❌ drop **action** keep the word "drop".

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
  - **Tracked sources import at a higher default `quota_weight`** (`config.tracked_default_weight`,
    default **2.0**, tunable on the admin Settings page) vs **1.0** for backlog, so real ongoing
    serials get priority over the finite archive in the stochastic budget pass. This is a
    *default-weight* nudge, not a planner hardcode — weights stay per-source tunable/votable.
- **Updates:** pre-drop, the poller reads each trigger feed's newest GUID; changed feeds are batched
  into one **async** fetch job (`scheduler.submit_and_track`) that downloads the new chapters into
  Calibre in the background. The triggering broadcast does **not** wait — new chapters land in the
  *next* one. Feed-less tracked stories are refreshed by a daily sweep. Both the poller and sweep
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
  - **`total_chapters` is kept honest every broadcast**, not only when a source is selected:
    `planner._get_chapters` writes `book.total_chapters = len(chapterize(epub))` for *every* active
    source it inspects. A caught-up source is never selected, so if its EPUB later shrinks (e.g. an
    author unpublishes chapters — common on RoyalRoad) a stale `total_chapters` would otherwise show
    a phantom "N waiting" on the dashboard forever.

### Drop Planner — per-channel slot round-robin
- Runs per channel each broadcast. **First, assign slots** (`_assign_slots`): promote queued backlog
  books into free slots up to `parallel_slots`, and pin every active tracked story to a balanced
  slot. *Then* select content — the selection is **slot-diverse** (below).
- Effective budget `B = channel.budget + channel.budget_credit` (signed carry-over so the long-run
  mean tracks the budget).
- Candidates = the next unit of **every active source in the channel**: each active backlog book's
  next chapter (≤ N) **plus** every tracked story with a chapter past its cursor (uncapped).
- **Slot round-robin (`_plan_drops` pass 2).** Rotate through the channel's **occupied slots**; on
  each slot's turn a **weight-proportional** random source pinned to that slot (`_weighted_choice`
  by `quota_weight`) drops its next unit of size `w` if a stochastic roll `p = clamp((B − used)/w,
  0, 1)` passes. Included → emit + advance cursor; excluded → **roll over** whole to a later
  broadcast. This spreads drops across the slot feeds (**diversity**), while `quota_weight` governs
  each source's share *within* a slot — so down-voting a source shrinks its slice relative to its
  slot-mates. A slot whose sources are all caught-up (or capped) **passes its turn, spilling its
  budget to slots that still have content** — no wasted budget, but when only one slot has content
  it still fills up.
- **Pure stochastic:** no guaranteed first chapter — over budget, even a source's first unit can
  defer; a low-share source may get nothing some cycles. **Never split a unit.**
- **Oversized units accumulate, they don't force-post.** A unit larger than the channel's *base*
  per-cycle budget can't fit in one cycle, so it is **not** dropped every cycle. Instead an
  accumulation pass (runs before the stochastic pass) posts it whole only once `B` (base +
  saved-up credit) can afford it — a 9k chapter on a 3k budget posts once every ~3 cycles, so its
  long-run rate still tracks the budget. It's posted whole (never split), at most one oversized
  unit per source per cycle.
- After the pass: `budget_credit += channel.budget − used`. Negative is clamped to one base
  budget; **positive is clamped to the largest pending unit** (so an oversized chapter can be
  saved up for) or to one base budget when nothing oversized is pending (so idle channels don't
  run away). Sources whose units rolled over are written to a per-broadcast **skip log**
  (`app_state[last_broadcast_skips]`) surfaced on the dashboard.
- Each emitted `drop`'s `feed_key` is its source's pinned `slot_index`, so the chapter lands in
  that slot's feed regardless of which other sources also dropped this broadcast.
- **Soft read-gating.** A source whose **most-recent delivered drop is still unacknowledged**
  (`drop.acknowledged_at is None`) has its stochastic acceptance multiplied by
  `_UNACKED_WEIGHT_PENALTY` (0.5) — an *absolute* back-off (unlike `quota_weight`, which is relative
  within a slot), so even a lone unread source trickles more slowly and an un-caught-up reader falls
  behind less. It's a *nudge, not a hard gate* (the source still trickles), and a source with no
  drops yet is never penalised. A drop is acknowledged on opening `/read/{slug}`, clicking **any**
  `/fb/` link, or the explicit **✓ Mark read** action. This is best-effort and reader-agnostic:
  passive read-detection is unreliable (most permalinks point at the source site, not `/read/`, and
  readers don't report reads), so we deliberately chose an explicit/opt-in signal over a tracking
  pixel — image proxies (e.g. Inoreader) prefetch on poll and would mark everything read on ingest.
  See `planner.py:_unacknowledged_books`.
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
Five ordered actions per drop: **🪝 extra · 👍 up · 👎 down · ⏸ pause · ❌ drop**.
- `up` → `thumbs_up++`, `quota_weight ×= 1.25`. **Instant bare GET** `GET /fb/{token}?action=up`.
- `down` → `thumbs_down++`, `quota_weight ×= 0.8`, **and** `cooldown_remaining = max(2, …)` so the
  source sits out the next ≥2 broadcasts (the planner excludes candidates with
  `cooldown_remaining > 0` via `_active_books_in` and ticks it down once per broadcast in
  `_tick_cooldowns`); at `>= thumbs_down_drop_threshold` the book is `dropped` instead.
  **Instant bare GET.**
- `extra` (super-up) → `thumbs_up += 3`, `quota_weight ×= config.extra_boost_multiplier`
  (admin-configurable, default **1.5**; was a hard-coded `1.25**3 ≈ 1.95`), **and** inject an
  out-of-cycle drop.
  **Confirm page** (`/fb/confirm/{token}`).
- `pause` → set `book.paused` — the source broadcasts nothing until resumed. **Instant bare GET**
  (reversible, so no confirm page). A **backlog** book frees its slot (re-enters the queue so the
  next queued book streams in); a **tracked** story just stops (keeps its sticky slot). Paused
  sources are excluded from candidate selection and slot assignment (`_active_books_in`,
  `_assign_slots` filter `paused`). **Resume is admin-UI-only** — a paused source emits no feed
  items, so the feed can't carry a resume link. Pause/resume is unified on `pause_book`/`resume_book`
  (`planner.py`): the feed ⏸ link, the dashboard toggle (`POST /admin/books/{id}/pause|resume`), and
  the **Tracked Stories** page toggle (`/admin/ongoing/{id}/toggle`, batch pause/resume) all call it.
- `drop` (super-down) → set book `dropped` immediately. **Confirm page.**
- `read` → neutral **✓ Mark read** acknowledgement: sets `drop.acknowledged_at`, no weight change,
  no side effects. **Instant bare GET.** Feeds the soft read-gate below.
- **Idempotent per `(drop_id, action)`** so reader/proxy prefetch and double-clicks count once.
- **Any feedback action also acknowledges its drop** (`drop.acknowledged_at`), as does opening
  `/read/{slug}` — see read-gating below.
- The **🪝 extra link renders only when a next unit exists** (`extra_available`): a chapter past
  the cursor in the current EPUB (same check for backlog and tracked sources).
- Tokens are per-drop and unguessable; a click binds to exactly one book/drop.

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

`channel` (`name`, `slug`, `genre_match`, `parallel_slots`, `budget_*`, `budget_mode`,
`budget_credit`, `queue_order`, `feed_item_limit`) · `book` (`calibre_id`, `tracked`, `feed_url?`,
`last_seen_guid?`, `last_fetch_at?`, `last_fetch_status?`, `source_url?`, `status`
queued|active|completed|dropped, `paused`, `cooldown_remaining`, `channel_id` **NOT NULL**,
`slot_index`, `queue_position`,
`quota_weight`, `cursor_chapter_index`, `chapter_label_offset` (legacy scalar), `label_map?`
(piecewise stub offsets, JSON), `cursor_floor`, thumbs) · `drop`
(`feedback_token`, `reader_slug`, `channel_id`, `feed_key`, `chapter_start/end`, `word_count`,
`source_url?`, `acknowledged_at?`) · `feedback_event` · `websub_subscription` (`topic_url`, `callback_url`,
`secret?`, `lease_expires_at`, `verified`) · `config` (single-row globals: `wpm`, `cadence_cron`,
`thumbs_down_drop_threshold`, `extra_boost_multiplier`, `tracked_default_weight`,
`websub_max_push_bytes`, `feed_secret`) ·
`app_state` (key/value runtime store, e.g.
`last_release_run_at` / `last_poll_run_at`). See `Architecture.md §5`.

The app **version** has a single source of truth — `[project].version` in `pyproject.toml`,
read at runtime by `app/version.py` (no baked env var). Bump it there on release.

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
- A trigger feed's new GUID drives a FanFicFare fetch into Calibre; the chapters then drop via the
  normal cursor path. A stub keeps labels continuous (`label_map`, per-chapter URL identity) and
  remaps the cursor to the first surviving chapter — no unread chapter is skipped, even mid-work.
