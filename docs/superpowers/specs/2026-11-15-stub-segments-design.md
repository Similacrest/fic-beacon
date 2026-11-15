# Stub stories as one book across several EPUBs — design

Date: 2026-11-15 · Target release: v0.26.0 · Builds on v0.25.6

## Intent and why

When a site cuts a story down (Royal Road "Stub": book 4, *New Life as a Max Level Archmage*, 151 chapters
held, 65 public, site at label 220), the user still wants to read the whole story and keep following it.
Merging chapters into one EPUB is rejected: the real data has non-consecutive gaps and chapters replaced
by stub placeholders, so EPUB surgery is fragile.

Instead, keep the EPUBs separate and make Fic-Beacon treat them as **one book**: rewinds, re-reads, the
progress bar and the cursor control work across all of them, and the UI shows which EPUB each part of the
book comes from.

## Goals

1. A stub story is an ordered list of **segments**, each a Calibre EPUB: the archive(s) kept on a stub
   (v0.25.6 already adds `url-archive:` entries), then the live EPUB.
2. One **unified chapter list** and one cursor over it. After a stub the user sees the old chapters
   unchanged and new site chapters appended (book 4: archive positions 1–151, new live chapters at positions 152, 153 whose author labels are about 219, 220).
3. Rewinding or re-reading at any position serves that chapter from whichever EPUB holds it.
4. **Author's chapter labels are what the user sees everywhere**: feed item titles, `/read/`, the story
   page, the cursor box and the progress-bar boundary numbers. The unified position is internal only.
   The story page's progress bar and cursor/rewind control are drawn in segment colours with small
   boundary chapter numbers (mockup: `~/outbox/mockup-stub-segments-progress.html`).
5. Existing stub behaviour is preserved, not replaced: `label_map` author labels, URL-identity cursor
   remapping and `cursor_floor` semantics keep working, extended across segments.

## Non-goals / anti-goals

- No EPUB merging, rewriting or other surgery, and no writes to Calibre from the app (Calibre stays
  read-only from beacon; only the fetcher writes).
- Stories that never stubbed behave exactly as today (single segment, no new UI noise).
- Not a failure-tolerant "best effort" that silently drops chapters: a chapter the unified list claims
  must be readable, or the story shows an attention item.
- Do not release chapters the user already read just because the unified list was rebuilt.

## Model (decided by the user unless marked *my call*)

- **Unified list** = every chapter of the archive segment(s) in order, followed by those chapters of the
  live EPUB whose canonical chapter URL (`<meta name="chapterurl">`) is **not** present in an earlier
  segment, in live-EPUB order. Overlaps, gaps and stub-placeholder chapters therefore never create
  duplicates; they are matched by URL identity, as the existing stub logic does.
- **Labels and cursor** use the existing `label_map` / `cursor_chapter_index` / URL-identity remap logic,
  extended so a unified position maps to (segment, physical index in that EPUB) and back. The cursor is a
  unified position.
- **Which copy of a chapter in both EPUBs is served:** the live copy when its length matches the archive's,
  the archive copy when it differs significantly (a stub placeholder is short). Rationale: live picks up
  author edits; the archive protects real content from placeholders.
  *My call:* "significantly" = live word count below 50% of the archive's (or above 200%); make it a
  config value (`segment_length_ratio`, default 0.5), and decide per chapter, cached with the segment
  mtimes so it is stable between releases.
- **Appended live chapters** (not in any archive) always come from the live EPUB.
- **When the site grows later** (65 → 67): new URLs append to the unified list; fresh-chapter handling
  (`fresh_from_index`) applies to them as today.
- **A second stub** later archives the then-live EPUB as another segment (segments: archive 1, archive 2,
  live); the same URL-identity rule orders and de-duplicates across all of them.

## What changes where (guidance; the builder owns the how)

- **Fetcher** (`fetcher/app.py`): on a stub it already downloads fresh and archives (0.25.6). New: report
  the archive's Calibre id and the live id so the app can record segments; never overwrite the live entry
  with an older book.
- **Data model:** a place to record a book's segments in order (Calibre id, role archive|live, chapter
  URLs or a pointer to compute them). Alembic migration required (never hand-edit a deployed schema);
  existing stubbed books (book 4, archive 4657) must be migratable by detecting `url-archive` entries or
  by an ops-run backfill — *my call:* auto-detect by story key on first load, no manual step.
- **Chapterizer / planner / reader / `/img`:** chapter retrieval, `total_chapters`, extra-chapter
  availability, `_get_chapters` and the reader's prev/next must go through the unified list;
  `/img/{calibre_id}/…` already takes the EPUB id, so images follow the chapter's source EPUB.
- **Story page:** segmented progress bar and cursor/rewind control with colours and boundary numbers
  (author labels, per the user); a compact "Sources" block with both Calibre ids and chapter
  counts. Colours distinguish: archive-only, archive-where-live-is-a-short-stub, live. Text must not rely
  on colour alone (labels/legend); works on a tablet.
- **Dashboard:** unchanged except attention items for unreadable segments.

## Constraints

- Follow CLAUDE.md: reader-agnostic feeds, Calibre read-only from the app, never split a unit, UI says
  "release" for the act, `total_chapters` is beacon's own chapterizer count, never `create_all` in prod.
- Feed items, GUIDs and permalinks stay byte-stable; already-emitted drops must keep resolving
  (`drop.chapter_start/end` are existing unified/physical positions — builder to confirm which and
  migrate if needed so old `/read/` links keep working).
- Memory budget is tight (6 GiB shared); chapterizing several EPUBs must stay mtime-cached and lazy.

## Decisions after the builder check

- *My call:* `cursor_floor` applies only to a story that stubbed with no archive; with segments it is 0 so
  the user can rewind into the archive.
- *My call:* an unreadable segment holds the story at its first unreadable chapter with a dashboard
  attention item; unified positions never shift when a segment is temporarily missing.
- *My call:* the fetcher refuses to replace the live entry with an EPUB having fewer chapter URLs unless
  it goes through the stub path.
- `drop.chapter_start/end` need no migration (old releases serve their stored `content_html`).
- A chapter that is new by URL is appended at the end even if the site puts it mid-story (rare, accepted).
- **Open (user to answer):** on migrating book 4, release the archive chapters the old stub skipped, or
  carry today's cursor across unchanged.

## Left to the builder

Table shape, how chapter URLs are cached per segment, the exact migration, template/CSS details, test
fixtures (e.g. 151-chapter archive + 65-chapter live with 2 new, gaps and stub placeholders).

## Acceptance

- Book 4 after migration: unified list is 153, the progress bar shows archive/live segments with
  boundary numbers, rewinding to 120 re-releases from the archive, 152–153 come from the live EPUB.
- Re-reading a chapter present in both copies serves the live copy when lengths match and the archive
  copy when the live one is a short placeholder.
- A stub, a second stub, and site growth all keep labels continuous and never skip or repeat an unread
  chapter.
- A non-stubbed story is byte-for-byte unchanged in behaviour and shows no segment UI.
