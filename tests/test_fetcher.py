"""Tests for the fetcher container's EPUB introspection helpers.

The fetcher is a separate container, but its pure helpers import cleanly in the app venv. Here we
cover `_chapter_urls` — the ordered per-chapter canonical URLs it diffs on a stub, which must be in
spine order and skip non-chapter (no-chapterurl) documents so its indices line up with the app's
physical `cursor_chapter_index`.
"""
from __future__ import annotations

import importlib.util
import os
import zipfile
from pathlib import Path

# Load fetcher/app.py under a distinct name — the app package already owns `app` in sys.modules.
_spec = importlib.util.spec_from_file_location(
    "fetcher_app", os.path.join(os.path.dirname(__file__), "..", "fetcher", "app.py")
)
fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher)


def _chapter_doc(url: str | None) -> bytes:
    meta = f'<meta name="chapterurl" content="{url}"/>' if url else ""
    return f'<html><head>{meta}</head><body><p>x</p></body></html>'.encode()


def _make_epub(path: Path, spine: list[tuple[str, str, str | None]]) -> None:
    """spine = [(idref, href, chapterurl|None), ...]; OPF lives under OEBPS/ to exercise the join."""
    manifest = "".join(
        f'<item id="{i}" href="{h}" media-type="application/xhtml+xml"/>' for i, h, _ in spine
    )
    itemrefs = "".join(f'<itemref idref="{i}"/>' for i, _, _ in spine)
    opf = (
        '<?xml version="1.0"?><package><manifest>'
        f'{manifest}</manifest><spine>{itemrefs}</spine></package>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("OEBPS/content.opf", opf)
        for _idref, href, url in spine:
            zf.writestr(f"OEBPS/{href}", _chapter_doc(url))


def test_chapter_urls_in_spine_order_skipping_non_chapters(tmp_path):
    epub = tmp_path / "book.epub"
    # Front-matter (no chapterurl) then two chapters; spine order is what counts.
    _make_epub(epub, [
        ("title", "title.xhtml", None),
        ("ch1", "chapter1.xhtml", "https://s/c1"),
        ("ch2", "sub/chapter2.xhtml", "https://s/c2"),
    ])
    assert fetcher._chapter_urls(epub) == ["https://s/c1", "https://s/c2"]


def test_chapter_urls_respects_spine_not_manifest_order(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub, [
        ("ch2", "chapter2.xhtml", "https://s/c2"),
        ("ch1", "chapter1.xhtml", "https://s/c1"),
    ])
    assert fetcher._chapter_urls(epub) == ["https://s/c2", "https://s/c1"]


def test_chapter_urls_empty_for_non_fanficfare_epub(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub, [("a", "a.xhtml", None), ("b", "b.xhtml", None)])
    assert fetcher._chapter_urls(epub) == []


def test_chapter_urls_unreadable_returns_empty(tmp_path):
    assert fetcher._chapter_urls(tmp_path / "missing.epub") == []
