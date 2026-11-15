"""One-time detection of already-stubbed books: their archives become segments."""
import json
import os
import sqlite3

import pytest

from app.calibre.adapter import CalibreAdapter
from app.models import Book, BookSegment, Channel, Drop
from app.segments_backfill import backfill_segments
from app.state import get_value
from tests.test_calibre_adapter import _build_calibre_db, library_path  # noqa: F401  (fixture)
from tests.test_segments_apply import FakeAdapter, _epub

URL = "https://www.royalroad.com/fiction/186926"


class Adapter(FakeAdapter):
    def __init__(self, files, archives):
        super().__init__(files)
        self.archives = archives   # story url -> [archive ids]

    def archive_ids(self, url):
        return list(self.archives.get(url, []))


def _book(db, cid, **kw):
    ch = db.query(Channel.id).order_by(Channel.id).limit(1).scalar()
    b = Book(calibre_id=cid, title="Stub", author="A", channel_id=ch, tracked=True, source_url=URL,
             queue_position=1, **kw)
    db.add(b)
    db.flush()
    return b


def _drop(db, b, url, start, end, n=[0]):
    n[0] += 1
    db.add(Drop(book_id=b.id, channel_id=b.channel_id, feed_key="1", word_count=1, chapter_start=start,
                chapter_end=end, chapter_titles="t", content_html="x", source_url=url,
                feedback_token=f"t{n[0]}", reader_slug=f"s{n[0]}"))
    db.flush()


@pytest.fixture
def files():
    f = {10: _epub([f"a{i}" for i in range(8)]), 20: _epub(["a0", "a1", "a2", "n1", "n2"])}
    yield f
    for p in f.values():
        os.unlink(p)


def test_adapter_lists_url_archive_identifiers_by_story_key(library_path):  # noqa: F811
    conn = sqlite3.connect(str(library_path / "metadata.db"))
    conn.execute("INSERT INTO identifiers VALUES (50, 2, 'url-archive', 'https://www.royalroad.com/fiction/186926/slug')")
    conn.execute("INSERT INTO identifiers VALUES (51, 1, 'url-archive', 'https://www.royalroad.com/fiction/186926')")
    conn.execute("INSERT INTO identifiers VALUES (52, 1, 'url-archive', 'https://www.royalroad.com/fiction/777777')")
    conn.commit()
    conn.close()
    assert CalibreAdapter(library_path).archive_ids(URL) == [1, 2]
    assert CalibreAdapter(library_path).archive_ids(None) == []


def test_stubbed_book_gets_segments_and_a_cursor_after_the_last_released_chapter(in_memory_db, files):
    db = in_memory_db
    b = _book(db, 20, cursor_chapter_index=2, cursor_floor=2, chapter_label_offset=3,
              label_map="[[0, 3]]", total_chapters=5)
    _drop(db, b, "https://s/a5", 5, 6)     # released a5-a6 under the old numbering → next is a7 (pos 7)
    _drop(db, b, "https://s/a3", 3, 3)

    rows = backfill_segments(db, Adapter(files, {URL: [10]}))

    assert [s.calibre_id for s in b.segments] == [10]
    assert b.cursor_chapter_index == 7 and b.cursor_floor == 0
    assert b.label_map is None and b.chapter_label_offset == 0 and b.total_chapters == 10
    assert rows == [{"book_id": b.id, "title": "Stub", "archive_ids": [10], "old_cursor": 2, "new_cursor": 7,
                     "old_label_offset": 3, "old_label_map": "[[0, 3]]"}]
    assert json.loads(get_value(db, "segment_backfill_report")) == rows


def test_without_a_matching_drop_the_cursor_is_kept_but_clamped(in_memory_db, files):
    db = in_memory_db
    b = _book(db, 20, cursor_chapter_index=99, cursor_floor=2)

    backfill_segments(db, Adapter(files, {URL: [10]}))

    assert b.cursor_chapter_index == 10 and [s.calibre_id for s in b.segments] == [10]


def test_backfill_is_idempotent(in_memory_db, files):
    db = in_memory_db
    b = _book(db, 20, cursor_chapter_index=2, cursor_floor=2)
    _drop(db, b, "https://s/a5", 5, 5)
    ad = Adapter(files, {URL: [10]})
    backfill_segments(db, ad)
    snap = (b.cursor_chapter_index, [s.calibre_id for s in b.segments])

    assert backfill_segments(db, ad) == []
    assert (b.cursor_chapter_index, [s.calibre_id for s in b.segments]) == snap


def test_book_with_no_archive_entry_is_untouched(in_memory_db, files):
    db = in_memory_db
    b = _book(db, 20, cursor_chapter_index=2, cursor_floor=2, label_map="[[0, 3]]")

    assert backfill_segments(db, Adapter(files, {})) == []
    assert b.segments == [] and b.cursor_floor == 2 and b.label_map == "[[0, 3]]"


def test_unreadable_archive_leaves_the_book_untouched(in_memory_db, files):
    db = in_memory_db
    b = _book(db, 20, cursor_chapter_index=2, cursor_floor=2)

    assert backfill_segments(db, Adapter({**files, 10: None}, {URL: [10]})) == []
    assert b.segments == [] and b.cursor_floor == 2


def test_startup_backfill_never_blocks_startup(in_memory_db, monkeypatch, tmp_path):
    from app.config import settings
    from app.segments_backfill import backfill_on_startup
    monkeypatch.setattr(settings, "calibre_library_path", tmp_path / "no-library")
    _book(in_memory_db, 20)

    assert backfill_on_startup(in_memory_db) == []     # no metadata.db: logged, not raised


# ---- slug-changed stub, duplicate archive, already-migrated books --------------------------------
from tests.make_epub import make_epub  # noqa: E402

RRB = "https://www.royalroad.com/fiction/118891/{slug}/chapter/{cid}/c{n}"


def _rr_epub(ids, slug):
    body = "<p>" + "word " * 500 + "</p>"
    return make_epub([(f"C{n}", body, RRB.format(slug=slug, cid=4328000 + n, n=n)) for n in ids])


@pytest.fixture
def rr_files():
    f = {10: _rr_epub(range(1, 9), "b2-stubbing-new-life"),
         11: _rr_epub(range(1, 9), "b2-stubbing-new-life"),          # duplicate archive
         20: _rr_epub(range(6, 11), "new-life")}                      # live: 6-8 shared, 9-10 new
    yield f
    for p in f.values():
        os.unlink(p)


def _rr_book(db, **kw):
    b = _book(db, 20, **kw)
    b.source_url = "https://www.royalroad.com/fiction/118891"
    db.flush()
    return b


def test_slug_changed_stub_with_a_duplicate_archive_lands_the_cursor_after_the_last_read(in_memory_db, rr_files):
    db = in_memory_db
    b = _rr_book(db, cursor_chapter_index=65, cursor_floor=65)
    _drop(db, b, RRB.format(slug="b2-stubbing-new-life", cid=4328008, n=8), 7, 7)   # read up to chapter 8
    ad = Adapter(rr_files, {b.source_url: [10, 11]})

    rows = backfill_segments(db, ad)

    assert [s.calibre_id for s in b.segments] == [10] and [s.ordinal for s in b.segments] == [0]
    assert b.total_chapters == 10                      # distinct chapter ids, nothing repeated
    assert b.cursor_chapter_index == 8 and b.total_chapters - b.cursor_chapter_index == 2
    assert rows[0]["archive_ids"] == [10]


def test_books_migrated_by_the_first_backfill_are_redone_once(in_memory_db, rr_files):
    from app.segments_backfill import backfill_with_repair
    from app.state import set_value
    db = in_memory_db
    b = _rr_book(db, cursor_chapter_index=345, total_chapters=357)
    db.add_all([BookSegment(book_id=b.id, ordinal=0, calibre_id=10), BookSegment(book_id=b.id, ordinal=1, calibre_id=11)])
    _drop(db, b, RRB.format(slug="b2-stubbing-new-life", cid=4328008, n=8), 7, 7)
    set_value(db, "segment_backfill_report", json.dumps([{"book_id": b.id, "archive_ids": [10, 11]}]))
    db.flush()
    db.refresh(b)
    ad = Adapter(rr_files, {b.source_url: [10, 11]})

    backfill_with_repair(db, ad)

    assert [s.calibre_id for s in b.segments] == [10] and b.cursor_chapter_index == 8
    assert get_value(db, "segment_backfill_version") == "2"
    b.cursor_chapter_index = 9                         # a later reader move must survive a restart
    backfill_with_repair(db, ad)
    assert b.cursor_chapter_index == 9 and [s.calibre_id for s in b.segments] == [10]
