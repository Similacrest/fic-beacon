# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
