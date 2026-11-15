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


# ---- apply_result: a stub keeps the old EPUB as a segment ----------------------------------
from app.fetch.client import apply_result  # noqa: E402


def _u(n):
    return f"https://s/{n}"


def _stub_raw(cid, old_urls, new_urls, archive_id):
    return {"url": "https://s/story", "calibre_id": cid, "chapter_count": len(new_urls),
            "stub": {"old": len(old_urls), "new": len(new_urls),
                     "old_urls": [_u(x) for x in old_urls], "new_urls": [_u(x) for x in new_urls],
                     "archive_calibre_id": archive_id}}


@pytest.fixture
def stub_files():
    files = {10: _epub([f"a{i}" for i in range(8)]),
             20: _epub(["a0", "a1", "a2", "n1", "n2"])}
    yield files
    for p in {str(v): v for v in files.values()}.values():
        os.unlink(p)


def _apply(db, book, raw, files):
    with patch("app.fetch.client._calibre_adapter", return_value=FakeAdapter(files)):
        return apply_result(book, raw)


def test_stub_with_archive_id_adds_a_segment_and_keeps_cursor_and_labels(in_memory_db, stub_files):
    b = _book(in_memory_db, 20, cursor=5, total_chapters=8, tracked=True, label_map="[[0, 0], [3, 2]]",
              fresh_from_index=None)
    raw = _stub_raw(20, [f"a{i}" for i in range(8)], ["a0", "a1", "a2", "n1", "n2"], 10)

    _apply(in_memory_db, b, raw, stub_files)

    assert [s.calibre_id for s in b.segments] == [10] and b.segments[0].ordinal == 0
    assert b.cursor_chapter_index == 5 and b.label_map == "[[0, 0], [3, 2]]"
    assert b.total_chapters == 10 and b.fresh_from_index == 8
    assert b.last_fetch_status == "ok (stub 8→5, kept as segment)"


def test_stub_when_caught_up_makes_the_new_chapters_fresh(in_memory_db, stub_files):
    b = _book(in_memory_db, 20, cursor=8, total_chapters=8, tracked=True)
    raw = _stub_raw(20, [f"a{i}" for i in range(8)], ["a0", "a1", "a2", "n1", "n2"], 10)

    res = _apply(in_memory_db, b, raw, stub_files)

    assert b.cursor_chapter_index == 8 and b.fresh_from_index == 8 and res.new_chapters


def test_second_stub_appends_another_segment(in_memory_db, stub_files):
    b = _book(in_memory_db, 20, cursor=5, total_chapters=10, tracked=True)
    in_memory_db.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    in_memory_db.flush()
    in_memory_db.refresh(b)
    stub_files[11] = stub_files[20]            # the first live EPUB, now archived as 11
    stub_files[20] = _epub(["a0", "a1", "n2", "m1"])
    raw = _stub_raw(20, ["a0", "a1", "a2", "n1", "n2"], ["a0", "a1", "n2", "m1"], 11)
    _apply(in_memory_db, b, raw, stub_files)

    assert [(s.ordinal, s.calibre_id) for s in b.segments] == [(0, 10), (1, 11)]
    assert b.cursor_chapter_index == 5 and b.total_chapters == 11   # a0..a7, n1, n2, m1


def test_stub_without_archive_id_uses_the_legacy_remap(in_memory_db, stub_files):
    b = _book(in_memory_db, 20, cursor=5, total_chapters=8, tracked=True)
    raw = _stub_raw(20, [f"a{i}" for i in range(8)], ["a0", "a1", "a2", "n1", "n2"], None)

    _apply(in_memory_db, b, raw, stub_files)

    assert b.segments == [] and "mapped" in b.last_fetch_status
    assert b.cursor_chapter_index == 5      # no chapter at/after a5 survives → caught up to the new end


def test_stub_retry_does_not_duplicate_the_segment(in_memory_db, stub_files):
    b = _book(in_memory_db, 20, cursor=5, total_chapters=10, tracked=True)
    in_memory_db.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    in_memory_db.flush()
    in_memory_db.refresh(b)
    raw = _stub_raw(20, [f"a{i}" for i in range(8)], ["a0", "a1", "a2", "n1", "n2"], 10)

    _apply(in_memory_db, b, raw, stub_files)

    assert [s.calibre_id for s in b.segments] == [10] and b.cursor_chapter_index == 5
