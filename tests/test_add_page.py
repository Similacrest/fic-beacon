"""The single Add page (S4): pasted URLs and/or Calibre books, one channel, one button, and a
per-item outcome list."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import Book, BookStatus, Channel
from tests.test_library_add import _build_library
from tests.test_story_page import ctx  # noqa: F401  (fixture)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    def _make(status="In-Progress", read=0):
        _build_library(tmp_path, status, read)
        monkeypatch.setattr(settings, "calibre_library_path", tmp_path)
    return _make


def _post(client, **data):
    with patch("app.scheduler.trigger_fetch_pending"):
        return client.post("/admin/add", data=data, follow_redirects=False)


class TestAddPage:
    def test_renders_with_both_sections_and_nav(self, ctx, lib):
        client, _ = ctx
        lib()
        t = client.get("/admin/add").text
        assert "From the web" in t and "From your Calibre library" in t
        assert 'name="urls"' in t and 'name="calibre_ids"' in t
        assert t.count(">Add</button>") == 1          # one button for both sources
        assert 'href="/admin/add"' in t and 'href="/admin/library"' not in t

    def test_old_library_url_redirects(self, ctx):
        client, _ = ctx
        r = client.get("/admin/library", follow_redirects=False)
        assert r.headers["location"] == "/admin/add"

    def test_nothing_selected(self, ctx, lib):
        client, _ = ctx
        lib()
        r = _post(client, urls="  \n")
        assert r.headers["location"] == "/admin/add?error=empty"
        assert "Nothing to add" in client.get(r.headers["location"]).text


class TestAddUrls:
    def test_several_urls_with_duplicate_and_invalid(self, ctx, lib):
        client, db = ctx
        lib()
        _post(client, urls="https://www.royalroad.com/fiction/186926")
        r = _post(client, urls=("https://www.royalroad.com/fiction/186926/some-slug\n"   # same story
                                "https://www.royalroad.com/fiction/777\n"
                                "--config=/etc/passwd\n"))
        loc = r.headers["location"]
        assert "added=" in loc and "dupes=" in loc and "invalid=1" in loc
        books = db.query(Book).order_by(Book.id).all()
        assert [b.tracked for b in books] == [True, True]
        page = client.get(loc).text
        assert "✓ Added 1" in page and "Already here (1)" in page
        assert "1 line(s) weren't a valid" in page
        assert "Awaiting download" in page            # outcome carries the story's status
        assert f'href="/admin/books/{books[1].id}"' in page

    def test_channel_applies_to_urls(self, ctx, lib):
        client, db = ctx
        lib()
        other = Channel(name="Other", slug="other", parallel_slots=1, weight=1.0)
        db.add(other)
        db.commit()
        _post(client, urls="https://www.royalroad.com/fiction/5", channel_id=other.id)
        assert db.query(Book).one().channel_id == other.id

    def test_fetch_is_triggered_only_for_urls(self, ctx, lib):
        client, _ = ctx
        lib()
        with patch("app.scheduler.trigger_fetch_pending") as trig:
            client.post("/admin/add", data={"calibre_ids": "1"}, follow_redirects=False)
            assert not trig.called
            client.post("/admin/add", data={"urls": "https://www.royalroad.com/fiction/9"},
                        follow_redirects=False)
            assert trig.called


class TestAddCalibreAndUrlsTogether:
    def test_one_submit_adds_both(self, ctx, lib):
        client, db = ctx
        lib("Completed")
        r = _post(client, urls="https://www.royalroad.com/fiction/42", calibre_ids="1")
        books = db.query(Book).order_by(Book.id).all()
        assert len(books) == 2
        url_book = next(b for b in books if b.calibre_id is None)
        cal_book = next(b for b in books if b.calibre_id == 1)
        assert url_book.tracked and cal_book.status == BookStatus.queued   # routed by #status
        page = client.get(r.headers["location"]).text
        assert "✓ Added 2" in page and "Queued #1" in page

    def test_already_imported_book_is_reported_not_duplicated(self, ctx, lib):
        client, db = ctx
        lib()
        _post(client, calibre_ids="1")
        r = _post(client, calibre_ids="1")
        assert db.query(Book).count() == 1
        assert "dupes=" in r.headers["location"] and "added=" not in r.headers["location"]

    def test_missing_calibre_book_is_reported(self, ctx, lib):
        client, db = ctx
        lib()
        r = _post(client, calibre_ids="999")
        assert "missing=1" in r.headers["location"]
        assert "no longer in Calibre" in client.get(r.headers["location"]).text


class TestResultParams:
    def test_garbage_ids_are_ignored(self, ctx, lib):
        client, _ = ctx
        lib()
        t = client.get("/admin/add?added=abc,9999,,1;2&dupes=-1&invalid=x").text
        assert client.get("/admin/add?added=abc").status_code == 200
        assert "Added" not in t


class TestSameStoryTwoWays:
    """A story pasted as a URL and the same story sitting in Calibre must be one source."""

    def _calibre_url(self, tmp_path):
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "metadata.db"))
        conn.execute("INSERT INTO identifiers VALUES (1, 1, 'url', 'https://www.royalroad.com/fiction/555')")
        conn.commit()
        conn.close()

    def test_calibre_book_matching_a_pasted_url_is_not_added_twice(self, ctx, lib, tmp_path):
        client, db = ctx
        lib()
        self._calibre_url(tmp_path)
        _post(client, urls="https://www.royalroad.com/fiction/555/some-slug")
        r = _post(client, calibre_ids="1")
        assert db.query(Book).count() == 1
        assert "dupes=" in r.headers["location"] and "added=" not in r.headers["location"]

    def test_add_page_hides_calibre_books_already_tracked_by_url(self, ctx, lib, tmp_path):
        client, db = ctx
        lib()
        self._calibre_url(tmp_path)
        assert 'value="1"' in client.get("/admin/add").text.split('name="calibre_ids"')[1]
        _post(client, urls="https://www.royalroad.com/fiction/555")
        t = client.get("/admin/add").text
        assert 'name="calibre_ids"' not in t and "already added" in t

    def test_repeated_line_in_one_paste_is_added_once(self, ctx, lib):
        client, db = ctx
        lib()
        r = _post(client, urls="https://www.royalroad.com/fiction/9\nhttps://www.royalroad.com/fiction/9/x")
        assert db.query(Book).count() == 1
        assert "dupes=" not in r.headers["location"]
