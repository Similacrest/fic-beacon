"""HTTP client to the FanFicFare/Calibre fetcher container.

Fic-Beacon never runs FanFicFare or `calibredb` itself — the Calibre library is mounted
read-only here. Instead it POSTs a story URL to the separate, isolated fetcher container,
which downloads/updates the EPUB *into* the Calibre library and reports back the resulting
`calibre_id`, the new chapter count, and any **stub** event (the site removed old chapters
and the EPUB was overwritten shorter). See `fetcher/` for the service and Architecture.md
for the contract.

The contract (POST {fetcher_url}/fetch, JSON {"url": ...}) returns JSON:
    {"calibre_id": int, "chapter_count": int,
     "stub": {"old": int, "new": int} | null, "error": str | null}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

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


def request_fetch(story_url: str) -> FetchResult:
    """Ask the fetcher container to download/update one story. Pure HTTP — no DB writes."""
    try:
        resp = httpx.post(
            f"{settings.fetcher_url.rstrip('/')}/fetch",
            json={"url": story_url},
            timeout=settings.fetcher_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network error, timeout, bad JSON, HTTP error
        logger.warning("Fetch failed for %s: %s", story_url, exc)
        return FetchResult(ok=False, error=str(exc))

    if data.get("error"):
        return FetchResult(ok=False, error=str(data["error"]))

    stub = data.get("stub")
    return FetchResult(
        ok=True,
        calibre_id=data.get("calibre_id"),
        chapter_count=data.get("chapter_count"),
        stub=StubInfo(old=int(stub["old"]), new=int(stub["new"])) if stub else None,
    )


def fetch_book(session: Session, book: Book) -> FetchResult:
    """Trigger a fetch for one tracked book and fold the result back into its row.

    Records `last_fetch_at`/`last_fetch_status`, links a freshly-downloaded `calibre_id`,
    updates `total_chapters`, and applies the **stub** mechanic when the site shrank the
    work (see Book.chapter_label_offset / cursor_floor and Architecture.md). Caller commits.
    """
    if not book.source_url:
        book.last_fetch_at = utcnow()
        book.last_fetch_status = "error: no source URL"
        return FetchResult(ok=False, error="no source URL")

    result = request_fetch(book.source_url)
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
