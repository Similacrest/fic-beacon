"""Fic-Beacon fetcher service — the only component that writes to the Calibre library.

A tiny HTTP wrapper around **FanFicFare** + **calibredb**, run as a separate, isolated
container (so the main app never needs Calibre installed and keeps the library read-only
on its side). It downloads/updates stories' EPUBs into Calibre and reports back what the
main app needs to track cursors.

Async, batched contract (FanFicFare is slow — a run can take ~15 minutes — and cold-starting
one process per story is wasteful):

    POST /fetch  {"urls": ["<url>", ...]}
      → 202 {"job_id": "<id>"}                       (accepted; work runs in the background)

    GET /fetch/{job_id}
      → 200 {"status": "running"|"done"|"unknown",
             "results": [{"url", "calibre_id", "chapter_count",
                          "stub": {"old", "new"} | null, "phase": str,
                          "error": str | null}, ...] | null}

The work runs in a single-worker thread pool, so all `calibredb` writes are serialized
(the library has exactly one writer). New stories are downloaded together in one
`fanficfare -i` pass (one warm process); existing stories are updated one at a time.

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
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fetcher")

LIBRARY = os.environ.get("CALIBRE_LIBRARY", "/calibre-library")
PERSONAL_INI = os.environ.get("FANFICFARE_INI", "/config/personal.ini")
RETRY_BASE_SECONDS = float(os.environ.get("FETCHER_RETRY_BASE_SECONDS", "30"))
RETRY_ATTEMPTS = 3
JOB_TTL_SECONDS = 3600  # keep a finished job's result available for an hour, then prune
# Per-subprocess wall-clock caps. FanFicFare can legitimately run ~15 min on a big story, so its
# cap is generous; calibredb operations are local and quick. Without these a single hung site
# socket would block the lone worker thread forever (and every queued job behind it).
FANFICFARE_TIMEOUT = float(os.environ.get("FETCHER_FANFICFARE_TIMEOUT", "1200"))  # 20 min
CALIBREDB_TIMEOUT = float(os.environ.get("FETCHER_CALIBREDB_TIMEOUT", "600"))  # 10 min

app = FastAPI(title="fic-beacon-fetcher")

# A "needs force" message from FanFicFare. The chapter-shrink variant (a true *stub*) is the
# common case; the generic guidance covers metadata/chapter mismatches that also want force.
_STUB_RE = re.compile(r"Existing epub contains (\d+) chapters?, web site only has (\d+)")
_NEEDS_FORCE_RE = re.compile(r"force_update_epub_always|Use Overwrite or", re.IGNORECASE)
# Transient site/network failures worth a retry (vs. a permanent "no such story" error).
_TRANSIENT_RE = re.compile(
    r"\b(50[234]|429|timed? ?out|timeout|connection|temporarily|rate.?limit|reset by peer)\b",
    re.IGNORECASE,
)

# One worker → calibredb writes never overlap (single-writer invariant).
_executor = ThreadPoolExecutor(max_workers=1)
# job_id -> {"status": "running"|"done", "results": [ {...}, ... ], "finished_at": float|None}
_jobs: dict[str, dict] = {}


class FetchRequest(BaseModel):
    urls: list[str]


def _run(cmd: list[str], cwd: str | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    logger.info("run: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the child. Surface a non-zero result whose text the
        # callers' returncode/stderr checks treat as failure; "timed out" also matches
        # _TRANSIENT_RE, so transient-retry paths back off and the worker thread always frees.
        logger.warning("timeout after %.0fs: %s", timeout or 0, " ".join(cmd))
        out = exc.stdout.decode("utf-8", "ignore") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout=out, stderr=f"timed out after {timeout:.0f}s")


def _calibredb(*args: str) -> subprocess.CompletedProcess:
    return _run(["calibredb", "--with-library", LIBRARY, *args], timeout=CALIBREDB_TIMEOUT)


def _fanficfare(*args: str, cwd: str) -> subprocess.CompletedProcess:
    base = ["fanficfare", "--non-interactive"]
    if Path(PERSONAL_INI).exists():
        base += ["-c", PERSONAL_INI]
    return _run([*base, *args], cwd=cwd, timeout=FANFICFARE_TIMEOUT)


def _with_retry(label: str, fn):
    """Run fn(), retrying only on transient failures with exponential backoff (3 attempts).

    fn returns (result_dict, transient_error_message | None). A None error means done
    (success or a permanent/handled failure) — no retry. The last transient error is
    surfaced as the result's error if every attempt fails.
    """
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        result, transient = fn()
        if transient is None:
            return result
        last_err = transient
        if attempt < RETRY_ATTEMPTS - 1:
            delay = RETRY_BASE_SECONDS * (2 ** attempt)
            logger.warning("%s: transient failure (%s); retry %d/%d in %.0fs",
                           label, transient, attempt + 1, RETRY_ATTEMPTS - 1, delay)
            time.sleep(delay)
    result["error"] = f"failed after {RETRY_ATTEMPTS} attempts: {last_err}"
    return result


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


def _epub_source_url(epub_path: Path) -> str | None:
    """The story URL FanFicFare wrote into the EPUB (dc:source / a url: identifier)."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_name = next(n for n in zf.namelist() if n.endswith(".opf"))
            opf = zf.read(opf_name).decode("utf-8", "ignore")
    except Exception:
        return None
    m = re.search(r"<dc:source[^>]*>([^<]+)</dc:source>", opf)
    if m:
        return m.group(1).strip()
    m = re.search(r"<dc:identifier[^>]*>(?:url:)?(https?://[^<]+)</dc:identifier>", opf)
    return m.group(1).strip() if m else None


def _only_epub(directory: Path) -> Path | None:
    epubs = list(directory.glob("*.epub"))
    return epubs[0] if epubs else None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "library": LIBRARY}


@app.post("/fetch", status_code=202)
def fetch(req: FetchRequest) -> dict:
    urls = [u.strip() for u in req.urls if u and u.strip()]
    job_id = uuid.uuid4().hex
    _prune_jobs()
    _jobs[job_id] = {
        "status": "running",
        "finished_at": None,
        "results": [{"url": u, "phase": "queued", "calibre_id": None,
                     "chapter_count": None, "stub": None, "error": None} for u in urls],
    }
    _executor.submit(_run_job, job_id)
    return {"job_id": job_id}


@app.get("/fetch/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "unknown", "results": None}, status_code=404)
    return {"status": job["status"], "results": job["results"]}


def _prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for jid in [j for j, v in _jobs.items()
                if v["status"] == "done" and (v["finished_at"] or 0) < cutoff]:
        _jobs.pop(jid, None)


def _run_job(job_id: str) -> None:
    """Background worker: process every URL in the job, updating per-URL phase as it goes."""
    job = _jobs[job_id]
    entries = job["results"]
    try:
        by_url = {e["url"]: e for e in entries}
        existing: list[str] = []
        new: list[str] = []
        for url in by_url:
            (existing if _find_calibre_id(url) is not None else new).append(url)

        if new:
            _process_new_batch(new, by_url)
        for url in existing:
            entry = by_url[url]
            entry["phase"] = "downloading"
            try:
                _with_retry(url, lambda u=url, e=entry: _update_one(u, e))
            except Exception as exc:  # never let one story sink the job
                logger.exception("update failed for %s", url)
                entry["error"] = str(exc)
            entry["phase"] = "error" if entry["error"] else "done"
    except Exception as exc:
        logger.exception("job %s failed", job_id)
        for e in entries:
            if e["phase"] not in ("done", "error"):
                e["error"], e["phase"] = str(exc), "error"
    finally:
        job["status"] = "done"
        job["finished_at"] = time.time()


def _process_new_batch(urls: list[str], by_url: dict[str, dict]) -> None:
    """Download all brand-new stories in a single warm `fanficfare -i` pass, add to Calibre."""
    for u in urls:
        by_url[u]["phase"] = "downloading"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        infile = work / "urls.txt"
        infile.write_text("\n".join(urls) + "\n")
        _fanficfare("-i", str(infile), cwd=str(work))
        produced = list(work.glob("*.epub"))
        matched: set[str] = set()
        for epub in produced:
            src = _epub_source_url(epub)
            entry = by_url.get(src) if src else None
            if entry is None:  # fall back to single unmatched url if exactly one remains
                remaining = [u for u in urls if u not in matched]
                entry = by_url[remaining[0]] if len(remaining) == 1 else None
            if entry is None:
                continue
            add = _calibredb("add", str(epub))
            m = re.search(r"ids?\s*[:#]?\s*(\d+)", add.stdout or "")
            entry["calibre_id"] = int(m.group(1)) if m else _find_calibre_id(entry["url"])
            entry["chapter_count"] = _count_chapters(epub)
            entry["phase"] = "done"
            matched.add(entry["url"])
        for u in urls:  # any new url that produced no epub
            if by_url[u]["phase"] != "done":
                by_url[u]["error"] = "fanficfare produced no epub"
                by_url[u]["phase"] = "error"


def _update_one(url: str, entry: dict) -> tuple[dict, str | None]:
    """Update one existing story in place. Returns (entry, transient_error|None) for retry."""
    calibre_id = _find_calibre_id(url)
    if calibre_id is None:  # vanished between split and now → treat as new
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            res = _fanficfare(url, cwd=str(work))
            epub = _only_epub(work)
            if epub is None:
                combined = (res.stdout or "") + (res.stderr or "")
                return entry, combined if _TRANSIENT_RE.search(combined) else _fail(entry, combined)
            add = _calibredb("add", str(epub))
            m = re.search(r"ids?\s*[:#]?\s*(\d+)", add.stdout or "")
            entry["calibre_id"] = int(m.group(1)) if m else _find_calibre_id(url)
            entry["chapter_count"] = _count_chapters(epub)
            return entry, None

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        exp = _calibredb("export", "--dont-save-cover", "--dont-write-opf",
                         "--to-dir", str(work), "--single-dir", "--formats", "epub",
                         str(calibre_id))
        epub = _only_epub(work)
        if epub is None:
            return entry, _fail(entry, f"could not export book {calibre_id}: {exp.stderr.strip()[:300]}")

        upd = _fanficfare("-u", str(epub.name), cwd=str(work))
        combined = (upd.stdout or "") + (upd.stderr or "")

        stub = None
        m = _STUB_RE.search(combined)
        if m:  # site dropped chapters → archive old EPUB, then force a clean re-download
            old, new = int(m.group(1)), int(m.group(2))
            _calibredb("add", str(epub))  # standalone backup of the longer pre-stub EPUB
            _fanficfare("-u", str(epub.name), "-o", "force_update_epub_always=true", cwd=str(work))
            stub = {"old": old, "new": new}
        elif _NEEDS_FORCE_RE.search(combined):  # non-shrink mismatch → just force, no archive
            _fanficfare("-u", str(epub.name), "-o", "force_update_epub_always=true", cwd=str(work))
        elif upd.returncode != 0 and _TRANSIENT_RE.search(combined):
            return entry, combined  # retryable

        _calibredb("add_format", str(calibre_id), str(epub))
        entry["calibre_id"] = calibre_id
        entry["chapter_count"] = _count_chapters(epub)
        entry["stub"] = stub
        return entry, None


def _fail(entry: dict, message: str) -> None:
    """Record a permanent (non-retryable) error on an entry; returns None (no retry)."""
    entry["error"] = message.strip()[:400]
    return None
