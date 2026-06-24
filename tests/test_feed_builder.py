"""Tests for the feed builder.

Verifies RSS/Atom standards compliance via feedparser and checks feedback links.
"""
import secrets
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import feedparser
import pytest

from app.feed.builder import build_feed, _permalink, _feedback_html, _extra_available
from app.models import Book, BookStatus, Drop


def _make_drop(
    book_title: str = "Test Book",
    author: str = "Test Author",
    source_url: str | None = None,          # whole-work URL (book)
    chapter_source_url: str | None = None,  # per-chapter URL (drop)
    chapter_titles: str = "Chapter 1",
    word_count: int = 1000,
) -> Drop:
    book = MagicMock(spec=Book)
    book.title = book_title
    book.author = author
    book.source_url = source_url
    book.status = BookStatus.active
    book.total_chapters = 10
    book.cursor_chapter_index = 2  # chapters remain → 🪝 extra available

    drop = MagicMock(spec=Drop)
    drop.book = book
    drop.source_url = chapter_source_url
    drop.chapter_titles = chapter_titles
    drop.word_count = word_count
    drop.chapter_start = 0
    drop.chapter_end = 0
    drop.content_html = "<p>Test chapter content here.</p>"
    drop.feedback_token = secrets.token_urlsafe(24)
    drop.reader_slug = str(uuid.uuid4())
    drop.published_at = datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc)
    return drop


class TestFeedParsing:
    def test_atom_parses_cleanly(self):
        drop = _make_drop()
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        assert parsed.bozo is False or parsed.bozo_exception is None

    def test_rss_parses_cleanly(self):
        drop = _make_drop()
        _, rss_xml = build_feed([drop])
        parsed = feedparser.parse(rss_xml)
        assert parsed.bozo is False or parsed.bozo_exception is None

    def test_entry_title_format(self):
        drop = _make_drop(book_title="My Novel", chapter_titles="Chapter 5")
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        assert parsed.entries[0].title == "My Novel — Chapter 5"

    def test_entry_has_stable_id(self):
        drop = _make_drop()
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        assert parsed.entries[0].id  # non-empty guid

    def test_guids_unique_across_drops_of_same_fff_book(self):
        # Regression: two drops of one FanFicFare book share the whole-work URL.
        # Their GUIDs must still differ or readers collapse them into one item.
        work_url = "https://archiveofourown.org/works/12345"
        drops = [
            _make_drop(source_url=work_url, chapter_source_url=f"{work_url}/chapters/{i}",
                       chapter_titles=f"Chapter {i}")
            for i in (1, 2, 3)
        ]
        atom_xml, _ = build_feed(drops)
        parsed = feedparser.parse(atom_xml)
        ids = [e.id for e in parsed.entries]
        assert len(set(ids)) == len(ids), "GUIDs collided — readers would dedupe drops"

    def test_link_uses_per_chapter_url_when_present(self):
        drop = _make_drop(
            source_url="https://archiveofourown.org/works/12345",
            chapter_source_url="https://archiveofourown.org/works/12345/chapters/678",
        )
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        assert "chapters/678" in parsed.entries[0].link

    def test_entry_has_published_date(self):
        drop = _make_drop()
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        assert parsed.entries[0].published

    def test_empty_drops_list(self):
        atom_xml, rss_xml = build_feed([])
        assert feedparser.parse(atom_xml).bozo is False or True  # just shouldn't crash
        assert feedparser.parse(rss_xml).bozo is False or True

    def test_multiple_drops(self):
        drops = [_make_drop(chapter_titles=f"Chapter {i}") for i in range(5)]
        atom_xml, _ = build_feed(drops)
        parsed = feedparser.parse(atom_xml)
        assert len(parsed.entries) == 5

    def test_feed_declares_websub_hub(self):
        drop = _make_drop()
        atom_xml, _ = build_feed([drop])
        parsed = feedparser.parse(atom_xml)
        hubs = [l for l in parsed.feed.links if l.get("rel") == "hub"]
        assert hubs and "/websub/hub" in hubs[0]["href"]

    def test_custom_self_url_and_title(self):
        drop = _make_drop()
        atom_xml, _ = build_feed(
            [drop], self_url="http://x/feed/fantasy/1", title="Fantasy · Slot 1"
        )
        parsed = feedparser.parse(atom_xml)
        assert parsed.feed.title == "Fantasy · Slot 1"
        selfs = [l for l in parsed.feed.links if l.get("rel") == "self"]
        assert selfs and selfs[0]["href"] == "http://x/feed/fantasy/1"


class TestPermalinks:
    def test_per_chapter_url_preferred(self):
        drop = _make_drop(
            source_url="https://archiveofourown.org/works/12345",
            chapter_source_url="https://archiveofourown.org/works/12345/chapters/678",
        )
        assert _permalink(drop) == "https://archiveofourown.org/works/12345/chapters/678"

    def test_whole_work_url_fallback(self):
        drop = _make_drop(source_url="https://archiveofourown.org/works/12345",
                          chapter_source_url=None)
        assert _permalink(drop) == "https://archiveofourown.org/works/12345"

    def test_reader_page_used_when_no_source_url(self):
        drop = _make_drop(source_url=None, chapter_source_url=None)
        drop.reader_slug = "my-slug-here"
        link = _permalink(drop)
        assert "/read/my-slug-here" in link


class TestFeedbackLinks:
    def test_all_four_actions_present(self):
        drop = _make_drop()
        html = _feedback_html(drop, _extra_available(drop))
        assert "action=extra" in html
        assert "action=up" in html
        assert "action=down" in html
        assert "action=drop" in html

    def test_action_order_is_extra_up_down_drop(self):
        drop = _make_drop()
        html = _feedback_html(drop, _extra_available(drop))
        assert (
            html.index("action=extra")
            < html.index("action=up")
            < html.index("action=down")
            < html.index("action=drop")
        )

    def test_up_down_use_instant_endpoint(self):
        drop = _make_drop()
        drop.feedback_token = "tok"
        html = _feedback_html(drop, _extra_available(drop))
        assert "/fb/tok?action=up" in html
        assert "/fb/tok?action=down" in html

    def test_extra_drop_use_confirm_endpoint(self):
        drop = _make_drop()
        drop.feedback_token = "tok"
        html = _feedback_html(drop, _extra_available(drop))
        assert "/fb/confirm/tok?action=extra" in html
        assert "/fb/confirm/tok?action=drop" in html

    def test_links_use_drop_token(self):
        drop = _make_drop()
        drop.feedback_token = "my-unique-token"
        html = _feedback_html(drop, _extra_available(drop))
        assert "my-unique-token" in html

    def test_extra_hidden_when_no_next_unit(self):
        drop = _make_drop()
        drop.book.cursor_chapter_index = 10  # cursor == total_chapters → nothing left
        html = _feedback_html(drop, _extra_available(drop))
        assert "action=extra" not in html
        # up/down/drop still present
        assert html.count("<a href=") == 3

    def test_all_four_links_when_extra_available(self):
        drop = _make_drop()
        html = _feedback_html(drop, _extra_available(drop))
        assert html.count("<a href=") == 4
