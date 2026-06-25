"""Fic-Beacon fetcher service — the only component that writes to the Calibre library.

A tiny HTTP wrapper around **FanFicFare** + **calibredb**, run as a separate, isolated
container (so the main app never needs Calibre installed and keeps the library read-only
on its side). It downloads/updates a story's EPUB into Calibre and reports back what the
main app needs to track cursors.

Contract:
    POST /fetch  {"url": "<story url>"}
      → 200 {"calibre_id": int, "chapter_count": int,
             "stub": {"old": int, "new": int} | null, "error": null}
      → 200 {"error": "<message>", ...}            (handled failure)

Stub = the site removed old chapters so the existing EPUB is longer than the live work.
FanFicFare refuses to update in place ("Existing epub contains N chapters, web site only
has M"); we then archive the old EPUB as a separate Calibre entry and force-overwrite,
returning {old: N, new: M} so the app can keep chapter labels continuous.

Site logins live in /config/personal.ini (edit it directly in this container).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetcher")

LIBRARY = os.environ.get("CALIBRE_LIBRARY", "/calibre-library")
PERSONAL_INI = os.environ.get("FANFICFARE_INI", "/config/personal.ini")

app = FastAPI(title="fic-beacon-fetcher")

_STUB_RE = re.compile(r"Existing epub contains (\d+) chapters?, web site only has (\d+)")


class FetchRequest(BaseModel):
    url: str


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _calibredb(*args: str) -> subprocess.CompletedProcess:
    return _run(["calibredb", "--with-library", LIBRARY, *args])


def _fanficfare(*args: str, cwd: str) -> subprocess.CompletedProcess:
    base = ["fanficfare"]
    if Path(PERSONAL_INI).exists():
        base += ["-c", PERSONAL_INI]
    return _run([*base, *args], cwd=cwd)


def _find_calibre_id(url: str) -> int | None:
    """Find an existing Calibre book whose `url` identifier matches this story."""
    res = _calibredb("search", f'identifiers:"=url:{url}"')
    out = (res.stdout or "").strip()
    if res.returncode != 0 or not out:
        return None
    # calibredb search prints a comma-separated id list (e.g. "12,15").
    first = out.split(",")[0].strip()
    return int(first) if first.isdigit() else None


def _count_chapters(epub_path: Path) -> int:
    """Count spine documents in an EPUB (rough chapter count; the app re-chapterizes)."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_name = next(n for n in zf.namelist() if n.endswith(".opf"))
            opf = zf.read(opf_name).decode("utf-8", "ignore")
    except Exception:
        return 0
    spine = re.search(r"<spine.*?</spine>", opf, re.DOTALL)
    return len(re.findall(r"<itemref\b", spine.group(0))) if spine else 0


def _only_epub(directory: Path) -> Path | None:
    epubs = list(directory.glob("*.epub"))
    return epubs[0] if epubs else None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "library": LIBRARY}


@app.post("/fetch")
def fetch(req: FetchRequest) -> dict:
    url = req.url.strip()
    if not url:
        return {"error": "empty url"}
    try:
        return _do_fetch(url)
    except Exception as exc:  # never crash the service on one bad story
        logger.exception("fetch failed for %s", url)
        return {"error": str(exc)}


def _do_fetch(url: str) -> dict:
    calibre_id = _find_calibre_id(url)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if calibre_id is None:
            return _fetch_new(url, work)
        return _update_existing(url, calibre_id, work)


def _fetch_new(url: str, work: Path) -> dict:
    """Brand-new story: download a fresh EPUB and add it to Calibre."""
    res = _fanficfare(url, cwd=str(work))
    epub = _only_epub(work)
    if epub is None:
        return {"error": f"fanficfare produced no epub: {res.stderr.strip()[:400]}"}
    add = _calibredb("add", str(epub))
    m = re.search(r"ids?\s*[:#]?\s*(\d+)", add.stdout or "")
    calibre_id = int(m.group(1)) if m else _find_calibre_id(url)
    return {"calibre_id": calibre_id, "chapter_count": _count_chapters(epub), "stub": None}


def _update_existing(url: str, calibre_id: int, work: Path) -> dict:
    """Existing story: export the EPUB, update it in place, handle stubs, re-add the format."""
    exp = _calibredb("export", "--dont-save-cover", "--dont-write-opf",
                     "--to-dir", str(work), "--single-dir", "--formats", "epub", str(calibre_id))
    epub = _only_epub(work)
    if epub is None:
        return {"error": f"could not export calibre book {calibre_id}: {exp.stderr.strip()[:400]}"}

    before = _count_chapters(epub)
    upd = _fanficfare("-u", str(epub.name), cwd=str(work))
    combined = (upd.stdout or "") + (upd.stderr or "")

    stub = None
    m = _STUB_RE.search(combined)
    if m:
        old, new = int(m.group(1)), int(m.group(2))
        # Archive the longer pre-stub EPUB as a separate Calibre entry before overwriting.
        _calibredb("add", str(epub))  # the still-old export, kept as a standalone backup
        # Force a clean re-download over the shorter live version.
        _fanficfare("-u", str(epub.name), "-o", "force_update_epub_always=true", cwd=str(work))
        stub = {"old": old, "new": new}

    after = _count_chapters(epub)
    # Replace the EPUB format on the canonical Calibre book.
    _calibredb("add_format", str(calibre_id), str(epub))
    return {"calibre_id": calibre_id, "chapter_count": after, "stub": stub}
