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

import json  # noqa: E402
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
    monkeypatch.setattr(fetcher, "_find_calibre_id", lambda url, strict=False: None)
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


# ── library lookup by story identity, pinned ids, archive on stub ─────────────────────────────

class FakeCalibre:
    """A tiny in-memory calibredb: entries {id: {"url": ..., "url-archive": ...}} with contains-style
    `identifiers:"=url:X"` search like the real thing, plus add / add_format / set_metadata."""

    def __init__(self, entries: dict[int, dict[str, str]], next_id: int = 5000, refuse_dupes=False):
        self.entries, self.next_id, self.refuse_dupes = entries, next_id, refuse_dupes
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        cmd = args[0]
        ok = lambda out="": subprocess.CompletedProcess(args, 0, stdout=out, stderr="")  # noqa: E731
        if cmd == "search":
            q = args[1]
            if q.startswith("id:"):
                n = int(q[3:])
                return ok(str(n) if n in self.entries else "")
            needle = q.split("=url:", 1)[1].rstrip('"')
            hits = [str(i) for i, ident in sorted(self.entries.items()) if needle in ident.get("url", "")]
            return ok(",".join(hits))
        if cmd == "list":
            ids = [int(t) for t in args[args.index("--search") + 1].replace("id:", "").split(" or ")]
            return ok(json.dumps([{"id": i, "identifiers": self.entries[i]} for i in ids if i in self.entries]))
        if cmd == "add":
            dup_ok = "-d" in args
            epub = Path(args[-1])
            if self.refuse_dupes and not dup_ok:
                return ok("The following books were not added as they already exist in the database\n")
            self.next_id += 1
            self.entries[self.next_id] = {"url": fetcher._epub_source_url(epub) or ""}
            return ok(f"Added book ids: {self.next_id}")
        if cmd == "set_metadata":
            scheme, _, value = args[args.index("--field") + 1].partition(":")[2].partition(":")
            self.entries[int(args[1])] = {scheme: value}
            return ok()
        return ok()


@pytest.fixture
def calibre(monkeypatch):
    def install(entries, **kw):
        fake = FakeCalibre(entries, **kw)
        monkeypatch.setattr(fetcher, "_calibredb", fake)
        return fake
    return install


RR = "https://www.royalroad.com/fiction/186926"


def test_lookup_matches_story_identity_not_exact_string(calibre):
    fake = calibre({
        4596: {"url-archive": RR},                                       # archive copy: never matches
        4629: {"url": RR},                                               # the live entry
        9: {"url": "https://www.royalroad.com/fiction/1869260"},         # longer id, contains-match
        12: {"url": "https://www.fanfiction.net/s/186926/1/x"},          # other site, same digits
    })
    # the app's spelling carries a slug the Calibre identifier doesn't
    assert fetcher._find_calibre_ids(RR + "/leave-luck-to-heaven-litrpg/") == [4629]
    assert fetcher._find_calibre_id(RR + "/leave-luck-to-heaven-litrpg/") == 4629
    assert fetcher._find_calibre_id("https://www.royalroad.com/fiction/777777") is None


def test_lookup_prefers_lowest_id_and_reads_string_identifiers(calibre, monkeypatch):
    fake = calibre({10: {"url": RR}, 11: {"url": RR}})
    orig = fake.__call__

    def stringy(*args):  # older calibredb: identifiers as "scheme:value,…"
        res = orig(*args)
        if args[0] == "list":
            rows = json.loads(res.stdout)
            for r in rows:
                r["identifiers"] = ",".join(f"{k}:{v}" for k, v in r["identifiers"].items())
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(rows), stderr="")
        return res
    monkeypatch.setattr(fetcher, "_calibredb", stringy)
    assert fetcher._find_calibre_ids(RR) == [10, 11]
    assert fetcher._find_calibre_id(RR) == 10


def test_lookup_falls_back_to_search_hits_when_listing_unreadable(monkeypatch):
    def calibredb(*args):
        if args[0] == "search":
            return subprocess.CompletedProcess(args, 0, stdout="4629", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")  # `list` fails
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)
    assert fetcher._find_calibre_ids(RR) == [4629]


def test_job_updates_the_pinned_entry_without_url_lookup(calibre, monkeypatch):
    fake = calibre({4596: {"url": RR}, 4629: {"url": RR}})  # ambiguous by URL alone
    seen = []

    def update_one(url, entry, calibre_id=None):
        seen.append((url, calibre_id))
        entry["calibre_id"] = calibre_id
        return entry, None
    monkeypatch.setattr(fetcher, "_update_one", update_one)
    job = {"status": "running", "finished_at": None, "pinned": {RR: 4629},
           "results": [_entry(RR)]}
    fetcher._jobs["jobpin"] = job

    fetcher._run_job("jobpin")

    assert seen == [(RR, 4629)]
    assert job["results"][0]["calibre_id"] == 4629 and job["results"][0]["phase"] == "done"
    assert not any(c[0] == "list" for c in fake.calls)  # no identifier search was needed


def test_job_looks_up_when_the_pinned_entry_is_gone(calibre, monkeypatch):
    calibre({4700: {"url": RR}})
    seen = []
    monkeypatch.setattr(fetcher, "_update_one",
                        lambda url, entry, calibre_id=None: (seen.append(calibre_id), (entry, None))[1])
    fetcher._jobs["jobgone"] = {"status": "running", "finished_at": None, "pinned": {RR: 4629},
                                "results": [_entry(RR)]}
    fetcher._run_job("jobgone")
    assert seen == [4700]


def test_stub_keeps_a_tagged_archive_that_cannot_win_the_lookup(calibre, monkeypatch, tmp_path):
    # refuse_dupes: the archive has the live entry's title+author, so only `add -d` keeps it.
    fake = calibre({4629: {"url": RR}}, refuse_dupes=True)
    outputs = iter(["Existing epub contains 150 chapters, web site only has 139", ""])

    def fanficfare(*args, cwd):
        return subprocess.CompletedProcess(args, 0, stdout=next(outputs, ""), stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)

    real_calibredb = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export":  # write the live EPUB into --to-dir
            _fff_epub(Path(args[args.index("--to-dir") + 1]) / "book.epub", RR, "In-Progress")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_calibredb(*args)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)

    entry = _entry(RR)
    result, transient = fetcher._update_one(RR, entry, 4629)

    assert transient is None and result["stub"]["old"] == 150
    assert result["calibre_id"] == 4629
    adds = [c for c in fake.calls if c[0] == "add"]
    assert len(adds) == 1 and "-d" in adds[0]
    archive_id = fake.next_id
    assert fake.entries[archive_id] == {"url-archive": RR}        # moved off `url:`
    assert fake.entries[4629] == {"url": RR}                      # live entry untouched
    assert fetcher._find_calibre_ids(RR) == [4629]                # …and only it is found by URL
    assert any(c[0] == "add_format" and c[1] == "4629" for c in fake.calls)


def test_new_story_refused_as_duplicate_refreshes_the_existing_entry(calibre, fake_tools, monkeypatch):
    # `calibredb add` exits 0 but adds nothing; the story IS in the library (lookup missed it).
    url = RR
    fake = calibre({4629: {"url": "https://www.royalroad.com/fiction/186926"}}, refuse_dupes=True)
    fake_tools["script"] = [([("a.epub", RR + "/t", "In-Progress")], "")]
    # `fake_tools` replaced _find_calibre_id with "never found"; restore the real lookup after add.
    monkeypatch.setattr(
        fetcher, "_find_calibre_id",
        lambda u, strict=False: 4629 if any(c[0] == "add" for c in fake.calls) else None)
    by_url = {url: _entry(url)}

    fetcher._download_new([url], by_url)

    e = by_url[url]
    assert e["phase"] == "done" and e["calibre_id"] == 4629
    assert any(c[0] == "add_format" and c[1] == "4629" for c in fake.calls)


def test_new_story_refused_and_not_verifiably_in_library_is_a_loud_error_not_a_duplicate(calibre, fake_tools):
    # Same title+author as an existing entry whose url: is a different story: do NOT force-add
    # (that would create a duplicate entry) — report it and let a human decide.
    url = "https://www.royalroad.com/fiction/555555"
    fake = calibre({4629: {"url": RR}}, refuse_dupes=True)
    fake_tools["script"] = [([("a.epub", url + "/t", "In-Progress")], "")]
    by_url = {url: _entry(url)}

    fetcher._download_new([url], by_url)

    e = by_url[url]
    assert e["phase"] == "error" and "calibredb added nothing" in e["error"]
    assert not any(c[0] == "add" and "-d" in c for c in fake.calls)
    assert not any(c[0] == "add_format" for c in fake.calls)


def test_unrecognised_listing_shape_is_unreadable_not_empty(calibre, monkeypatch):
    fake = calibre({4629: {"url": RR}})
    orig = fake.__call__

    def odd(*args):  # parses as JSON but isn't rows of {id, identifiers}
        if args[0] == "list":
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{"book": 4629}]), stderr="")
        return orig(*args)
    monkeypatch.setattr(fetcher, "_calibredb", odd)
    assert fetcher._identifier_urls([4629]) is None
    assert fetcher._find_calibre_ids(RR) == [4629]                 # keeps the raw search hits
    assert fetcher._find_calibre_ids(RR, strict=True) == []        # …but won't act on unverified ones


def test_refused_add_with_unreadable_listing_does_not_overwrite_a_guessed_entry(
        calibre, fake_tools, monkeypatch):
    fake = calibre({4629: {"url": RR}}, refuse_dupes=True)
    fake_tools["script"] = [([("a.epub", RR + "/t", "In-Progress")], "")]
    monkeypatch.setattr(fetcher, "_identifier_urls", lambda ids: None)
    monkeypatch.setattr(fetcher, "_find_calibre_id",
                        lambda u, strict=False: None if strict else 4629)
    by_url = {RR: _entry(RR)}

    fetcher._download_new([RR], by_url)

    assert by_url[RR]["phase"] == "error"
    assert not any(c[0] == "add_format" for c in fake.calls)


def test_new_story_that_adds_nothing_is_an_error_not_a_phantom_success(fake_tools, monkeypatch):
    url = "https://www.royalroad.com/fiction/555555"
    fake_tools["script"] = [([("a.epub", url + "/t", "In-Progress")], "")]
    monkeypatch.setattr(fetcher, "_calibredb",
                        lambda *a: subprocess.CompletedProcess(a, 0, stdout="nothing added", stderr=""))
    by_url = {url: _entry(url)}

    fetcher._download_new([url], by_url)

    assert by_url[url]["phase"] == "error" and "added nothing" in by_url[url]["error"]


# ── `-u` must not be short-circuited by the exported file's mtime ────────────────────────────

def _update_with(calibre, monkeypatch, outputs):
    """Run _update_one on 4629 with a scripted FanFicFare; returns (entry, transient, ff calls, mtimes)."""
    fake = calibre({4629: {"url": RR}})
    calls, mtimes = [], []
    outputs = iter(outputs)

    def fanficfare(*args, cwd):
        calls.append(args)
        mtimes.append((Path(cwd) / "book.epub").stat().st_mtime)
        return subprocess.CompletedProcess(args, 0, stdout=next(outputs, ""), stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    real_calibredb = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export":
            _fff_epub(Path(args[args.index("--to-dir") + 1]) / "book.epub", RR, "In-Progress")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_calibredb(*args)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)
    entry, transient = fetcher._update_one(RR, _entry(RR), 4629)
    return fake, entry, transient, calls, mtimes


def test_update_backdates_the_export_and_always_overwrites(calibre, monkeypatch):
    fake, entry, transient, calls, mtimes = _update_with(calibre, monkeypatch, ["Do update - epub(17) vs url(27)"])
    assert transient is None and not entry.get("error")
    assert calls[0][:2] == ("-u", "./book.epub") and "always_overwrite=true" in calls[0]
    assert mtimes == [0]  # older than any story's dateUpdated, so the writer can't skip
    assert any(c[0] == "add_format" for c in fake.calls)


def test_force_rerun_is_backdated_again(calibre, monkeypatch):
    _, _, _, calls, mtimes = _update_with(
        calibre, monkeypatch, ["Use Overwrite or force_update_epub_always", ""])
    assert len(calls) == 2 and "force_update_epub_always=true" in calls[1]
    assert "always_overwrite=true" in calls[1] and mtimes == [0, 0]


def test_writer_skip_is_an_error_not_ok(calibre, monkeypatch):
    skipped = ("Do update - epub(17) vs url(27)\n"
               "File(./book.epub) Updated(2026-10-13) more recently than Story(2026-10-12) - Skipping")
    fake, entry, transient, _, _ = _update_with(calibre, monkeypatch, [skipped])
    assert transient is None
    assert "skipped writing" in entry["error"] and "more recently than Story" in entry["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)  # Calibre entry left as it was
