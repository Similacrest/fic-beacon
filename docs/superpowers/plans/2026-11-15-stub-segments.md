# Stub Segments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stubbed story is one book across several Calibre EPUBs (archives + live): one cursor, one chapter list, author labels everywhere, and a segment-coloured progress bar.

**Architecture:** A `book_segment` table lists a book's archive EPUBs in order; the live EPUB stays `book.calibre_id`. A new pure module builds a *unified chapter list* (archive chapters, then live chapters whose URL is new, overlaps resolved by a length rule). The planner, stub handling, admin cursor controls and story page read that list instead of one EPUB. Because an archive is the pre-stub EPUB, unified positions of archive chapters equal the old physical indices, so the cursor and `label_map` stay valid and a stub no longer remaps anything.

**Tech Stack:** Python, FastAPI, SQLAlchemy + Alembic, Jinja/HTMX, pytest (`uv run pytest`), fetcher in `fetcher/app.py`.

**Spec:** `docs/superpowers/specs/2026-11-15-stub-segments-design.md` (mockup: `~/outbox/mockup-stub-segments-progress.html`)

## Global Constraints

- Calibre is read-only from the app; only the fetcher writes (CLAUDE.md).
- Schema changes only via Alembic (`alembic revision --autogenerate`, reviewed); never `create_all` in prod.
- Never split a unit; `total_chapters` is beacon's own chapterizer count — now of the **unified** list.
- UI says "release" for the act; "drop" only for cancelling a source.
- Feed/permalink/GUID bytes unchanged; stored `drop.content_html` is never rewritten; `/img/{calibre_id}/…` already takes any EPUB's id.
- Segment length rule: live copy of a chapter present in both EPUBs is used unless its word count is `< ratio ×` or `> (1/ratio) ×` the archive's; `ratio = settings.segment_length_ratio`, default `0.5`, env `BEACON_SEGMENT_LENGTH_RATIO`.
- Author label of unified position `p` = `p + label_offset_at(book, p) + 1` (today's `absolute_chapter_number`, applied to unified positions). The progress-bar boundary numbers and everything user-visible use it.
- A non-stubbed story (no segments) behaves byte-for-byte as today and shows no segment UI.
- Never start subagents on `fable`; reviewers use `opus`. Stop Gradle/daemons n/a. Bundle/tag/push only on the user's request.

## Review Focus

- A live EPUB with no `chapterurl`s (non-FanFicFare): unified list must degrade to "archive then live appended whole" without crashing or duplicating (test in Task 2).
- Two stubs in a row: positions of archive 1 + archive 2 chapters equal the earlier unified list, cursor untouched (Task 4).
- An archive EPUB deleted from Calibre / unreadable: story holds at the first unreadable chapter, positions don't shift, ⚠ note set, other stories unaffected (Task 3).
- The same chapter URL appearing twice inside one EPUB (FanFicFare quirk): first occurrence wins, no duplicate unified rows (Task 2).
- Rewind into an archive chapter whose images live in the archive zip: drop HTML must point `/img/` at the *archive's* calibre id (Task 3).

## File Structure

- Create `app/epub/unified.py` — pure unified-list builder + IO loader/cache. One responsibility: "which chapters does this book have, from which EPUB".
- Create `app/segments_view.py` — pure view-model for the segment bar (spans, boundary labels) used by the story template.
- Create `app/segments_backfill.py` — one-time detection/migration of already-stubbed books.
- Modify `app/models.py` (`BookSegment`), `app/config.py`, `app/planner/planner.py`, `app/fetch/client.py`, `app/routers/admin.py`, `app/templates/admin/story.html`, `app/database.py` (cascade delete + startup hook), `app/calibre/adapter.py` (`archive_ids`), `fetcher/app.py`, docs.
- Tests: `tests/test_unified.py`, `tests/test_segments_apply.py`, `tests/test_segments_view.py`, `tests/test_segments_backfill.py`, additions to `tests/test_fetcher.py`.

---

### Task 1: Fetcher reports the archive id and refuses to shrink the live entry

**Files:**
- Modify: `fetcher/app.py` (`_archive_copy`, `_update_one`)
- Test: `tests/test_fetcher.py`

**Interfaces:**
- Produces: `_archive_copy(epub: Path, url: str) -> int | None` (id of the archive entry, existing or new; None if it could not be added); stub result dict gains `"archive_calibre_id": int | None`.

- [ ] **Step 1: Failing tests** in `tests/test_fetcher.py` (reuse `_stub_env`): `test_stub_reports_the_archive_calibre_id` — after `_update_one` on a stub, `result["stub"]["archive_calibre_id"] == fake.next_id` (the id `add -d` returned); `test_stub_retry_reports_the_existing_archive_id` — with `fake.entries[4657]={"url-archive": RR}` and its bytes in `fake.files`, `archive_calibre_id == 4657` and no `add` call; `test_plain_update_that_loses_chapter_urls_fails` — `-u` run rewrites the exported EPUB with fewer chapters and prints no stub message; assert `"fewer chapters" in result["error"]` and no `add_format`.
- [ ] **Step 2: Run** `uv run pytest tests/test_fetcher.py -q` — the three fail.
- [ ] **Step 3: Implement.** `_archive_copy` returns the id (`_archive_exists` becomes `_find_archive(epub, url) -> int | None`). In `_update_one` put the id in `stub["archive_calibre_id"]`. After a non-stub update, if `_real_count(epub) < _real_count_before` (count taken from the exported EPUB before `_fanficfare_update`), `_fail(entry, "update would shrink the book (N → M chapters, no stub reported): …")` and skip `add_format`.
- [ ] **Step 4: Run** the file again; expect all pass, then `uv run pytest -q` green.
- [ ] **Step 5: Commit** `feat(fetcher): report the stub archive's calibre id; refuse silent shrink`.

### Task 2: `BookSegment` model, migration, setting

**Files:**
- Modify: `app/models.py`, `app/config.py`, `app/database.py:16` (cascade delete)
- Create: Alembic revision `alembic/versions/<ts>_book_segments.py`
- Test: `tests/test_unified.py` (model part)

**Interfaces:**
- Produces: `BookSegment(id, book_id FK→book.id NOT NULL index, ordinal int NOT NULL, calibre_id int NOT NULL unique, created_at)`; `Book.segments` relationship ordered by `ordinal`, cascade delete-orphan; `settings.segment_length_ratio: float = 0.5`.

- [ ] **Step 1: Failing test** `test_book_segments_order_and_cascade`: create a book with two segments (ordinals 1, 0 inserted out of order) → `book.segments` returns ordinal order; `delete_book_cascade` removes the rows.
- [ ] **Step 2: Run** → fails (no model).
- [ ] **Step 3: Implement** model + relationship, setting, cascade in `delete_book_cascade`; generate the migration with `uv run alembic revision --autogenerate -m "book segments"`, review it contains only `book_segment`.
- [ ] **Step 4: Run** `uv run pytest tests/test_unified.py tests -q`; also run the app's migration test if present (`grep -l alembic tests`) — expect green.
- [ ] **Step 5: Commit** `feat: book_segment table for stubbed stories`.

### Task 3: Unified chapter list

**Files:**
- Create: `app/epub/unified.py`
- Test: `tests/test_unified.py`

**Interfaces:**
- Consumes: `app.epub.chapterizer.Chapter`, `chapterize`, `Book.segments`, `CalibreAdapter.get_book/epub_path`.
- Produces:
  - `@dataclass UChapter(position: int, title: str, html: str, word_count: int, source_url: str | None, calibre_id: int, physical_index: int, segment: int, short_live: bool)` — `segment` is the index into the ordered segment list (archives…, live last); `short_live` true when a live copy exists but the archive copy was chosen by the length rule.
  - `build_unified(segments: list[tuple[int, list[Chapter]]], ratio: float) -> list[UChapter]` — pure; `segments` ordered archives first, live last, each `(calibre_id, chapters)`.
  - `load_unified(book: Book, adapter: CalibreAdapter) -> UnifiedResult` with `UnifiedResult(chapters: list[UChapter], unreadable: list[int])`; reads `book.segments` + `book.calibre_id`, chapterizes each (mtime-cached already), keeps a small LRU keyed by the tuple of `(calibre_id, mtime)` of all segments; a missing/unreadable archive id goes in `unreadable` and `chapters` is truncated **at the first unreadable segment's position** is NOT allowed — instead return the chapters of readable segments and positions of the unreadable segment's chapters are unknowable, so `load_unified` returns `chapters` only up to that segment and `unreadable=[ids]` (callers hold the story).
  - `chapter_label(book: Book, position: int) -> int` = `absolute_chapter_number(book, position)` (thin alias so views import one name).

- [ ] **Step 1: Failing tests** (synthetic `Chapter` objects, `src(n)` helper giving URLs):
  - `test_single_segment_is_identity` — one live segment → positions 0..n-1, `segment==0`, same html.
  - `test_archive_then_new_live_chapters_appended` — archive urls a1..a5, live urls a1..a3,b1,b2 → 7 chapters, positions 5,6 are `b1,b2` from the live id.
  - `test_overlap_uses_live_when_lengths_match` / `test_overlap_uses_archive_when_live_is_short` — live copy of a2 with 10 words vs archive 1000 (ratio 0.5) → archive html, `short_live=True`; 900 vs 1000 → live html.
  - `test_duplicate_url_inside_one_epub_counts_once`.
  - `test_epub_without_chapter_urls_appends_live_whole` — archive 3 chapters no URLs, live 2 chapters no URLs → 5 chapters, no crash.
  - `test_second_stub_keeps_earlier_positions` — segments `[arch1(1..5), arch2(1..3,b1), live(1..2,b1,c1)]` → positions 0–4 arch1, 5 is `b1`, 6 is `c1`.
  - `test_unreadable_archive_is_reported_and_stops_the_list` (loader, fake adapter whose `get_book` returns None for the archive id).
  - `test_images_point_at_the_serving_epub` — `UChapter.calibre_id` of an archive-served chapter is the archive id.
- [ ] **Step 2: Run** `uv run pytest tests/test_unified.py -q` → fail.
- [ ] **Step 3: Implement** the three functions. URL identity map built from archives in order (first wins); live chapters absent from it are appended in live order; present ones replace by the length rule using word counts.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat: unified chapter list across segments`.

### Task 4: Planner reads the unified list

**Files:**
- Modify: `app/planner/planner.py` (`_remaining_units` ~L103, `_get_chapters` ~L689, `_materialise` ~L706, `_advance_cursor` ~L742, `Unit`), `app/feed/builder.py` (no change expected; verify `_extra_available`)
- Test: `tests/test_segments_apply.py`

**Interfaces:**
- Consumes: `load_unified`, `UChapter` (Task 3).
- Produces: `Unit` gains `calibre_id: int`; `Chapter`-shaped objects from `_get_chapters` become `UChapter` (same `.index`-like access: add `index` property returning `position` so existing selection code is untouched).

- [ ] **Step 1: Failing tests:** with a two-EPUB fixture book (archive 5 chapters, live 3 incl. 1 new) and `cursor_chapter_index=4`, `run_release_cycle` on a budget fitting two chapters emits chapters at positions 4 and 5, advances the cursor to 6, `total_chapters == 6`, and the drop HTML for position 5 uses the live id in its `/img/` URL while position 4's uses the archive id (assert on `materialize_image_urls` output); `test_unreadable_archive_holds_the_story` — archive missing from the adapter → nothing released for that book, `app_state[book_note:{id}]` mentions the archive id, `total_chapters` unchanged, another book in the channel still releases; `test_unsegmented_book_unchanged` — existing planner tests still pass.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.** `_get_chapters` → `load_unified`; on `unreadable` set the book note (existing `BOOK_NOTE_PREFIX` mechanism) and return `[]` without touching `total_chapters`; clear the note when readable again. `_materialise` materialises image URLs **per chapter** with that chapter's `calibre_id` before joining. `_advance_cursor` uses the unified length for `new_cursor`/`total_chapters`/catch-up.
- [ ] **Step 4: Run** `uv run pytest tests/test_segments_apply.py tests -q` → green (all 625+ existing tests).
- [ ] **Step 5: Commit** `feat(planner): release from the unified chapter list`.

### Task 5: Stub keeps the old EPUB as a segment (no remap)

**Files:**
- Modify: `app/fetch/client.py` (`StubInfo`, `_to_result`, `apply_result`, `_apply_stub`, `_epub_chapter_count`)
- Test: `tests/test_segments_apply.py`

**Interfaces:**
- Produces: `StubInfo.archive_calibre_id: int | None = None`; `_epub_chapter_count(book)` returns `len(load_unified(...).chapters)` (None if unreadable); `_apply_stub_as_segment(book: Book, stub: StubInfo) -> None`.

- [ ] **Step 1: Failing tests:** `test_stub_with_archive_id_adds_a_segment_and_keeps_cursor_and_labels` — book with `cursor_chapter_index=140`, 151-chapter archive id 4657, live 65 (63 shared URLs + 2 new) → `book.segments == [4657]`, cursor still 140, `label_map` unchanged, `total_chapters == 153`, `fresh_from_index == 151`, `last_fetch_status == "ok (stub 151→65, kept as segment)"`; `test_second_stub_appends_another_segment` (ordinal 1, positions of the first 151 unchanged); `test_stub_without_archive_id_uses_the_legacy_remap` (existing behaviour, existing tests keep passing); `test_stub_new_chapters_are_fresh_after_catching_up`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.** In `apply_result`, when `result.stub` carries an `archive_calibre_id`, call `_apply_stub_as_segment`: append `BookSegment(ordinal=len(book.segments), calibre_id=archive_id)`, leave cursor/`label_map`/`cursor_floor` alone, do **not** clear `fresh_from_index`, status text as above. Otherwise the existing `_apply_stub`. Order matters: add the segment before computing `count` so `total_chapters` and `fresh_from_index` use the unified count — move the segment step above the count block.
- [ ] **Step 4: Run** full suite → green.
- [ ] **Step 5: Commit** `feat(fetch): a stub's old EPUB becomes a segment instead of remapping the cursor`.

### Task 6: Admin cursor controls and the story page

**Files:**
- Create: `app/segments_view.py`
- Modify: `app/routers/admin.py` (set-cursor ~L895, `_book_chapter_count`, cursor-latest/start/undo, requeue ~L1191, `story_page` context), `app/templates/admin/story.html`, `app/static/` CSS if the template keeps styles inline elsewhere (follow the file's existing style)
- Test: `tests/test_segments_view.py`, `tests/test_story_page.py`

**Interfaces:**
- Produces: `segment_bar(book: Book, unified: list[UChapter]) -> list[BarSpan]` with `BarSpan(kind: "archive"|"archive_short_live"|"live", start_label: int, end_label: int, start_pos: int, end_pos: int, width_pct: float, calibre_id: int)`; runs of consecutive chapters with the same `(kind, calibre_id)` merge into one span; labels via `chapter_label`. `effective_cursor_floor(book) -> int` = 0 when `book.segments` else `book.cursor_floor`.

- [ ] **Step 1: Failing tests:** view-model test with the book-4 shape (spans: archive-only, archive_short_live, live; widths sum to 100; boundary labels are author labels, e.g. `end_label` of the first span = `chapter_label(book, end_pos)`); `test_segmented_story_page_shows_bar_sources_and_legend` (asserts boundary numbers, both Calibre ids, "old EPUB"/"live EPUB" legend text — colour is not the only signal); `test_unsegmented_story_page_has_no_segment_ui`; `test_rewind_below_old_floor_allowed_with_segments` (POST set-cursor to 120 with `cursor_floor=65` and a segment → cursor 120); same POST without segments still clamps to the floor; max is the unified count.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** the view-model; replace `cursor_floor` uses in admin with `effective_cursor_floor`; `_book_chapter_count` and cursor-latest use the unified count; story page passes `bar`, `sources` (segments with titles and chapter counts) and shows "Author's chapter N" next to the cursor input via `abs_chapter`; render the bar per the mockup (segment `<i>` with `style="width:X%"`, tick labels under boundaries, pin at the cursor, legend with text).
- [ ] **Step 4: Run** `uv run pytest tests -q` → green; start the app (`/run`-style: `uv run uvicorn app.main:app` with `BEACON_ADMIN_OPEN=1` against a tmp DB) and load a segmented story page; note any visual problem.
- [ ] **Step 5: Commit** `feat(admin): segmented progress bar, author labels, rewind into archives`.

### Task 7: Backfill already-stubbed books

**Files:**
- Create: `app/segments_backfill.py`
- Modify: `app/calibre/adapter.py` (`archive_ids(story_key_or_url) -> list[int]` reading `url-archive:` identifiers from `metadata.db`), `app/database.py:init_db` (call after migrations, guarded so a missing library doesn't stop startup)
- Test: `tests/test_segments_backfill.py`

**Interfaces:**
- Produces: `backfill_segments(session: Session, adapter: CalibreAdapter) -> list[dict]` — one report row per migrated book `{book_id, title, archive_ids, old_cursor, new_cursor, old_label_offset, old_label_map}`; idempotent (books with segments are skipped); also stored as `app_state["segment_backfill_report"]` JSON.

- [ ] **Step 1: Failing tests:** book with `cursor_floor>0` (stubbed), an adapter fake returning `[4657]` for its story key → one segment, `label_map=None`, `chapter_label_offset=0`, `cursor_floor=0`, cursor set to the position after the last chapter released (drops with `source_url`s inside the unified list; `max(position_of(drop.source_url) + chapter_end - chapter_start) + 1`; with no matching drop, leave the cursor at its current value but clamp to the unified length); running twice changes nothing; a book with no archive entry is untouched; a book that never stubbed is untouched.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** and wire into `init_db`; log one warning line per migrated book with the report row. Decision recorded in the spec's "Migrating already-stubbed books": old label state is reset to zero offset (the old stub left it inconsistent for archive chapters); the report keeps the old values for ops.
- [ ] **Step 4: Run** full suite → green.
- [ ] **Step 5: Commit** `feat: backfill segments for already-stubbed books`.

### Task 8: Docs, version, final verification

**Files:** `CLAUDE.md` (Sources & units: "Stub handling" section updated for segments; Calibre access; Data model), `Architecture.md §5`, `CHANGELOG.md`, `pyproject.toml` (→ `0.26.0`), `README.md` only if it lists env vars (`BEACON_SEGMENT_LENGTH_RATIO`).

- [ ] **Step 1:** Update the docs to describe segments, the length rule, labels over unified positions, the backfill report; CHANGELOG `0.26.0 — Added/Changed/Fixed`.
- [ ] **Step 2:** `uv run pytest -q` → all pass; `git status` clean after commit.
- [ ] **Step 3:** Dispatch one `opus` reviewer on the whole branch against the spec.
- [ ] **Step 4: Commit** `docs: stub segments (0.26.0)`. Tag/bundle/push only if the user asks.
