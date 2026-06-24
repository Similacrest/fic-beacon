# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Similacrest/fic-beacon/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Similacrest/fic-beacon/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Similacrest/fic-beacon/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Similacrest/fic-beacon/releases/tag/v0.1.0
[0.1.0]: https://github.com/Similacrest/fic-beacon/releases/tag/v0.1.0
