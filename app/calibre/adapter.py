"""Read-only access to a Calibre library (metadata.db + EPUB files)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CalibreBook:
    calibre_id: int
    title: str
    author: str
    path: str          # relative dir from library root, e.g. "Author/Book Title (42)"
    epub_name: str     # filename without extension, e.g. "Book Title - Author"
    source_url: str | None
    last_modified: str | None = None  # Calibre books.last_modified (ISO string)


class CalibreAdapter:
    def __init__(self, library_path: Path) -> None:
        self.library_path = library_path
        self._db_path = library_path / "metadata.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def list_books(self) -> list[CalibreBook]:
        """Return all books that have an EPUB format."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    b.id,
                    b.title,
                    b.path,
                    b.last_modified,
                    d.name AS epub_name,
                    COALESCE(a.name, 'Unknown') AS author
                FROM books b
                JOIN data d ON d.book = b.id AND d.format = 'EPUB'
                LEFT JOIN books_authors_link bal ON bal.book = b.id
                LEFT JOIN authors a ON a.id = bal.author
                ORDER BY b.last_modified DESC
                """,
            ).fetchall()
            ids = [r["id"] for r in rows]
            urls = self._fetch_source_urls(conn, ids)
            return [
                CalibreBook(
                    calibre_id=r["id"],
                    title=r["title"],
                    author=r["author"],
                    path=r["path"],
                    epub_name=r["epub_name"],
                    source_url=urls.get(r["id"]),
                    last_modified=r["last_modified"],
                )
                for r in rows
            ]

    def get_book(self, calibre_id: int) -> CalibreBook | None:
        """Return a single book by its Calibre ID."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    b.id,
                    b.title,
                    b.path,
                    b.last_modified,
                    d.name AS epub_name,
                    COALESCE(a.name, 'Unknown') AS author
                FROM books b
                JOIN data d ON d.book = b.id AND d.format = 'EPUB'
                LEFT JOIN books_authors_link bal ON bal.book = b.id
                LEFT JOIN authors a ON a.id = bal.author
                WHERE b.id = ?
                """,
                (calibre_id,),
            ).fetchone()
            if row is None:
                return None
            urls = self._fetch_source_urls(conn, [calibre_id])
            return CalibreBook(
                calibre_id=row["id"],
                title=row["title"],
                author=row["author"],
                path=row["path"],
                epub_name=row["epub_name"],
                source_url=urls.get(calibre_id),
                last_modified=row["last_modified"],
            )

    def epub_path(self, book: CalibreBook) -> Path:
        return self.library_path / book.path / f"{book.epub_name}.epub"

    def _fetch_source_urls(
        self, conn: sqlite3.Connection, book_ids: list[int]
    ) -> dict[int, str]:
        """Return {calibre_id: url} for all books that have a 'url' identifier."""
        if not book_ids:
            return {}
        placeholders = ",".join("?" * len(book_ids))
        rows = conn.execute(
            f"SELECT book, val FROM identifiers WHERE type='url' AND book IN ({placeholders})",
            book_ids,
        ).fetchall()
        return {r["book"]: r["val"] for r in rows}
