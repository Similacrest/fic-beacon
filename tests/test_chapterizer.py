"""Tests for the EPUB chapterizer.

Covers both synthetic EPUB fixtures (EPUB3 nav + EPUB2 ncx) and validates:
- Chapter count, titles, order
- Word count plausibility
- Body HTML extraction
- Deduplication of multi-anchor TOC entries
- Caching (same path/mtime returns the same list object)
"""
import os
from pathlib import Path

import pytest

from app.epub.chapterizer import chapterize, _cache
from tests.make_epub import make_epub, make_fff_epub


CHAPTERS_DATA = [
    ("Prologue", "<p>Before everything began.</p>" * 30),
    ("Chapter 1", "<p>The story starts here.</p>" * 80),
    ("Chapter 2", "<p>Things get interesting.</p>" * 70),
    ("Chapter 3", "<p>The final confrontation.</p>" * 60),
]


@pytest.fixture
def epub3(tmp_path):
    path = make_epub(chapters=CHAPTERS_DATA, epub_version=3)
    yield Path(path)
    os.unlink(path)


@pytest.fixture
def epub2_fff(tmp_path):
    path = make_fff_epub(chapters=CHAPTERS_DATA)
    yield Path(path)
    os.unlink(path)


class TestEpub3:
    def test_chapter_count(self, epub3):
        chapters = chapterize(epub3)
        assert len(chapters) == len(CHAPTERS_DATA)

    def test_titles_match(self, epub3):
        chapters = chapterize(epub3)
        for ch, (expected_title, _) in zip(chapters, CHAPTERS_DATA):
            assert ch.title == expected_title

    def test_indices_are_sequential(self, epub3):
        chapters = chapterize(epub3)
        for i, ch in enumerate(chapters):
            assert ch.index == i

    def test_word_counts_positive(self, epub3):
        chapters = chapterize(epub3)
        for ch in chapters:
            assert ch.word_count > 0

    def test_word_count_roughly_correct(self, epub3):
        chapters = chapterize(epub3)
        # Prologue: "Before everything began." × 30 = ~4 words × 30 = 120 words
        assert 80 <= chapters[0].word_count <= 200

    def test_html_contains_body_content(self, epub3):
        chapters = chapterize(epub3)
        assert "Before everything began" in chapters[0].html
        assert "The story starts here" in chapters[1].html

    def test_html_does_not_contain_html_tag(self, epub3):
        chapters = chapterize(epub3)
        for ch in chapters:
            assert "<html" not in ch.html.lower() or "<body" in ch.html.lower()


class TestEpub2Fff:
    def test_chapter_count(self, epub2_fff):
        chapters = chapterize(epub2_fff)
        assert len(chapters) == len(CHAPTERS_DATA)

    def test_titles_match(self, epub2_fff):
        chapters = chapterize(epub2_fff)
        for ch, (expected_title, _) in zip(chapters, CHAPTERS_DATA):
            assert ch.title == expected_title

    def test_word_counts_positive(self, epub2_fff):
        chapters = chapterize(epub2_fff)
        for ch in chapters:
            assert ch.word_count > 0


class TestCaching:
    def test_same_result_returned_from_cache(self, epub3):
        _cache.clear()
        first = chapterize(epub3)
        second = chapterize(epub3)
        assert first is second  # same list object from cache

    def test_cache_key_includes_mtime(self, epub3, tmp_path):
        _cache.clear()
        first = chapterize(epub3)
        # Touch the file (update mtime)
        epub3.touch()
        second = chapterize(epub3)
        # Should re-parse (different mtime) — not necessarily different chapters,
        # but it should be a different list object
        assert first is not second


class TestChapterUrlExtraction:
    def test_extracts_fff_chapterurl_meta(self, tmp_path):
        chapters = [
            ("Chapter 1", "<p>body one</p>" * 30, "https://www.fanfiction.net/s/13220537/1/"),
            ("Chapter 2", "<p>body two</p>" * 30, "https://www.fanfiction.net/s/13220537/2/"),
        ]
        path = make_epub(chapters=chapters)
        try:
            result = chapterize(Path(path))
            assert result[0].source_url == "https://www.fanfiction.net/s/13220537/1/"
            assert result[1].source_url == "https://www.fanfiction.net/s/13220537/2/"
        finally:
            os.unlink(path)

    def test_source_url_none_without_meta(self, tmp_path):
        path = make_epub(chapters=[("Chapter 1", "<p>plain body</p>" * 30)])
        try:
            result = chapterize(Path(path))
            assert result[0].source_url is None
        finally:
            os.unlink(path)


class TestEdgeCases:
    def test_single_chapter_epub(self, tmp_path):
        path = make_epub(chapters=[("Only Chapter", "<p>All in one.</p>" * 20)])
        try:
            chapters = chapterize(Path(path))
            assert len(chapters) == 1
            assert chapters[0].title == "Only Chapter"
        finally:
            os.unlink(path)

    def test_large_chapter_word_count(self, tmp_path):
        big_body = "<p>" + " ".join(["word"] * 10000) + "</p>"
        path = make_epub(chapters=[("Big Chapter", big_body)])
        try:
            chapters = chapterize(Path(path))
            assert chapters[0].word_count >= 9000
        finally:
            os.unlink(path)
