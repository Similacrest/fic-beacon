"""End-to-end tests for the smart Library "Add" endpoint (/admin/library/add).

Verifies the routing rules and the cursor fix: an ongoing serial the user has caught up to
(#status updating + #read=Yes) must start its cursor at the current EPUB end so only *new*
chapters drop — not from chapter 1.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.models import Book, BookStatus
from app.routers.admin import add_from_library, cursor_latest, cursor_start
from tests.make_epub import make_epub

_BOOK_DIR = "AuthorA/Story (1)"
_EPUB_NAME = "Story - Author A"
N_CHAPTERS = 4


def _build_library(tmp: Path, status: str | None, read: int | None) -> None:
    """One-book Calibre library with a real 4-chapter EPUB and the given #status/#read."""
    epub = make_epub(chapters=[(f"Chapter {i}", "<p>" + "word " * 200 + "</p>")
                               for i in range(1, N_CHAPTERS + 1)])
    dest = tmp / _BOOK_DIR
    dest.mkdir(parents=True)
    shutil.copy(epub, dest / f"{_EPUB_NAME}.epub")

    conn = sqlite3.connect(str(tmp / "metadata.db"))
    conn.executescript(f"""
    CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, path TEXT,
        author_sort TEXT, last_modified TIMESTAMP);
    CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
    CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
    CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
    CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
    CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple BOOL, normalized BOOL);
    -- #status: single-value enumeration → normalized link layout (matches real Calibre).
    INSERT INTO custom_columns VALUES (2, 'status', 'Status', 'enumeration', 0, 1);
    INSERT INTO custom_columns VALUES (3, 'read', 'Read', 'bool', 0, 0);
    CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, value TEXT, link TEXT);
    CREATE TABLE books_custom_column_2_link (id INTEGER PRIMARY KEY, book INTEGER, value INTEGER);
    CREATE TABLE custom_column_3 (id INTEGER PRIMARY KEY, book INTEGER, value BOOL, UNIQUE(book));
    INSERT INTO books VALUES (1, 'Story', 'story', '{_BOOK_DIR}', 'AuthorA', '2026-01-01 10:00:00');
    INSERT INTO authors VALUES (1, 'Author A', 'A, Author');
    INSERT INTO books_authors_link VALUES (1, 1);
    INSERT INTO data VALUES (1, 1, 'EPUB', '{_EPUB_NAME}', 0);
    """)
    if status is not None:
        conn.execute("INSERT INTO custom_column_2 VALUES (1, ?, '')", (status,))
        conn.execute("INSERT INTO books_custom_column_2_link VALUES (1, 1, 1)")
    if read is not None:
        conn.execute("INSERT INTO custom_column_3 VALUES (1, 1, ?)", (read,))
    conn.commit()
    conn.close()


@pytest.fixture
def add(in_memory_db, tmp_path, monkeypatch):
    """Build a one-book library with the given #status/#read, then run the Add endpoint."""
    def _run(status, read):
        _build_library(tmp_path, status, read)
        monkeypatch.setattr(settings, "calibre_library_path", tmp_path)
        add_from_library(calibre_ids=[1], channel_id=None, db=in_memory_db)
        return in_memory_db.query(Book).filter(Book.calibre_id == 1).one()
    return _run


def test_caught_up_ongoing_starts_at_end(add):
    """#status In-Progress + #read=Yes ⇒ tracked, cursor at EPUB end (the bug fix)."""
    book = add("In-Progress", 1)
    assert book.tracked is True
    assert book.status == BookStatus.active
    assert book.cursor_chapter_index == N_CHAPTERS  # only new chapters drop
    assert book.total_chapters == N_CHAPTERS


def test_unread_ongoing_starts_at_chapter_one(add):
    """#status In-Progress + #read unset ⇒ tracked, cursor at 0 (read from start)."""
    book = add("In-Progress", None)
    assert book.tracked is True
    assert book.cursor_chapter_index == 0


def test_completed_goes_to_backlog_queue(add):
    """#status Completed ⇒ untracked backlog queue from chapter 1."""
    book = add("Completed", 1)
    assert book.tracked is False
    assert book.status == BookStatus.queued
    assert book.cursor_chapter_index == 0


def test_blank_status_goes_to_backlog_queue(add):
    book = add(None, None)
    assert book.tracked is False
    assert book.status == BookStatus.queued


def test_cursor_latest_jumps_to_end_on_demand(add, in_memory_db):
    """A freshly-queued backlog book (total_chapters unset) can switch to ongoing handling."""
    book = add("Completed", None)
    assert book.cursor_chapter_index == 0
    assert book.total_chapters is None  # not computed at add time for backlog
    cursor_latest(book.id, db=in_memory_db)
    assert book.cursor_chapter_index == N_CHAPTERS  # computed on demand
    assert book.total_chapters == N_CHAPTERS


def test_cursor_start_rewinds_to_floor(add, in_memory_db):
    book = add("In-Progress", 1)  # tracked, cursor at end
    assert book.cursor_chapter_index == N_CHAPTERS
    cursor_start(book.id, db=in_memory_db)
    assert book.cursor_chapter_index == 0
    assert book.total_chapters == N_CHAPTERS
