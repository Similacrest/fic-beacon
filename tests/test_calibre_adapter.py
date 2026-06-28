"""Tests for the Calibre adapter using a synthetic metadata.db."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.calibre.adapter import CalibreAdapter


def _build_calibre_db(library_path: Path) -> None:
    """Create a minimal Calibre metadata.db with two books."""
    db_path = library_path / "metadata.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE books (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        sort TEXT,
        path TEXT NOT NULL,
        author_sort TEXT,
        last_modified TIMESTAMP
    );
    CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
    CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
    CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
    CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);

    -- custom_columns matches Calibre: `normalized` decides the storage layout (link table
    -- for normalized columns, direct (book,value) for non-normalized).
    CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple BOOL, normalized BOOL);
    -- #genre_manual (1) and #genre (4): multi-value text → normalized link layout.
    INSERT INTO custom_columns VALUES (1, 'genre_manual', 'Genre (manual)', 'text', 1, 1);
    INSERT INTO custom_columns VALUES (4, 'genre', 'Genre', 'text', 1, 1);
    CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT);
    CREATE TABLE books_custom_column_1_link (id INTEGER PRIMARY KEY, book INTEGER, value INTEGER);
    CREATE TABLE custom_column_4 (id INTEGER PRIMARY KEY, value TEXT);
    CREATE TABLE books_custom_column_4_link (id INTEGER PRIMARY KEY, book INTEGER, value INTEGER);
    -- Book 1: #genre_manual = Fantasy.Rational ; Book 2: blank manual, #genre = LitRPG
    INSERT INTO custom_column_1 VALUES (1, 'Fantasy.Rational');
    INSERT INTO books_custom_column_1_link VALUES (1, 1, 1);
    INSERT INTO custom_column_4 VALUES (1, 'LitRPG');
    INSERT INTO books_custom_column_4_link VALUES (1, 2, 1);

    -- #status (2): single-value ENUMERATION → normalized link layout (the #status bug).
    -- #read (3): bool → non-normalized direct (book,value) layout.
    INSERT INTO custom_columns VALUES (2, 'status', 'Status', 'enumeration', 0, 1);
    INSERT INTO custom_columns VALUES (3, 'read', 'Read', 'bool', 0, 0);
    CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, value TEXT, link TEXT);
    CREATE TABLE books_custom_column_2_link (id INTEGER PRIMARY KEY, book INTEGER, value INTEGER);
    CREATE TABLE custom_column_3 (id INTEGER PRIMARY KEY, book INTEGER, value BOOL, UNIQUE(book));
    -- Book 1: In-Progress + read; Book 2: Completed + unread.
    INSERT INTO custom_column_2 VALUES (1, 'In-Progress', ''), (2, 'Completed', '');
    INSERT INTO books_custom_column_2_link VALUES (1, 1, 1), (2, 2, 2);
    INSERT INTO custom_column_3 VALUES (1, 1, 1);
    INSERT INTO custom_column_3 VALUES (2, 2, 0);

    INSERT INTO books VALUES (1, 'Story One', 'story one', 'AuthorA/Story One (1)', 'AuthorA', '2026-01-01 10:00:00');
    INSERT INTO books VALUES (2, 'Story Two', 'story two', 'AuthorB/Story Two (2)', 'AuthorB', '2026-06-01 10:00:00');
    INSERT INTO authors VALUES (1, 'Author A', 'A, Author');
    INSERT INTO authors VALUES (2, 'Author B', 'B, Author');
    INSERT INTO books_authors_link VALUES (1, 1);
    INSERT INTO books_authors_link VALUES (2, 2);
    INSERT INTO data VALUES (1, 1, 'EPUB', 'Story One - Author A', 0);
    INSERT INTO data VALUES (2, 2, 'EPUB', 'Story Two - Author B', 0);
    -- Book 1 has a FanFicFare source URL
    INSERT INTO identifiers VALUES (1, 1, 'url', 'https://archiveofourown.org/works/99999');
    -- Book 1 is tagged Fantasy.Epic + Complete; book 2 has no tags
    INSERT INTO tags VALUES (1, 'Fantasy.Epic');
    INSERT INTO tags VALUES (2, 'Complete');
    INSERT INTO books_tags_link VALUES (1, 1, 1);
    INSERT INTO books_tags_link VALUES (2, 1, 2);
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def library_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        _build_calibre_db(path)
        yield path


class TestListBooks:
    def test_returns_all_epub_books(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = adapter.list_books()
        assert len(books) == 2

    def test_book_fields(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        assert books[1].title == "Story One"
        assert books[1].author == "Author A"
        assert books[1].epub_name == "Story One - Author A"

    def test_source_url_present_for_fff_book(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        assert books[1].source_url == "https://archiveofourown.org/works/99999"

    def test_source_url_none_for_non_fff_book(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        assert books[2].source_url is None

    def test_tags_present(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        assert books[1].tags == ["Complete", "Fantasy.Epic"]  # ordered by name
        assert books[2].tags == []

    def test_custom_genre_columns(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        # Book 1 has a curated #genre_manual; book 2 has only a raw #genre tag.
        assert books[1].genres == ["Fantasy.Rational"]
        assert books[1].genre_tags == []
        assert books[2].genres == []
        assert books[2].genre_tags == ["LitRPG"]

    def test_status_and_read_columns(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = {b.calibre_id: b for b in adapter.list_books()}
        assert books[1].source_status == "In-Progress"
        assert books[1].read is True
        assert books[2].source_status == "Completed"
        assert books[2].read is False  # bool value 0 ⇒ not read

    def test_status_map(self, library_path):
        adapter = CalibreAdapter(library_path)
        assert adapter.status_map([1, 2]) == {1: "In-Progress", 2: "Completed"}
        assert adapter.status_map([]) == {}

    def test_ordered_by_last_modified_desc(self, library_path):
        adapter = CalibreAdapter(library_path)
        books = adapter.list_books()
        # Story Two (2026-06) is more recently modified than Story One (2026-01)
        assert [b.calibre_id for b in books] == [2, 1]
        assert books[0].last_modified.startswith("2026-06")

    def test_multi_author_book_not_duplicated(self, library_path):
        """A book with two authors must appear once, not once per author."""
        import sqlite3
        db_path = library_path / "metadata.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO authors VALUES (3, 'Author C', 'C, Author')")
        conn.execute("INSERT INTO books_authors_link VALUES (1, 3)")  # book 1 now has 2 authors
        conn.commit()
        conn.close()

        adapter = CalibreAdapter(library_path)
        books = adapter.list_books()
        assert len(books) == 2  # still 2 books, not 3
        book1 = next(b for b in books if b.calibre_id == 1)
        assert book1.author in ("Author A", "Author C")  # one of them, deterministic


class TestGetBook:
    def test_returns_correct_book(self, library_path):
        adapter = CalibreAdapter(library_path)
        book = adapter.get_book(1)
        assert book is not None
        assert book.calibre_id == 1
        assert book.title == "Story One"

    def test_returns_none_for_missing_id(self, library_path):
        adapter = CalibreAdapter(library_path)
        assert adapter.get_book(999) is None


class TestEpubPath:
    def test_path_construction(self, library_path):
        adapter = CalibreAdapter(library_path)
        book = adapter.get_book(1)
        path = adapter.epub_path(book)
        expected = library_path / "AuthorA/Story One (1)" / "Story One - Author A.epub"
        assert path == expected
