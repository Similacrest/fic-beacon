"""App side of the FlareSolverr fallback: the fetcher's `via` shows up in the fetch status."""
from app.fetch.client import apply_result
from app.models import Book


def test_status_says_when_a_fetch_went_through_flaresolverr():
    book = Book(tracked=True)
    apply_result(book, {"calibre_id": None, "chapter_count": None, "via": "flaresolverr"})
    assert book.last_fetch_status.startswith("ok (via flaresolverr")
    apply_result(book, {"error": "403 even so", "via": "flaresolverr"})
    assert book.last_fetch_status.startswith("error: 403 even so (via flaresolverr")


def test_status_unchanged_without_via():
    book = Book(tracked=True)
    apply_result(book, {"calibre_id": None, "chapter_count": None})
    assert book.last_fetch_status == "ok"
