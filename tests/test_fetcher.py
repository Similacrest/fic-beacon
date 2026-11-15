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

    def fanficfare(*args, cwd, flare=False):
        call = len(state["calls"])
        state.setdefault("flare", []).append(flare)
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
        self.files: dict[int, bytes] = {}
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
            scheme = "url-archive" if "=url-archive:" in q else "url"
            needle = q.split(f"={scheme}:", 1)[1].rstrip('"')
            hits = [str(i) for i, ident in sorted(self.entries.items()) if needle in ident.get(scheme, "")]
            return ok(",".join(hits))
        if cmd == "list":
            ids = [int(t) for t in args[args.index("--search") + 1].replace("id:", "").split(" or ")]
            return ok(json.dumps([{"id": i, "identifiers": self.entries[i]} for i in ids if i in self.entries]))
        if cmd == "export":  # only entries given bytes via `files` have an EPUB to export
            i = int(args[-1])
            if i in self.files:
                (Path(args[args.index("--to-dir") + 1]) / "book.epub").write_bytes(self.files[i])
            return ok()
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


def _chapters(path: Path, n: int) -> None:
    _make_epub(path, [(f"c{i}", f"c{i}.xhtml", f"https://s/c{i}") for i in range(1, n + 1)])


def _stub_env(calibre, monkeypatch, fresh_chapters, live_chapters=5, msg=None, **kw):
    """Live entry 4629 exports a `live_chapters` EPUB; FanFicFare's `-u` run prints the stub message and
    leaves it alone; a fresh (no `-u`) run writes `fresh_chapters` chapters (None → writes nothing)."""
    fake = calibre({4629: {"url": RR}}, **kw)
    msg = msg or f"./book.epub contains {live_chapters} chapters, more than source: 3."
    runs = []

    def fanficfare(*args, cwd, flare=False):
        runs.append(args)
        if "-u" in args:
            return subprocess.CompletedProcess(args, 0, stdout=msg, stderr="")
        if fresh_chapters:
            _chapters(Path(cwd) / "fresh.epub", fresh_chapters)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    real = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export" and args[-1] == "4629":
            _chapters(Path(args[args.index("--to-dir") + 1]) / "book.epub", live_chapters)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real(*args)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)
    return fake, runs


def test_stub_downloads_fresh_instead_of_updating_the_old_epub(calibre, monkeypatch):
    # refuse_dupes: the archive has the live entry's title+author, so only `add -d` keeps it.
    fake, runs = _stub_env(calibre, monkeypatch, fresh_chapters=3, refuse_dupes=True)

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and result.get("error") is None
    assert result["stub"]["old"] == 5 and result["stub"]["new"] == 3
    assert result["stub"]["old_urls"] == [f"https://s/c{i}" for i in range(1, 6)]
    assert result["stub"]["new_urls"] == [f"https://s/c{i}" for i in range(1, 4)]
    assert any("-u" not in r for r in runs)                       # a fresh run happened
    assert result["calibre_id"] == 4629 and result["chapter_count"] == 3
    adds = [c for c in fake.calls if c[0] == "add"]
    assert len(adds) == 1 and "-d" in adds[0]
    archive_id = fake.next_id
    assert fake.entries[archive_id] == {"url-archive": RR}        # moved off `url:`
    assert fake.entries[4629] == {"url": RR}                      # live entry untouched
    assert fetcher._find_calibre_ids(RR) == [4629]                # …and only it is found by URL
    added = [c for c in fake.calls if c[0] == "add_format" and c[1] == "4629"]
    assert len(added) == 1 and Path(added[0][2]).name == "fresh.epub"


def test_stub_whose_fresh_download_is_not_smaller_fails_and_reports_no_stub(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=5)   # same size: nothing was rebuilt

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and "did not rebuild" in result["error"]
    assert not result.get("stub")
    assert not any(c[0] in ("add_format", "add") for c in fake.calls)   # no archive, no overwrite


def test_stub_with_no_fresh_epub_fails_loudly(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=None)

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and "no epub" in result["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)


def test_stub_retry_does_not_add_a_second_archive(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=3)
    live = Path(__file__).parent / "_live.epub"
    _chapters(live, 5)
    try:
        fake.entries[4657] = {"url-archive": RR}
        fake.files[4657] = live.read_bytes()      # an earlier attempt already archived these exact bytes
        # `_make_epub` is not byte-stable across calls only if it embeds time; assert identity first.
        probe = Path(__file__).parent / "_probe.epub"
        _chapters(probe, 5)
        same = probe.read_bytes() == live.read_bytes()
        probe.unlink()
    finally:
        live.unlink()
    if not same:
        pytest.skip("epub builder is not byte-stable")

    result, _ = fetcher._update_one(RR, _entry(RR), 4629)

    assert result["stub"]["old"] == 5
    assert not any(c[0] == "add" for c in fake.calls)


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

def _update_with(calibre, monkeypatch, outputs, returncode=0, name="book.epub"):
    """Run _update_one on 4629 with a scripted FanFicFare; returns (entry, transient, ff calls, mtimes)."""
    fake = calibre({4629: {"url": RR}})
    calls, mtimes = [], []
    outputs = iter(outputs)

    def fanficfare(*args, cwd):
        calls.append(args)
        mtimes.append((Path(cwd) / name.replace("%", "_")).stat().st_mtime)
        return subprocess.CompletedProcess(args, returncode, stdout=next(outputs, ""), stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    real_calibredb = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export":
            _fff_epub(Path(args[args.index("--to-dir") + 1]) / name, RR, "In-Progress")
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


def test_failed_update_is_an_error_not_ok(calibre, monkeypatch):
    """A non-zero FanFicFare exit that isn't transient (e.g. a 403) used to fall through to
    add_format of the unchanged EPUB and report ok."""
    out = "HTTPErrorFFF: Giving up on https://www.royalroad.com/fiction/83080 403 Client Error: Forbidden"
    fake, entry, transient, _, _ = _update_with(calibre, monkeypatch, [out], returncode=1)
    assert transient is None
    assert "403" in entry["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)


# ---- FlareSolverr fallback -------------------------------------------------------------------
_403 = "HTTPErrorFFF: Giving up on https://www.royalroad.com/fiction/83080 403 Client Error: Forbidden"
_RR = "https://www.royalroad.com/fiction/83080"


@pytest.fixture
def flare(fake_tools, monkeypatch, tmp_path):
    ini = tmp_path / "flaresolverr.ini"
    ini.write_text("[defaults]\n")
    monkeypatch.setattr(fetcher, "FLARE_INI", str(ini))
    monkeypatch.setattr(fetcher, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(fetcher, "_state", None)
    fake_tools["script"] = [([], _403)]
    return fake_tools


def _run_ff(tmp_path):
    return fetcher._ff([_RR], "-u", "x.epub", cwd=str(tmp_path))


def test_first_403_is_not_escalated(flare, tmp_path):
    res = _run_ff(tmp_path)
    assert flare["flare"] == [False] and res.via_flare is False


def test_second_consecutive_403_retries_through_flaresolverr(flare, tmp_path):
    _run_ff(tmp_path)
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [False, True] and res.via_flare is True


def test_non_403_error_resets_the_streak(flare, tmp_path):
    _run_ff(tmp_path)
    flare["script"] = [([], "some other failure")]
    _run_ff(tmp_path)
    flare["script"] = [([], _403)]
    flare["flare"].clear()
    _run_ff(tmp_path)
    assert flare["flare"] == [False]  # count restarted at 1


def test_flagged_source_goes_straight_to_flaresolverr_and_state_survives_restart(
        flare, tmp_path, monkeypatch):
    _run_ff(tmp_path); _run_ff(tmp_path)
    monkeypatch.setattr(fetcher, "_state", None)  # restart: reload from disk
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [True] and res.via_flare is True


def test_daily_probe_clears_the_flag_when_direct_works(flare, tmp_path, monkeypatch):
    _run_ff(tmp_path); _run_ff(tmp_path)
    key = fetcher._host_key(_RR)
    fetcher._state[key]["probed_at"] -= fetcher.FLARE_PROBE_SECONDS + 1
    flare["script"] = [([], "all good")]
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [False] and res.via_flare is False
    assert key not in fetcher._state


def test_failed_probe_falls_back_in_the_same_run_and_waits_another_day(flare, tmp_path):
    _run_ff(tmp_path); _run_ff(tmp_path)
    key = fetcher._host_key(_RR)
    fetcher._state[key]["probed_at"] -= fetcher.FLARE_PROBE_SECONDS + 1
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [False, True] and res.via_flare is True
    flare["flare"].clear()
    _run_ff(tmp_path)
    assert flare["flare"] == [True]  # probed just now: no second direct try


def test_batch_403_across_hosts_is_not_attributed_to_any_host(flare, tmp_path):
    for _ in range(3):
        fetcher._ff([_RR, "https://archiveofourown.org/works/1"], "-i", "u.txt", cwd=str(tmp_path))
    assert flare["flare"] == [False] * 3 and fetcher._state == {}


def test_missing_flaresolverr_ini_degrades_to_the_plain_error(flare, tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "FLARE_INI", str(tmp_path / "nope.ini"))
    _run_ff(tmp_path)
    res = _run_ff(tmp_path)
    assert flare["flare"] == [False, False] and res.via_flare is False


def test_flaresolverr_timeout_is_not_retried():
    msg = f"flaresolverr fetch timed out after 5400s {fetcher._NO_RETRY}"
    assert fetcher._transient("connection reset by peer") is True
    assert fetcher._transient(msg) is False


def test_403_regex_needs_fanficfare_error_not_just_no_epub():
    assert fetcher._403_RE.search(_403)
    assert not fetcher._403_RE.search("fanficfare produced no epub")
    assert not fetcher._403_RE.search("fetched chapter 403 of the story")


def test_job_result_records_via_flaresolverr(flare, tmp_path):
    flare["script"] = [([], _403), ([("a.epub", _RR, "In-Progress")], "ok")]
    entry = _entry(_RR)
    fetcher._state_load()[fetcher._host_key(_RR)] = {"count": 1}
    fetcher._download_new([_RR], {_RR: entry})
    assert entry["via"] == "flaresolverr" and entry["phase"] == "done"


_RR2 = "https://www.royalroad.com/fiction/99999"


def test_403s_from_different_stories_on_one_host_flag_the_host(flare, tmp_path):
    fetcher._ff([_RR], "-u", "x.epub", cwd=str(tmp_path))
    flare["flare"].clear()
    fetcher._ff([_RR2], "-u", "y.epub", cwd=str(tmp_path))  # second 403 from the host
    assert flare["flare"] == [False, True]
    flare["flare"].clear()
    res = fetcher._ff(["https://www.royalroad.com/fiction/5"], "-u", "z.epub", cwd=str(tmp_path))
    assert flare["flare"] == [True] and res.via_flare is True  # a never-seen story rides along


def test_sites_ini_lists_flagged_hosts_without_logins(flare, tmp_path):
    (tmp_path / "flaresolverr.ini").write_text(
        "[defaults]\nuse_flaresolverr_proxy: directimages\nflaresolverr_proxy_address: flaresolverr\n"
        "[www.royalroad.com]\npassword: hunter2\n")
    _run_ff(tmp_path); _run_ff(tmp_path)
    txt = fetcher._sites_ini().read_text()
    assert "[royalroad.com]" in txt and "[www.royalroad.com]" in txt
    assert "use_flaresolverr_proxy: directimages" in txt and "hunter2" not in txt


def test_unflagging_removes_the_section(flare, tmp_path):
    _run_ff(tmp_path); _run_ff(tmp_path)
    key = fetcher._host_key(_RR)
    fetcher._state[key]["probed_at"] -= fetcher.FLARE_PROBE_SECONDS + 1
    flare["script"] = [([], "all good")]
    _run_ff(tmp_path)
    assert "royalroad" not in fetcher._sites_ini().read_text()


def test_old_per_story_state_folds_into_its_host(flare, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "flaresolverr.json").write_text(
        '{"royalroad.com|83080": {"count": 2, "flare": true, "probed_at": 1}, "royalroad.com|1": {"count": 1}}')
    st = fetcher._state_load()
    assert list(st) == ["royalroad.com"] and st["royalroad.com"]["flare"] is True


def test_418_is_an_outage_not_a_block():
    assert not fetcher._403_RE.search("HTTPErrorFFF: HTTP Error in FFF '418 Client Error: I'm a teapot'")


def test_tail_leads_with_the_exception_line():
    out = "\n".join(["Traceback (most recent call last):", "  File \"a.py\", line 1, in x",
                      "    raise exceptions.HTTPErrorFFF(", "    ^^^^^^^^^", "      ^^^^^",
                      "fanficfare.exceptions.HTTPErrorFFF: HTTP Error in FFF '418 Client'", "   ^^^^"])
    assert fetcher._tail(out).startswith("fanficfare.exceptions.HTTPErrorFFF: HTTP Error in FFF '418")


def test_percent_in_the_export_name_is_not_passed_to_fanficfare(calibre, monkeypatch):
    """FanFicFare stores the EPUB name as a config value; a bare % crashes configparser."""
    _, entry, transient, calls, _ = _update_with(
        calibre, monkeypatch, ["Do update - epub(17) vs url(27)"], name="snipers solve 99% of all.epub")
    assert transient is None and not entry.get("error")
    assert calls[0][1] == "./snipers solve 99_ of all.epub"


def test_legacy_state_is_saved_and_ini_exists_before_a_flare_run(flare, tmp_path):
    import time as _t
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "flaresolverr.json").write_text(
        '{"royalroad.com|83080": {"count": 2, "flare": true, "probed_at": %d}}' % int(_t.time()))
    seen = {}
    orig = fetcher._fanficfare

    def spy(*a, cwd, flare=False):
        if flare:
            seen["ini"] = fetcher._sites_ini().read_text() if fetcher._sites_ini().exists() else None
        return orig(*a, cwd=cwd, flare=flare)

    fetcher._state = None
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(fetcher, "_fanficfare", spy)
    try:
        res = _run_ff(tmp_path)
    finally:
        mp.undo()
    assert res.via_flare is True and "[royalroad.com]" in seen["ini"]
    assert "royalroad.com|" not in (tmp_path / "state" / "flaresolverr.json").read_text()


def test_deleted_ini_is_regenerated_before_a_flare_run(flare, tmp_path):
    _run_ff(tmp_path); _run_ff(tmp_path)
    fetcher._sites_ini().unlink()
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [True] and res.via_flare is True
    assert "[www.royalroad.com]" in fetcher._sites_ini().read_text()


def test_unwritable_ini_fails_clearly_instead_of_running_direct(flare, tmp_path, monkeypatch):
    _run_ff(tmp_path); _run_ff(tmp_path)
    fetcher._sites_ini().unlink()

    def boom(path, text):
        raise OSError("read-only file system")
    monkeypatch.setattr(fetcher, "_write_atomic", boom)
    flare["flare"].clear()
    res = _run_ff(tmp_path)
    assert flare["flare"] == [] and res.via_flare is False
    assert "could not be written" in res.stderr and "read-only" in res.stderr


@pytest.mark.parametrize("text,expected", [
    ("Existing epub contains 150 chapters, web site only has 139", (150, 139)),
    ("./a.epub contains 151 chapters, more than source: 65.", (151, 65)),
    ("a.epub contains 151 chapters, more than the source: 65", (151, 65)),
    ("contains 151 chapters, more than web site has 65", (151, 65)),
    ("Existing epub contains 10 chapters, web site only has 12", None),   # growth is not a stub
    ("./a.epub already contains 151 chapters.", None),
])
def test_stub_counts_wordings(text, expected):
    assert fetcher._stub_counts(text) == expected


def test_unrecognised_chapter_message_with_no_write_fails_loudly(calibre, monkeypatch):
    fake = calibre({4629: {"url": RR}})

    def fanficfare(*args, cwd):
        return subprocess.CompletedProcess(args, 0, stdout="a.epub has 9 chapters; source differs: 3", stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    real = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export":
            _fff_epub(Path(args[args.index("--to-dir") + 1]) / "book.epub", RR, "In-Progress")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real(*args)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and "unrecognised FanFicFare chapter message" in result["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)


def test_needs_force_that_changes_nothing_fails_instead_of_add_format(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=None,
                        msg="Use Overwrite or force_update_epub_always to update")

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and "left the EPUB unchanged" in result["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)


def test_stub_reports_the_archive_calibre_id(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=3, refuse_dupes=True)

    result, _ = fetcher._update_one(RR, _entry(RR), 4629)

    assert result["stub"]["archive_calibre_id"] == fake.next_id


def test_stub_retry_reports_the_existing_archive_id(calibre, monkeypatch):
    fake, _ = _stub_env(calibre, monkeypatch, fresh_chapters=3)
    live = Path(__file__).parent / "_live2.epub"
    _chapters(live, 5)
    try:
        fake.entries[4657] = {"url-archive": RR}
        fake.files[4657] = live.read_bytes()
    finally:
        live.unlink()

    result, _ = fetcher._update_one(RR, _entry(RR), 4629)

    if not any(c[0] == "export" and c[-1] == "4657" for c in fake.calls):
        pytest.skip("archive lookup did not run")
    if any(c[0] == "add" for c in fake.calls):
        pytest.skip("epub builder is not byte-stable")
    assert result["stub"]["archive_calibre_id"] == 4657


def test_plain_update_that_loses_chapter_urls_fails(calibre, monkeypatch):
    fake = calibre({4629: {"url": RR}})

    def fanficfare(*args, cwd, flare=False):
        if "-u" in args:   # rewrites the exported EPUB smaller, prints no stub message
            _chapters(Path(cwd) / "book.epub", 3)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(fetcher, "_fanficfare", fanficfare)
    real = fetcher._calibredb

    def calibredb(*args):
        if args[0] == "export" and args[-1] == "4629":
            _chapters(Path(args[args.index("--to-dir") + 1]) / "book.epub", 5)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real(*args)
    monkeypatch.setattr(fetcher, "_calibredb", calibredb)

    result, transient = fetcher._update_one(RR, _entry(RR), 4629)

    assert transient is None and "fewer chapters" in result["error"]
    assert not any(c[0] == "add_format" for c in fake.calls)
