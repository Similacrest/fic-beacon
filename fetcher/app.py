"""Fic-Beacon fetcher service — the only component that writes to the Calibre library.

A tiny HTTP wrapper around **FanFicFare** + **calibredb**, run as a separate, isolated
container (so the main app never needs Calibre installed and keeps the library read-only
on its side). It downloads/updates stories' EPUBs into Calibre and reports back what the
main app needs to track cursors.

Async, batched contract (FanFicFare is slow — a run can take ~15 minutes — and cold-starting
one process per story is wasteful):

    POST /fetch  {"urls": ["<url>", ...], "calibre_ids": {"<url>": <id>, ...}?}
      → 202 {"job_id": "<id>"}                       (accepted; work runs in the background)
      `calibre_ids` (optional) pins a URL to the Calibre entry the app already reads for it: that
      exact entry is updated, with no URL search. Without it (a new story, or an older app) the
      entry is found by story identity (`_story_key`), not by exact `url:` string.

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
past a gap anywhere, and the reader's cursor remaps to the first surviving chapter). The archive
keeps its story URL under a `url-archive:` identifier, so it can never be found (and updated) by a
`url:` lookup in place of the live entry.

Site logins live in /config/personal.ini (edit it directly in this container).
"""
from __future__ import annotations

import json
import logging
import hmac
import os
import posixpath
import re
import subprocess
import threading
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
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

# FlareSolverr fallback (see `_ff`). A source that gets FLARE_AFTER consecutive 403s is fetched
# through FlareSolverr (config file layered over personal.ini; it holds no logins). The solve costs
# ~20 s *per request*, so a many-chapter story needs far more than FANFICFARE_TIMEOUT: the flat cap
# below (default 90 min, ~200 chapters) replaces it for those runs. A flagged source is probed
# directly again every FLARE_PROBE_SECONDS (24 h), and the flag clears when the probe works.
FLARE_INI = os.environ.get("FANFICFARE_FLARE_INI", "/config/flaresolverr.ini")
FLARE_TIMEOUT = float(os.environ.get("FETCHER_FLARESOLVERR_TIMEOUT", "5400"))
FLARE_AFTER = int(os.environ.get("FETCHER_FLARESOLVERR_AFTER", "2"))
FLARE_PROBE_SECONDS = float(os.environ.get("FETCHER_FLARESOLVERR_PROBE_SECONDS", "86400"))
STATE_DIR = Path(os.environ.get("FETCHER_STATE_DIR", "/state"))

FETCHER_TOKEN = os.environ.get("FETCHER_TOKEN", "")


def _require_token(x_fetcher_token: str | None = Header(None)) -> None:
    """Shared-secret gate (when FETCHER_TOKEN is set): the fetcher writes the Calibre library and
    makes outbound requests for any URL it is given, so it must not be callable by every
    container on the network. /health stays open for container healthchecks."""
    if FETCHER_TOKEN and not hmac.compare_digest(
        (x_fetcher_token or "").encode(), FETCHER_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="bad fetcher token")


def _is_http_url(url: str) -> bool:
    """Plain http(s) URL only — a value starting with `-` would be parsed as a FanFicFare option."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.hostname)


app = FastAPI(title="fic-beacon-fetcher")

# A "needs force" message from FanFicFare. The chapter-shrink variant (a true *stub*) is the
# common case; the generic guidance covers metadata/chapter mismatches that also want force.
_STUB_RE = re.compile(r"Existing epub contains (\d+) chapters?, web site only has (\d+)")
_NEEDS_FORCE_RE = re.compile(r"force_update_epub_always|Use Overwrite or", re.IGNORECASE)
# FanFicFare's writer refuses to overwrite an output file whose mtime *date* is later than the
# story's last-updated date ("File(x.epub) Updated(…) more recently than Story(…) - Skipping"). It
# only gets that far when the site has more chapters than the EPUB, so this always means "new
# chapters were found and then not written".
_SKIPPED_RE = re.compile(r"more recently than Story\(.*?\)\s*-\s*Skipping", re.IGNORECASE)
# Transient site/network failures worth a retry (vs. a permanent "no such story" error).
_TRANSIENT_RE = re.compile(
    r"\b(50[234]|429|timed? ?out|timeout|connection|temporarily|rate.?limit|reset by peer)\b",
    re.IGNORECASE,
)

# A Cloudflare/site refusal. Matched on FanFicFare's own error line, never on a bare "no epub".
_403_RE = re.compile(r"HTTPErrorFFF[^\n]*\b403\b|\b403\b[^\n]*Forbidden", re.IGNORECASE)
# Marker on a FlareSolverr-run timeout: it is not retried (a retry would hit the same limit and
# tie FlareSolverr up for another long stretch).
_NO_RETRY = "(not retried)"

# One worker → calibredb writes never overlap (single-writer invariant).
_executor = ThreadPoolExecutor(max_workers=1)
# job_id -> {"status": "running"|"done", "results": [ {...}, ... ], "finished_at": float|None}
_jobs: dict[str, dict] = {}


class FetchRequest(BaseModel):
    urls: list[str]
    # url -> Calibre id the app already reads for it (see the contract above). Optional.
    calibre_ids: dict[str, int] = {}


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


def _fanficfare(*args: str, cwd: str, flare: bool = False) -> subprocess.CompletedProcess:
    base = ["fanficfare", "--non-interactive"]
    if Path(PERSONAL_INI).exists():
        base += ["-c", PERSONAL_INI]
    if not flare:
        return _run([*base, *args], cwd=cwd, timeout=FANFICFARE_TIMEOUT)
    # Later -c files override earlier ones, so the flaresolverr keys win and logins stay in personal.ini.
    res = _run([*base, "-c", FLARE_INI, *args], cwd=cwd, timeout=FLARE_TIMEOUT)
    if res.returncode == 124:
        res.stderr = f"flaresolverr fetch timed out after {FLARE_TIMEOUT:.0f}s {_NO_RETRY}"
    return res


def _transient(text: str) -> bool:
    """Worth a retry: a transient site/network error, but not a FlareSolverr-run timeout."""
    return bool(_TRANSIENT_RE.search(text or "")) and _NO_RETRY not in (text or "")


# ---- per-source 403 state (survives restarts) ------------------------------------------------
_state_lock = threading.Lock()
_state: dict[str, dict] | None = None


def _state_load() -> dict[str, dict]:
    global _state
    if _state is None:
        try:
            _state = json.loads((STATE_DIR / "flaresolverr.json").read_text())
        except (OSError, ValueError):
            _state = {}
    return _state


def _state_save() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / "flaresolverr.json.tmp"
        tmp.write_text(json.dumps(_state))
        tmp.replace(STATE_DIR / "flaresolverr.json")
    except OSError as exc:  # keep working from memory; the counter just won't survive a restart
        logger.warning("could not persist flaresolverr state in %s: %s", STATE_DIR, exc)


def _ff(urls: list[str], *args: str, cwd: str) -> subprocess.CompletedProcess:
    """Run FanFicFare directly, falling back to FlareSolverr for a source that keeps getting 403s.

    - FLARE_AFTER consecutive 403s on a source flag it: that run, and later ones, go through
      FlareSolverr. Any other outcome (success, or a non-403 error) resets the count.
    - A flagged source is probed directly at most every FLARE_PROBE_SECONDS. A probe that works
      clears the flag; one that 403s falls straight back to FlareSolverr in the same run.
    - A 403 *through* FlareSolverr is recorded as an ordinary error: no loop, the next cycle's
      attempt (and the existing give-up cap on the app side) applies.
    The result carries `.via_flare`. Counting is per source, so it is only attributed when `urls`
    is a single story; a batch is just run direct (its failures are re-run alone by the caller).
    """
    keys = [_story_key(u) for u in urls]
    now = time.time()
    with _state_lock:
        st = _state_load()
        flagged = [k for k in keys if st.get(k, {}).get("flare")]
        stay_on_flare = any(now - st[k].get("probed_at", 0) < FLARE_PROBE_SECONDS for k in flagged)
    can_flare = Path(FLARE_INI).exists()

    def finish(res, via):
        res.via_flare = via
        return res

    if stay_on_flare and can_flare:
        res = _fanficfare(*args, cwd=cwd, flare=True)
        logger.info("fetched via flaresolverr: %s", " ".join(urls))
        return finish(res, True)

    res = _fanficfare(*args, cwd=cwd)
    out = (res.stdout or "") + (res.stderr or "")
    if not _403_RE.search(out):
        with _state_lock:  # success or a non-403 error: the 403 streak is over, a probe worked
            if any(k in st for k in keys):
                if flagged:
                    logger.info("direct fetch works again, flaresolverr off: %s", " ".join(urls))
                for k in keys:
                    st.pop(k, None)
                _state_save()
        return finish(res, False)
    if len(keys) != 1:
        return finish(res, False)

    k = keys[0]
    with _state_lock:
        rec = st.setdefault(k, {"count": 0})
        rec["count"] = rec.get("count", 0) + 1
        escalate = bool(rec.get("flare")) or rec["count"] >= FLARE_AFTER
        if escalate:
            rec["flare"], rec["probed_at"] = True, now
        _state_save()
    if not escalate:
        return finish(res, False)
    if not can_flare:
        logger.warning("%s: 403s, but %s is missing - cannot use flaresolverr", urls[0], FLARE_INI)
        return finish(res, False)
    logger.info("%s: %d consecutive 403s - retrying through flaresolverr", urls[0], rec["count"])
    res = _fanficfare(*args, cwd=cwd, flare=True)
    return finish(res, True)


def _fanficfare_update(epub: Path, *opts: str, cwd: str, url: str) -> subprocess.CompletedProcess:
    """`fanficfare -u` an exported EPUB in place, without the writer's mtime short-circuit.

    `calibredb export` stamps the file with mtime=now, and FanFicFare skips writing when that date
    is later than the story's last update — so an update only landed when the author posted on the
    fetch's own calendar day. Backdating the file and passing `always_overwrite` each defeat that on
    their own; both are applied so a lost personal.ini line or a changed FanFicFare check can't
    bring it back. Re-backdated before every call: a previous pass wrote the file at mtime=now."""
    os.utime(epub, (0, 0))
    return _ff([url], "-u", f"./{epub.name}", "-o", "always_overwrite=true", *opts, cwd=cwd)


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


def _quote(value: str) -> str:
    """Escape a value for a calibre search expression inside double quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _search_ids(query: str) -> list[int]:
    """Ids matching a calibre search expression (`calibredb search` prints `12,15`)."""
    res = _calibredb("search", query)
    out = (res.stdout or "").strip()
    if res.returncode != 0 or not out:
        return []
    return [int(t) for t in re.split(r"[,\s]+", out) if t.isdigit()]


def _identifier_urls(ids: list[int]) -> dict[int, str] | None:
    """{id: its `url:` identifier} for `ids`, or None if calibredb's listing can't be read.

    `--for-machine` gives JSON where `identifiers` is a `{scheme: value}` dict (older builds: a
    `scheme:value,…` string), so both shapes are accepted.
    """
    res = _calibredb("list", "--for-machine", "--fields", "identifiers",
                     "--search", " or ".join(f"id:{i}" for i in ids))
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    out: dict[int, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            return None
        ident = row.get("identifiers")
        if isinstance(ident, str):
            ident = dict(p.split(":", 1) for p in ident.split(",") if ":" in p)
        if isinstance(row.get("id"), int) and isinstance(ident, dict):
            out[row["id"]] = str(ident.get("url") or "")
    # A listing that doesn't account for every id asked about is a shape we don't understand, not
    # "none of them match": report unreadable so callers don't filter every hit away.
    return out if all(i in out for i in ids) else None


def _find_calibre_ids(url: str, strict: bool = False) -> list[int]:
    """Every live Calibre entry whose `url:` identifier is this story, lowest id first.

    `strict` returns only hits whose identifier was actually read and key-checked — an unreadable
    listing then means "no match" rather than falling back to the raw search hits. Use it before
    any action that overwrites an entry on the strength of the match.

    Matching is by **story identity** (`_story_key`), never exact string: Calibre's identifier is
    whatever spelling FanFicFare wrote (`/fiction/186926`), the app may hold another
    (`/fiction/186926/some-slug/`), and `calibredb`'s `=url:X` is a *contains* match on the value,
    so the exact string misses the former and a short id can also hit a longer one
    (`…/1188910` for `…/118891`). The search is therefore only a cheap pre-filter on the story's
    numeric id; each hit's identifier is then checked against the key.

    Archive copies kept on a stub carry a `url-archive:` identifier, so they never match here.
    """
    key = _story_key(url)
    if not key:
        return []
    ids = _search_ids(f'identifiers:"=url:{_quote(url)}"')
    needle = key.split("|", 1)[1]
    if needle:
        ids += [i for i in _search_ids(f'identifiers:"=url:{_quote(needle)}"') if i not in ids]
    if not ids:
        return []
    urls = _identifier_urls(ids)
    if urls is not None:
        ids = [i for i in ids if _story_key(urls.get(i)) == key]
    elif strict:
        return []
    return sorted(set(ids))


def _find_calibre_id(url: str, strict: bool = False) -> int | None:
    """The live Calibre entry for this story, or None. With several matches the lowest id wins
    (the fetcher adds a stub's archive *after* the live entry, so it always has the higher id)."""
    ids = _find_calibre_ids(url, strict)
    if len(ids) > 1:
        logger.warning("%s matches several Calibre entries %s — using %s", url, ids, ids[0])
    return ids[0] if ids else None


def _calibre_id_exists(calibre_id: int) -> bool:
    return calibre_id in _search_ids(f"id:{calibre_id}")


def _added_id(add: subprocess.CompletedProcess) -> int | None:
    """The id `calibredb add` reports ("Added book ids: 4700"), or None (refused / unparsable)."""
    m = re.search(r"ids?\s*[:#]?\s*(\d+)", add.stdout or "")
    return int(m.group(1)) if m else None


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


@app.post("/fetch", status_code=202, dependencies=[Depends(_require_token)])
def fetch(req: FetchRequest) -> dict:
    urls = [u.strip() for u in req.urls if u and u.strip()]
    bad = [u for u in urls if not _is_http_url(u)]
    if bad:
        raise HTTPException(status_code=422, detail=f"not an http(s) URL: {bad[0][:80]!r}")
    job_id = uuid.uuid4().hex
    _prune_jobs()
    _jobs[job_id] = {
        "status": "running",
        "finished_at": None,
        "pinned": {u: i for u, i in req.calibre_ids.items() if u in urls},
        "results": [{"url": u, "phase": "queued", "calibre_id": None,
                     "chapter_count": None, "stub": None, "error": None,
                     "story_url": None, "story_status": None, "via": None} for u in urls],
    }
    _executor.submit(_run_job, job_id)
    return {"job_id": job_id}


@app.get("/fetch/{job_id}", dependencies=[Depends(_require_token)])
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
        pinned: dict[str, int] = job.get("pinned") or {}
        existing: dict[str, int] = {}  # url -> the Calibre entry to update
        new: list[str] = []
        for url in by_url:
            cid = pinned.get(url)
            if cid is not None and _calibre_id_exists(cid):
                existing[url] = cid  # the app told us exactly which entry it reads
                continue
            if cid is not None:
                logger.warning("pinned calibre id %s for %s no longer exists — looking it up", cid, url)
            cid = _find_calibre_id(url)
            if cid is not None:
                existing[url] = cid
            else:
                new.append(url)

        if new:
            _process_new_batch(new, by_url)
        for url, cid in existing.items():
            entry = by_url[url]
            entry["phase"] = "downloading"
            try:
                _with_retry(url, lambda u=url, e=entry, c=cid: _update_one(u, e, c))
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
        res = _ff(urls, "-i", str(infile), cwd=str(work))
        if getattr(res, "via_flare", False):
            for u in urls:
                by_url[u]["via"] = "flaresolverr"
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
            calibre_id = _added_id(add)
            if calibre_id is None:
                # Exit 0 but nothing added: calibredb refuses a book whose title+author match an
                # existing one. If that entry is verifiably this very story (a lookup miss), refresh
                # it in place. Otherwise fail loudly and let a human decide — force-adding would
                # create the duplicate entry this whole path exists to avoid, and the "different work
                # sharing a title and author" case it would serve is rare.
                calibre_id = _find_calibre_id(src or url, strict=True)
                if calibre_id is not None:
                    _calibredb("add_format", str(calibre_id), str(epub))
            if calibre_id is None:
                _fail(entry, "calibredb added nothing (a duplicate title and author is the usual "
                      "cause) and no library entry has this story's url: identifier — add it by hand "
                      "or fix the existing entry's url: identifier. calibredb said: "
                      f"{_tail((add.stdout or '') + (add.stderr or ''))}")
                entry["phase"] = "error"
                matched.add(url)
                continue
            entry["calibre_id"] = calibre_id
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
        if not pending or not _transient(output):
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
                _no_epub(by_url[u], single, transient=_transient(single))
        return
    _no_epub(by_url[pending[0]], output, transient=_transient(output),
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


def _archive_copy(epub: Path, url: str) -> None:
    """Keep the pre-stub EPUB as its own Calibre entry, findable but never mistaken for the story.

    `-d` matters: the archive has the same title+author as the live entry, which a plain
    `calibredb add` silently refuses as a duplicate (so no archive was ever kept). Its story URL is
    then moved from `url:` to `url-archive:` so a later `url:` lookup can't land on it and update
    the archive instead of the live book.
    """
    add = _calibredb("add", "-d", str(epub))
    archive_id = _added_id(add)
    if add.returncode != 0 or archive_id is None:
        logger.warning("stub archive of %s was not added: %s", url,
                       _tail((add.stdout or "") + (add.stderr or "")))
        return
    tag = _calibredb("set_metadata", str(archive_id), "--field", f"identifiers:url-archive:{url}")
    if tag.returncode != 0:
        logger.warning("could not tag archive %s of %s as url-archive: %s", archive_id, url,
                       _tail((tag.stdout or "") + (tag.stderr or "")))


def _update_one(url: str, entry: dict, calibre_id: int | None = None) -> tuple[dict, str | None]:
    """Update one existing story in place. Returns (entry, transient_error|None) for retry.

    `calibre_id` is the entry to update (pinned by the app, or found by story identity); without
    one the story is looked up again here."""
    if calibre_id is None:
        calibre_id = _find_calibre_id(url)
    if calibre_id is None:  # vanished between split and now → treat as new
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            res = _ff([url], "--", url, cwd=str(work))
            epub = _only_epub(work)
            if epub is None:
                combined = (res.stdout or "") + (res.stderr or "")
                return entry, combined if _transient(combined) else _fail(entry, combined)
            add = _calibredb("add", str(epub))
            entry["calibre_id"] = _added_id(add) or _find_calibre_id(url)
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

        upd = _fanficfare_update(epub, cwd=str(work), url=url)
        if getattr(upd, "via_flare", False):
            entry["via"] = "flaresolverr"
        combined = (upd.stdout or "") + (upd.stderr or "")

        stub = None
        m = _STUB_RE.search(combined)
        if m:  # site dropped chapters → archive old EPUB, then force a clean re-download
            old, new = int(m.group(1)), int(m.group(2))
            _archive_copy(epub, _epub_source_url(epub) or url)  # standalone backup of the pre-stub EPUB
            old_urls = _chapter_urls(epub)  # per-chapter identity BEFORE the overwrite
            forced = _fanficfare_update(epub, "-o", "force_update_epub_always=true", cwd=str(work), url=url)
            if _SKIPPED_RE.search((forced.stdout or "") + (forced.stderr or "")):
                return entry, _fail(entry, "fanficfare skipped writing the stub re-download: "
                                    + _tail((forced.stdout or "") + (forced.stderr or "")))
            new_urls = _chapter_urls(epub)  # …and AFTER, so the app can diff by URL
            stub = {"old": old, "new": new, "old_urls": old_urls, "new_urls": new_urls}
        elif _NEEDS_FORCE_RE.search(combined):  # non-shrink mismatch → just force, no archive
            forced = _fanficfare_update(epub, "-o", "force_update_epub_always=true", cwd=str(work), url=url)
            combined = (forced.stdout or "") + (forced.stderr or "")
        elif upd.returncode != 0 and _transient(combined):
            return entry, combined  # retryable
        if not stub and _SKIPPED_RE.search(combined):
            # New chapters were found but not written. Reporting the unchanged EPUB as `ok` is what
            # hid this — fail loudly instead, and leave the Calibre entry as it was.
            return entry, _fail(entry, "fanficfare found new chapters but skipped writing them: "
                                + _tail(combined))

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
