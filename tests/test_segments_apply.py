"""Stubbed stories: the planner releases from the unified chapter list (archive + live EPUBs)."""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.calibre.adapter import CalibreBook
from app.models import Book, BookSegment, BookStatus, Channel, Schedule
from app.planner.planner import run_release_cycle
from app.state import BOOK_NOTE_PREFIX, get_value
from tests.make_epub import make_epub

BODY = "<p>" + "word " * 500 + "</p>"


def _epub(urls, img=None):
    body = BODY + (f'<img src="images/{img}.png"/>' if img else "")
    return Path(make_epub([(f"C {u}", body, f"https://s/{u}") for u in urls]))


class FakeAdapter:
    def __init__(self, files):
        self.files = files   # calibre_id -> Path | None (None = gone from Calibre)

    def get_book(self, cid):
        if self.files.get(cid) is None:
            return None
        return CalibreBook(calibre_id=cid, title="t", author="a", path="", epub_name="", source_url=None)

    def epub_path(self, cb):
        return self.files[cb.calibre_id]


def _book(db, cid, title="Stub", cursor=0, **kw):
    ch = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    b = Book(calibre_id=cid, title=title, author="A", status=BookStatus.active, queue_position=1,
             quota_weight=1.0, channel_id=ch, cursor_chapter_index=cursor, **kw)
    db.add(b)
    db.flush()
    return b


def _release(db, adapter, budget=100000):
    db.query(Schedule).first().budget = budget
    db.flush()
    with patch("app.planner.planner.CalibreAdapter", return_value=adapter):
        return run_release_cycle(db, Path("/fake"))


@pytest.fixture
def two_epubs():
    arch = _epub(["a0", "a1", "a2", "a3", "a4"], img="arch")
    live = _epub(["a0", "a1", "a2", "b1"], img="live")
    yield {10: arch, 20: live}
    for p in (arch, live):
        os.unlink(p)


def test_release_spans_archive_and_live_with_each_chapters_image_id(in_memory_db, two_epubs):
    db = in_memory_db
    b = _book(db, 20, cursor=4)
    db.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    db.flush()
    db.refresh(b)

    drops = _release(db, FakeAdapter(two_epubs))

    assert len(drops) == 1
    d = drops[0]
    assert (d.chapter_start, d.chapter_end) == (4, 5)
    assert b.cursor_chapter_index == 6 and b.total_chapters == 6
    assert "/img/10/" in d.content_html and "/img/20/" in d.content_html
    assert d.content_html.index("/img/10/") < d.content_html.index("/img/20/")


def test_unreadable_archive_holds_the_story_but_not_others(in_memory_db, two_epubs):
    db = in_memory_db
    b = _book(db, 20, cursor=4, total_chapters=6)
    db.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    other_file = _epub(["o0", "o1"])
    other = _book(db, 30, title="Other")
    other.queue_position = 2
    db.flush()
    db.refresh(b)
    files = {10: None, 20: two_epubs[20], 30: other_file}
    try:
        drops = _release(db, FakeAdapter(files))
    finally:
        os.unlink(other_file)

    assert {d.book_id for d in drops} == {other.id}
    assert b.cursor_chapter_index == 4 and b.total_chapters == 6
    assert "10" in (get_value(db, f"{BOOK_NOTE_PREFIX}{b.id}") or "")


def test_archive_note_clears_when_readable_again(in_memory_db, two_epubs):
    db = in_memory_db
    b = _book(db, 20, cursor=4)
    db.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    db.flush()
    db.refresh(b)
    _release(db, FakeAdapter({10: None, 20: two_epubs[20]}))
    assert get_value(db, f"{BOOK_NOTE_PREFIX}{b.id}")

    drops = _release(db, FakeAdapter(two_epubs))

    assert drops and get_value(db, f"{BOOK_NOTE_PREFIX}{b.id}") is None
