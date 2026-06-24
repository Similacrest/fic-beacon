"""Tests for ongoing-serial syndication.

Covers OPML parsing, poller helpers, and the buffer-and-release pipeline (the poller
buffers new entries; the planner releases them, weighted in the channel budget).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from feedparser.util import FeedParserDict

from app.ongoing.opml import parse_opml
from app.ongoing.poller import (
    poll_all_feeds, poll_source, _entry_published, _entry_content, _count_words,
)
from app.models import Book, BookKind, BookStatus, OngoingEntry


# ── OPML parsing ─────────────────────────────────────────────────────────────

OPML_SIMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<opml version="2.0">
  <head><title>My feeds</title></head>
  <body>
    <outline text="Practical Villainy" xmlUrl="https://pv.example.com/feed" type="rss"/>
    <outline text="He Who Fights With Monsters" xmlUrl="https://hwfwm.example.com/atom" type="rss"/>
  </body>
</opml>"""

OPML_NESTED = b"""<?xml version="1.0"?>
<opml version="1.0">
  <body>
    <outline text="Fiction" title="Fiction">
      <outline text="Serial A" xmlUrl="https://a.example.com/feed"/>
      <outline text="Serial B" xmlUrl="https://b.example.com/feed"/>
    </outline>
  </body>
</opml>"""

OPML_CASE_INSENSITIVE = b"""<opml><body>
  <outline text="T" xmlurl="https://lowercase.example.com/feed"/>
</body></opml>"""


class TestOPMLParsing:
    def test_simple_opml(self):
        results = parse_opml(OPML_SIMPLE)
        assert len(results) == 2
        assert ("Practical Villainy", "https://pv.example.com/feed") in results

    def test_nested_opml_flattened(self):
        urls = [r[1] for r in parse_opml(OPML_NESTED)]
        assert "https://a.example.com/feed" in urls
        assert "https://b.example.com/feed" in urls

    def test_xmlurl_case_insensitive(self):
        assert parse_opml(OPML_CASE_INSENSITIVE)[0][1] == "https://lowercase.example.com/feed"

    def test_invalid_xml_raises(self):
        with pytest.raises(ValueError, match="Invalid OPML"):
            parse_opml(b"not xml at all <<<")

    def test_empty_opml_returns_empty(self):
        assert parse_opml(b"<opml><body></body></opml>") == []


# ── Poller helpers ────────────────────────────────────────────────────────────

class TestPollerHelpers:
    def test_entry_published_parsed(self):
        entry = MagicMock()
        entry.published_parsed = (2025, 6, 1, 10, 0, 0, 0, 0, 0)
        entry.updated_parsed = None
        assert _entry_published(entry).year == 2025

    def test_entry_published_fallback_to_updated(self):
        entry = MagicMock()
        entry.published_parsed = None
        entry.updated_parsed = (2025, 5, 15, 8, 0, 0, 0, 0, 0)
        assert _entry_published(entry).month == 5

    def test_entry_published_none_when_absent(self):
        entry = MagicMock()
        entry.published_parsed = None
        entry.updated_parsed = None
        assert _entry_published(entry) is None

    def test_entry_word_count_from_content(self):
        entry = MagicMock()
        entry.content = [{"value": "<p>" + " ".join(["word"] * 200) + "</p>"}]
        assert _count_words(_entry_content(entry)) == 200


# ── Buffering ─────────────────────────────────────────────────────────────────

def _ongoing_source(db, feed_url="https://s.example.com/feed"):
    from app.models import Channel
    channel_id = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    src = Book(
        kind=BookKind.ongoing, feed_url=feed_url, title="Serial", author="(ongoing)",
        status=BookStatus.active, queue_position=1, channel_id=channel_id,
    )
    db.add(src)
    db.flush()
    return src


def _mock_feed(*entries):
    parsed = MagicMock()
    parsed.entries = list(entries)
    return parsed


def _entry(guid, words, when=(2025, 6, 1, 10, 0, 0, 0, 0, 0)):
    # FeedParserDict mirrors real feedparser entries (attribute + key access).
    return FeedParserDict(
        id=guid,
        link=f"https://s.example.com/{guid}",
        title=guid,
        content=[FeedParserDict(value="<p>" + " ".join(["word"] * words) + "</p>")],
        published_parsed=when,
    )


class TestBuffering:
    def test_poll_buffers_new_entries(self, in_memory_db):
        src = _ongoing_source(in_memory_db)
        feed = _mock_feed(_entry("c1", 100), _entry("c2", 200))
        with patch("app.ongoing.poller.feedparser.parse", return_value=feed):
            new = poll_source(in_memory_db, src)
        assert new == 2
        entries = in_memory_db.query(OngoingEntry).filter_by(source_id=src.id).all()
        assert {e.guid for e in entries} == {"c1", "c2"}
        assert all(e.released is False for e in entries)
        assert {e.word_count for e in entries} == {100, 200}

    def test_poll_dedupes_by_guid(self, in_memory_db):
        src = _ongoing_source(in_memory_db)
        feed1 = _mock_feed(_entry("c1", 100))
        feed2 = _mock_feed(_entry("c1", 100), _entry("c2", 150))
        with patch("app.ongoing.poller.feedparser.parse", return_value=feed1):
            poll_source(in_memory_db, src)
        with patch("app.ongoing.poller.feedparser.parse", return_value=feed2):
            new = poll_source(in_memory_db, src)
        assert new == 1  # only c2 is new
        assert in_memory_db.query(OngoingEntry).count() == 2

    def test_poll_all_skips_epub_and_survives_errors(self, in_memory_db):
        src = _ongoing_source(in_memory_db, feed_url="https://ok.example.com/feed")
        # An epub book must be ignored by the poller.
        in_memory_db.add(Book(calibre_id=1, kind=BookKind.epub, title="E", author="A",
                              status=BookStatus.active, queue_position=2,
                              channel_id=src.channel_id))
        in_memory_db.flush()
        with patch("app.ongoing.poller.feedparser.parse", return_value=_mock_feed(_entry("x", 50))):
            new = poll_all_feeds(in_memory_db)
        assert new == 1


class TestOngoingDrops:
    def test_planner_releases_buffered_entries(self, in_memory_db):
        from app.planner.planner import run_drop_cycle
        from app.models import Drop
        src = _ongoing_source(in_memory_db)
        # Three buffered chapters, ~100 words each; small budget releases a subset.
        from app.models import Channel
        channel = in_memory_db.get(Channel, src.channel_id)
        channel.budget_words = 200  # 100-word chapters → exactly 2 fit, 1 rolls over
        for i in range(1, 4):
            in_memory_db.add(OngoingEntry(
                source_id=src.id, guid=f"c{i}", title=f"Ch {i}",
                link=f"https://s/{i}", content_html="<p>x</p>", word_count=100,
                published_at=datetime(2025, 6, i, tzinfo=timezone.utc), released=False,
            ))
        in_memory_db.commit()

        drops = run_drop_cycle(in_memory_db, Path("/fake"))

        assert drops, "expected at least one ongoing drop"
        assert all(d.feed_key == "ongoing" for d in drops)
        released = in_memory_db.query(OngoingEntry).filter_by(released=True).count()
        unreleased = in_memory_db.query(OngoingEntry).filter_by(released=False).count()
        assert released == 2 and unreleased == 1  # batched: 2 fit the budget, 1 rolls over
        # Released entries are linked to a drop.
        linked = in_memory_db.query(OngoingEntry).filter(OngoingEntry.drop_id.isnot(None)).count()
        assert linked == released
