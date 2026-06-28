"""HTTP client to the FanFicFare/Calibre fetcher container.

Fic-Beacon never runs FanFicFare or `calibredb` itself — the Calibre library is mounted
read-only here. Instead it submits story URLs to the separate, isolated fetcher container,
which downloads/updates the EPUBs *into* the Calibre library in the background and reports
back each story's `calibre_id`, new chapter count, and any **stub** event (the site removed
old chapters and the EPUB was overwritten shorter).

Fetches are **async**: FanFicFare runs can take ~15 minutes, far too long to block a drop
cycle or an admin request. `submit_fetch` POSTs a batch of URLs and gets a `job_id` back
immediately (HTTP 202); the scheduler then polls `poll_fetch` until the job is `done` and
folds each result into its Book row with `apply_result`. See `fetcher/` for the service and
Architecture.md for the contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.models import Book, utcnow

logger = logging.getLogger(__name__)


@dataclass
class StubInfo:
    """The site dropped chapters: the EPUB had `old`, the site now has `new` (< old)."""
    old: int
    new: int


@dataclass
class FetchResult:
    ok: bool
    calibre_id: int | None = None
    chapter_count: int | None = None
    stub: StubInfo | None = None
    error: str | None = None


def submit_fetch(urls: list[str]) -> str | None:
    """Submit a batch of story URLs to the fetcher. Returns the job_id, or None on failure."""
    urls = [u for u in urls if u]
    if not urls:
        return None
    try:
        resp = httpx.post(
            f"{settings.fetcher_url.rstrip('/')}/fetch",
            json={"urls": urls},
            timeout=settings.fetcher_timeout,
        )
        resp.raise_for_status()
        return resp.json().get("job_id")
    except Exception as exc:  # network error, timeout, bad JSON, HTTP error
        logger.warning("Fetch submit failed for %d url(s): %s", len(urls), exc)
        return None


def poll_fetch(job_id: str) -> dict | None:
    """Poll a fetch job. Returns {"status", "results"} or None on a transient HTTP error.

    `status` is "running" | "done" | "unknown" (the job_id is gone — fetcher restarted or
    the result was pruned). `results` is a list of per-URL dicts (see the fetcher contract).
    """
    try:
        resp = httpx.get(
            f"{settings.fetcher_url.rstrip('/')}/fetch/{job_id}",
            timeout=settings.fetcher_timeout,
        )
        if resp.status_code == 404:
            return {"status": "unknown", "results": None}
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Fetch poll failed for job %s: %s", job_id, exc)
        return None


def _to_result(raw: dict) -> FetchResult:
    """Turn one per-URL dict from the fetcher into a FetchResult."""
    if raw.get("error"):
        return FetchResult(ok=False, error=str(raw["error"]))
    stub = raw.get("stub")
    return FetchResult(
        ok=True,
        calibre_id=raw.get("calibre_id"),
        chapter_count=raw.get("chapter_count"),
        stub=StubInfo(old=int(stub["old"]), new=int(stub["new"])) if stub else None,
    )


def apply_result(book: Book, raw: dict) -> FetchResult:
    """Fold one finished per-URL fetch result into its Book row. Caller commits.

    Records `last_fetch_at`/`last_fetch_status`, links a freshly-downloaded `calibre_id`,
    updates `total_chapters`, and applies the **stub** mechanic when the site shrank the
    work (see Book.chapter_label_offset / cursor_floor and Architecture.md).
    """
    result = _to_result(raw)
    book.last_fetch_at = utcnow()

    if not result.ok:
        book.last_fetch_status = f"error: {result.error}"
        return result

    if book.calibre_id is None and result.calibre_id is not None:
        book.calibre_id = result.calibre_id
    if result.chapter_count is not None:
        book.total_chapters = result.chapter_count

    if result.stub and result.stub.old > result.stub.new:
        # The site rewrote/removed chapters. The fetcher archived the old EPUB as a
        # separate Calibre entry and overwrote this one. Keep our labels continuous and
        # forbid rewinding into the rewritten body: offset by the removed count, mark the
        # reader caught-up to the new (shorter) body, and floor the cursor there.
        removed = result.stub.old - result.stub.new
        book.chapter_label_offset += removed
        book.cursor_chapter_index = result.stub.new
        book.cursor_floor = result.stub.new
        book.last_fetch_status = f"ok (stub {result.stub.old}→{result.stub.new})"
    else:
        book.last_fetch_status = "ok"

    return result
