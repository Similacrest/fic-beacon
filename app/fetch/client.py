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

import json
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import object_session

from app.config import settings
from app.models import Book, label_offset_at, utcnow

logger = logging.getLogger(__name__)


@dataclass
class StubInfo:
    """The site dropped chapters: the EPUB had `old`, the site now has `new` (< old).

    `old_urls`/`new_urls` are the ordered per-chapter canonical URLs of the pre- and post-stub
    EPUBs (present for FanFicFare books), letting the app match chapters by identity. Absent (or
    length-inconsistent with the counts) → the app uses the count-only linear fallback.
    """
    old: int
    new: int
    old_urls: list[str] | None = None
    new_urls: list[str] | None = None


@dataclass
class FetchResult:
    ok: bool
    calibre_id: int | None = None
    chapter_count: int | None = None
    stub: StubInfo | None = None
    error: str | None = None
    # Canonical story URL and publication status read from the downloaded EPUB by the fetcher.
    story_url: str | None = None
    story_status: str | None = None


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
        story_url=raw.get("story_url"),
        story_status=raw.get("story_status"),
        stub=StubInfo(
            old=int(stub["old"]), new=int(stub["new"]),
            old_urls=stub.get("old_urls"), new_urls=stub.get("new_urls"),
        ) if stub else None,
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
        _adopt_canonical_url(book, result.story_url)
    if result.story_status:
        book.story_status = result.story_status
    if result.chapter_count is not None:
        book.total_chapters = result.chapter_count

    if result.stub and result.stub.old > result.stub.new:
        _apply_stub(book, result.stub)
    else:
        book.last_fetch_status = "ok"

    return result


def _adopt_canonical_url(book: Book, story_url: str | None) -> None:
    """After a story's *first* download, adopt the canonical URL FanFicFare recorded.

    The Calibre `url:` identifier the fetcher later searches by is the canonical one (FFN adds the
    `/1/Title` slug, XenForo drops `/page-N`, …), not what the user pasted — so keeping the pasted
    URL made every later update miss the library entry and re-download the story as a duplicate.
    Skipped if another source already owns that URL.
    """
    if not story_url or story_url == book.source_url:
        return
    session = object_session(book)
    if session is not None and session.query(Book.id).filter(
        Book.source_url == story_url, Book.id != book.id
    ).first() is not None:
        return
    book.source_url = story_url


def _apply_stub(book: Book, stub: StubInfo) -> None:
    """Fold a stub (site removed chapters) into the book.

    The fetcher archived the old EPUB and overwrote this one shorter. When it also reported the
    ordered per-chapter URLs of both bodies (and their lengths agree with the counts), we match
    chapters by identity — labels stay exact past a gap *anywhere* and the reader's cursor is
    preserved (see `_apply_stub_by_identity`). Otherwise we fall back to the legacy linear shift.
    """
    old_urls, new_urls = stub.old_urls, stub.new_urls
    if (old_urls and new_urls
            and len(old_urls) == stub.old and len(new_urls) == stub.new):
        _apply_stub_by_identity(book, old_urls, new_urls)
        book.last_fetch_status = f"ok (stub {stub.old}→{stub.new}, mapped)"
    else:
        # Count-only fallback (non-FanFicFare EPUB, or inconsistent URL lists): a single linear
        # offset, mark the reader caught-up to the new body, and floor the cursor there.
        book.chapter_label_offset += stub.old - stub.new
        book.cursor_chapter_index = stub.new
        book.cursor_floor = stub.new
        book.last_fetch_status = f"ok (stub {stub.old}→{stub.new})"


def _apply_stub_by_identity(book: Book, old_urls: list[str], new_urls: list[str]) -> None:
    """Rebuild `label_map` and remap the cursor by matching chapters on their canonical URL."""
    old_index = {u: i for i, u in enumerate(old_urls)}
    new_index = {u: i for i, u in enumerate(new_urls)}
    new_len = len(new_urls)

    # 1) Piecewise label map. A surviving new chapter keeps the absolute label it had in the OLD
    #    body (whose own offset may already be piecewise from a prior stub, so this composes); a
    #    brand-new chapter with no old match continues the previous offset. `label_offset_at` reads
    #    the book's CURRENT (pre-update) map, so compute every offset before committing the new map.
    breakpoints: list[list[int]] = []
    prev_off: int | None = None
    for i, url in enumerate(new_urls):
        j = old_index.get(url)
        # label(new i) := label(old j) = j + offset_old(j) + 1  ⇒  offset(i) = j + offset_old(j) - i
        off = (j + label_offset_at(book, j) - i) if j is not None else (prev_off or 0)
        if off != prev_off:
            breakpoints.append([i, off])
            prev_off = off
    book.label_map = json.dumps(breakpoints) if breakpoints else None

    # 2) Remap the cursor (and floor) to the first surviving chapter at/after the old position, so
    #    a reader who was behind resumes exactly where they were and keeps every unread chapter.
    book.cursor_chapter_index = _remap_forward(book.cursor_chapter_index, old_urls, new_index, new_len)
    book.cursor_floor = _remap_forward(
        book.cursor_floor, old_urls, new_index, new_len, default=book.cursor_chapter_index
    )


def _remap_forward(
    old_pos: int, old_urls: list[str], new_index: dict[str, int], new_len: int,
    default: int | None = None,
) -> int:
    """New physical index of the first surviving chapter at or after `old_pos` (old index).

    None survives → `default` (or the new length: caught-up to the end). Monotonic, so it also
    preserves floor ≤ cursor when `default` is the remapped cursor.
    """
    for j in range(max(old_pos, 0), len(old_urls)):
        ni = new_index.get(old_urls[j])
        if ni is not None:
            return ni
    return new_len if default is None else default
