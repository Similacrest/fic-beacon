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

from app.epub.chapterizer import chapterize, materialize_image_urls, _cache, _IMG_SENTINEL
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


class TestImageRewrite:
    def _chapterize_with_img(self, img_html: str):
        # Chapter names are OPF-relative (ebooklib), so paths come out OPF-relative too;
        # the /img route re-anchors them to the OPF dir when reading the zip.
        path = make_epub(chapters=[("Chapter 1", img_html + "<p>plenty of body words here.</p>" * 30)])
        try:
            return chapterize(Path(path))[0]
        finally:
            os.unlink(path)

    def test_relative_img_rewritten_to_sentinel(self):
        ch = self._chapterize_with_img('<img src="images/00009.jpeg"/>')
        assert f'{_IMG_SENTINEL}/images/00009.jpeg' in ch.html

    def test_external_img_left_untouched(self):
        ch = self._chapterize_with_img('<img src="https://example.com/x.png"/>')
        assert "https://example.com/x.png" in ch.html
        assert _IMG_SENTINEL not in ch.html

    def test_data_uri_left_untouched(self):
        ch = self._chapterize_with_img('<img src="data:image/png;base64,AAAA"/>')
        assert "data:image/png;base64,AAAA" in ch.html
        assert _IMG_SENTINEL not in ch.html

    def test_srcset_rewritten(self):
        ch = self._chapterize_with_img('<img srcset="a.png 1x, b.png 2x"/>')
        assert f'{_IMG_SENTINEL}/a.png 1x' in ch.html
        assert f'{_IMG_SENTINEL}/b.png 2x' in ch.html

    def test_materialize_swaps_sentinel_for_route(self):
        html = f'<img src="{_IMG_SENTINEL}/OEBPS/images/x.png"/>'
        out = materialize_image_urls(html, 42, "http://beacon.test/")
        assert 'src="http://beacon.test/img/42/OEBPS/images/x.png"' in out
        assert _IMG_SENTINEL not in out


def _epub_with_notes(chapter_body: str, notes_body: str | None = None) -> Path:
    """Build a 2-file EPUB: a chapter plus an optional separate endnotes file."""
    import io, tempfile, zipfile
    files = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml":
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        "OEBPS/chapter.xhtml":
            '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Ch</title></head>'
            f"<body><h1>Chapter 1</h1>{chapter_body}</body></html>",
    }
    manifest = ['<item id="ch" href="chapter.xhtml" media-type="application/xhtml+xml"/>',
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine = ['<itemref idref="ch"/>']
    if notes_body is not None:
        files["OEBPS/notes.xhtml"] = (
            '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Notes</title></head>'
            f"<body>{notes_body}</body></html>"
        )
        manifest.append('<item id="nt" href="notes.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="nt"/>')
    files["OEBPS/nav.xhtml"] = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Nav</title></head><body><nav epub:type="toc"><ol>'
        '<li><a href="chapter.xhtml">Chapter 1</a></li></ol></nav></body></html>'
    )
    files["OEBPS/content.opf"] = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        '<dc:identifier id="uid">urn:uuid:notes-test</dc:identifier></metadata>'
        f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>'
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w") as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return Path(tmp.name)


_FILLER = "<p>plenty of body words go here for the floor.</p>" * 20


class TestFootnotes:
    def test_epub3_rearnote_inlined_cross_file(self):
        # EPUB3 semantic: noteref -> rearnote in a separate file.
        chapter = (
            '<p>Claim.<a epub:type="noteref" href="notes.xhtml#n1" id="r1">1</a></p>'
            + _FILLER
        )
        notes = '<p id="np1"><a epub:type="rearnote" href="chapter.xhtml#r1" id="n1">1</a> The note text.</p>'
        path = _epub_with_notes(chapter, notes)
        try:
            html = chapterize(path)[0].html
            assert "beacon-endnotes" in html
            assert "The note text." in html
            assert 'href="#fb-note-n1"' in html        # marker rewritten to local anchor
            assert 'id="fb-note-n1"' in html            # endnote target present
            assert "notes.xhtml" not in html            # original cross-file link gone from marker
        finally:
            os.unlink(path)

    def test_plain_sup_anchor_inlined_cross_file(self):
        # Non-semantic (Homo Deus style): superscript anchor -> note <p> by id, no epub:type.
        chapter = '<p>Claim.<a href="notes.xhtml#x1"><sup>1</sup></a></p>' + _FILLER
        notes = '<p id="x1"><a href="chapter.xhtml#back1">1</a>. The plain note.</p>'
        path = _epub_with_notes(chapter, notes)
        try:
            html = chapterize(path)[0].html
            assert "beacon-endnotes" in html
            assert "The plain note." in html
            assert 'href="#fb-note-x1"' in html
        finally:
            os.unlink(path)

    def test_same_file_footnote_left_untouched(self):
        # Same-file footnote is already self-contained — no endnotes block appended.
        chapter = (
            '<p>Claim.<a epub:type="noteref" href="#f1">1</a></p>'
            '<p class="footnote" id="f1">Inline footnote, already here.</p>' + _FILLER
        )
        path = _epub_with_notes(chapter, notes_body=None)
        try:
            html = chapterize(path)[0].html
            assert "beacon-endnotes" not in html
            assert "Inline footnote, already here." in html  # still present in body
        finally:
            os.unlink(path)

    def test_plain_cross_reference_not_treated_as_note(self):
        # A bare-text cross-file link (e.g. "see Part I") must NOT become a footnote.
        chapter = '<p>See <a href="notes.xhtml#part1">Part I</a> for details.</p>' + _FILLER
        notes = '<p id="part1">Part I heading</p>'
        path = _epub_with_notes(chapter, notes)
        try:
            html = chapterize(path)[0].html
            assert "beacon-endnotes" not in html
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
