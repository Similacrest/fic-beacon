"""End-to-end tests for the EPUB image route (/img/{calibre_id}/{path})."""
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import media

# 1x1 transparent PNG.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _build_library(root: Path) -> None:
    """A one-book Calibre library whose EPUB carries OEBPS/images/pic.png."""
    conn = sqlite3.connect(str(root / "metadata.db"))
    conn.executescript("""
    CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, path TEXT,
        author_sort TEXT, last_modified TIMESTAMP);
    CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
    CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
    CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
    CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
    CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple BOOL, normalized BOOL);
    INSERT INTO books VALUES (7, 'Picture Book', 'picture book', 'AuthorA/Picture Book (7)', 'AuthorA', '2026-01-01 10:00:00');
    INSERT INTO authors VALUES (1, 'Author A', 'A, Author');
    INSERT INTO books_authors_link VALUES (7, 1);
    INSERT INTO data VALUES (1, 7, 'EPUB', 'Picture Book - Author A', 0);
    """)
    conn.commit()
    conn.close()

    book_dir = root / "AuthorA/Picture Book (7)"
    book_dir.mkdir(parents=True)
    with zipfile.ZipFile(book_dir / "Picture Book - Author A.epub", "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr("OEBPS/images/pic.png", _PNG)


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_library(root)
        monkeypatch.setattr(settings, "calibre_library_path", root)
        app = FastAPI()
        app.include_router(media.router)
        yield TestClient(app)


def test_serves_image_resolved_under_opf_dir(client):
    # Path is OPF-relative ("images/pic.png"); the route re-anchors it to OEBPS/.
    resp = client.get("/img/7/images/pic.png")
    assert resp.status_code == 200
    assert resp.content == _PNG
    assert resp.headers["content-type"] == "image/png"
    assert "immutable" in resp.headers["cache-control"]


def test_missing_image_404(client):
    assert client.get("/img/7/images/missing.png").status_code == 404


def test_unknown_book_404(client):
    assert client.get("/img/999/images/pic.png").status_code == 404


def test_path_traversal_rejected(client):
    assert client.get("/img/7/../../../etc/passwd").status_code == 404
