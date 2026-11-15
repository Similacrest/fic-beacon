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
