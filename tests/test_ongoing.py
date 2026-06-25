"""Tests for tracked-story update detection and fetching.

RSS feeds are now only a *notification* that new chapters exist; the fetcher container
downloads them into Calibre. These tests cover GUID-change detection, the fetch client's
stub mechanic (chapter-label offset + cursor floor), and the feedless sweep.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from feedparser.util import FeedParserDict

from app.models import Book, BookStatus, Channel, absolute_chapter_number
from app.ongoing.poller import _newest_guid, poll_all_feeds, sweep_feedless
from app.fetch.client import FetchResult, StubInfo, fetch_book


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
        with patch("app.ongoing.poller.feedparser.parse", return_value=_feed("c5")), \
             patch("app.ongoing.poller.fetch_book") as mock_fetch:
            fetched = poll_all_feeds(in_memory_db)
        assert fetched == 0
        mock_fetch.assert_not_called()
        assert src.last_seen_guid == "c5"

    def test_new_guid_triggers_fetch(self, in_memory_db):
        src = _tracked(in_memory_db, last_seen_guid="c4")
        with patch("app.ongoing.poller.feedparser.parse", return_value=_feed("c5")), \
             patch("app.ongoing.poller.fetch_book") as mock_fetch:
            fetched = poll_all_feeds(in_memory_db)
        assert fetched == 1
        mock_fetch.assert_called_once()
        assert src.last_seen_guid == "c5"

    def test_unchanged_guid_no_fetch(self, in_memory_db):
        _tracked(in_memory_db, last_seen_guid="c5")
        with patch("app.ongoing.poller.feedparser.parse", return_value=_feed("c5")), \
             patch("app.ongoing.poller.fetch_book") as mock_fetch:
            fetched = poll_all_feeds(in_memory_db)
        assert fetched == 0
        mock_fetch.assert_not_called()

    def test_sweep_fetches_feedless_only(self, in_memory_db):
        feedless = _tracked(in_memory_db, feed_url=None, source_url="https://x/story", calibre_id=next(_next_calibre_id))
        _tracked(in_memory_db, feed_url="https://y/feed", source_url="https://y/story", calibre_id=next(_next_calibre_id))
        with patch("app.ongoing.poller.fetch_book") as mock_fetch:
            fetched = sweep_feedless(in_memory_db)
        assert fetched == 1
        assert mock_fetch.call_args[0][1] is feedless


# ── fetch client: result folding + stub mechanic ───────────────────────────────

class TestFetchBook:
    def test_ok_updates_fields(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.calibre_id = None
        result = FetchResult(ok=True, calibre_id=99, chapter_count=12, stub=None)
        with patch("app.fetch.client.request_fetch", return_value=result):
            fetch_book(in_memory_db, src)
        assert src.calibre_id == 99
        assert src.total_chapters == 12
        assert src.last_fetch_status == "ok"
        assert src.last_fetch_at is not None

    def test_error_leaves_book_untouched(self, in_memory_db):
        src = _tracked(in_memory_db)
        before = src.cursor_chapter_index
        with patch("app.fetch.client.request_fetch", return_value=FetchResult(ok=False, error="boom")):
            fetch_book(in_memory_db, src)
        assert src.cursor_chapter_index == before
        assert src.last_fetch_status.startswith("error")

    def test_stub_offsets_labels_and_floors_cursor(self, in_memory_db):
        src = _tracked(in_memory_db)
        src.cursor_chapter_index = 130
        src.total_chapters = 141
        result = FetchResult(ok=True, calibre_id=42, chapter_count=101,
                             stub=StubInfo(old=141, new=101))
        with patch("app.fetch.client.request_fetch", return_value=result):
            fetch_book(in_memory_db, src)
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
        result = FetchResult(ok=True, calibre_id=42, chapter_count=90,
                             stub=StubInfo(old=101, new=90))
        with patch("app.fetch.client.request_fetch", return_value=result):
            fetch_book(in_memory_db, src)
        assert src.chapter_label_offset == 51   # 40 + (101-90)


class TestAbsoluteChapterNumber:
    def test_no_offset_is_one_based(self, in_memory_db):
        src = _tracked(in_memory_db)
        assert absolute_chapter_number(src, 0) == 1
        assert absolute_chapter_number(src, 9) == 10
