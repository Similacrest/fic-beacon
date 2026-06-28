"""Read-only access to a Calibre library (metadata.db + EPUB files)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def _first(values: list | None) -> str | None:
    """First value of a custom-column list as a stripped string, or None."""
    if not values:
        return None
    text = str(values[0]).strip()
    return text or None


def _is_yes(values: list | None) -> bool:
    """Interpret a Calibre Yes/No (bool) custom column. Bool columns store 0/1; text
    columns may store "Yes"/"True". Anything else (incl. unset/0/No) is falsy."""
    first = _first(values)
    return first is not None and first.lower() in ("1", "yes", "true")


@dataclass
class CalibreBook:
    calibre_id: int
    title: str
    author: str
    path: str          # relative dir from library root, e.g. "Author/Book Title (42)"
    epub_name: str     # filename without extension, e.g. "Book Title - Author"
    source_url: str | None
    last_modified: str | None = None  # Calibre books.last_modified (ISO string)
    tags: list[str] = field(default_factory=list)          # Calibre tags
    genres: list[str] = field(default_factory=list)        # #genre_manual (curated, hierarchical)
    genre_tags: list[str] = field(default_factory=list)    # #genre (raw, for auto-classification)
    source_status: str | None = None  # #status (In-Progress/Completed/Hiatus/… — publication state)
    read: bool = False                # #read (Yes ⇒ finished / caught up to the current end)


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
                SELECT b.id, b.title, b.path, b.last_modified, d.name AS epub_name
                FROM books b
                JOIN data d ON d.book = b.id AND d.format = 'EPUB'
                ORDER BY b.last_modified DESC
                """,
            ).fetchall()
            ids = [r["id"] for r in rows]
            authors = self._fetch_authors(conn, ids)
            urls = self._fetch_source_urls(conn, ids)
            tags = self._fetch_tags(conn, ids)
            genres = self._fetch_custom_text(conn, "genre_manual", ids)
            genre_tags = self._fetch_custom_text(conn, "genre", ids)
            statuses = self._fetch_custom_text(conn, "status", ids)
            reads = self._fetch_custom_text(conn, "read", ids)
            return [
                CalibreBook(
                    calibre_id=r["id"],
                    title=r["title"],
                    author=authors.get(r["id"], "Unknown"),
                    path=r["path"],
                    epub_name=r["epub_name"],
                    source_url=urls.get(r["id"]),
                    last_modified=r["last_modified"],
                    tags=tags.get(r["id"], []),
                    genres=genres.get(r["id"], []),
                    genre_tags=genre_tags.get(r["id"], []),
                    source_status=_first(statuses.get(r["id"])),
                    read=_is_yes(reads.get(r["id"])),
                )
                for r in rows
            ]

    def get_book(self, calibre_id: int) -> CalibreBook | None:
        """Return a single book by its Calibre ID."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT b.id, b.title, b.path, b.last_modified, d.name AS epub_name
                FROM books b
                JOIN data d ON d.book = b.id AND d.format = 'EPUB'
                WHERE b.id = ?
                """,
                (calibre_id,),
            ).fetchone()
            if row is None:
                return None
            authors = self._fetch_authors(conn, [calibre_id])
            urls = self._fetch_source_urls(conn, [calibre_id])
            tags = self._fetch_tags(conn, [calibre_id])
            genres = self._fetch_custom_text(conn, "genre_manual", [calibre_id])
            genre_tags = self._fetch_custom_text(conn, "genre", [calibre_id])
            statuses = self._fetch_custom_text(conn, "status", [calibre_id])
            reads = self._fetch_custom_text(conn, "read", [calibre_id])
            return CalibreBook(
                calibre_id=row["id"],
                title=row["title"],
                author=authors.get(calibre_id, "Unknown"),
                path=row["path"],
                epub_name=row["epub_name"],
                source_url=urls.get(calibre_id),
                last_modified=row["last_modified"],
                tags=tags.get(calibre_id, []),
                genres=genres.get(calibre_id, []),
                genre_tags=genre_tags.get(calibre_id, []),
                source_status=_first(statuses.get(calibre_id)),
                read=_is_yes(reads.get(calibre_id)),
            )

    def epub_path(self, book: CalibreBook) -> Path:
        return self.library_path / book.path / f"{book.epub_name}.epub"

    def status_map(self, calibre_ids: list[int]) -> dict[int, str | None]:
        """Return {calibre_id: #status value} for the given books (None where unset).

        Used by the fetch sweep/poller to skip stories the source site marks done without
        re-reading every column — see app/calibre/status.py.
        """
        ids = [i for i in calibre_ids if i is not None]
        if not ids:
            return {}
        with self._connect() as conn:
            statuses = self._fetch_custom_text(conn, "status", ids)
        return {i: _first(statuses.get(i)) for i in ids}

    def _fetch_authors(
        self, conn: sqlite3.Connection, book_ids: list[int]
    ) -> dict[int, str]:
        """Return {calibre_id: author_name} — first author only for multi-author books."""
        if not book_ids:
            return {}
        placeholders = ",".join("?" * len(book_ids))
        rows = conn.execute(
            f"""
            SELECT bal.book AS book, a.name AS name
            FROM books_authors_link bal
            JOIN authors a ON a.id = bal.author
            WHERE bal.book IN ({placeholders})
            ORDER BY bal.book, a.name
            """,
            book_ids,
        ).fetchall()
        result: dict[int, str] = {}
        for r in rows:
            if r["book"] not in result:
                result[r["book"]] = r["name"]
        return result

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

    def _fetch_custom_text(
        self, conn: sqlite3.Connection, label: str, book_ids: list[int]
    ) -> dict[int, list[str]]:
        """Return {calibre_id: [values]} for a custom text column by its lookup label.

        Resolves the column id from `custom_columns`, then reads the appropriate layout.
        Calibre stores **normalized** columns (multi-value *and* enumeration/category, even
        when single-valued) in a link table (`books_custom_column_N_link` → `custom_column_N`),
        and **non-normalized** columns (free text, int, bool, datetime) directly as
        `custom_column_N(book, value)`. Branching on `is_multiple` alone misses single-value
        enumerations like `#status`, so we branch on `normalized`. Missing column / tables →
        empty (a library without these custom columns just yields nothing).
        """
        if not book_ids:
            return {}
        try:
            meta = conn.execute(
                "SELECT id, normalized FROM custom_columns WHERE label = ?", (label,)
            ).fetchone()
        except sqlite3.OperationalError:
            return {}  # library without custom columns
        if meta is None:
            return {}

        col, normalized = meta["id"], meta["normalized"]
        placeholders = ",".join("?" * len(book_ids))
        if normalized:
            sql = f"""
                SELECT l.book AS book, v.value AS value
                FROM books_custom_column_{col}_link l
                JOIN custom_column_{col} v ON v.id = l.value
                WHERE l.book IN ({placeholders})
                ORDER BY v.value
            """
        else:
            sql = f"""
                SELECT book, value FROM custom_column_{col}
                WHERE book IN ({placeholders})
            """
        try:
            rows = conn.execute(sql, book_ids).fetchall()
        except sqlite3.OperationalError:
            return {}
        result: dict[int, list[str]] = {}
        for r in rows:
            if r["value"] is not None:
                result.setdefault(r["book"], []).append(r["value"])
        return result

    def _fetch_tags(
        self, conn: sqlite3.Connection, book_ids: list[int]
    ) -> dict[int, list[str]]:
        """Return {calibre_id: [tag names]} for the given books (ordered by name)."""
        if not book_ids:
            return {}
        placeholders = ",".join("?" * len(book_ids))
        try:
            rows = conn.execute(
                f"""
                SELECT btl.book AS book, t.name AS name
                FROM books_tags_link btl
                JOIN tags t ON t.id = btl.tag
                WHERE btl.book IN ({placeholders})
                ORDER BY t.name
                """,
                book_ids,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # library without tag tables (unusual)
        result: dict[int, list[str]] = {}
        for r in rows:
            result.setdefault(r["book"], []).append(r["name"])
        return result
