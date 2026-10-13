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
from app.ongoing.add import add_tracked_story
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
        book = add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "",
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
        book = add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "My Name",
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
        first = add_tracked_story(in_memory_db, "https://www.royalroad.com/fiction/186926", "", cid)
        assert first is not None
        # pasted with the slug, or after the fetcher canonicalised an FFN url
        assert add_tracked_story(
            in_memory_db, "https://www.royalroad.com/fiction/186926/leave-luck/", "", cid) is None
        ffn = add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824/1/New-Blood", "", cid)
        assert ffn is not None
        assert add_tracked_story(in_memory_db, "https://www.fanfiction.net/s/13051824", "", cid) is None

    def test_added_url_is_normalised_and_feed_inferred(self, in_memory_db):
        b = add_tracked_story(
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
        from datetime import datetime, timezone
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1,
                  last_fetch_status="fetching: downloading", last_fetch_at=datetime.now(timezone.utc))
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


# ── logging survives the startup migration ───────────────────────────────────────────────────

class TestLoggingSurvivesMigrations:
    def test_embedded_alembic_run_leaves_logging_alone(self, tmp_path, monkeypatch):
        import logging

        from alembic import command

        from app import database
        from app.config import settings
        monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/m.db")
        app_logger = logging.getLogger("app.fetch.client")
        uvicorn_logger = logging.getLogger("uvicorn.access")
        root = logging.getLogger()
        app_logger.setLevel(logging.INFO)
        before = (root.level, list(root.handlers), app_logger.level, uvicorn_logger.level)

        command.upgrade(database._alembic_config(), "head")

        assert not app_logger.disabled and not uvicorn_logger.disabled
        assert (root.level, list(root.handlers), app_logger.level, uvicorn_logger.level) == before
        assert app_logger.isEnabledFor(logging.INFO)

    def test_cli_logging_config_does_not_disable_existing_loggers(self, tmp_path, monkeypatch):
        import logging

        from alembic import command
        from alembic.config import Config

        from app.config import settings
        monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/c.db")
        cfg = Config("alembic.ini")  # what the `alembic` CLI builds: no configure_logger attribute
        cfg.set_main_option("script_location", "alembic")
        probe = logging.getLogger("app.probe.cli")
        saved_root = (logging.getLogger().level, list(logging.getLogger().handlers))
        try:
            command.upgrade(cfg, "head")
            assert not probe.disabled
        finally:
            root = logging.getLogger()
            root.setLevel(saved_root[0])
            root.handlers[:] = saved_root[1]


# ── an EPUB that chapterizes to nothing must not hold a slot ─────────────────────────────────

def _epub_with_nav(nav_hrefs: list[str], tmp_path, chapters=3, words=120, name="toc.epub"):
    """A make_epub EPUB whose nav.xhtml is rewritten to point at `nav_hrefs`."""
    import zipfile
    src = make_epub(chapters=[(f"Chapter {i}", f"<p>{'word ' * words}</p>") for i in range(1, chapters + 1)])
    dst = tmp_path / name
    items = "".join(f'<li><a href="{h}">Real {i}</a></li>' for i, h in enumerate(nav_hrefs, 1))
    nav = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
        f'<nav epub:type="toc"><ol>{items}</ol></nav></body></html>'
    )
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = nav.encode() if info.filename == "OEBPS/nav.xhtml" else zin.read(info.filename)
            zout.writestr(info, data)
    return dst


class TestChapterizerSurvivesAnUnusableToc:
    def test_toc_pointing_nowhere_falls_back_to_the_spine(self, tmp_path):
        from app.epub.chapterizer import chapterize
        epub = _epub_with_nav(["Text/gone1.xhtml", "Text/gone2.xhtml"], tmp_path)
        assert len(chapterize(epub)) == 3  # was 0: TOC non-empty but resolved to nothing

    def test_toc_in_another_directory_resolves_by_basename(self, tmp_path):
        from app.epub.chapterizer import chapterize
        epub = _epub_with_nav(
            ["../Text/chapter001.xhtml", "../Text/chapter002.xhtml", "../Text/chapter003.xhtml"], tmp_path)
        chapters = chapterize(epub)
        assert [c.title for c in chapters] == ["Real 1", "Real 2", "Real 3"]  # TOC titles kept


class TestEmptyBacklogBookIsSetAside:
    def _setup(self, db, tmp_path):
        from app.models import BookStatus
        from tests.test_planner import _make_book
        broken = _make_book(db, calibre_id=4639, title="Yumi", status=BookStatus.active, queue_position=1)
        good = _make_book(db, calibre_id=4640, title="Next", status=BookStatus.queued, queue_position=2)
        # A TOC that resolves to nothing over a document too short to count as a chapter: the TOC
        # pass finds nothing and so does the spine fallback — the shape that stranded book 4639.
        empty = _epub_with_nav(["Text/gone.xhtml"], tmp_path, chapters=1, words=3, name="empty.epub")
        full = make_epub(chapters=[(f"C{i}", f"<p>{'word ' * 300}</p>") for i in range(1, 4)])
        return broken, good, empty, full

    def test_slot_passes_to_the_next_book_and_reason_is_recorded(self, in_memory_db, tmp_path):
        from pathlib import Path

        from app.planner.planner import run_release_cycle
        from app.state import BOOK_NOTE_PREFIX, get_value
        broken, good, empty, full = self._setup(in_memory_db, tmp_path)
        paths = {4639: empty, 4640: full}

        adapter = MagicMock()
        adapter.get_book.side_effect = lambda cid: MagicMock(calibre_id=cid)
        adapter.epub_path.side_effect = lambda cb: paths[cb.calibre_id]
        with patch("app.planner.planner.CalibreAdapter", return_value=adapter):
            drops = run_release_cycle(in_memory_db, Path("/fake"))

        assert broken.paused and broken.status == BookStatus.queued and broken.slot_index is None
        assert "no readable chapters" in get_value(in_memory_db, BOOK_NOTE_PREFIX + str(broken.id))
        assert good.cursor_chapter_index > 0   # promoted out of the queue and streamed
        assert [d.book_id for d in drops] == [good.id]   # the freed slot streamed this very cycle

    def test_resume_clears_the_note(self, in_memory_db):
        from app.planner.planner import pause_book, resume_book
        from app.state import BOOK_NOTE_PREFIX, get_value, set_value
        b = _book(in_memory_db, tracked=False, calibre_id=9)
        set_value(in_memory_db, BOOK_NOTE_PREFIX + str(b.id), "why")
        pause_book(in_memory_db, b)
        resume_book(in_memory_db, b)
        assert get_value(in_memory_db, BOOK_NOTE_PREFIX + str(b.id)) is None

    def test_tracked_story_with_no_chapters_yet_is_left_alone(self, in_memory_db, tmp_path):
        from app.planner.planner import _set_aside_empty_backlog
        b = _book(in_memory_db, calibre_id=11, total_chapters=0)  # tracked, still downloading
        adapter = MagicMock()
        assert _set_aside_empty_backlog(in_memory_db, [b], adapter) is False
        assert not b.paused


# ── feedback links on their own origin; legacy image hosts ───────────────────────────────────

FB = "https://fb.ilac.rest"


class TestFeedbackOrigin:
    def _drop(self):
        from tests.test_feed_builder import _make_drop
        return _make_drop(chapter_source_url=None, source_url=None)

    def test_feedback_links_use_the_feedback_origin(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import _feedback_html, _permalink
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", FB + "/")
        drop = self._drop()
        html = _feedback_html(drop, True)
        assert f'href="{FB}/fb/{drop.feedback_token}?action=up"' in html
        assert f'href="{FB}/fb/confirm/{drop.feedback_token}?action=extra"' in html
        assert "funnel.example/fb" not in html
        # with no reader origin set, /read links follow the feedback origin
        assert _permalink(drop) == f"{FB}/read/{drop.reader_slug}"

    def test_defaults_to_base_url(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import _feedback_html
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", None)
        drop = self._drop()
        assert f'href="https://funnel.example/fb/{drop.feedback_token}?action=up"' in _feedback_html(drop, False)

    def test_reader_origin_overrides_and_defaults(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import _permalink
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", None)
        monkeypatch.setattr(settings, "reader_base_url", None)
        drop = self._drop()
        assert _permalink(drop) == f"https://funnel.example/read/{drop.reader_slug}"
        monkeypatch.setattr(settings, "reader_base_url", "https://read.example/")
        assert _permalink(drop) == f"https://read.example/read/{drop.reader_slug}"
        assert "https://read.example" in settings.image_alias_bases

    def test_feed_keeps_public_images_and_hub_with_reader_origin(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import build_feed
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", FB)
        monkeypatch.setattr(settings, "reader_base_url", None)
        drop = self._drop()
        drop.content_html = f'<p>x</p><img src="{FB}/img/7/images/a.png">'
        atom, _rss = build_feed([drop], secret="feedsecret")
        text = atom.decode() if isinstance(atom, bytes) else atom
        assert f"{FB}/read/{drop.reader_slug}" in text
        assert "https://funnel.example/img/7/images/a.png?sig=" in text
        assert f"{FB}/img/" not in text
        assert "https://funnel.example/websub/hub" in text

    def test_feed_urls_do_not_move(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import build_feed
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", FB)
        atom, _rss = build_feed([self._drop()])
        text = atom.decode() if isinstance(atom, bytes) else atom
        assert 'rel="hub"' in text and "https://funnel.example/websub/hub" in text
        assert f"{FB}/websub" not in text

    @pytest.fixture
    def gated_app(self, monkeypatch):
        """The real app + proxy gate with a token set, as deployed behind the Funnel.

        Yields (client, db). StaticPool: the TestClient serves requests on another thread and must
        see the same in-memory DB the test seeded."""
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.config import settings
        from app.database import ensure_default_channel, get_db
        from app.main import app
        from app.models import Base, Config
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        db.add(Config(id=1, wpm=250, feed_secret="test-secret"))
        db.flush()
        ensure_default_channel(db)
        db.commit()
        monkeypatch.setattr(settings, "proxy_token", "s3cret")
        monkeypatch.setattr(settings, "admin_open", False)
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", FB)
        app.dependency_overrides[get_db] = lambda: db
        yield TestClient(app, raise_server_exceptions=False), db
        app.dependency_overrides.pop(get_db, None)
        db.close()
        engine.dispose()

    def _seed(self, db):
        from app.models import Drop
        book = _book(db, tracked=False, calibre_id=3, total_chapters=5, cursor_chapter_index=1)
        drop = Drop(book_id=book.id, channel_id=book.channel_id, feed_key="1", word_count=10,
                    chapter_start=0, chapter_end=0, chapter_titles="C1", content_html="<p>x</p>",
                    feedback_token="tok-fb", reader_slug="slug-fb")
        db.add(drop)
        db.commit()
        return book, drop

    def test_confirm_form_posts_from_the_feedback_host(self, gated_app):
        client, in_memory_db = gated_app
        book, _ = self._seed(in_memory_db)
        headers = {"Host": "fb.ilac.rest", "Origin": FB, "Sec-Fetch-Site": "same-origin"}
        page = client.get("/fb/confirm/tok-fb?action=drop", headers=headers)
        assert page.status_code == 200
        assert 'action="/fb/confirm/tok-fb"' in page.text  # relative: posts back to whichever host served it
        resp = client.post("/fb/confirm/tok-fb", data={"action": "drop"}, headers=headers,
                           follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/fb/done"
        in_memory_db.refresh(book)
        assert book.status == BookStatus.dropped

    def test_instant_links_work_on_the_feedback_host(self, gated_app):
        client, in_memory_db = gated_app
        book, _ = self._seed(in_memory_db)
        resp = client.get("/fb/tok-fb?action=up", headers={"Host": "fb.ilac.rest"})
        assert resp.status_code == 200
        in_memory_db.refresh(book)
        assert book.thumbs_up == 1

    def test_read_page_on_the_reader_host_uses_relative_urls(self, gated_app):
        client, in_memory_db = gated_app
        _, drop = self._seed(in_memory_db)
        drop.content_html = ('<p>x</p><img src="https://funnel.example/img/3/images/a.png">'
                             '<img src="https://cdn.example/img/3/b.png">')
        in_memory_db.commit()
        page = client.get("/read/slug-fb", headers={"Host": "fb.ilac.rest"})
        assert page.status_code == 200
        assert 'src="/img/3/images/a.png?sig=' in page.text
        assert 'src="https://cdn.example/img/3/b.png"' in page.text  # third party: untouched
        assert 'href="/fb/tok-fb?action=up"' in page.text
        assert "funnel.example" not in page.text and FB not in page.text
        sig = page.text.split('src="/img/3/images/a.png?sig=')[1].split('"')[0]
        assert client.get(f"/img/3/images/a.png?sig={sig}",
                          headers={"Host": "fb.ilac.rest"}).status_code != 401

    def test_admin_stays_unreachable_through_the_feedback_host(self, gated_app):
        client, _ = gated_app
        resp = client.get("/admin/", headers={"Host": "fb.ilac.rest"})
        assert resp.status_code == 401


class TestLegacyImageHosts:
    SECRET = "k"

    def test_alias_hosts_are_rewritten_to_base_and_signed(self):
        from app.security import image_signature, sign_image_urls
        html = (f'<img src="{FB}/img/7/images/a.png"><img src="https://funnel.example/img/7/images/b.png">'
                '<img src="https://cdn.example/img/7/images/c.png">')
        out = sign_image_urls(html, self.SECRET, "https://funnel.example", [FB])
        sig = image_signature(self.SECRET, 7, "images/a.png")
        assert f'src="https://funnel.example/img/7/images/a.png?sig={sig}"' in out
        assert "images/b.png?sig=" in out
        assert 'src="https://cdn.example/img/7/images/c.png"' in out  # third party: untouched

    def test_without_aliases_other_hosts_are_left_alone(self):
        from app.security import sign_image_urls
        html = f'<img src="{FB}/img/7/images/a.png">'
        assert sign_image_urls(html, self.SECRET, "https://funnel.example") == html

    def test_settings_alias_bases(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "base_url", "https://funnel.example/")
        monkeypatch.setattr(settings, "feedback_base_url", FB)
        monkeypatch.setattr(settings, "reader_base_url", None)
        monkeypatch.setattr(settings, "legacy_base_urls", "https://old.example/, https://funnel.example ,")
        assert settings.image_alias_bases == [FB, "https://old.example"]

    def test_feed_renders_old_drops_with_signed_current_host_images(self, monkeypatch):
        from app.config import settings
        from app.feed.builder import _add_entry
        monkeypatch.setattr(settings, "base_url", "https://funnel.example")
        monkeypatch.setattr(settings, "feedback_base_url", FB)
        from tests.test_feed_builder import _make_drop
        drop = _make_drop()
        drop.content_html = f'<p>x</p><img src="{FB}/img/7/images/a.png">'
        fg = MagicMock()
        _add_entry(fg, drop, secret="feedsecret")
        content = fg.add_entry.return_value.content.call_args[0][0]
        assert "https://funnel.example/img/7/images/a.png?sig=" in content
        assert f"{FB}/img/" not in content


# ── orphaned fetch_job keys ──────────────────────────────────────────────────────────────────

class TestStaleFetchJobs:
    def _job(self, db, job_id, book, age_s):
        from datetime import datetime, timedelta, timezone
        set_value(db, FETCH_JOB_PREFIX + job_id, json.dumps({
            "submitted_at": (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(),
            "url_to_book": {book.source_url: [book.id]},
        }))

    def test_old_orphan_is_cleared_and_its_book_unstuck(self, in_memory_db, monkeypatch):
        monkeypatch.setattr(scheduler, "_unschedule_poll", lambda j: None)
        b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
        self._job(in_memory_db, "old", b, scheduler.settings.fetcher_job_timeout * 3)
        assert scheduler.sweep_stale_fetch_jobs(in_memory_db) == 1
        in_memory_db.flush()
        assert get_value(in_memory_db, FETCH_JOB_PREFIX + "old") is None
        assert b.last_fetch_status == "error: fetch result lost"

    def test_recent_job_and_unrelated_status_are_left_alone(self, in_memory_db, monkeypatch):
        monkeypatch.setattr(scheduler, "_unschedule_poll", lambda j: None)
        b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
        self._job(in_memory_db, "fresh", b, 60)
        c = _book(in_memory_db, calibre_id=2, source_url="https://other/s", last_fetch_status="ok")
        self._job(in_memory_db, "old2", c, scheduler.settings.fetcher_job_timeout * 3)
        assert scheduler.sweep_stale_fetch_jobs(in_memory_db) == 1
        assert get_value(in_memory_db, FETCH_JOB_PREFIX + "fresh") is not None
        assert b.last_fetch_status == "fetching…"
        assert c.last_fetch_status == "ok"  # a newer fetch's status is not overwritten

    def test_garbage_mapping_is_dropped(self, in_memory_db, monkeypatch):
        monkeypatch.setattr(scheduler, "_unschedule_poll", lambda j: None)
        set_value(in_memory_db, FETCH_JOB_PREFIX + "junk", "not json")
        assert scheduler.sweep_stale_fetch_jobs(in_memory_db) == 1

    def test_lost_fetching_status_does_not_block_feed_polling_forever(self, in_memory_db):
        from datetime import datetime, timedelta, timezone
        b = _book(in_memory_db, feed_url="https://s/feed", last_seen_guid="old", calibre_id=1,
                  last_fetch_status="fetching…",
                  last_fetch_at=datetime.now(timezone.utc) - timedelta(
                      seconds=scheduler.settings.fetcher_job_timeout * 3))
        with patch("app.ongoing.poller._fetch_feed", return_value=_feed("new")), \
             patch("app.scheduler.submit_and_track") as submit:
            assert poller.poll_all_feeds(in_memory_db) == 1
        assert list(submit.call_args[0][1]) == [b]


class TestChapterListStaysPutForWorkingBooks:
    def test_partial_toc_miss_is_not_papered_over_by_basename_matching(self, tmp_path):
        """One TOC href resolves exactly, another only by basename. The book already chapterizes
        (to 1 chapter) so its list — and every reader's cursor into it — must not change."""
        from app.epub.chapterizer import chapterize
        epub = _epub_with_nav(["chapter001.xhtml", "../Text/chapter002.xhtml"], tmp_path)
        chapters = chapterize(epub)
        assert [c.title for c in chapters] == ["Real 1"]

    def test_all_exact_toc_is_unchanged(self, tmp_path):
        from app.epub.chapterizer import chapterize
        epub = _epub_with_nav(["chapter001.xhtml", "chapter002.xhtml", "chapter003.xhtml"], tmp_path)
        assert [c.title for c in chapterize(epub)] == ["Real 1", "Real 2", "Real 3"]


class TestAbandonedFetchesCountAsAttempts:
    def _mapping(self, db, book, guid, age_s=0):
        from datetime import datetime, timedelta, timezone
        set_value(db, FETCH_JOB_PREFIX + "jobx", json.dumps({
            "submitted_at": (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(),
            "url_to_book": {book.source_url: [book.id]}, "guids": {str(book.id): guid},
        }))

    def _fold(self, db, monkeypatch, poll_result):
        from contextlib import contextmanager

        @contextmanager
        def fake_session():
            yield db
        monkeypatch.setattr(scheduler, "db_session", fake_session)
        monkeypatch.setattr(scheduler, "_unschedule_poll", lambda j: None)
        with patch("app.fetch.client.poll_fetch", return_value=poll_result):
            scheduler._poll_fetch_job("jobx")

    def _attempts(self, db, book):
        raw = get_value(db, TRIGGER_RETRY_PREFIX + str(book.id))
        return json.loads(raw)["n"] if raw else None

    def test_timeout_counts(self, in_memory_db, monkeypatch):
        b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
        self._mapping(in_memory_db, b, "g", age_s=scheduler.settings.fetcher_job_timeout * 2)
        self._fold(in_memory_db, monkeypatch, {"status": "running", "results": []})
        assert b.last_fetch_status == "error: fetch timed out"
        assert self._attempts(in_memory_db, b) == 1

    def test_missing_result_counts(self, in_memory_db, monkeypatch):
        b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
        self._mapping(in_memory_db, b, "g")
        self._fold(in_memory_db, monkeypatch, {"status": "done", "results": []})
        assert self._attempts(in_memory_db, b) == 1

    def test_fetcher_outage_does_not_count(self, in_memory_db, monkeypatch):
        b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
        self._mapping(in_memory_db, b, "g", age_s=scheduler.settings.fetcher_job_timeout * 2)
        self._fold(in_memory_db, monkeypatch, None)  # poll_fetch → None: fetcher unreachable
        assert b.last_fetch_status == "error: fetcher unreachable"
        assert self._attempts(in_memory_db, b) is None
        assert b.last_seen_guid is None

    def test_repeated_timeouts_eventually_write_the_guid_off(self, in_memory_db, monkeypatch):
        b = _book(in_memory_db, calibre_id=1, last_seen_guid="old")
        for _ in range(scheduler.TRIGGER_MAX_ATTEMPTS):
            b.last_fetch_status = "fetching…"
            self._mapping(in_memory_db, b, "g", age_s=scheduler.settings.fetcher_job_timeout * 2)
            self._fold(in_memory_db, monkeypatch, {"status": "running", "results": []})
        assert b.last_seen_guid == "g"


# ── v0.14.0 follow-up B: a fetch poll must not give up before its mapping is committed ───────

class TestFetchPollAfterCommit:
    def test_poll_is_registered_only_when_the_submit_commits(self, in_memory_db, monkeypatch):
        scheduled = []
        monkeypatch.setattr(scheduler, "_schedule_poll", scheduled.append)
        b = _book(in_memory_db, calibre_id=1)
        in_memory_db.commit()
        with patch("app.fetch.client.submit_fetch", return_value="job1"):
            assert scheduler.submit_and_track(in_memory_db, [b]) == "job1"
        assert scheduled == []          # a long release cycle hasn't committed yet: no poll
        in_memory_db.commit()
        assert scheduled == ["job1"]    # …and once it does, the poll starts
        in_memory_db.commit()
        assert scheduled == ["job1"]    # once only

    def test_orphaned_mapping_gets_its_poll_back(self, in_memory_db, monkeypatch):
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler()
        sched.start(paused=True)
        try:
            monkeypatch.setattr(scheduler, "_scheduler", sched)
            b = _book(in_memory_db, calibre_id=1, last_fetch_status="fetching…")
            set_value(in_memory_db, FETCH_JOB_PREFIX + "orphan", json.dumps({
                "submitted_at": "2026-10-13T17:40:00+00:00", "url_to_book": {b.source_url: [b.id]}}))
            scheduler._poll_misses["orphan"] = scheduler._POLL_MISS_LIMIT
            assert scheduler.ensure_fetch_polls(in_memory_db) == 1
            assert sched.get_job("fetch_poll_orphan") is not None
            assert "orphan" not in scheduler._poll_misses
            assert scheduler.ensure_fetch_polls(in_memory_db) == 0  # already polled: left alone
        finally:
            sched.shutdown(wait=False)


# ── v0.14.0 follow-up C: URL secrets never reach the access log ──────────────────────────────

class TestAccessLogMasking:
    def test_feed_token_sig_and_feedback_token_are_masked(self):
        import logging
        from app.main import _AccessLogSecretFilter
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4:5", "GET", "/feed/fantasy/1?token=s3cret&x=1", "1.1", 200),
                                None)
        assert _AccessLogSecretFilter().filter(rec)
        msg = rec.getMessage()
        assert "s3cret" not in msg and "token=***&x=1" in msg
        from app.main import _mask_url_secrets as m
        assert m("/img/4/a.png?sig=abc") == "/img/4/a.png?sig=***"
        assert m("/fb/tok123?action=up") == "/fb/***?action=up"
        assert m("/fb/confirm/tok123") == "/fb/confirm/***"
        assert m("/read/slug-1") == "/read/slug-1"

    def test_filter_is_installed_once(self):
        import logging
        from app.main import _AccessLogSecretFilter, _configure_logging
        _configure_logging()
        _configure_logging()
        access = logging.getLogger("uvicorn.access")
        assert sum(isinstance(f, _AccessLogSecretFilter) for f in access.filters) == 1
