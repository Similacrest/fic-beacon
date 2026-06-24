"""Tests for genre classification and channel matching (app/calibre/genre.py)."""
from dataclasses import dataclass

from app.calibre.genre import (
    classify_genre, effective_genres, pick_channel_id,
    FANFICTION, SCIFI, FANTASY, CLASSICAL, NONFICTION,
)


@dataclass
class _Ch:
    id: int
    genre_match: str | None


class TestClassify:
    def test_science_fiction_tags(self):
        assert classify_genre(["Science Fiction", "Strong Lead"]) == SCIFI
        assert classify_genre(["LitRPG"]) == FANTASY
        assert classify_genre(["High Fantasy", "Magic"]) == FANTASY

    def test_sci_pop_is_nonfiction_not_scifi(self):
        # The tightened sci-fi filter must not catch popular science.
        assert classify_genre(["Sci-pop"]) == NONFICTION
        assert classify_genre(["Popular Science"]) == NONFICTION

    def test_fanfiction_and_classical_and_nonfiction(self):
        assert classify_genre(["Fanfiction"]) == FANFICTION
        assert classify_genre(["Classic Literature"]) == CLASSICAL
        assert classify_genre(["Self-improvement", "Productivity"]) == NONFICTION

    def test_no_match_returns_none(self):
        assert classify_genre(["Drama", "Slice of Life"]) is None
        assert classify_genre([]) is None

    def test_order_scifi_before_nonfiction(self):
        # "Science Fiction" contains "science" (a non-fiction keyword) but sci-fi wins.
        assert classify_genre(["Science Fiction"]) == SCIFI


class TestEffectiveGenres:
    def test_manual_genres_win(self):
        assert effective_genres(["Fantasy.Rational"], ["Sci-fi"]) == ["Fantasy.Rational"]

    def test_falls_back_to_derived_bucket(self):
        assert effective_genres([], ["LitRPG"]) == [FANTASY]

    def test_empty_when_nothing_matches(self):
        assert effective_genres([], ["Drama"]) == []


class TestPickChannel:
    def test_prefix_match(self):
        channels = [_Ch(1, "Fantasy"), _Ch(2, "Sci-Fi"), _Ch(3, "Classical")]
        assert pick_channel_id(["Fantasy.Rational"], channels, default_id=99) == 1
        assert pick_channel_id(["Sci-Fi"], channels, default_id=99) == 2
        assert pick_channel_id(["Classical.Ukrainian.Poetry"], channels, default_id=99) == 3

    def test_no_partial_word_match(self):
        # "Sci-pop" must NOT match a "Sci-Fi" channel.
        channels = [_Ch(1, "Sci-Fi")]
        assert pick_channel_id(["Sci-pop"], channels, default_id=99) == 99

    def test_falls_back_to_default(self):
        channels = [_Ch(1, "Fantasy")]
        assert pick_channel_id(["Programming"], channels, default_id=42) == 42
        assert pick_channel_id([], channels, default_id=42) == 42

    def test_ignores_channels_without_genre_match(self):
        channels = [_Ch(1, None), _Ch(2, "Fantasy")]
        assert pick_channel_id(["Fantasy"], channels, default_id=99) == 2
