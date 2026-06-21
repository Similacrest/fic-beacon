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
        author_sort TEXT
    );
    CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
    CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
    CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
    CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);

    INSERT INTO books VALUES (1, 'Story One', 'story one', 'AuthorA/Story One (1)', 'AuthorA');
    INSERT INTO books VALUES (2, 'Story Two', 'story two', 'AuthorB/Story Two (2)', 'AuthorB');
    INSERT INTO authors VALUES (1, 'Author A', 'A, Author');
    INSERT INTO authors VALUES (2, 'Author B', 'B, Author');
    INSERT INTO books_authors_link VALUES (1, 1);
    INSERT INTO books_authors_link VALUES (2, 2);
    INSERT INTO data VALUES (1, 1, 'EPUB', 'Story One - Author A', 0);
    INSERT INTO data VALUES (2, 2, 'EPUB', 'Story Two - Author B', 0);
    -- Book 1 has a FanFicFare source URL
    INSERT INTO identifiers VALUES (1, 1, 'url', 'https://archiveofourown.org/works/99999');
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
