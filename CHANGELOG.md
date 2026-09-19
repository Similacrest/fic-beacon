# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — release schedules with their own budgets (migration `a3b4c5d6e7f8`)
- New **Schedules** page: any number of `{cron, budget, words|minutes, enabled}` rows — e.g. 5000 words
  on weekday mornings, 10000 on weekday evenings, 20000 at weekends. Each enabled row is its own cron
  job; the firing schedule's budget is the whole release's allowance. Crons are validated on save
  (the old bare `try/except: pass` around the reschedule is gone).
- **Channel `budget` → `weight`.** A release's budget is split across the channels **that have content**
  in proportion to their weight, so an idle channel's share goes to the others instead of being lost.
  Carried `budget_credit` is re-clamped to each release's scale so a big release's leftover can't inflate
  the next small one.
- The dashboard's **▶ Run** button now has a schedule dropdown, and System status lists every schedule's
  next fire. The last schedule can't be deleted; a disabled one can still be run by hand.
- Migration: the old `config.cadence_cron` becomes a `Default` schedule whose budget is the sum of the old
  channel budgets (minutes converted with `config.wpm`); each channel's weight is its old budget relative to
  the smallest. `config.cadence_cron`, `channel.budget` and `channel.budget_mode` are removed.
- Fixed: `trigger_feed_check` returned `None` instead of `True` when it queued a run.

### Added — new upstream chapters are released in the next batch (migration `f2a3b4c5d6e7`)
- A chapter that arrives by an upstream update now ships in the **next available batch unless there is no
  budget for it**, instead of being left to the stochastic roll. `apply_result` flags
  `book.fresh_from_index` when a fetch lands more chapters than the EPUB had at the last broadcast (never
  on a story's first download or an import backfill), and a new **fresh pass** in the planner releases
  each fresh tracked story's next whole unit deterministically — no roll, no unread penalty, no weight fade.
- Guard rails: **one unit per fresh source per cycle** (a 10-chapter dump can't eat the batch; leftovers stay
  fresh and go first next cycle), oldest arrival first, a unit that doesn't fit the remaining budget waits
  (never split), oversized units keep their accumulation pass, and a weight-0 source never posts. A reader
  still behind on older chapters keeps reading them in order.

### Fixed — adding a tracked story no longer fails with "fanficfare produced no epub" (migration `e1f2a3b4c5d6`)
- **Likely root cause of the spurious error:** the fetcher matched each downloaded EPUB to a submitted URL by
  exact string, but FanFicFare canonicalises URLs (FFN `/s/123` → `/s/123/1/Title`, dropped `www.`,
  XenForo `/page-N`), so in a bulk add a *successful* download was reported as "no epub" and the EPUB
  silently discarded. Matching is now by story identity (host + numeric id).
- New-story downloads are **retried with backoff** on transient errors (429/503/timeout) like updates
  already were, instead of failing permanently on the first try.
- A genuine failure now carries **FanFicFare's own output** in the error (attributed per URL — a batch's
  failures are re-run alone), rather than the bare "produced no epub".
- The first download adopts the canonical story URL as `source_url`, so later updates find the library
  entry instead of re-downloading the story as a duplicate.
- The Tracked Stories tab now reports a duplicate/blank add instead of silently ignoring it.

### Added — ongoing vs completed stories are recorded
- New `book.story_status` (migration `e1f2a3b4c5d6`), filled from the downloaded EPUB (title page
  `Status:` / OPF subject). Shown on the Tracked Stories tab; a completed story stays tracked (keeps its
  slot, keeps delivering chapters) but is no longer re-fetched by the poller/sweep. Calibre is not written.

### Added — cap on extra-chapter requests (migration `d0e1f2a3b4c5`)
- Each channel honours at most `config.extra_per_channel_per_cycle` 🪝 requests (default **1**,
  Settings page; `0` disables) between two release cycles, so extras can't hook you into binging.
  The count is kept in `app_state` and reset at the start of every release cycle.
- An over-limit click gets a page naming the channel and the next release and changes nothing. The
  refusal happens *before* the feedback event is recorded, so the same drop can be 🪝'd again after the
  next release. The 🪝 link still appears in feed items (bodies stay byte-stable); it's enforced on click.
- 🪝 on a paused/dropped/completed source no longer injects a chapter, and the feed hides the link for
  those sources.

### Changed — additive weights, gentler penalties (migration `c9d0e1f2a3b4`)
- **Votes are additive.** 👍/👎 add/subtract `config.vote_step` (default 0.25) and 🪝 adds
  `config.extra_boost_step` (default 0.5) to `quota_weight`, which keeps its natural ~0–3 scale but is
  now hard-capped at **100.0**. This replaces the ×1.25 / ×0.8 / ×`extra_boost_multiplier` compounding.
  Existing per-source weights are untouched.
- **Below a weight floor a source fades, it isn't skipped.** Under `config.weight_skip_floor`
  (default 1.0) acceptance is scaled by `weight / floor`; weight 0 never posts. The ≥2-broadcast 👎
  blackout is gone (👎 now sits a source out one broadcast, on top of the weight drop). A 👎 that takes
  the weight to **0** auto-drops the source; `thumbs_down_drop_threshold` is retired. The admin weight
  box accepts 0–100 and never auto-drops.
- **Unread penalty is a transient ramp, not a flat 0.5×.** One unread drop costs nothing; consecutive
  unread drops scale acceptance to 0.8×, 0.6×, 0.4× … (floor `config.unacked_penalty_floor`, 0.2). It is
  derived from the drop rows, resets when any drop is read, and never touches `quota_weight`.
- Migration drops `config.thumbs_down_drop_threshold` and `config.extra_boost_multiplier`, adds
  `vote_step`, `extra_boost_step`, `weight_skip_floor`, `unacked_penalty_floor`.
- Settings page: the two retired fields are replaced by the four new ones.

### Fixed — asking for an extra chapter no longer penalises the source
- The 🪝-injected drop was created unacknowledged, so on the next cycle the read-gate saw an unread
  drop and halved the source's acceptance — "I asked for more and got skipped". It is now born
  acknowledged (asking for a chapter is engagement), which also resets the unread streak.

### Fixed — a lone cooling-down source could freeze its channel forever
- `run_release_cycle` skipped a channel with no active sources *before* ticking 👎 cooldowns, and a
  cooling-down source isn't "active" — so a channel whose only source got a 👎 never counted its
  cooldown down. The tick now runs at the start of the channel's turn, before the early exit.
- Because the tick moved ahead of selection, the 👎 cooldown (`2`) now means the source sits out
  **one** broadcast, not two (the reader's requested gentler back-off).

### Fixed — new stories no longer pile onto slot 1
- A tracked story added from the Tracked Stories tab (single or bulk) or imported from the Library
  had no slot until the next release cycle, so it was invisible on the dashboard and every add landed
  together. It is now placed immediately (`planner.assign_channel_slots`).
- Slot balancing now sums each slot's pinned **`quota_weight`** instead of counting works, so a slot
  holding a heavyweight serial is no longer treated as equal to one holding a dormant story.
  Backlog imports are still left `queued` for the release cycle to promote.

### Changed — feedback buttons are emoji-only
- The per-drop action row (🪝 · 👍 · 👎 · ⏸ · ❌ · ✓) no longer carries text labels in feed items or on
  `/read/{slug}`; each link keeps its old wording as `title=` + `aria-label=` so it stays discoverable
  on hover and accessible. Behaviour of every link is unchanged.

## [0.11.0] — 2026-07-08

### Changed — "drop cycle" renamed to **release cycle** (disambiguation)
- The scheduled cycle that emits `drop` rows is now called a **release cycle** everywhere the user
  sees it — the dashboard button (**▶ Run release cycle now**), the "Last/Next release cycle" status
  rows, `run_release_cycle()`, the `release_cycle` cron job, and the `POST /admin/release-now` route.
  This removes the long-standing ambiguity between "run a **drop** cycle" and ❌ "**drop** a source".
  The `drop` **row/table** and the ❌ drop **action** intentionally keep the word "drop".
- The persisted `app_state` key `last_drop_run_at` is renamed to `last_release_run_at` via migration
  `b8c9d0e1f2a3` (data is preserved — the dashboard keeps showing the last run).

### Changed — manual triggers run off the request path (fixes "unresponsive" buttons)
- **"Run release cycle now" and "Check feeds now" no longer block the HTTP request.** They enqueue a
  one-shot scheduler job and return immediately, so the tab is **safe to close or refresh** — the
  work runs in the scheduler thread, not the request. Previously the whole cycle (feed polling +
  chapterizing every EPUB) ran inline; on the ARM prod box that could exceed the reverse-proxy read
  timeout and appear to hang with no feedback.

### Added — release-cycle progress indicator + concurrency safeguard
- While a release cycle / feed check is running the dashboard shows a **live progress banner**
  (spinner + "running in the background; safe to close or refresh this tab") and **disables both
  trigger buttons**. The banner HTMX-polls `#dash-main` every 3s and clears itself when the run
  finishes.
- An **in-process lock** (`scheduler.cycle_status()`; the app runs single-worker) refuses a second
  run while one is in flight — the scheduled cron and a manual click share the same guard, so they
  can never overlap. A staleness guard (20 min) prevents a crash mid-cycle from wedging the buttons.

## [0.10.0] — 2026-07-08

### Added — fetcher runs as the library owner (no more root:root writes)
- The fetcher container is the sole library writer and ran as **root**, so every EPUB/folder
  `calibredb` created came out `root:root`. New `FETCHER_UID`/`FETCHER_GID` env (defaults `0:0` =
  old behaviour) make just the fetcher run as your Calibre library's owner; `HOME` is pointed at
  `/tmp` so `calibredb`'s config works under a non-root uid. Documented in `.env.example`/README.

### Added — `scripts/refresh_cursors.py` maintenance script
- After EPUBs are updated in Calibre *outside* the fetcher (e.g. a manual bulk transfer), this
  recomputes every source's `total_chapters` (the dashboard "max chapter") — caught-up or not —
  and clamps any `cursor_chapter_index`/`cursor_floor` left pointing past the EPUB end. In-range
  reading positions are untouched, so unread new chapters still drop. Dry-run by default;
  `--apply` writes. Run inside the `beacon` container.

### Fixed — XenForo threadmark ordering (stale chapters leaking to the feed)
- **XenForo serials (SB/SV/QQ) group threadmarks by category, not chronologically**, so FanFicFare
  builds the EPUB with Story/Sidestory/Apocrypha/…/Informational in a fixed order. A new Story
  chapter therefore lands *mid-EPUB* and shifts the trailing categories forward — and because the
  reading cursor is a positional, append-only index, the stale tail chapters (e.g. old
  Informationals) leaked to the feed instead of the genuinely-new chapter.
- **Fixed via fetcher config, no app change:** `fetcher/personal.ini.example` now ships a
  `[base_xenforoforum]` block that keeps only Story + Sidestory (`skip_threadmarks_categories`) and
  date-orders them (`order_threadmarks_by_date_categories`) so new chapters always append at the EPUB
  end. Documented in the README and CLAUDE.md. Existing XenForo tracked stories should be re-added
  from scratch so they import in the new order with a fresh cursor.

### Changed — drop selection is now a slot round-robin (feed diversity + weight throttles share)
- **The per-channel budget pass rotates through the channel's occupied slots** instead of draining
  the whole channel budget into whichever source had the most pending chapters. Each slot takes
  turns, so drops spread across the numbered slot feeds (diversity) rather than one slot (e.g. a
  large backlog book) flooding the feed while other slots stay quiet.
- **`quota_weight` now throttles a source's share *within* its slot** via a weighted random pick
  each turn — so down-voting a source genuinely shrinks its slice relative to its slot-mates
  (previously weight only nudged the probability at the budget margin, so a low-weight source with
  small chapters could still fill the whole budget).
- **An idle slot spills its budget to slots that still have content** — no wasted budget, but when
  only one slot has pending chapters it still fills up. Soft read-gating is unchanged in spirit but
  is now an *absolute* acceptance back-off (so even a lone unread source trickles slower), separate
  from the relative `quota_weight` share. No schema change.

### Fixed — URL-added tracked stories: default weight & title
- **A story added by URL on the Tracked Stories page now gets `config.tracked_default_weight`**
  (default 2.0), the same priority nudge library-imported serials already received. It was being
  created at the 1.0 backlog default, so URL-added serials competed with the archive instead of
  being prioritised over it.
- **Its URL placeholder title is replaced with the real Calibre title once the first fetch lands**
  (`scheduler._sync_title`). Previously a story added without a title stayed named after its URL
  forever, because `apply_result` never refreshed the title from Calibre.

### Added — correct chapter labels & cursor after mid-work chapter removal (stubs)
- **Stubs are now handled by per-chapter URL identity, not a single linear offset.** When a site
  removes chapters, the fetcher returns the ordered per-chapter canonical URLs (`chapterurl`) of the
  pre- and post-stub EPUBs; the app matches chapters by URL to build a piecewise `book.label_map`
  (JSON `[physical_index, offset]` breakpoints) so `absolute_chapter_number` stays exact past a gap
  **anywhere** — front, middle (labels jump, e.g. 4 → 73), or tail — and **composes** across
  repeated stubs on one book (a chapter that was label 100 stays 100). Migration adds
  `book.label_map`; the legacy scalar `chapter_label_offset` remains as the fallback.
- **The reader's cursor is remapped by identity instead of reset to "caught-up".**
  `cursor_chapter_index` (and `cursor_floor`) now move to the new physical index of the first
  surviving chapter at or after the reader's old position, so a reader who was behind resumes at the
  exact same chapter and keeps every surviving unread chapter. Previously any stub set the cursor to
  the new (shorter) length, silently skipping unread chapters after a non-tail removal.
- **Count-only fallback preserved:** if a body carries no per-chapter URLs (non-FanFicFare EPUBs),
  `apply_result` still bumps the scalar `chapter_label_offset` by `old − new` and sets cursor/floor
  to `new`, exactly as before.

### Fixed — WebSub realtime broke on large feeds (Inoreader dropped oversized pings)
- **The realtime push is now trimmed to a byte budget** (`config.websub_max_push_bytes`, default
  100 KB, on the Settings page; `0` disables). A slot feed carries up to `feed_item_limit`
  *full-content* chapters (~1.6 MB for 50 items), and **Inoreader silently discards oversized fat
  pings** — it returns `200` but never ingests, so realtime dies and the feed only updates on
  Inoreader's slow poll (the "dashboard/Inoreader both say Realtime, but chapters arrive hours
  late" symptom). The push now carries only the newest drops that fit the budget (always ≥1, never
  splitting a chapter). This means a pushed body is **no longer byte-identical** to a GET of the
  topic — that invariant wasn't buying realtime anyway; readers merge the pushed newest items into
  the polled feed by GUID (WebSub permits this). The polled `/feed/…` route is unchanged. Migration
  adds `config.websub_max_push_bytes`.

### Fixed — a story added by URL could strand at "pending" forever
- **Initial downloads now self-heal.** The first FanFicFare download of a URL-added tracked story
  was a one-shot job fired only at add time; if it was lost (restart/race) nothing retried it —
  `poll_all_feeds` only *seeds* a feed's first-sight GUID without downloading, so the story sat at
  `pending` with no `calibre_id` indefinitely. `poller.fetch_pending` now also runs as a backstop
  at the start of every drop cycle, re-submitting any tracked book still missing its EPUB (skipping
  ones already `fetching…`).

### Fixed — phantom "N chapters waiting" after a source shrinks
- **`total_chapters` is refreshed for every active source each broadcast**, not only when it's
  selected for a drop. A caught-up source is never selected, so if its EPUB later shrank (e.g. an
  author unpublished chapters) its stale `total_chapters` kept showing a phantom backlog on the
  dashboard. (The planner already used the live chapterizer, so it never actually dropped phantom
  chapters — this was a display bug.) The related mid-work-removal label problem is now fixed too —
  see the identity-based `label_map` entry above.

### Changed — Tracked Stories page pause is now the real pause
- **The "Tracked Stories" (`/admin/ongoing`) pause/resume now uses the same `paused` flag** as the
  feed ⏸ link and the dashboard, via the shared `pause_book`/`resume_book`. Previously it faked a
  pause by setting the source's status to `dropped` — a different, pre-1.1.B behaviour that was
  easy to confuse with actually dropping the source. Now all three pause surfaces are identical.
- **Internal cleanup:** the cascade-delete of a source (drops → feedback events → book) is a single
  `database.delete_book_cascade` helper shared by the admin "clear dropped" sweep and the tracked
  delete, and the channel/slot feed body is built once by `feed.builder.build_channel_slot_feed`.
  (The WebSub publisher later grew its own byte-budgeted variant — see the Unreleased WebSub fix —
  so a push is intentionally a subset of a GET now.)

### Added — per-channel feed length cap
- **Each channel now sets how many items its slot feeds carry** (`channel.feed_item_limit`,
  default 50, editable on the Channels page). Feeds serve the newest N items; **older drops stay
  in the DB** so their `/read/` permalinks keep working — they just fall off the tail of the feed.
  Previously this was a single global `BEACON_FEED_ITEM_LIMIT` applied to every feed; it now seeds
  the per-channel default. Migration adds `channel.feed_item_limit`.

### Added — soft read-gating (✓ Mark read)
- **A source you haven't caught up on now trickles more slowly.** If a source's most-recent
  delivered drop is still unacknowledged, its effective inclusion weight is halved in the
  stochastic budget pass, so an un-read backlog can't pile up as fast. It's a nudge, not a hard
  gate (the source still drops occasionally), and a source with no drops yet is never penalised.
- **A drop is acknowledged** on opening its `/read/{slug}` page, clicking **any** `/fb/` feedback
  link, or the new **✓ Mark read** action (a neutral, instant bare-GET acknowledgement with no
  weight effect). This is deliberately explicit rather than a tracking pixel: image proxies (e.g.
  Inoreader) prefetch images on feed poll and would mark everything read on ingest, and no-image
  readers would never fire — so passive detection is unreliable and reader-dependent. Migration
  adds `drop.acknowledged_at`.

### Changed — ongoing serials now outrank the backlog by default
- **Tracked (ongoing) sources import at `quota_weight = 2.0`** instead of `1.0`, so the user's
  actively-followed serials get priority over the finite Calibre backlog in the per-channel
  stochastic budget pass. The default is a **config-row knob** (`config.tracked_default_weight`,
  tunable on the admin Settings page). It's a default-weight nudge applied at import — weights
  remain per-source tunable and votable, and no planner logic is hardcoded to source kind.
  Migration adds `config.tracked_default_weight`.

### Changed — 👎 down now backs a source off for a couple of broadcasts
- **A thumbs-down sets a cooldown of ≥2 broadcasts** (`book.cooldown_remaining`) on top of the
  existing `×0.8` weight nudge, so a 👎 is felt immediately instead of only as a slow drift. The
  planner excludes any candidate with `cooldown_remaining > 0` and ticks it down once per
  broadcast, so a downed source sits out the next two cycles before it's eligible again. Reaching
  `thumbs_down_drop_threshold` still drops the source outright. Migration adds
  `book.cooldown_remaining`; requeue resets it.

### Added — ⏸ Pause a source
- **A new `⏸ Pause this source` feedback action** joins the per-drop row (🪝 extra · 👍 up ·
  👎 down · ⏸ pause · ❌ drop). Pausing removes a source from the rotation until it's resumed:
  it broadcasts nothing and is excluded from candidate selection and slot assignment. Like
  up/down it's an **instant bare-GET** (reversible, so no confirmation page), and it appears on
  the `/read/` page too via the shared feedback row.
- **A paused backlog book frees its slot** — it re-enters the queue and the next queued book
  streams into the freed slot; a **tracked** story just stops (keeping its sticky slot). **Resume
  is dashboard-only** (a paused source emits no feed items to carry a resume link): the Active and
  Queue tables gain a `⏸ pause` / `▶ resume` toggle and a `⏸ paused` badge. Migration adds
  `book.paused`.

### Changed — out-of-range slot feeds now 404
- **`GET /feed/{channel}/{slot}` rejects a slot outside `1..parallel_slots`** (or non-numeric)
  with 404 instead of serving an empty feed. E.g. with a 3-slot Fantasy channel, `/feed/fantasy/4`
  is now an error. The token is still checked first, so an invalid token stays a 403.

### Added — feedback actions on the /read/ reader page
- **The `/read/{slug}` page now shows the same feedback row as the feed items** (🪝 extra · 👍 up ·
  👎 down · ❌ drop). Previously the reader page dead-ended with no way to vote. The row is a
  single shared builder (`app/feed/builder.py:feedback_block`), so the feed and reader page always
  stay in lock-step (and both pick up new actions like ⏸ pause automatically).

### Changed — oversized chapters now pace by budget accumulation
- **A chapter larger than a channel's per-cycle budget no longer drops every cycle.** Previously
  the planner force-posted any unit bigger than the budget as the source's first unit (`p=1.0`),
  so a 6–9k-word non-fiction chapter on a 3k budget fired *every* broadcast and buried the reader.
  Now such an oversized unit **accumulates budget credit across cycles and posts whole once the
  effective budget (base + saved-up credit) can afford it** — a 9k chapter on a 3k budget posts
  once every ~3 cycles, so the long-run rate tracks the budget. Units are still never split, and
  the positive credit cap rises to the largest pending unit so an oversized chapter can be saved
  up for (falling back to one base budget when nothing oversized is pending, so idle channels
  don't run away). See `app/planner/planner.py` (`_plan_drops` accumulation pass).

### Fixed — tracked stories stuck at `fetching…` forever (scheduler timezone bug)
- **Fetch poll jobs were orphaning themselves, so completed fetches were never collected.** The
  scheduler is configured with `BEACON_TZ`, but a container's OS clock is typically UTC.
  `_schedule_poll`/`trigger_fetch_pending` computed their run time from a **naive**
  `datetime.now()`, which APScheduler localised to `BEACON_TZ` — so on a UTC-clock box with a
  non-UTC `BEACON_TZ` the run time landed *hours in the past*. An interval poll therefore fired
  **immediately**, before the drop cycle/sweep that submitted it had committed the `fetch_job`
  mapping; the poll's fresh session saw no mapping, assumed "already handled", and unscheduled
  itself for good. The book was then stranded at `fetching…` forever, its `fetch_job:` app_state
  key never cleaned up, and the fetcher's finished result never folded in — while the daily sweep
  re-submitted the same stories every cycle. The same bug dropped the initial-download date job
  (`_run_fetch_pending`) as an instant misfire, so **newly-added tracked stories never fetched**.
  Now all scheduler run times use a timezone-aware `now` (`app/scheduler.py:_now`), the poll
  tolerates a few "mapping not found yet" ticks before giving up, and the container clock is
  aligned to `BEACON_TZ` via `TZ` in `docker-compose.yml`. Orphaned jobs self-heal on restart
  (`_resume_pending_polls` re-schedules them and they now poll correctly). This is distinct from
  the 0.8.0 fetcher-side subprocess-timeout fix below.

## [0.8.0] — 2026-06-29

### Changed — schema is now Alembic-migration-owned
- **Database schema moved from `create_all` to Alembic migrations.** `init_db()` runs
  `alembic upgrade head` on startup instead of `Base.metadata.create_all`. An existing
  `create_all`-built database (no `alembic_version` table) is auto-detected, **stamped at the
  baseline revision, then upgraded** — so deployed volumes are no longer recreated on
  schema-changing upgrades. The Docker image now ships `alembic.ini` + `alembic/`. Going forward,
  **every schema change is a migration** (`alembic revision --autogenerate`). Tests still build the
  schema via `create_all` from the models.

### Fixed — fetcher could wedge permanently on a hung site
- **All FanFicFare/`calibredb` subprocesses now run under a wall-clock `timeout=`**
  (`FETCHER_FANFICFARE_TIMEOUT`, default 1200s; `FETCHER_CALIBREDB_TIMEOUT`, default 600s).
  Previously a single hung site socket blocked the lone `ThreadPoolExecutor(max_workers=1)` worker
  forever, stalling that fetch *and every job queued behind it* at `fetching…` indefinitely. On
  timeout the child is killed and reported as a transient error, so the worker always frees.

### Added — drag sources between slots
- **The dashboard's "Now broadcasting" slot cards are drag-and-drop.** Drag a source pill onto
  another slot (within the same channel) to repin it via `POST /admin/books/{id}/set-slot`. Backlog
  books stay one-per-slot (moving onto an occupied slot **swaps** the two); tracked stories are
  uncapped and just move. The pin is sticky, so it survives subsequent broadcasts.

### Changed — footnotes expand inline instead of jumping
- **Cross-file notes now render as a collapsible `<details>` disclosure in place** of the marker,
  replacing the old end-of-chapter `<aside>` + `#fb-note-{id}` anchor. Clicking the marker expands
  the note where it's cited — no scroll-to-bottom, and crucially no relative anchor that a feed
  reader (e.g. Inoreader) would resolve against the item permalink and navigate away to. The same
  HTML is served to the feed and the reader page; works in any reader.

### Added — library filters
- **The Library page can filter by Read, Status, and Source website** (in addition to the
  existing title/author search). The filters compose with each other and the text search; the
  source list is derived from each book's `url:` identifier host. All client-side and instant.

### Fixed — admin UI
- **Chapter-progress bar now fills.** Its `.progress-bar-fill` is a `<span>` (inline), so the
  computed `width:%` was ignored and the bar always looked empty; `display:block` fixes it.
- **Channel budget converts losslessly between Words and Minutes.** Toggling the mode now
  multiplies/divides by WPM *without rounding* (the value round-trips exactly), the budget is
  stored as a float, and the New-channel form's mode select also converts (it was missing the
  hook). Budget inputs accept `step="any"`.

### Added — configurable 🪝 extra boost
- The 🪝 *extra* (super-up) weight boost is now **admin-configurable**
  (`config.extra_boost_multiplier`, set on the Settings page) with a **gentler default of 1.5×**
  (was a hard-coded `1.25**3 ≈ 1.95×`).

## [0.7.0] — 2026-06-29

### Added — in-EPUB images render in feeds
- **EPUB images are now served and their URLs mapped.** Chapter HTML carries relative
  `<img src="images/…">` paths that live *inside* the EPUB zip; nothing served those bytes, so
  readers resolved them against the beacon origin and 404'd (e.g. `GET /images/00009.jpeg`). The
  chapterizer now rewrites every in-EPUB `<img>`/SVG `<image>`/`srcset` reference (resolving it
  against the chapter's OPF-relative directory) to a sentinel, and `materialize_image_urls()` swaps
  the sentinel for `{base_url}/img/{calibre_id}/{path}` when a drop's `content_html` is built — so
  stored content stays self-contained and byte-stable (WebSub-safe). A new read-only route
  `GET /img/{calibre_id}/{path}` (`app/routers/media.py`) streams the matching entry straight out of
  the EPUB zip (re-anchored to the EPUB's OPF directory), never writing the library. External
  (`http(s)://`, `data:`, root-absolute) references are left untouched; path traversal is rejected.

### Added — endnotes/footnotes inlined into chapter drops
- **Cross-file notes now travel with the chapter that cites them.** End/footnotes usually live in
  a back-matter file separate from the chapter, so a single-chapter drop left every note marker
  dangling (its `href` pointed at an undropped file). The chapterizer now builds a book-wide note
  index and appends the notes a chapter cites as a styled end-of-chapter `<aside class="beacon-
  endnotes">`, rewriting each marker to a local `#fb-note-{id}` anchor (ids are book-unique, so
  notes never collide when several chapters share one drop). Two conventions are detected:
  - **EPUB3 semantic** — `epub:type="noteref"` → `rearnote`/`footnote`/`endnote` (e.g.
    *More Everything Forever*).
  - **Plain/older** — a superscript anchor `<a href="…#id"><sup>…</sup></a>` with no note
    semantics (e.g. Harari's *Homo Deus*). The `<sup>` wrapper is the discriminator, so bare-text
    Part/chapter cross-links in nav-heavy books are *not* misread as notes.
  Only **cross-file** notes are inlined; **same-file** footnotes (e.g. Tim Urban's *What's Our
  Problem*) are already self-contained and left untouched. The note's own number/back-link anchors
  are unwrapped (dangling links removed, authored number kept) and any in-EPUB note images go
  through the same image route.

### Fixed — WebSub validation
- **Tokened `rel=self`/topic.** The advertised WebSub topic was token-free, but the feed route
  gates on `?token=`, so a subscriber fetching the topic URL got a 422 — failing WebSub content
  distribution ("notification body did not match the contents of the topic URL"). The advertised
  `rel=self` and the publisher's push topic now carry the token, so the topic is fetchable and
  byte-identical to the pushed body. Push still matches subscriptions registered with *or* without
  the `?token=`.
- **`HEAD` on slot feeds.** The `/feed/{slug}/{key}` route answered `HEAD` with `405`; WebSub
  validators and some proxies probe with `HEAD` first. It now allows `GET`+`HEAD` (Starlette
  strips the body), returning `200`.
- **Subscription verification now actually completes for Inoreader.** The hub's intent-verification
  handshake had three independent defects that each made a real subscribe silently fail (the dashboard
  stayed empty despite `202`s). All three are fixed:
  - **Honour `hub.verify` (PuSH 0.3 sync vs. WebSub async).** Inoreader/Superfeedr request `sync`
    verification: they arm their verification callback only *while* their subscribe request is open.
    We always verified out-of-band (after responding), so the callback arrived too late and returned a
    bare `200` with an empty body (no challenge echo). The hub now reads `hub.verify` and verifies
    **inline** for `sync` subscribers (callback during the open request → `204`), keeping the
    immediate-`202` + background path (with `_VERIFY_DELAYS` backoff retries) for `async`.
  - **Echo `hub.verify_token`.** The verification GET dropped the subscriber-supplied
    `hub.verify_token`; PuSH 0.3 subscribers match a pending subscription on *both* `hub.topic` and
    `hub.verify_token` before echoing the challenge. It's now forwarded.
  - **Preserve the callback's own query params (the real Inoreader blocker).** The verification GET
    passed `params=` to `httpx.get(callback, …)`, but httpx (≥0.28) *replaces* a URL's existing query
    rather than merging — wiping the `?feed_id=…&hub_id=…` Inoreader puts in its callback to key the
    pending verification. With those gone, Inoreader couldn't match the request and answered a bare
    empty `200` (no challenge echo) — identically for sync, async, and with/without `verify_token`,
    which is why earlier handshake fixes didn't land. The hub now **merges** `hub.*` into the
    callback URL (`httpx.URL(callback).copy_merge_params(...)`), keeping the subscriber's params.
  - **Diagnosability.** Failed verification logs the subscriber's response (status + body snippet) at
    `WARNING`, and the full inbound hub form (key + value) is debug-logged, so a dropped/required
    param can't hide again.

### Added
- **`BEACON_LOG_LEVEL`** (default `INFO`). Set to `DEBUG` to trace the full WebSub
  subscribe → verify → store → push flow (and other app logs); configured at startup.

## [0.6.1] — 2026-06-28

### Added
- **Editable channel slug.** The Channels edit form now exposes the slug (defaults stable on a
  plain rename). Changing it rewrites that channel's `/feed/{slug}/{key}` URLs — re-subscribe in
  your reader afterwards. Uniqueness is enforced (suffixing `-2`, `-3`, …).

### Changed — actions happen in place (HTMX), no full-page reload/jump
- **Auto-save fields.** The per-book **cursor** and **weight** inputs save automatically ~0.5s
  after a change (debounced) with a brief green flash, instead of needing an "apply" (↩) button.
  They reply `204 No Content` to HTMX so focus and scroll are preserved.
- **In-place dashboard actions.** Drop, move, re-queue, track on/off, ⏮/⏭ cursor jumps,
  move-to-channel, run-drop/poll-now, batch drop, clear-dropped post via HTMX and swap only the
  dashboard body — the page no longer reloads and scrolls to the top.
- **In-place Tracked Stories actions.** The per-row pause/resume, fetch-now, delete and the batch
  fetch/pause/resume/delete actions now swap the list in place too (batch endpoints tolerate an
  empty selection). Delete uses an HTMX confirm.
- **Section state is remembered.** Expanding/collapsing a dashboard section persists (localStorage)
  across the in-place swaps and page loads, instead of resetting to defaults after every action.
- All forms keep their `method`/`action`, so everything degrades gracefully without JavaScript.

### Changed — Library import driven by Calibre `#status` / `#read`
- **One smart "Add" button.** The Library page's two inconsistent actions (batch "Add to queue"
  + a per-row "📡 Track updates" button) are replaced by a single batch **Add selected** that
  routes each book by its Calibre **`#status`** custom column: ongoing serials (In-Progress /
  Incomplete / Hiatus) become **tracked** auto-updating sources; everything else (Completed /
  Abandoned / Published / blank) joins the **backlog** queue. New `Status` / `Read` columns show
  each book's verdict. Endpoint: `POST /admin/library/add` (replaces `/admin/import` +
  `/admin/library/track`). See `app/calibre/status.py`.
- **Cursor fix — caught-up serials start at the end.** A tracked book marked **`#read=Yes`**
  starts its cursor at the current EPUB end so only *new* chapters drop; previously a freshly
  tracked story replayed from chapter 1. Unread (`#read` unset) tracked books still start at
  chapter 1 and auto-update.
- **Skip done stories on fetch.** The feedless sweep and feed poller now skip tracked stories
  whose `#status` is Completed / Abandoned / Published (read live from `metadata.db`), so the
  fetcher isn't run on finished works; their already-downloaded chapters still drop.
- **Tracked Stories page.** The "Last fetch" cell now shows the `last_fetch_at` timestamp, and
  batch **Fetch / Pause / Resume / Delete** actions act on the selected rows.
- The Calibre adapter now reads the `#status` and `#read` custom columns
  (`CalibreBook.source_status` / `CalibreBook.read`, plus `CalibreAdapter.status_map`).
- **Switch handling per book.** Each dashboard row gains a **📡 on/off** toggle (ongoing
  auto-update vs finite backlog — untracking re-queues an active book) and **⏮ / ⏭** cursor
  jumps (read from start / jump to latest). The chapter count is computed on demand, so the
  switch works immediately after adding — before any drop cycle has populated `total_chapters`.

### Fixed
- **`#status` was never read** (so import routing fell back to backlog for everything): the
  adapter chose the storage layout by `is_multiple`, but Calibre stores single-value
  **enumeration** columns like `#status` in the *normalized* link table. It now branches on
  `normalized`, matching how genre / enumeration / bool columns are actually stored.

## [0.5.0] — 2026-06-25

### Changed — RSS is now a trigger, not a content source (major redesign)
- **Ongoing serials become real Calibre books.** Web serials only syndicate a *preview* in
  RSS, not full chapter text — the old buffer-the-feed-body model was structurally broken.
  Now RSS is used *only* to notice that a story updated; a separate **fetcher container**
  runs FanFicFare + `calibredb` to download the new chapters into the Calibre library, and
  Fic-Beacon serves them as a normal EPUB through the existing chapterizer/cursor path.
- **Unified source model.** The `epub`/`ongoing` `BookKind` split is gone. Every source is a
  library EPUB; a `tracked` flag (with an optional `feed_url` for fast RSS notification) marks
  the ones that auto-update. One code path through the planner, feed builder, and cursor logic.
- **Calibre is read-only *from Fic-Beacon*.** The app's library mount is `:ro`; only the
  isolated fetcher container writes. Coexists with an external calibre-web on the same library.
- **Fetch scheduling — batched & async.** Feeds are polled **pre-drop** (the hourly poll job is
  gone). Because a FanFicFare run can take ~15 min, fetches are **asynchronous**: `POST /fetch
  {urls}` returns a `job_id` immediately and the app polls `GET /fetch/{job_id}`; the triggering
  broadcast never waits, so freshly fetched chapters land in the **next** cycle. Changed feeds are
  submitted in **one batch** (new stories share a single warm `fanficfare -i` pass); the fetcher
  runs them in a single-worker pool (serialized `calibredb` writes) with **force-detection** and a
  **3-try exponential backoff** borrowed (trimmed) from AutomatedFanfic. Tracked stories without a
  feed (auth-gated) are refreshed by a **daily sweep**. The dashboard shows an *in-progress* panel
  (per-story phase + elapsed); the job→book map is persisted so a restart resumes polling.
- **Stub handling.** When the site removes old chapters (FanFicFare: "Existing epub contains N
  chapters, web site only has M"), the fetcher archives the old EPUB as a separate Calibre
  entry and overwrites the book; Fic-Beacon keeps chapter labels continuous via a new
  `chapter_label_offset` and forbids rewinding into the rewritten body via `cursor_floor`.
- **Admin UI.** "Ongoing Serials" → **Tracked Stories**: add by story URL (single or a paste
  of URLs, one per line), per-source last-fetch status and "fetch now". The Library page gains
  a **"📡 Track updates"** action. OPML file upload removed.

### Removed
- The `ongoing_entry` table and all RSS-body buffering: entry content extraction, chapter-number
  regex, `seed_source_as_read`, OPML parsing (`app/ongoing/opml.py`), the hourly poll job, and
  the `BookKind` / `linked_calibre_id` / `chapter_num` fields.

### Migration
- Schema-changing upgrade — **recreate the app DB volume** (no migration path). Re-add tracked
  stories by URL. Stand up the new `fetcher` container (see `docker-compose.yml`, `./fetcher`).

## [0.4.0] — 2026-06-25

### Added
- **Dashboard observability** — collapsible sections on `/admin`:
  - *Now broadcasting* — per channel, what each numbered slot feed currently carries
    (the streaming EPUB + pinned ongoings) and the most recent drops in that feed
    (post + chapter level).
  - *Next broadcast* — queued EPUBs (waiting for a free slot), ongoings holding buffered
    chapters, and the **held-out log** from the last broadcast (sources whose next unit
    lost the stochastic budget roll and rolled over).
  - *System status* — when the drop and poll crons last ran and when they next fire,
    plus the list of WebSub subscribers (verified / unverified / expired).
- **Per-broadcast skip log** — the planner records which sources had units roll over
  (held out entirely vs. partly deferred); persisted to `app_state` and shown on the dashboard.
- **Regenerate feed secret** — a Settings-page button rotates `config.feed_secret` (every
  feed URL's `?token=` changes) and clears stale WebSub subscriptions, for a hard reader-cache
  reset. Per-drop feedback links are unaffected.
- **Cron run tracking** — `app_state` key/value table records `last_drop_run_at` /
  `last_poll_run_at` (a new table, so `create_all` adds it without recreating the volume).

### Fixed
- **WebSub realtime push for tokened feeds** — subscribers that register a topic with the
  `?token=…` query string (the URL pasted into the reader) are now matched on push; the
  publisher previously only matched the token-free `rel=self` URL, so pushes were silently
  dropped and feeds only updated on the reader's slow poll. This is the likely cause of new
  chapters not appearing promptly in InoReader.

### Changed
- **Feeds are always polled right before a broadcast** — both the scheduled drop cycle and the
  manual "Run drop cycle" trigger poll every ongoing feed first, so a broadcast releases the
  freshest chapters instead of waiting for the next hourly poll. (The hourly poll still runs.)
- **Single source of truth for the version** — `app/version.py` reads `[project].version`
  from `pyproject.toml` at runtime (copied into the image). Removed the baked-in
  `APP_VERSION`/`VERSION` build-arg/`git describe` plumbing (Dockerfile, docker-compose,
  rebuild.sh) and fixed the doubled-`v` (`vv0.2.0-…`) in the UI.

## [0.3.0] — 2026-06-24

Major redesign turning Fic-Beacon into a single weighted queue for the completed backlog **and**
the user's real ongoing serials. Landing incrementally:

### Added
- **Channels & per-slot feeds** — group sources by Calibre tag prefix; each channel has its own
  budget and parallel slots; one feed per numbered slot (`/feed/{channel}/{slot}`), occupied by
  both EPUB backlog and ongoing serials. No all-channels union feed.
- **Ongoing serial syndication** — register a serial's RSS feed into a channel; new chapters are
  buffered hourly and released, batched, at drop time, weighted against the backlog. Votable and
  droppable like any source.
- **Stochastic per-channel budgeting** — whole units (chapters/entries) are included
  probabilistically as the cycle runs over budget; weight/votes bias the draw; a `budget_credit`
  carry-over makes the long-run mean track the budget. Units are never split.
- **WebSub push** — self-hosted hub; feeds declare `rel=hub`; realtime push to subscribers
  (works on InoReader's free plan).
- **Feedback redesign** — four ordered actions per drop: 🪝 extra (super-up) · 👍 up · 👎 down ·
  ❌ drop (super-down). `up`/`down` are instant bare-GET and idempotent per `(drop, action)`;
  `extra`/`drop` use a one-tap confirm page. `extra` appears only when a next unit exists.
- **Clear dropped** queue (manual button) and optional `dropped_retention_days` auto-purge.
- **`BEACON_TZ`** setting so drop/poll schedules use a configured timezone (previously UTC).
- Calibre adapter now reads **tags** (used for channel matching).

### Upgrade note
- This release changes the database schema. The app builds the schema with
  `create_all`; **recreate the SQLite volume** when upgrading from 0.1.0 (set
  `BEACON_FEED_SECRET` first so your feed URLs don't change), then re-import books and
  recreate channels. (No in-place Alembic migration is shipped for this jump.)

### Changed
- **Slots are feed buckets, not single-book reservations** — each numbered slot's feed now carries
  the one EPUB streaming in that slot **plus** the ongoings pinned to it, interleaved. EPUBs are
  capped at `parallel_slots` active per channel (one per slot; extras stay queued); **ongoings are
  uncapped and never queued**, load-balanced (sticky) across slots by fewest pinned works, tie-break
  fewest chapters ever dropped there. Fixes the bug where N ongoings consumed all slots and starved
  the EPUB backlog (and where ongoings were spread one-per-slot past `parallel_slots`). Content
  selection is unchanged and slot-agnostic; only slot *assignment* changed (`_assign_slots`).
- **Genre-based channel routing** — channels match on the Calibre custom column
  `#genre_manual` (hierarchical, e.g. `Fantasy.Rational`) via a per-channel `genre_match`
  prefix (replaces the old `tag_match`). On import, books auto-route to the first matching
  channel; when `#genre_manual` is blank, a genre is derived by keyword-grepping the raw
  `#genre` column into Fanfiction / Sci-Fi / Fantasy / Classical / Non-fiction (popular
  science "Sci-pop" classifies as Non-fiction, not Sci-Fi). Unmatched → General. Choosing a
  channel explicitly on the import page still forces all selected books there.
- **Every source now belongs to a channel** (`book.channel_id` is NOT NULL). The implicit
  default/global group is gone; budget and parallel slots live only on channels. A **"General"**
  channel is auto-created on first run so imports always have a home.
- **Move books between channels** (without dropping) and **rename channels** (slug and feed URLs
  stay stable) from the admin dashboard / channels page.
- Deleting a channel now **reassigns its sources** to another channel instead of orphaning them;
  the last remaining channel can't be deleted.
- Documentation (`CLAUDE.md`, `Architecture.md`) rewritten for the channels / ongoing /
  stochastic / WebSub design; added `README.md` and this changelog.

### Removed
- **All-channels union feed `GET /feed`** — subscribe to each channel/slot feed instead.
- **Global budget settings** — `config.global_budget_words`, `global_budget_minutes`,
  `budget_mode`, `parallel_slots`, and the default-group `budget_credit`. Budget/slots/mode are
  per-channel; `config` keeps only the true globals (`wpm`, `cadence_cron`,
  `thumbs_down_drop_threshold`, `feed_secret`).
- **Superseded v2 "ongoing balancing"** — the `ongoing_feed` table, `target_total_words` config,
  and the budget-subtraction-by-word-count logic. Ongoings are now syndicated as in-budget
  sources instead of merely subtracted.
- **`overshoot_tolerance` config** — a leftover of the old round-robin planner. The stochastic
  budget handles overshoot via the signed `budget_credit` carry-over, so the knob no longer did
  anything; dropped from the `config` table and the admin config form.

## [0.2.0] — 2026-06-23

(Slot & ongoing-syndication iteration — see git log for details.)

## [0.1.0] — 2026-06-21

### Added
- Initial Fic-Beacon: Calibre adapter (RO `metadata.db`), EPUB chapterizer, global round-robin
  drop planner (never split a chapter; overshoot tolerance), `feedgen` RSS 2.0 + Atom feed,
  per-drop tokenized feedback links (up/down/extra via a confirm page), self-hosted reader pages,
  Jinja + HTMX admin UI, APScheduler drop cycle, SQLite app state.
- Source-aware per-chapter permalinks (FanFicFare `chapterurl`); per-drop GUIDs.
- Docker / docker-compose with read-only Calibre mount; switched to the `uv` package manager;
  configurable Calibre library path.
- v2-designed (not built) ongoing-feed balancing scaffolding (later superseded — see Unreleased).

[Unreleased]: https://github.com/Similacrest/fic-beacon/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Similacrest/fic-beacon/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Similacrest/fic-beacon/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Similacrest/fic-beacon/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Similacrest/fic-beacon/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Similacrest/fic-beacon/releases/tag/v0.1.0
