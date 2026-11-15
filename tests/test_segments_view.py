"""The segment bar view-model and the cursor floor rule for stubbed stories."""
import pytest

from app.epub.chapterizer import Chapter
from app.epub.unified import build_unified
from app.models import Book, BookSegment
from app.segments_view import effective_cursor_floor, segment_bar


def ch(i, url, words=100):
    return Chapter(index=i, title=f"t{i}", html="x", word_count=words, source_url=url)


@pytest.fixture
def unified():
    arch = [ch(i, f"a{i}") for i in range(10)]
    live = [ch(0, "a0"), ch(1, "a1"), ch(2, "a2"), ch(3, "a3"), ch(4, "a4"),
            ch(5, "a5", 5), ch(6, "a6", 5), ch(7, "n1"), ch(8, "n2")]
    return build_unified([(10, arch), (20, live)], 0.5)


def _book(label_map=None):
    b = Book(title="T", channel_id=1, calibre_id=20, label_map=label_map, cursor_floor=4)
    b.segments = [BookSegment(ordinal=0, calibre_id=10)]
    return b


def test_bar_spans_merge_runs_and_use_author_labels(unified):
    # 12 chapters: a0-a4 live copies, a5-a6 archive (short live), a7-a9 archive only, n1-n2 new live
    spans = segment_bar(_book("[[0, 0], [7, 3]]"), unified)
    assert [s.kind for s in spans] == ["live", "archive_short_live", "archive", "live"]
    assert [s.calibre_id for s in spans] == [20, 10, 10, 20]
    assert [(s.start_label, s.end_label) for s in spans] == [(1, 5), (6, 7), (11, 13), (14, 15)]
    assert [(s.start_pos, s.end_pos) for s in spans] == [(0, 4), (5, 6), (7, 9), (10, 11)]
    assert sum(s.width_pct for s in spans) == pytest.approx(100)


def test_effective_floor_is_zero_with_segments_else_the_stored_floor():
    seg = _book()
    plain = Book(title="T", channel_id=1, cursor_floor=4)
    assert effective_cursor_floor(seg) == 0
    assert effective_cursor_floor(plain) == 4
