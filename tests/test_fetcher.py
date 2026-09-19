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


# ── new-story download path (the "fanficfare produced no epub" bug) ───────────────────────────

import subprocess  # noqa: E402

import pytest  # noqa: E402


def _fff_epub(path: Path, source_url: str, status: str | None = "In-Progress") -> None:
    """A FanFicFare-shaped EPUB: dc:source + status subject in the OPF, status on the title page."""
    subject = f"<dc:subject>{status}</dc:subject>" if status else ""
    opf = (
        '<?xml version="1.0"?><package xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<metadata>{subject}<dc:source>{source_url}</dc:source></metadata>'
        '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    title = (
        f'<html><body><b>Status:</b> {status}<br /></body></html>' if status
        else '<html><body></body></html>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.opf", opf)
        zf.writestr("OEBPS/title_page.xhtml", title)
        zf.writestr("c1.xhtml", _chapter_doc(source_url))


def _entry(url: str) -> dict:
    return {"url": url, "phase": "queued", "calibre_id": None, "chapter_count": None,
            "stub": None, "error": None, "story_url": None, "story_status": None}


@pytest.fixture
def fake_tools(monkeypatch):
    """Replace the subprocess wrappers. `script` maps a call number to (files_to_write, output)."""
    state = {"calls": [], "script": [], "next_id": 100}

    def fanficfare(*args, cwd):
        call = len(state["calls"])
        state["calls"].append(args)
        files, output = state["script"][min(call, len(state["script"]) - 1)]
        for name, url, status in files:
            _fff_epub(Path(cwd) / name, url, status)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def calibredb(*args):
        if args[0] == "add":
            state["next_id"] += 1
            return subprocess.CompletedProcess(args, 0, stdout=f"Added book ids: {state['next_id']}", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)
    monkeypatch.setattr(fetcher, "_find_calibre_id", lambda url: None)
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    return state


def test_story_key_matches_canonicalised_urls():
    k = fetcher._story_key
    assert k("https://www.fanfiction.net/s/13051824") == k("https://www.fanfiction.net/s/13051824/1/New-Blood")
    assert k("https://forums.spacebattles.com/threads/elixir.1258552/page-3") == \
        k("https://forums.spacebattles.com/threads/elixir.1258552/")
    assert k("https://archiveofourown.org/works/19992961") == k("http://archiveofourown.org/works/19992961/chapters/1")
    assert k("https://royalroad.com/fiction/12345") == k("https://www.royalroad.com/fiction/12345/some-title")
    assert k("https://www.fanfiction.net/s/1") != k("https://www.fanfiction.net/s/2")
    assert k(None) == ""


def test_epub_status_prefers_title_page_then_opf(tmp_path):
    a = tmp_path / "a.epub"; _fff_epub(a, "https://x/s/1", "Completed")
    assert fetcher._epub_status(a) == "Completed"
    b = tmp_path / "b.epub"; _fff_epub(b, "https://x/s/2", None)
    assert fetcher._epub_status(b) is None
    # No title page, status only as an OPF subject.
    c = tmp_path / "c.epub"
    with zipfile.ZipFile(c, "w") as zf:
        zf.writestr("content.opf", "<package><dc:subject>fantasy</dc:subject><dc:subject>Hiatus</dc:subject></package>")
    assert fetcher._epub_status(c) == "Hiatus"


def test_bulk_add_matches_canonical_urls_and_records_status(fake_tools):
    """Regression: submitted FFN/SB URLs differ from the canonical dc:source FanFicFare writes, so
    exact matching reported success as 'produced no epub' and discarded the EPUB."""
    a, b = "https://www.fanfiction.net/s/13051824", "https://forums.spacebattles.com/threads/x.906680/page-2"
    fake_tools["script"] = [([
        ("a.epub", "https://www.fanfiction.net/s/13051824/1/New-Blood", "In-Progress"),
        ("b.epub", "https://forums.spacebattles.com/threads/x.906680/", "Completed"),
    ], "")]
    by_url = {a: _entry(a), b: _entry(b)}

    fetcher._process_new_batch([a, b], by_url)

    assert by_url[a]["phase"] == by_url[b]["phase"] == "done"
    assert by_url[a]["error"] is None and by_url[b]["error"] is None
    assert by_url[a]["story_url"] == "https://www.fanfiction.net/s/13051824/1/New-Blood"
    assert (by_url[a]["story_status"], by_url[b]["story_status"]) == ("In-Progress", "Completed")
    assert by_url[a]["calibre_id"] != by_url[b]["calibre_id"]


def test_new_story_retries_transient_failure(fake_tools):
    url = "https://www.royalroad.com/fiction/12345"
    fake_tools["script"] = [
        ([], "HTTP Error 503: Service Unavailable"),                       # attempt 1: nothing
        ([("a.epub", "https://www.royalroad.com/fiction/12345/t", "In-Progress")], ""),  # attempt 2
    ]
    by_url = {url: _entry(url)}

    fetcher._process_new_batch([url], by_url)

    assert by_url[url]["phase"] == "done" and by_url[url]["error"] is None
    assert len(fake_tools["calls"]) == 2


def test_permanent_failure_reports_fanficfare_output(fake_tools):
    url = "https://www.royalroad.com/fiction/99999"
    fake_tools["script"] = [([], "Traceback...\nStoryDoesNotExist: story not found at 99999")]
    by_url = {url: _entry(url)}

    fetcher._process_new_batch([url], by_url)

    e = by_url[url]
    assert e["phase"] == "error"
    assert "fanficfare produced no epub" in e["error"] and "StoryDoesNotExist" in e["error"]
    assert len(fake_tools["calls"]) == 1        # not transient → no pointless retries


def test_transient_exhaustion_says_so(fake_tools):
    url = "https://www.royalroad.com/fiction/12345"
    fake_tools["script"] = [([], "connection reset by peer")]
    by_url = {url: _entry(url)}

    fetcher._process_new_batch([url], by_url)

    assert by_url[url]["error"].startswith("failed after 3 attempts")
    assert len(fake_tools["calls"]) == 3


def test_multi_url_failure_is_attributed_per_url(fake_tools):
    good = "https://www.royalroad.com/fiction/111"
    bad = "https://www.royalroad.com/fiction/222"

    def script_for(call_args):  # decided by which URL each run was given
        return None

    # batch: only `good` produced; then `bad` is re-run alone and fails with its own message.
    fake_tools["script"] = [
        ([("g.epub", "https://www.royalroad.com/fiction/111/g", "Completed")], "batch output"),
        ([], "StoryDoesNotExist: 222 is private"),
    ]
    by_url = {good: _entry(good), bad: _entry(bad)}

    fetcher._process_new_batch([good, bad], by_url)

    assert by_url[good]["phase"] == "done"
    assert by_url[bad]["phase"] == "error"
    assert "222 is private" in by_url[bad]["error"]
