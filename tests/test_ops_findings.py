"""Regression tests for the 2026-10-13 ops findings (fetch identity, trigger GUIDs, titles, …)."""
from __future__ import annotations

import importlib.util
import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from feedparser.util import FeedParserDict

from app import scheduler
from app.fetch import client
from app.fetch.client import apply_result
from app.models import Book, BookStatus, Channel
from app.ongoing import poller
from app.ongoing.story_key import normalize_story_url, story_key
from app.routers.ongoing import _add_tracked_story
from app.state import FETCH_JOB_PREFIX, TRIGGER_RETRY_PREFIX, get_value, set_value
from tests.make_epub import make_epub

_spec = importlib.util.spec_from_file_location(
    "fetcher_app_ops", os.path.join(os.path.dirname(__file__), "..", "fetcher", "app.py")
)
fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher)


def _channel_id(db) -> int:
    return db.query(Channel.id).order_by(Channel.id).limit(1).scalar()


def _book(db, **kw) -> Book:
    fields = dict(
        tracked=True, source_url="https://s.example.com/story", title="Serial", author="A",
        status=BookStatus.active, queue_position=1, channel_id=_channel_id(db), calibre_id=None,
    )
    fields.update(kw)
    b = Book(**fields)
    db.add(b)
    db.flush()
    return b


@pytest.fixture
def fold(in_memory_db, monkeypatch):
    """Drive scheduler._poll_fetch_job against the in-memory DB with a canned fetcher reply."""
    @contextmanager
    def fake_session():
        yield in_memory_db
    monkeypatch.setattr(scheduler, "db_session", fake_session)
    monkeypatch.setattr(scheduler, "_unschedule_poll", lambda job_id: None)

    def run(books, results, guids=None, job_id="job1"):
        url_to_book: dict[str, list[int]] = {}
        for b in books:
            url_to_book.setdefault(b.source_url, []).append(b.id)
        set_value(in_memory_db, FETCH_JOB_PREFIX + job_id, json.dumps({
            "submitted_at": scheduler.datetime.now(scheduler.timezone.utc).isoformat(),
            "url_to_book": url_to_book,
            "guids": {str(b.id): g for b, g in zip(books, guids or [])},
        }))
        with patch("app.fetch.client.poll_fetch", return_value={"status": "done", "results": results}):
            scheduler._poll_fetch_job(job_id)
    return run


# ── the "name becomes the URL" regression ────────────────────────────────────────────────────

class TestTitleResolvesAfterFirstDownload:
    def test_title_from_calibre_even_though_canonical_url_was_adopted(self, in_memory_db, fold):
        # Paste /s/123; FanFicFare canonicalises to /s/123/1/Title. The adopted source_url no
        # longer equals the placeholder title, which used to make the title stick as the URL.
        book = _add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "",
                                  _channel_id(in_memory_db))
        assert book.title == "https://www.fanfiction.net/s/13051824"
        cbook = MagicMock(title="New Blood", author="artemisgirl")
        with patch("app.calibre.adapter.CalibreAdapter.get_book", return_value=cbook):
            fold([book], [{
                "url": book.source_url, "calibre_id": 1559, "chapter_count": 5, "stub": None,
                "story_url": "https://www.fanfiction.net/s/13051824/1/New-Blood",
                "story_status": "In-Progress", "error": None, "phase": "done",
            }])
        assert book.source_url == "https://www.fanfiction.net/s/13051824/1/New-Blood"
        assert book.title == "New Blood"
        assert book.author == "artemisgirl"

    def test_user_supplied_name_is_kept(self, in_memory_db, fold):
        book = _add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "My Name",
                                  _channel_id(in_memory_db))
        cbook = MagicMock(title="New Blood", author="artemisgirl")
        with patch("app.calibre.adapter.CalibreAdapter.get_book", return_value=cbook):
            fold([book], [{"url": book.source_url, "calibre_id": 1559, "chapter_count": 5,
                           "stub": None, "story_url": None, "story_status": None,
                           "error": None, "phase": "done"}])
        assert book.title == "My Name"

    @pytest.mark.parametrize("title,expected", [
        ("", True), ("https://a/b", True), ("http://a/b/c?d=1", True),
        ("A Real Title", False), ("Why https://x matters", False),
    ])
    def test_placeholder_detection(self, in_memory_db, title, expected):
        b = _book(in_memory_db, title=title or "x")
        b.title = title
        assert scheduler._is_placeholder_title(b) is expected

    def test_unreadable_library_does_not_break_the_fold(self, in_memory_db):
        b = _book(in_memory_db, title="https://s/x", calibre_id=5)
        with patch("app.calibre.adapter.CalibreAdapter.get_book", side_effect=OSError("db gone")):
            scheduler._sync_title(b)  # must not raise
        assert b.title == "https://s/x"


# ── story identity / dedupe / normalisation ──────────────────────────────────────────────────

class TestStoryIdentity:
    def test_fetcher_and_app_keys_agree(self):
        urls = [
            "https://www.fanfiction.net/s/13051824", "https://www.fanfiction.net/s/13051824/1/New-Blood",
            "https://forums.spacebattles.com/threads/elixir.1258552/page-3",
            "https://forums.spacebattles.com/threads/elixir.1258552/reader/",
            "http://archiveofourown.org/works/19992961/chapters/1",
            "https://www.royalroad.com/fiction/186926/leave-luck-to-heaven-litrpg/",
            "https://royalroad.com/fiction/186926", "https://x.example/no-digits/here/", "", None,
        ]
        for u in urls:
            assert story_key(u) == fetcher._story_key(u), u

    def test_normalize(self):
        assert normalize_story_url(
            " https://www.royalroad.com/fiction/186926/leave-luck-to-heaven-litrpg/#c "
        ) == "https://www.royalroad.com/fiction/186926"
        assert normalize_story_url(
            "https://forums.spacebattles.com/threads/crusader.1234/reader/page-2"
        ) == "https://forums.spacebattles.com/threads/crusader.1234/"
        assert normalize_story_url("https://www.fanfiction.net/s/1/2/T") == "https://www.fanfiction.net/s/1/2/T"

    def test_same_story_other_spelling_is_a_duplicate(self, in_memory_db):
        cid = _channel_id(in_memory_db)
        first = _add_tracked_story(in_memory_db, "https://www.royalroad.com/fiction/186926", "", cid)
        assert first is not None
        # pasted with the slug, or after the fetcher canonicalised an FFN url
        assert _add_tracked_story(
            in_memory_db, "https://www.royalroad.com/fiction/186926/leave-luck/", "", cid) is None
        ffn = _add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824/1/New-Blood", "", cid)
        assert ffn is not None
        assert _add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "", cid) is None

    def test_added_url_is_normalised_and_feed_inferred(self, in_memory_db):
        b = _add_tracked_story(
            in_memory_db, "https://forums.spacebattles.com/threads/crusader.1234/reader/", "",
            _channel_id(in_memory_db))
        assert b.source_url == "https://forums.spacebattles.com/threads/crusader.1234/"
        assert b.feed_url == "https://forums.spacebattles.com/threads/crusader.1234/threadmarks.rss"


# ── fetcher is told which Calibre entry to update ────────────────────────────────────────────

class TestPinnedCalibreIds:
    def test_pinned_when_all_books_for_url_agree(self, in_memory_db):
        a = _book(in_memory_db, source_url="https://a/1", calibre_id=11)
        b = _book(in_memory_db, source_url="https://b/2", calibre_id=None)
        assert scheduler._pinned_calibre_ids([a, b]) == {"https://a/1": 11}

    def test_ambiguous_url_not_pinned(self, in_memory_db):
        a = _book(in_memory_db, source_url="https://a/1", calibre_id=11)
        b = _book(in_memory_db, source_url="https://a/1", calibre_id=12)
        assert scheduler._pinned_calibre_ids([a, b]) == {}

    def test_submit_fetch_payload(self):
        with patch("app.fetch.client.httpx.post") as post:
            post.return_value.json.return_value = {"job_id": "j"}
            assert client.submit_fetch(["https://a/1", "https://b/2"], {"https://a/1": 11}) == "j"
        assert post.call_args.kwargs["json"] == {
            "urls": ["https://a/1", "https://b/2"], "calibre_ids": {"https://a/1": 11}}

    def test_submit_fetch_without_pins_keeps_the_old_payload(self):
        with patch("app.fetch.client.httpx.post") as post:
            post.return_value.json.return_value = {"job_id": "j"}
            client.submit_fetch(["https://a/1"])
        assert post.call_args.kwargs["json"] == {"urls": ["https://a/1"]}


# ── apply_result: which EPUB, and whose chapter count ────────────────────────────────────────

def _raw(calibre_id, count=None, stub=None, url="https://s.example.com/story"):
    return {"url": url, "calibre_id": calibre_id, "chapter_count": count, "stub": stub,
            "story_url": None, "story_status": None, "error": None, "phase": "done"}


def _adapter_for(path):
    adapter = MagicMock()
    adapter.get_book.return_value = MagicMock()
    adapter.epub_path.return_value = path
    return adapter


class TestApplyResultIdentityAndCounts:
    def test_total_is_the_chapterizer_count_not_the_fetchers(self, in_memory_db):
        epub = make_epub()  # 3 content chapters
        book = _book(in_memory_db, calibre_id=4646, total_chapters=3, cursor_chapter_index=3)
        with patch("app.fetch.client._calibre_adapter", return_value=_adapter_for(epub)):
            res = apply_result(book, _raw(4646, count=29))  # fetcher counted another EPUB: 29
        assert book.total_chapters == 3
        assert res.new_chapters is False
        assert book.fresh_from_index is None  # 29 > 3 must not conjure "26 fresh chapters"

    def test_growth_is_detected_by_chapterizer_counts(self, in_memory_db):
        epub = make_epub(chapters=[("c%d" % i, "<p>word </p>" * 80) for i in range(5)])
        book = _book(in_memory_db, calibre_id=4646, total_chapters=3, cursor_chapter_index=3)
        with patch("app.fetch.client._calibre_adapter", return_value=_adapter_for(epub)):
            res = apply_result(book, _raw(4646, count=99))
        assert (book.total_chapters, book.fresh_from_index, res.new_chapters) == (5, 3, True)

    def test_falls_back_to_fetcher_count_when_epub_unreadable(self, in_memory_db):
        book = _book(in_memory_db, calibre_id=4646, total_chapters=3)
        res = apply_result(book, _raw(4646, count=7))  # no library here → can't chapterize
        assert (book.total_chapters, res.new_chapters) == (7, True)

    def test_result_for_another_entry_is_ignored(self, in_memory_db):
        epub = make_epub()
        book = _book(in_memory_db, calibre_id=4629, total_chapters=3, cursor_chapter_index=2)
        stub = {"old": 150, "new": 139, "old_urls": None, "new_urls": None}
        with patch("app.fetch.client._calibre_adapter", return_value=_adapter_for(epub)):
            apply_result(book, _raw(4596, count=139, stub=stub))
        assert book.calibre_id == 4629                       # beacon keeps reading its own entry
        assert (book.total_chapters, book.cursor_chapter_index) == (3, 2)  # stub NOT applied
        assert book.last_fetch_status.startswith("warn:")

    def test_follows_the_new_entry_if_ours_was_deleted(self, in_memory_db):
        book = _book(in_memory_db, calibre_id=4629)
        adapter = MagicMock()
        adapter.get_book.return_value = None  # metadata.db readable, entry gone
        with patch("app.fetch.client._calibre_adapter", return_value=adapter):
            apply_result(book, _raw(4700, count=4))
        assert book.calibre_id == 4700


# ── trigger GUIDs: only "seen" once chapters landed ──────────────────────────────────────────

def _feed(*guids):
    parsed = MagicMock()
    parsed.entries = [FeedParserDict(id=g, link=f"https://s/{g}", title=g) for g in guids]
    return parsed


class TestTriggerGuid:
    def test_poll_does_not_mark_seen(self, in_memory_db):
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1)
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("new")), \
             patch("app.scheduler.submit_and_track") as submit:
            poller.poll_all_feeds(in_memory_db)
        assert b.last_seen_guid == "old"
        assert submit.call_args[0][2] == {b.id: "new"}

    def test_book_with_fetch_in_flight_is_not_requeued(self, in_memory_db):
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1,
                  last_fetch_status="fetching: downloading")
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("new")), \
             patch("app.scheduler.submit_and_track") as submit:
            assert poller.poll_all_feeds(in_memory_db) == 0
        submit.assert_not_called()
        assert b.last_seen_guid == "old"

    def test_finished_story_entry_is_simply_marked_seen(self, in_memory_db):
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1,
                  story_status="Completed")
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("new")), \
             patch("app.scheduler.submit_and_track") as submit:
            poller.poll_all_feeds(in_memory_db)
        submit.assert_not_called()
        assert b.last_seen_guid == "new"

    def test_304_means_no_change(self, in_memory_db):
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1)
        resp = MagicMock(status_code=304)
        stream = MagicMock()
        stream.__enter__.return_value = resp
        http = MagicMock()
        http.__enter__.return_value.stream.return_value = stream
        with patch("app.ongoing.poller.httpx.Client", return_value=http), \
             patch("app.scheduler.submit_and_track") as submit:
            assert poller.poll_all_feeds(in_memory_db) == 0
        resp.raise_for_status.assert_not_called()
        submit.assert_not_called()
        assert b.last_seen_guid == "old"

    def test_landed_chapters_mark_it_seen(self, in_memory_db, fold):
        b = _book(in_memory_db, calibre_id=4646, total_chapters=3, last_seen_guid="old")
        fold([b], [_raw(4646, count=5)], guids=["new"])
        assert b.last_seen_guid == "new"

    def test_fruitless_fetch_is_retried_then_written_off(self, in_memory_db, fold):
        b = _book(in_memory_db, calibre_id=4646, total_chapters=5, last_seen_guid="old")
        fold([b], [_raw(4646, count=5)], guids=["new"], job_id="j1")
        assert b.last_seen_guid == "old"                              # nothing new → retry next cycle
        fold([b], [_raw(4646, count=5)], guids=["new"], job_id="j2")
        assert b.last_seen_guid == "old"
        fold([b], [_raw(4646, count=5)], guids=["new"], job_id="j3")  # 3rd fruitless fetch
        assert b.last_seen_guid == "new"                              # …stop refetching it forever
        assert get_value(in_memory_db, TRIGGER_RETRY_PREFIX + str(b.id)) is None

    def test_failed_fetch_keeps_the_entry_unseen(self, in_memory_db, fold):
        b = _book(in_memory_db, calibre_id=4646, total_chapters=5, last_seen_guid="old")
        raw = _raw(4646)
        raw["error"] = "failed after 3 attempts: 503"
        fold([b], [raw], guids=["new"])
        assert b.last_seen_guid == "old"
        assert json.loads(get_value(in_memory_db, TRIGGER_RETRY_PREFIX + str(b.id))) == {"guid": "new", "n": 1}

    def test_a_different_guid_restarts_the_count(self, in_memory_db, fold):
        b = _book(in_memory_db, calibre_id=4646, total_chapters=5, last_seen_guid="old")
        fold([b], [_raw(4646, count=5)], guids=["g1"], job_id="j1")
        fold([b], [_raw(4646, count=5)], guids=["g2"], job_id="j2")
        assert json.loads(get_value(in_memory_db, TRIGGER_RETRY_PREFIX + str(b.id))) == {"guid": "g2", "n": 1}
