"""Tests for tracked-story update detection and the async fetch path.

RSS feeds are only a *notification* that new chapters exist; the fetcher container downloads
them into Calibre asynchronously (a batch job the scheduler polls). These tests cover
GUID-change detection, that the poller *batches* due sources into one submit, and the
fetch-result folding (chapter-label offset + cursor floor on a stub).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from feedparser.util import FeedParserDict

from app.models import Book, BookStatus, Channel, absolute_chapter_number
from app.ongoing.poller import _newest_guid, fetch_pending, poll_all_feeds, sweep_feedless
from app.fetch.client import apply_result


# ── helpers ───────────────────────────────────────────────────────────────────

_next_calibre_id = iter(range(1000, 100000))


def _tracked(db, feed_url="https://s.example.com/feed", source_url="https://s.example.com/story",
             last_seen_guid=None, calibre_id=42) -> Book:
    channel_id = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    src = Book(
        tracked=True, feed_url=feed_url, source_url=source_url, calibre_id=calibre_id,
        title="Serial", author="A", status=BookStatus.active, queue_position=1,
        channel_id=channel_id, last_seen_guid=last_seen_guid, total_chapters=10,
        cursor_chapter_index=10,
    )
    db.add(src)
    db.flush()
    return src


def _feed(*guids):
    parsed = MagicMock()
    parsed.entries = [FeedParserDict(id=g, link=f"https://s/{g}", title=g) for g in guids]
    return parsed


# ── GUID-change detection ──────────────────────────────────────────────────────

class TestNewestGuid:
    def test_picks_first_entry(self):
        assert _newest_guid(_feed("c3", "c2", "c1")) == "c3"

    def test_empty_feed_is_none(self):
        assert _newest_guid(_feed()) is None


class TestPollTriggers:
    def test_first_sight_seeds_without_fetch(self, in_memory_db):
        src = _tracked(in_memory_db, last_seen_guid=None)
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("c5")), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = poll_all_feeds(in_memory_db)
        assert queued == 0
        mock_submit.assert_not_called()
        assert src.last_seen_guid == "c5"

    def test_new_guid_queues_fetch(self, in_memory_db):
        src = _tracked(in_memory_db, last_seen_guid="c4")
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("c5")), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = poll_all_feeds(in_memory_db)
        assert queued == 1
        mock_submit.assert_called_once()
        assert list(mock_submit.call_args[0][1]) == [src]
        # Not "seen" yet: the fold marks it once the fetch lands chapters (scheduler._settle_trigger),
        # so a failed/fruitless fetch is retried next cycle. The GUID travels with the job instead.
        assert src.last_seen_guid == "c4"
        assert mock_submit.call_args[0][2] == {src.id: "c5"}

    def test_unchanged_guid_no_fetch(self, in_memory_db):
        _tracked(in_memory_db, last_seen_guid="c5")
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("c5")), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = poll_all_feeds(in_memory_db)
        assert queued == 0
        mock_submit.assert_not_called()

    def test_changed_feeds_submit_in_one_batch(self, in_memory_db):
        a = _tracked(in_memory_db, feed_url="https://a/feed", source_url="https://a/s",
                     last_seen_guid="old", calibre_id=next(_next_calibre_id))
        b = _tracked(in_memory_db, feed_url="https://b/feed", source_url="https://b/s",
                     last_seen_guid="old", calibre_id=next(_next_calibre_id))
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("new")), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = poll_all_feeds(in_memory_db)
        assert queued == 2
        mock_submit.assert_called_once()  # one batch, not two calls
        assert set(mock_submit.call_args[0][1]) == {a, b}

    def test_fetch_pending_submits_never_downloaded(self, in_memory_db):
        """A tracked book with no calibre_id (initial fetch lost) is (re)submitted — the
        self-heal backstop, so it can't strand at 'pending' forever."""
        src = _tracked(in_memory_db, calibre_id=None)
        src.last_fetch_status = "pending"
        in_memory_db.flush()
        with patch("app.scheduler.submit_and_track") as mock_submit:
            n = fetch_pending(in_memory_db)
        assert n == 1
        assert list(mock_submit.call_args[0][1]) == [src]

    def test_fetch_pending_skips_in_flight(self, in_memory_db):
        """A book already 'fetching…' must not be re-submitted (its slow fetch is still running)."""
        src = _tracked(in_memory_db, calibre_id=None)
        src.last_fetch_status = "fetching: downloading"
        in_memory_db.flush()
        with patch("app.scheduler.submit_and_track") as mock_submit:
            n = fetch_pending(in_memory_db)
        assert n == 0
        mock_submit.assert_not_called()

    def test_fetch_pending_ignores_downloaded(self, in_memory_db):
        """A book with a calibre_id is already downloaded — never re-fetched by the backstop."""
        _tracked(in_memory_db, calibre_id=next(_next_calibre_id))
        with patch("app.scheduler.submit_and_track") as mock_submit:
            n = fetch_pending(in_memory_db)
        assert n == 0
        mock_submit.assert_not_called()

    def test_sweep_submits_feedless_only(self, in_memory_db):
        feedless = _tracked(in_memory_db, feed_url=None, source_url="https://x/story",
                            calibre_id=next(_next_calibre_id))
        _tracked(in_memory_db, feed_url="https://y/feed", source_url="https://y/story",
                 calibre_id=next(_next_calibre_id))
        with patch("app.scheduler.submit_and_track") as mock_submit:
            queued = sweep_feedless(in_memory_db)
        assert queued == 1
        assert list(mock_submit.call_args[0][1]) == [feedless]

    def test_sweep_skips_done_status(self, in_memory_db):
        """A Completed/Abandoned #status story is skipped — its EPUB is already complete."""
        ongoing = _tracked(in_memory_db, feed_url=None, source_url="https://o/story",
                           calibre_id=next(_next_calibre_id))
        done = _tracked(in_memory_db, feed_url=None, source_url="https://d/story",
                        calibre_id=next(_next_calibre_id))
        adapter = MagicMock()
        adapter.status_map.return_value = {
            ongoing.calibre_id: "In-Progress", done.calibre_id: "Completed"
        }
        with patch("app.ongoing.poller.CalibreAdapter", return_value=adapter), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = sweep_feedless(in_memory_db)
        assert queued == 1
        assert list(mock_submit.call_args[0][1]) == [ongoing]


    def test_sweep_skips_story_marked_done_by_the_fetcher(self, in_memory_db):
        """A URL-added story has a blank Calibre #status forever; the fetcher-recorded
        story_status is what lets the sweep stop re-fetching it once it completes."""
        ongoing = _tracked(in_memory_db, feed_url=None, source_url="https://o/story",
                           calibre_id=next(_next_calibre_id))
        done = _tracked(in_memory_db, feed_url=None, source_url="https://d/story",
                        calibre_id=next(_next_calibre_id))
        done.story_status = "Completed"
        adapter = MagicMock()
        adapter.status_map.return_value = {}          # Calibre #status blank for both
        with patch("app.ongoing.poller.CalibreAdapter", return_value=adapter), \
             patch("app.scheduler.submit_and_track") as mock_submit:
            queued = sweep_feedless(in_memory_db)
        assert queued == 1
        assert list(mock_submit.call_args[0][1]) == [ongoing]


# ── fetch result folding + stub mechanic ────────────────────────────────────────

class TestApplyResult:
    def test_ok_updates_fields(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.calibre_id = None
        apply_result(src, {"calibre_id": 99, "chapter_count": 12, "stub": None, "error": None})
        assert src.calibre_id == 99
        assert src.total_chapters == 12
        assert src.last_fetch_status == "ok"
        assert src.last_fetch_at is not None

    def test_error_leaves_book_untouched(self, in_memory_db):
        src = _tracked(in_memory_db)
        before = src.cursor_chapter_index
        apply_result(src, {"error": "boom"})
        assert src.cursor_chapter_index == before
        assert src.last_fetch_status.startswith("error")

    def test_stub_offsets_labels_and_floors_cursor(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 130
        src.total_chapters = 141
        apply_result(src, {"calibre_id": 42, "chapter_count": 101,
                           "stub": {"old": 141, "new": 101}, "error": None})
        # 40 chapters removed → next chapter still labels continuously.
        assert src.chapter_label_offset == 40
        assert src.cursor_chapter_index == 101   # caught up to the rewritten body
        assert src.cursor_floor == 101           # cannot rewind into it
        # Physical chapter 101 (the next new one) reads as absolute chapter 142.
        assert absolute_chapter_number(src, 101) == 142
        assert "stub" in src.last_fetch_status

    def test_offsets_compose_across_stubs(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.chapter_label_offset = 40
        apply_result(src, {"calibre_id": 42, "chapter_count": 90,
                           "stub": {"old": 101, "new": 90}, "error": None})
        assert src.chapter_label_offset == 51   # 40 + (101-90)


def _u(n: int) -> str:
    return f"https://s/c{n}"


def _urls(*chapter_numbers: int) -> list[str]:
    return [_u(n) for n in chapter_numbers]


class TestStubIdentity:
    """Stub folding by per-chapter URL identity (mid-work removal + cursor remap)."""

    def test_middle_removal_labels_and_cursor(self, in_memory_db):
        # 150 chapters; author removes the middle c5..c72 (68 chapters) → 82 survive.
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 100      # read through old c100 (unread: c101..c150)
        src.cursor_floor = 0
        old_urls = _urls(*range(1, 151))
        new_urls = _urls(1, 2, 3, 4, *range(73, 151))
        apply_result(src, {"calibre_id": 42, "chapter_count": 82,
                           "stub": {"old": 150, "new": 82,
                                    "old_urls": old_urls, "new_urls": new_urls}, "error": None})
        # Labels stay exact across the 4 → 73 jump.
        assert absolute_chapter_number(src, 0) == 1
        assert absolute_chapter_number(src, 3) == 4
        assert absolute_chapter_number(src, 4) == 73
        assert absolute_chapter_number(src, 81) == 150
        # Cursor remaps to the first surviving chapter ≥ old cursor (c101), which sits at new
        # physical index 4 + (101-73) = 32 — the reader keeps c101..c150.
        assert src.cursor_chapter_index == 32
        assert absolute_chapter_number(src, 32) == 101
        assert src.cursor_floor == 0        # c1 survived at index 0
        assert "mapped" in src.last_fetch_status

    def test_reader_inside_removed_range_lands_on_first_survivor(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 30       # next-unread was c31, but c31..c72 are gone
        old_urls = _urls(*range(1, 151))
        new_urls = _urls(1, 2, 3, 4, *range(73, 151))
        apply_result(src, {"calibre_id": 42, "chapter_count": 82,
                           "stub": {"old": 150, "new": 82,
                                    "old_urls": old_urls, "new_urls": new_urls}, "error": None})
        assert src.cursor_chapter_index == 4        # first survivor ≥ 30 is c73 at index 4
        assert absolute_chapter_number(src, 4) == 73

    def test_reader_caught_up_stays_caught_up(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 150      # read everything
        old_urls = _urls(*range(1, 151))
        new_urls = _urls(1, 2, 3, 4, *range(73, 151))
        apply_result(src, {"calibre_id": 42, "chapter_count": 82,
                           "stub": {"old": 150, "new": 82,
                                    "old_urls": old_urls, "new_urls": new_urls}, "error": None})
        assert src.cursor_chapter_index == 82       # no survivor past the end → caught up

    def test_repeated_stub_composes_labels(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 150
        # Round 1: 150 → 82 (remove c5..c72).
        r1 = _urls(1, 2, 3, 4, *range(73, 151))
        apply_result(src, {"calibre_id": 42, "chapter_count": 82,
                           "stub": {"old": 150, "new": 82,
                                    "old_urls": _urls(*range(1, 151)), "new_urls": r1}, "error": None})
        # Round 2: from the 82-body, remove c79..c83 (5 chapters) → 77.
        r2 = [u for u in r1 if u not in set(_urls(79, 80, 81, 82, 83))]
        apply_result(src, {"calibre_id": 42, "chapter_count": 77,
                           "stub": {"old": 82, "new": 77,
                                    "old_urls": r1, "new_urls": r2}, "error": None})
        # A chapter that was label 100 is still label 100 despite two gaps at different positions.
        idx_c100 = r2.index(_u(100))
        assert absolute_chapter_number(src, idx_c100) == 100
        # And the label just past the second gap is right: c84 follows c78.
        assert absolute_chapter_number(src, r2.index(_u(84))) == 84
        assert absolute_chapter_number(src, r2.index(_u(78))) == 78

    def test_inconsistent_urls_fall_back_to_linear(self, in_memory_db):
        # URL lists whose lengths disagree with the counts → count-only linear fallback.
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 130
        src.total_chapters = 141
        apply_result(src, {"calibre_id": 42, "chapter_count": 101,
                           "stub": {"old": 141, "new": 101,
                                    "old_urls": ["only-one"], "new_urls": []}, "error": None})
        assert src.label_map is None
        assert src.chapter_label_offset == 40
        assert src.cursor_chapter_index == 101
        assert "mapped" not in src.last_fetch_status


class TestAddTrackedStory:
    def test_url_added_story_gets_tracked_default_weight(self, in_memory_db):
        # A story added by URL must get the same priority nudge as a library-imported
        # serial (config.tracked_default_weight), not the 1.0 backlog default.
        from app.ongoing.add import add_tracked_story
        cid = in_memory_db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
        book = add_tracked_story(in_memory_db, "https://s/story", "", cid)
        assert book.quota_weight == 2.0  # conftest Config uses the model default
        # Blank title falls back to the URL placeholder until the first fetch resolves it.
        assert book.title == "https://s/story"


class TestSyncTitle:
    def test_placeholder_title_replaced_with_calibre_title(self, in_memory_db):
        from app import scheduler
        src = _tracked(in_memory_db, source_url="https://s/story", calibre_id=777)
        src.title = "https://s/story"  # URL placeholder from a blank-title add
        cbook = MagicMock(title="Real Story Title")
        with patch("app.calibre.adapter.CalibreAdapter.get_book", return_value=cbook):
            scheduler._sync_title(src)
        assert src.title == "Real Story Title"

    def test_real_title_is_left_alone(self, in_memory_db):
        from app import scheduler
        src = _tracked(in_memory_db, source_url="https://s/story", calibre_id=778)
        src.title = "An Already-Named Serial"
        with patch("app.calibre.adapter.CalibreAdapter.get_book") as get_book:
            scheduler._sync_title(src)
            get_book.assert_not_called()  # not a placeholder → no Calibre lookup
        assert src.title == "An Already-Named Serial"


class TestAbsoluteChapterNumber:
    def test_no_offset_is_one_based(self, in_memory_db):
        src = _tracked(in_memory_db)
        assert absolute_chapter_number(src, 0) == 1
        assert absolute_chapter_number(src, 9) == 10


class TestStoryStatusAndCanonicalUrl:
    def test_status_recorded_from_fetch_result(self, in_memory_db):
        src = _tracked(in_memory_db)
        apply_result(src, {"url": src.source_url, "calibre_id": 42, "chapter_count": 12,
                           "story_status": "Completed", "error": None, "stub": None})
        assert src.story_status == "Completed"

    def test_status_kept_when_fetcher_reports_none(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.story_status = "In-Progress"
        apply_result(src, {"url": src.source_url, "calibre_id": 42, "chapter_count": 12,
                           "story_status": None, "error": None, "stub": None})
        assert src.story_status == "In-Progress"

    def test_first_download_adopts_canonical_url(self, in_memory_db):
        """The pasted URL differs from the canonical one FanFicFare recorded; keeping it made every
        later update miss the Calibre `url:` identifier and re-download the story as a duplicate."""
        src = _tracked(in_memory_db, source_url="https://www.fanfiction.net/s/13051824",
                       calibre_id=None)
        apply_result(src, {"url": src.source_url, "calibre_id": 77, "chapter_count": 5,
                           "story_url": "https://www.fanfiction.net/s/13051824/1/New-Blood",
                           "story_status": "In-Progress", "error": None, "stub": None})
        assert src.calibre_id == 77
        assert src.source_url == "https://www.fanfiction.net/s/13051824/1/New-Blood"

    def test_canonical_url_not_adopted_when_already_downloaded(self, in_memory_db):
        src = _tracked(in_memory_db, source_url="https://s.example.com/story", calibre_id=42)
        apply_result(src, {"url": src.source_url, "calibre_id": 42, "chapter_count": 11,
                           "story_url": "https://s.example.com/story/canonical",
                           "story_status": None, "error": None, "stub": None})
        assert src.source_url == "https://s.example.com/story"

    def test_canonical_url_not_adopted_if_another_source_owns_it(self, in_memory_db):
        _tracked(in_memory_db, source_url="https://x/s/1/1/Title", calibre_id=1)
        src = _tracked(in_memory_db, source_url="https://x/s/1", calibre_id=None)
        apply_result(src, {"url": src.source_url, "calibre_id": 2, "chapter_count": 5,
                           "story_url": "https://x/s/1/1/Title", "error": None, "stub": None})
        assert src.source_url == "https://x/s/1"


class TestAddStories:
    """Adding by URL goes through the Add page's single endpoint (`POST /admin/add`)."""

    def test_added_story_is_placed_in_a_slot_immediately(self, in_memory_db):
        from app.routers.admin import add_sources
        cid = in_memory_db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
        with patch("app.scheduler.trigger_fetch_pending"):
            add_sources(urls="https://s/story", calibre_ids=[], channel_id=cid, db=in_memory_db)
        book = in_memory_db.query(Book).filter(Book.source_url == "https://s/story").one()
        assert book.slot_index is not None

    def test_outcome_counts_added_duplicate_and_invalid(self, in_memory_db):
        from app.routers.admin import add_sources
        cid = in_memory_db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
        with patch("app.scheduler.trigger_fetch_pending"):
            add_sources(urls="https://s/a\nhttps://s/b", calibre_ids=[], channel_id=cid, db=in_memory_db)
            resp = add_sources(urls="https://s/a\nhttps://s/c\n\n--config=x\nhttps://s/c",
                               calibre_ids=[], channel_id=cid, db=in_memory_db)
        loc = resp.headers["location"]
        assert "invalid=1" in loc
        added = in_memory_db.query(Book).filter(Book.source_url == "https://s/c").one()
        a = in_memory_db.query(Book).filter(Book.source_url == "https://s/a").one()
        # /c is new (and not repeated as a duplicate of itself); /a was already tracked
        assert f"added={added.id}" in loc and f"dupes={a.id}" in loc


# ── drift: an EPUB that is behind a feed whose newest GUID we have already "seen" ──────────────

RR_BASE = "https://www.royalroad.com/fiction/1/s/chapter"


def _links_feed(*ids):
    """Newest-first feed whose entries link to RoyalRoad-style chapter URLs."""
    parsed = MagicMock()
    parsed.entries = [FeedParserDict(id=f"guid-{i}", link=f"{RR_BASE}/{i}/c-{i}", title=str(i)) for i in ids]
    return parsed


def _epub_urls(*ids):
    return [f"{RR_BASE}/{i}/c-{i}" for i in ids]


class TestFeedDrift:
    def _poll(self, db, feed, urls):
        with patch("app.ongoing.poller._fetch_feed", return_value=feed), \
             patch("app.ongoing.poller._epub_chapter_urls", return_value=urls), \
             patch("app.scheduler.submit_and_track") as submit:
            queued = poll_all_feeds(db)
        return queued, submit

    def test_epub_behind_a_seen_guid_is_refetched(self, in_memory_db):
        src = _tracked(in_memory_db, last_seen_guid="guid-5000")
        queued, submit = self._poll(in_memory_db, _links_feed(5000, 4999, 4998), _epub_urls(4998))
        assert queued == 1
        assert submit.call_args[0][2] == {src.id: "guid-5000"}  # settled like any trigger

    def test_epub_with_the_newest_entry_is_left_alone(self, in_memory_db):
        _tracked(in_memory_db, last_seen_guid="guid-5000")
        queued, submit = self._poll(in_memory_db, _links_feed(5000, 4999), _epub_urls(4999, 5000))
        assert queued == 0
        submit.assert_not_called()

    def test_incomparable_links_are_not_drift(self, in_memory_db):
        """No feed entry matches any chapter URL → we can't tell, so don't refetch forever."""
        _tracked(in_memory_db, last_seen_guid="guid-5000")
        queued, _ = self._poll(in_memory_db, _links_feed(5000, 4999), _epub_urls(1, 2, 3))
        assert queued == 0

    def test_unreadable_epub_is_not_drift(self, in_memory_db):
        _tracked(in_memory_db, last_seen_guid="guid-5000")
        queued, _ = self._poll(in_memory_db, _links_feed(5000, 4999), [])
        assert queued == 0

    def test_given_up_guid_is_not_retried(self, in_memory_db):
        from app.state import FETCH_GIVEUP_PREFIX, set_value
        src = _tracked(in_memory_db, last_seen_guid="guid-5000")
        set_value(in_memory_db, FETCH_GIVEUP_PREFIX + str(src.id), '{"guid": "guid-5000"}')
        queued, _ = self._poll(in_memory_db, _links_feed(5000, 4999, 4998), _epub_urls(4998))
        assert queued == 0

    def test_new_entry_after_a_giveup_is_tried_again(self, in_memory_db):
        from app.state import FETCH_GIVEUP_PREFIX, set_value
        src = _tracked(in_memory_db, last_seen_guid="guid-4999")
        set_value(in_memory_db, FETCH_GIVEUP_PREFIX + str(src.id), '{"guid": "guid-4999"}')
        queued, _ = self._poll(in_memory_db, _links_feed(5000, 4999), _epub_urls(4999))
        assert queued == 1


class TestGiveUpIsRecorded:
    def _fruitless(self, db, book, guid="g"):
        from app import scheduler
        res = MagicMock(ok=True, new_chapters=0)
        for _ in range(scheduler.TRIGGER_MAX_ATTEMPTS):
            scheduler._settle_trigger(db, book, res, guid)

    def test_giving_up_leaves_a_record_for_the_dashboard(self, in_memory_db):
        from app.state import FETCH_GIVEUP_PREFIX, get_value
        src = _tracked(in_memory_db, last_seen_guid="old")
        self._fruitless(in_memory_db, src)
        assert '"g"' in get_value(in_memory_db, FETCH_GIVEUP_PREFIX + str(src.id))

    def test_landing_chapters_clears_it_even_for_a_manual_fetch(self, in_memory_db):
        from app import scheduler
        from app.state import FETCH_GIVEUP_PREFIX, get_value
        src = _tracked(in_memory_db, last_seen_guid="old")
        self._fruitless(in_memory_db, src)
        in_memory_db.flush()  # a later cycle: the record is committed by then
        scheduler._settle_trigger(in_memory_db, src, MagicMock(ok=True, new_chapters=2), None)  # Fetch now
        in_memory_db.flush()
        assert get_value(in_memory_db, FETCH_GIVEUP_PREFIX + str(src.id)) is None
