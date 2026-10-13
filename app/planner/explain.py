"""One-line "why" for each source on the dashboard: why it did or didn't drop.

Two kinds of fact feed it, kept apart on purpose:

* **Live state** — paused, queued, cooling down, caught up, downloading, weight 0, unread streak.
  These forecast the *next* release and are read from the book as it is now.
* **The last release's record** — the planner's skip-log entry (`app_state[last_broadcast_skips]`),
  written when it decided: oversized and saving up, unread back-off, weight fade, budget ran out,
  lost the roll. Recomputing those from current state would lie (e.g. reading two drops after the
  release would turn "held back for unread drops" into "lost the roll").

`explain()` is pure: the router gathers the inputs, this picks the one reason that matters most.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import Book, BookStatus


@dataclass(frozen=True)
class SourceStatus:
    tone: str     # ok | wait | warn | off — drives the chip colour
    icon: str
    text: str     # the reason itself; must read on its own (tablets don't show title= tooltips)
    detail: str = ""  # optional extra, shown as a tooltip / secondary line


def pending_chapters(book: Book) -> int:
    """Whole chapters sitting past the cursor (0 when the chapter count isn't known yet)."""
    if book.total_chapters is None:
        return 0
    return max(0, book.total_chapters - book.cursor_chapter_index)


def _fetch_state(book: Book) -> str:
    st = book.last_fetch_status or ""
    if st.startswith("fetching"):
        return "fetching"
    if st.startswith("error"):
        return "error"
    return ""


def _fetch_error(book: Book) -> str:
    return (book.last_fetch_status or "").removeprefix("error:").strip()


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def explain(
    book: Book,
    last: dict | None = None,
    *,
    unread: int = 0,
    note: str | None = None,
    queue_pos: int | None = None,
) -> SourceStatus:
    """The single most useful reason for `book`'s state. `last` is its entry in the last release's
    skip log (None if it wasn't held back), `unread` its live unread-drop streak, `note` a
    set-aside note, `queue_pos` its 1-based place in its channel's queue."""
    if book.status == BookStatus.dropped:
        return SourceStatus("off", "🗑", "Dropped")
    if book.status == BookStatus.completed:
        return SourceStatus("off", "✅", "Finished — every chapter is out")
    if book.paused:
        if note:  # set aside by the planner (e.g. an EPUB with no chapters) — that's the story
            return SourceStatus("warn", "⚠", note, "Paused until you resume it.")
        return SourceStatus("off", "⏸", "Paused", "Resume it to broadcast again.")
    if note:
        return SourceStatus("warn", "⚠", note)
    if book.status == BookStatus.queued:
        where = f" — #{queue_pos} in line" if queue_pos else ""
        return SourceStatus("wait", "📋", f"Queued{where}",
                            "Starts streaming when a slot in this channel frees up.")

    fetch = _fetch_state(book)
    if book.tracked and book.calibre_id is None:
        if fetch == "fetching":
            return SourceStatus("wait", "⤓", "Downloading for the first time…")
        if fetch == "error":
            return SourceStatus("warn", "⚠", "First download failed", _fetch_error(book))
        return SourceStatus("wait", "⤓", "Waiting for its first download")

    if last and last.get("missing"):
        return SourceStatus("warn", "⚠", "Not found in Calibre",
                            "Its Calibre entry or EPUB is missing, so nothing can drop.")
    if book.cooldown_remaining >= 2:
        return SourceStatus("off", "👎", "Sitting out the next release",
                            "A 👎 makes a source skip one release.")
    if book.quota_weight <= 0:
        return SourceStatus("off", "⛔", "Weight 0 — never posts")

    pending = pending_chapters(book)
    if pending == 0:
        if not book.tracked:
            return SourceStatus("ok", "✓", "All chapters out")
        if fetch == "fetching":
            return SourceStatus("wait", "⤓", "Caught up — fetching new chapters…")
        if fetch == "error":
            return SourceStatus("warn", "⚠", "Caught up — last fetch failed", _fetch_error(book))
        return SourceStatus("ok", "✓", "Caught up — waiting for the author")

    waiting = f"{pending} waiting"
    fresh = (
        book.tracked and book.fresh_from_index is not None
        and book.cursor_chapter_index >= book.fresh_from_index
    )

    if last:
        if last.get("oversized"):
            words = last.get("next_words") or 0
            base = last.get("base")
            detail = "Posts whole once enough budget has carried over."
            if base:
                detail = f"Bigger than this channel's {base:,}-word share per release. " + detail
            return SourceStatus("wait", "💾", f"Saving up for a {words:,}-word chapter", detail)
        if not last.get("held_out"):
            got = last.get("dropped") or 0
            return SourceStatus("ok", "▶", f"Dropped {_n(got, 'chapter')} last release — {waiting}")
        if last.get("fresh"):
            return SourceStatus("wait", "🆕", f"New chapter didn't fit the last release — {waiting}",
                                "It goes first next time there's room.")
        penalty = last.get("penalty", 1.0)
        if penalty < 1.0:
            # The live streak may have reset since (reading clears it); say so if it has.
            if unread > 1:
                return SourceStatus("wait", "📭", f"Held back: {unread} unread drops — {waiting}",
                                    f"Chance cut to ×{penalty:g}. Reading or ✓-marking a drop lifts it.")
            return SourceStatus("wait", "📭", f"Held back last release for unread drops — {waiting}",
                                "Since read, so no back-off next time.")
        fade = last.get("fade", 1.0)
        if fade < 1.0:
            return SourceStatus("wait", "🔉", f"Low weight slows it (×{fade:g}) — {waiting}",
                                "Below the fade floor, a source posts less often. 👍 raises it.")
        budget, used = last.get("budget"), last.get("used")
        if budget is not None and used is not None and used + (last.get("next_words") or 0) > budget:
            return SourceStatus("wait", "⌛", f"Budget ran out — {waiting}",
                                f"The channel used {used:,} of {budget:,} words.")
        return SourceStatus("wait", "🎲", f"Lost the draw last release — {waiting}",
                            "Selection is weighted random; it rolls again next release.")

    if fresh:
        return SourceStatus("ok", "🆕", "New chapter — goes out next release if it fits",
                            waiting)
    if book.cooldown_remaining == 1:
        return SourceStatus("ok", "👎", f"Sat out last release — back next time — {waiting}")
    if unread > 1:
        return SourceStatus("wait", "📭", f"{unread} unread drops — slowing down — {waiting}",
                            "Reading or ✓-marking a drop lifts it.")
    return SourceStatus("ok", "▶", f"On air — {waiting}")
