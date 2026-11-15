from app.database import delete_book_cascade
from app.models import Book, BookSegment, Channel


def _book(s, **kw):
    ch = s.query(Channel).first()
    b = Book(title="T", channel_id=ch.id, **kw)
    s.add(b)
    s.flush()
    return b


def test_book_segments_order_and_cascade(in_memory_db):
    s = in_memory_db
    b = _book(s)
    s.add_all([BookSegment(book_id=b.id, ordinal=1, calibre_id=11),
               BookSegment(book_id=b.id, ordinal=0, calibre_id=10)])
    s.flush()
    s.refresh(b)
    assert [x.calibre_id for x in b.segments] == [10, 11]

    delete_book_cascade(s, b)
    s.flush()
    assert s.query(BookSegment).count() == 0


# ---- unified chapter list -------------------------------------------------------------------
import pytest  # noqa: E402

from app.epub.chapterizer import Chapter  # noqa: E402
from app.epub.unified import build_unified, load_unified  # noqa: E402
from tests.make_epub import make_epub  # noqa: E402


def ch(i, url="auto", words=100, html=None):
    u = f"https://s/{i}" if url == "auto" else url
    return Chapter(index=i, title=f"t{i}", html=html or f"<p>{u}</p>", word_count=words, source_url=u)


def urls(res):
    return [c.source_url for c in res]


def test_single_segment_is_identity():
    res = build_unified([(7, [ch(0, "u0"), ch(1, "u1"), ch(2, "u2")])], 0.5)
    assert [c.position for c in res] == [0, 1, 2]
    assert {c.segment for c in res} == {0} and {c.calibre_id for c in res} == {7}
    assert [c.html for c in res] == ["<p>u0</p>", "<p>u1</p>", "<p>u2</p>"]


def test_archive_then_new_live_chapters_appended():
    arch = [ch(i, f"a{i}") for i in range(5)]
    live = [ch(0, "a0"), ch(1, "a1"), ch(2, "a2"), ch(3, "b1"), ch(4, "b2")]
    res = build_unified([(10, arch), (20, live)], 0.5)
    assert len(res) == 7
    assert urls(res)[5:] == ["b1", "b2"] and [c.calibre_id for c in res[5:]] == [20, 20]
    assert [c.position for c in res] == list(range(7))


def test_overlap_uses_live_when_lengths_match():
    arch = [ch(0, "a0", 1000, "<p>old</p>")]
    live = [ch(0, "a0", 900, "<p>new</p>")]
    (c,) = build_unified([(10, arch), (20, live)], 0.5)
    assert c.html == "<p>new</p>" and c.calibre_id == 20 and not c.short_live


def test_overlap_uses_archive_when_live_is_short():
    arch = [ch(0, "a0", 1000, "<p>old</p>")]
    live = [ch(0, "a0", 10, "<p>stub</p>")]
    (c,) = build_unified([(10, arch), (20, live)], 0.5)
    assert c.html == "<p>old</p>" and c.calibre_id == 10 and c.short_live and c.segment == 0


def test_duplicate_url_inside_a_later_epub_counts_once():
    live = [ch(0, "a0"), ch(1, "x", html="<p>first</p>"), ch(2, "x", html="<p>dup</p>"), ch(3, "y")]
    res = build_unified([(10, [ch(0, "a0")]), (20, live)], 0.5)
    assert urls(res) == ["a0", "x", "y"] and res[1].html == "<p>first</p>"


def test_first_epub_is_taken_verbatim_so_positions_equal_physical_indices():
    # an unsegmented story (and the oldest archive) must never shift under an existing cursor
    res = build_unified([(7, [ch(0, "x"), ch(1, "x"), ch(2, "y")])], 0.5)
    assert urls(res) == ["x", "x", "y"] and [c.physical_index for c in res] == [0, 1, 2]


def test_epub_without_chapter_urls_appends_live_whole():
    arch = [ch(i, None) for i in range(3)]
    live = [ch(i, None) for i in range(2)]
    res = build_unified([(10, arch), (20, live)], 0.5)
    assert len(res) == 5 and [c.calibre_id for c in res] == [10, 10, 10, 20, 20]


def test_second_stub_keeps_earlier_positions():
    arch1 = [ch(i, f"a{i}") for i in range(5)]
    arch2 = [ch(0, "a0"), ch(1, "a1"), ch(2, "a2"), ch(3, "b1")]
    live = [ch(0, "a0"), ch(1, "a1"), ch(2, "b1"), ch(3, "c1")]
    res = build_unified([(1, arch1), (2, arch2), (3, live)], 0.5)
    assert urls(res) == ["a0", "a1", "a2", "a3", "a4", "b1", "c1"]


class _FakeAdapter:
    def __init__(self, paths):
        self.paths = paths   # calibre_id -> Path | None

    def get_book(self, cid):
        from app.calibre.adapter import CalibreBook
        if self.paths.get(cid) is None:
            return None
        return CalibreBook(calibre_id=cid, title="t", author="a", path="", epub_name="", source_url=None)

    def epub_path(self, b):
        return self.paths[b.calibre_id]


def _epub(n, prefix):
    return make_epub([(f"C{i}", "<p>word </p>" * 80, f"https://s/{prefix}{i}") for i in range(n)])


def test_loader_builds_across_epubs_and_tags_serving_id(in_memory_db):
    s = in_memory_db
    b = _book(s, calibre_id=20)
    s.add(BookSegment(book_id=b.id, ordinal=0, calibre_id=10))
    s.flush()
    s.refresh(b)
    res = load_unified(b, _FakeAdapter({10: _epub(3, "a"), 20: _epub(2, "b")}))
    assert res.unreadable == [] and len(res.chapters) == 5
    assert [c.calibre_id for c in res.chapters] == [10, 10, 10, 20, 20]


def test_unreadable_archive_is_reported_and_stops_the_list(in_memory_db):
    s = in_memory_db
    b = _book(s, calibre_id=20)
    s.add_all([BookSegment(book_id=b.id, ordinal=0, calibre_id=10),
               BookSegment(book_id=b.id, ordinal=1, calibre_id=11)])
    s.flush()
    s.refresh(b)
    res = load_unified(b, _FakeAdapter({10: _epub(3, "a"), 11: None, 20: _epub(2, "b")}))
    assert res.unreadable == [11] and len(res.chapters) == 3
    assert {c.calibre_id for c in res.chapters} == {10}


def test_loader_without_segments_matches_the_live_epub(in_memory_db):
    s = in_memory_db
    b = _book(s, calibre_id=20)
    res = load_unified(b, _FakeAdapter({20: _epub(4, "b")}))
    assert [c.position for c in res.chapters] == [0, 1, 2, 3] and res.unreadable == []
