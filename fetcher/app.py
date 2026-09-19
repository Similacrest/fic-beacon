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
                          "stub": {"old", "new", "old_urls", "new_urls"} | null, "phase": str,
                          "error": str | null}, ...] | null}

The work runs in a single-worker thread pool, so all `calibredb` writes are serialized
(the library has exactly one writer). New stories are downloaded together in one
`fanficfare -i` pass (one warm process); existing stories are updated one at a time.

Stub = the site removed old chapters so the existing EPUB is longer than the live work.
FanFicFare refuses to update in place ("Existing epub contains N chapters, web site only
has M"); we then archive the old EPUB as a separate Calibre entry and force-overwrite,
returning {old: N, new: M, old_urls, new_urls} — the ordered per-chapter canonical URLs of
the pre- and post-stub EPUBs — so the app can match chapters by identity (labels stay exact
past a gap anywhere, and the reader's cursor remaps to the first surviving chapter).

Site logins live in /config/personal.ini (edit it directly in this container).
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import subprocess
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

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


_CHAPTERURL_RE = re.compile(
    rb'<meta[^>]*\bname=["\']chapterurl["\'][^>]*\bcontent=["\']([^"\']+)["\']'
)


def _chapter_urls(epub_path: Path) -> list[str]:
    """Ordered per-chapter canonical URLs (FanFicFare <meta name="chapterurl">), in spine order.

    Only spine documents that actually carry a chapterurl are included — the same "real chapter"
    definition the app's chapterizer uses, so these indices line up with the app's physical
    `cursor_chapter_index`. Used on a stub to diff pre- vs post-removal bodies by chapter identity.
    Returns [] if the EPUB is unreadable or carries no chapterurls (non-FanFicFare book) — the app
    then falls back to the count-only linear stub path.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_name = next(n for n in zf.namelist() if n.endswith(".opf"))
            opf = zf.read(opf_name).decode("utf-8", "ignore")
            opf_dir = posixpath.dirname(opf_name)
            # manifest id -> href (attribute order varies, so scan each <item> tag)
            manifest: dict[str, str] = {}
            for tag in re.findall(r"<item\b[^>]*?>", opf):
                idm = re.search(r'\bid=["\']([^"\']+)["\']', tag)
                hrefm = re.search(r'\bhref=["\']([^"\']+)["\']', tag)
                if idm and hrefm:
                    manifest[idm.group(1)] = hrefm.group(1)
            spine = re.search(r"<spine.*?</spine>", opf, re.DOTALL)
            if not spine:
                return []
            names = set(zf.namelist())
            urls: list[str] = []
            for idref in re.findall(r'<itemref\b[^>]*?\bidref=["\']([^"\']+)["\']', spine.group(0)):
                href = manifest.get(idref)
                if not href:
                    continue
                name = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
                if name not in names:  # fall back to a basename match
                    base = href.rsplit("/", 1)[-1]
                    name = next((n for n in names if n.rsplit("/", 1)[-1] == base), None)
                    if name is None:
                        continue
                m = _CHAPTERURL_RE.search(zf.read(name))
                if m:
                    urls.append(m.group(1).decode("utf-8", "replace").strip())
            return urls
    except Exception:
        return []


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


def _story_key(url: str | None) -> str:
    """A site-stable identity for a story URL, so a submitted URL can be matched to the canonical
    one FanFicFare writes into the EPUB.

    FanFicFare rewrites URLs (FFN `/s/123` → `/s/123/1/Story-Title`, a dropped `www.`, a trailing
    slash, XenForo `/threads/slug.123/page-2` → `/threads/slug.123/`), so an exact-string compare
    misses a perfectly good download. We key on `host` + the story's numeric id where there is one
    (XenForo's id is the number after the last dot of the thread slug; elsewhere the first long run
    of digits in the path), falling back to the normalised path. The query string is ignored, so
    query-keyed archives (`viewstory.php?sid=N`) would collide in one batch — none of the supported
    sites (FFN, AO3, SB/SV/QQ, RoyalRoad, Wattpad) key on it.
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = parsed.path
    xf = re.search(r"/threads/[^/]*?\.(\d+)(?:/|$)", path)
    if xf:
        return f"{host}|{xf.group(1)}"
    num = re.search(r"\d{3,}", path)
    if num:
        return f"{host}|{num.group(0)}"
    return f"{host}|{path.rstrip('/').lower()}"


_STATUS_TOKENS = {
    "completed", "complete", "in-progress", "in progress", "hiatus", "abandoned",
    "incomplete", "ongoing", "published",
}


def _epub_status(epub_path: Path) -> str | None:
    """The story's publication status as FanFicFare recorded it (e.g. `Completed`, `In-Progress`).

    FanFicFare writes it in two places: the title page's `<b>Status:</b> X<br />` line and as a
    `dc:subject` in the OPF. The title page is the specific one, so prefer it; fall back to a
    recognised status token among the OPF subjects.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names = zf.namelist()
            title = next((n for n in names if n.endswith("title_page.xhtml")), None)
            if title:
                m = re.search(r"<b>Status:</b>\s*([^<]+?)\s*<", zf.read(title).decode("utf-8", "ignore"))
                if m and m.group(1).strip():
                    return m.group(1).strip()
            opf_name = next((n for n in names if n.endswith(".opf")), None)
            if opf_name:
                opf = zf.read(opf_name).decode("utf-8", "ignore")
                for subject in re.findall(r"<dc:subject>([^<]*)</dc:subject>", opf):
                    if subject.strip().lower() in _STATUS_TOKENS:
                        return subject.strip()
    except Exception:
        return None
    return None


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
                     "chapter_count": None, "stub": None, "error": None,
                     "story_url": None, "story_status": None} for u in urls],
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


def _tail(text: str, limit: int = 300) -> str:
    """The last few non-empty lines of subprocess output — where FanFicFare puts the reason."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-4:])[-limit:]


def _download_new(urls: list[str], by_url: dict[str, dict]) -> str:
    """One `fanficfare` pass over `urls` (a warm `-i` batch, or a single URL), adding every EPUB
    it produced to Calibre and marking its entry done. Returns FanFicFare's combined output.

    Each EPUB is matched back to a submitted URL by **story identity** (`_story_key`), not exact
    string: FanFicFare canonicalises URLs, and an exact compare used to report a successful
    download as "no epub" *and* silently discard it. A lone unmatched EPUB against a lone
    unmatched URL still pairs up as a last resort.
    """
    for u in urls:
        by_url[u]["phase"] = "downloading"
    by_key = {_story_key(u): u for u in urls}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        infile = work / "urls.txt"
        infile.write_text("\n".join(urls) + "\n")
        res = _fanficfare("-i", str(infile), cwd=str(work))
        output = (res.stdout or "") + (res.stderr or "")
        matched: set[str] = set()
        for epub in sorted(work.glob("*.epub")):
            src = _epub_source_url(epub)
            url = by_key.get(_story_key(src)) if src else None
            if url is None or url in matched:  # fall back to the single URL still unmatched
                remaining = [u for u in urls if u not in matched]
                url = remaining[0] if len(remaining) == 1 else None
            if url is None:
                logger.warning("no submitted URL matches produced epub %s (source %s)", epub.name, src)
                continue
            entry = by_url[url]
            add = _calibredb("add", str(epub))
            if add.returncode != 0:
                _fail(entry, f"calibredb add failed: {_tail((add.stdout or '') + (add.stderr or ''))}")
                entry["phase"] = "error"
                matched.add(url)
                continue
            m = re.search(r"ids?\s*[:#]?\s*(\d+)", add.stdout or "")
            entry["calibre_id"] = int(m.group(1)) if m else _find_calibre_id(src or url)
            entry["chapter_count"] = _count_chapters(epub)
            entry["story_url"] = src
            entry["story_status"] = _epub_status(epub)
            entry["phase"] = "done"
            matched.add(url)
    return output


def _process_new_batch(urls: list[str], by_url: dict[str, dict]) -> None:
    """Download brand-new stories and add them to Calibre.

    A warm batch first; URLs that come back empty are retried with exponential backoff while the
    failure looks transient (a 429/503/timeout used to become a permanent error on the first try —
    only the EXISTING path retried). Whatever is still missing is reported with FanFicFare's real
    output instead of a bare "produced no epub". When several URLs fail together the batch output
    can't be attributed to any one of them, so each is re-run alone to get its own reason.
    """
    def _pending() -> list[str]:
        return [u for u in urls if by_url[u]["phase"] not in ("done", "error")]

    pending = list(urls)
    output = ""
    ran = len(pending)  # how many URLs the last run covered (1 ⇒ its output is that URL's alone)
    for attempt in range(RETRY_ATTEMPTS):
        ran = len(pending)
        output = _download_new(pending, by_url)
        pending = _pending()
        if not pending or not _TRANSIENT_RE.search(output):
            break
        if attempt < RETRY_ATTEMPTS - 1:
            delay = RETRY_BASE_SECONDS * (2 ** attempt)
            logger.warning("new-story batch: transient failure; retry %d/%d in %.0fs",
                           attempt + 1, RETRY_ATTEMPTS - 1, delay)
            time.sleep(delay)
    if not pending:
        return

    if ran > 1:  # the output covers several stories — re-run each failure alone for its own reason
        for u in list(pending):
            single = _download_new([u], by_url)
            if by_url[u]["phase"] not in ("done", "error"):
                _no_epub(by_url[u], single, transient=bool(_TRANSIENT_RE.search(single)))
        return
    _no_epub(by_url[pending[0]], output, transient=bool(_TRANSIENT_RE.search(output)),
             attempts=RETRY_ATTEMPTS)


def _no_epub(entry: dict, output: str, transient: bool, attempts: int | None = None) -> None:
    """Record why a new story produced no EPUB, using what FanFicFare actually said."""
    reason = _tail(output) or "no output"
    if not transient:
        prefix = "fanficfare produced no epub"
    else:
        prefix = f"failed after {attempts} attempts" if attempts else "transient error"
    entry["error"] = f"{prefix}: {reason}"[:400]
    entry["phase"] = "error"


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
            entry["story_url"] = _epub_source_url(epub)
            entry["story_status"] = _epub_status(epub)
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
            old_urls = _chapter_urls(epub)  # per-chapter identity BEFORE the overwrite
            _fanficfare("-u", str(epub.name), "-o", "force_update_epub_always=true", cwd=str(work))
            new_urls = _chapter_urls(epub)  # …and AFTER, so the app can diff by URL
            stub = {"old": old, "new": new, "old_urls": old_urls, "new_urls": new_urls}
        elif _NEEDS_FORCE_RE.search(combined):  # non-shrink mismatch → just force, no archive
            _fanficfare("-u", str(epub.name), "-o", "force_update_epub_always=true", cwd=str(work))
        elif upd.returncode != 0 and _TRANSIENT_RE.search(combined):
            return entry, combined  # retryable

        _calibredb("add_format", str(calibre_id), str(epub))
        entry["calibre_id"] = calibre_id
        entry["chapter_count"] = _count_chapters(epub)
        entry["story_url"] = _epub_source_url(epub)
        entry["story_status"] = _epub_status(epub)
        entry["stub"] = stub
        return entry, None


def _fail(entry: dict, message: str) -> None:
    """Record a permanent (non-retryable) error on an entry; returns None (no retry)."""
    entry["error"] = message.strip()[:400]
    return None
