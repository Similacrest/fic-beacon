"""Tests for the v2 ongoing-feed balancing strategy.

Covers:
  - OPML parsing (various formats)
  - Feed polling word counting
  - _compute_budget: without target, with target, with ongoing volume
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.ongoing.opml import parse_opml
from app.ongoing.poller import poll_feed, _entry_published, _entry_word_count
from app.planner.planner import _compute_budget, _effective_budget
from app.models import BudgetMode, Config, OngoingFeed


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
    <outline text="Non-fiction" title="Non-fiction">
      <outline text="Blog C" xmlUrl="https://c.example.com/feed"/>
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
        results = parse_opml(OPML_NESTED)
        urls = [r[1] for r in results]
        assert "https://a.example.com/feed" in urls
        assert "https://b.example.com/feed" in urls
        assert "https://c.example.com/feed" in urls

    def test_xmlurl_case_insensitive(self):
        results = parse_opml(OPML_CASE_INSENSITIVE)
        assert results[0][1] == "https://lowercase.example.com/feed"

    def test_invalid_xml_raises(self):
        with pytest.raises(ValueError, match="Invalid OPML"):
            parse_opml(b"not xml at all <<<")

    def test_empty_opml_returns_empty(self):
        results = parse_opml(b"<opml><body></body></opml>")
        assert results == []


# ── Feed polling helpers ──────────────────────────────────────────────────────

class TestPollerHelpers:
    def test_entry_published_parsed(self):
        entry = MagicMock()
        entry.published_parsed = (2025, 6, 1, 10, 0, 0, 0, 0, 0)
        entry.updated_parsed = None
        dt = _entry_published(entry)
        assert dt is not None
        assert dt.year == 2025

    def test_entry_published_fallback_to_updated(self):
        entry = MagicMock()
        entry.published_parsed = None
        entry.updated_parsed = (2025, 5, 15, 8, 0, 0, 0, 0, 0)
        dt = _entry_published(entry)
        assert dt is not None
        assert dt.month == 5

    def test_entry_published_none_when_absent(self):
        entry = MagicMock()
        entry.published_parsed = None
        entry.updated_parsed = None
        assert _entry_published(entry) is None

    def test_entry_word_count_from_content(self):
        entry = MagicMock()
        entry.content = [{"value": "<p>" + " ".join(["word"] * 200) + "</p>"}]
        count = _entry_word_count(entry)
        assert count == 200

    def test_entry_word_count_from_summary(self):
        entry = MagicMock(spec=[])  # no .content attribute
        entry.summary = "<p>" + " ".join(["word"] * 100) + "</p>"
        entry.description = ""
        count = _entry_word_count(entry)
        assert count == 100

    def test_poll_feed_counts_recent_entries(self):
        now = datetime.now(timezone.utc)
        recent_time = tuple(now.timetuple())
        old_time = tuple((now - timedelta(days=2)).timetuple())

        mock_parsed = MagicMock()
        recent_entry = MagicMock()
        recent_entry.published_parsed = recent_time
        recent_entry.updated_parsed = None
        recent_entry.content = [{"value": "<p>" + " ".join(["word"] * 500) + "</p>"}]

        old_entry = MagicMock()
        old_entry.published_parsed = old_time
        old_entry.updated_parsed = None
        old_entry.content = [{"value": "<p>" + " ".join(["word"] * 999) + "</p>"}]

        mock_parsed.entries = [recent_entry, old_entry]

        with patch("app.ongoing.poller.feedparser.parse", return_value=mock_parsed):
            words = poll_feed("https://example.com/feed", window_hours=24)

        assert words == 500  # old entry excluded


# ── Budget computation ────────────────────────────────────────────────────────

class TestComputeBudget:
    def test_no_target_returns_base_budget(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = None
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == _effective_budget(cfg)

    def test_target_with_no_ongoing_feeds(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = 8000
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == 8000

    def test_target_minus_ongoing_volume(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = 10000
        in_memory_db.flush()
        in_memory_db.add(OngoingFeed(
            title="My Serial",
            feed_url="https://example.com/feed",
            estimated_words_per_cycle=3000,
            is_active=True,
        ))
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == 7000

    def test_multiple_active_feeds_summed(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = 10000
        in_memory_db.flush()
        for i, words in enumerate([2000, 1500, 500]):
            in_memory_db.add(OngoingFeed(
                title=f"Feed {i}",
                feed_url=f"https://f{i}.example.com/feed",
                estimated_words_per_cycle=words,
                is_active=True,
            ))
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == 6000  # 10000 - 4000

    def test_inactive_feeds_excluded(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = 10000
        in_memory_db.flush()
        in_memory_db.add(OngoingFeed(
            title="Inactive",
            feed_url="https://paused.example.com/feed",
            estimated_words_per_cycle=5000,
            is_active=False,
        ))
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == 10000  # inactive excluded

    def test_clamped_at_zero_when_ongoing_exceeds_target(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.target_total_words = 3000
        in_memory_db.flush()
        in_memory_db.add(OngoingFeed(
            title="Prolific",
            feed_url="https://huge.example.com/feed",
            estimated_words_per_cycle=5000,
            is_active=True,
        ))
        in_memory_db.flush()
        assert _compute_budget(in_memory_db, cfg) == 0  # clamped, never negative
