"""0.13 hardening: sanitiser, XSS escaping/CSP, CSRF guard, URL validation, fetcher auth,
bounded caches, and the review follow-ups."""
import importlib.util
import os
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import scheduler
from app.config import settings
from app.main import app
from app.models import Book, BookStatus, Channel
from app.ongoing.feed_url import is_fetchable_url
from app.sanitize import CSP_HEADERS, sanitize_html

_spec = importlib.util.spec_from_file_location(
    "fetcher_app_h", os.path.join(os.path.dirname(__file__), "..", "fetcher", "app.py"))
fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetcher)

TOKEN = "proxy-secret"
HDR = {"X-Beacon-Proxy-Token": TOKEN}


# ── sanitiser ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_strips_script_embeds_and_handlers(self):
        out = sanitize_html(
            '<p onclick="x()">hi</p><script>alert(1)</script><iframe src="//e"></iframe>'
            '<img src="a.png" onerror="alert(1)"><object data="x"></object><embed src="y">')
        for bad in ("script", "iframe", "onclick", "onerror", "<object", "<embed", "alert"):
            assert bad not in out
        assert "hi" in out and 'src="a.png"' in out

    @pytest.mark.parametrize("href", ["javascript:alert(1)", " JaVa\tScRiPt:alert(1)",
                                      "data:text/html,<script>1</script>", "vbscript:x"])
    def test_strips_dangerous_urls(self, href):
        assert "href" not in sanitize_html(f'<a href="{href}">x</a>')

    def test_keeps_safe_urls_and_inline_images(self):
        out = sanitize_html('<a href="https://e.com/a">x</a><img src="data:image/png;base64,AAAA">')
        assert 'href="https://e.com/a"' in out and "data:image/png" in out

    def test_unwraps_forms_and_keeps_text(self):
        out = sanitize_html('<form action="//evil"><input name=x>Pay now</form>')
        assert "form" not in out and "input" not in out and "Pay now" in out

    def test_keeps_footnote_details(self):
        html = '<details class="beacon-note"><summary>1</summary>note</details>'
        assert sanitize_html(html) == html

    def test_chapterizer_sanitises_chapter_bodies(self):
        from app.epub.chapterizer import _extract_body
        raw = (b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p onclick="x()">t</p>'
               b'<script>alert(1)</script></body></html>')
        out = _extract_body(raw, "OEBPS/c.xhtml")
        assert "script" not in out and "onclick" not in out and ">t<" in out


# ── XSS in public pages ───────────────────────────────────────────────────────

class TestEscaping:
    def test_reader_html_escapes_title_and_author(self):
        from app.routers.reader import _reader_html
        out = _reader_html("<script>1</script>", '"><img src=x onerror=1>', 5, "<p>c</p>")
        assert "<script>" not in out and "&lt;script&gt;" in out
        assert "<img src=x" not in out

    def test_confirm_page_escapes_book_title(self):
        from app.routers.feedback import _confirm_page
        out = _confirm_page("tok", "drop", "L", "D", "<img src=x onerror=alert(1)>")
        assert "<img src=x" not in out and "&lt;img" in out
        assert "javascript:" not in out  # CSP would block it; the Cancel link is gone

    def test_csp_blocks_scripts_and_framing(self):
        csp = CSP_HEADERS["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp


# ── CSRF + gate options ───────────────────────────────────────────────────────

@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "proxy_token", TOKEN)
    monkeypatch.setattr(settings, "admin_open", False)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    return TestClient(app, raise_server_exceptions=False)


class TestCsrf:
    @pytest.mark.parametrize("extra", [{"Sec-Fetch-Site": "cross-site"},
                                       {"Sec-Fetch-Site": "same-site"},
                                       {"Origin": "https://evil.example"}])
    def test_cross_site_admin_post_refused_even_with_valid_token(self, gated, extra):
        resp = gated.post("/admin/config/regenerate-secret", headers={**HDR, **extra})
        assert resp.status_code == 403

    @pytest.mark.parametrize("extra", [{}, {"Sec-Fetch-Site": "same-origin"},
                                       {"Sec-Fetch-Site": "none"},
                                       {"Origin": "http://testserver"},
                                       {"Origin": "https://fb.example", "X-Forwarded-Host": "fb.example"}])
    def test_same_origin_or_non_browser_posts_pass_the_gate(self, gated, extra):
        resp = gated.post("/admin/config/regenerate-secret", headers={**HDR, **extra})
        assert resp.status_code not in (401, 403, 503)

    def test_gets_are_never_origin_checked(self, gated):
        resp = gated.get("/admin/", headers={**HDR, "Sec-Fetch-Site": "cross-site"})
        assert resp.status_code not in (401, 403)

    def test_public_feedback_post_is_not_origin_checked(self, gated):
        resp = gated.post("/fb/confirm/nope", data={"action": "drop"},
                          headers={"Sec-Fetch-Site": "cross-site"})
        assert resp.status_code not in (401, 403)  # reached the handler (it 500s: no DB here)


class TestAdminOpen:
    def test_opt_out_opens_admin_when_no_token(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_token", None)
        monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
        monkeypatch.setattr(settings, "admin_open", True)
        c = TestClient(app, raise_server_exceptions=False)
        assert c.get("/admin/").status_code not in (401, 503)

    def test_opt_out_never_overrides_a_configured_token(self, gated, monkeypatch):
        monkeypatch.setattr(settings, "admin_open", True)
        assert gated.get("/admin/").status_code == 401


# ── URL validation ────────────────────────────────────────────────────────────

class TestUrlValidation:
    @pytest.mark.parametrize("url", ["https://www.royalroad.com/fiction/1", "http://s/story"])
    def test_accepts_http_urls(self, url):
        assert is_fetchable_url(url)

    @pytest.mark.parametrize("url", ["-oexclude=x", "--config=/etc/x", "file:///etc/passwd",
                                     "ftp://h/x", "javascript:1", "", None, "https://", " https://a/b",
                                     "https://a/b c", "https://a/b\nc"])
    def test_rejects_everything_else(self, url):
        assert not is_fetchable_url(url)
        assert fetcher._is_http_url(url or "") == is_fetchable_url(url)

    def test_add_story_rejects_option_like_url(self, in_memory_db):
        from app.routers.ongoing import add_story
        cid = in_memory_db.query(Channel.id).limit(1).scalar()
        with patch("app.scheduler.trigger_fetch_pending"):
            resp = add_story(source_url="--config=/etc/x", title="", channel_id=cid, db=in_memory_db)
        assert resp.headers["location"].endswith("?error=invalid")
        assert in_memory_db.query(Book).count() == 0

    def test_add_story_with_unknown_channel_falls_back(self, in_memory_db):
        from app.routers.ongoing import add_story
        with patch("app.scheduler.trigger_fetch_pending"):
            add_story(source_url="https://s/x", title="", channel_id=9999, db=in_memory_db)
        assert in_memory_db.query(Book).one().channel_id is not None

    def test_submit_and_track_skips_unsafe_urls(self, in_memory_db):
        cid = in_memory_db.query(Channel.id).limit(1).scalar()
        b = Book(title="t", source_url="-oevil", tracked=True, channel_id=cid)
        in_memory_db.add(b)
        in_memory_db.flush()
        with patch("app.fetch.client.submit_fetch") as submit:
            assert scheduler.submit_and_track(in_memory_db, [b]) is None
        submit.assert_not_called()


class TestFetcherAuth:
    def test_token_enforced_when_configured(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(fetcher, "FETCHER_TOKEN", "s3")
        with pytest.raises(HTTPException) as ei:
            fetcher._require_token(None)
        assert ei.value.status_code == 401
        with pytest.raises(HTTPException):
            fetcher._require_token("wrong")
        assert fetcher._require_token("s3") is None

    def test_open_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(fetcher, "FETCHER_TOKEN", "")
        assert fetcher._require_token(None) is None

    def test_fetch_rejects_option_like_urls(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            fetcher.fetch(fetcher.FetchRequest(urls=["https://ok/x", "-oevil=1"]))
        assert ei.value.status_code == 422

    def test_client_sends_token_header(self, monkeypatch):
        from app.fetch import client
        monkeypatch.setattr(settings, "fetcher_token", "s3")
        assert client._headers() == {"X-Fetcher-Token": "s3"}
        monkeypatch.setattr(settings, "fetcher_token", None)
        assert client._headers() == {}


# ── caches, polling, scheduler, planner follow-ups ────────────────────────────

class TestChapterCache:
    def test_superseded_versions_are_not_retained_and_cache_is_bounded(self, tmp_path, monkeypatch):
        from app.epub import chapterizer
        calls = []
        monkeypatch.setattr(chapterizer, "_parse", lambda p: calls.append(p) or [])
        monkeypatch.setattr(chapterizer, "_CACHE_MAX", 3)
        chapterizer._cache.clear()
        f = tmp_path / "a.epub"
        f.write_bytes(b"x")
        chapterizer.chapterize(f)
        os.utime(f, (1, 1))
        chapterizer.chapterize(f)  # new mtime → re-parse, *replacing* the entry
        assert len(chapterizer._cache) == 1 and len(calls) == 2
        for i in range(6):
            g = tmp_path / f"b{i}.epub"
            g.write_bytes(b"x")
            chapterizer.chapterize(g)
        assert len(chapterizer._cache) == 3

    def test_normalize_name_keeps_leading_dot_names(self):
        from app.epub.chapterizer import _normalize_name
        assert _normalize_name("./a/b.xhtml") == "a/b.xhtml"
        assert _normalize_name("/a.xhtml") == "a.xhtml"
        assert _normalize_name(".hidden.xhtml") == ".hidden.xhtml"


class TestFeedFetch:
    def test_uses_bounded_httpx_not_feedparser_url_fetch(self, monkeypatch):
        from app.ongoing import poller
        rss = b"<rss version='2.0'><channel><item><guid>g1</guid></item></channel></rss>"
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=rss))
        real = httpx.Client
        monkeypatch.setattr(poller.httpx, "Client", lambda **kw: real(transport=transport, **kw))
        assert poller._newest_guid(poller._fetch_feed("https://s/feed")) == "g1"
        assert poller._FEED_TIMEOUT.read == 20.0

    def test_http_error_propagates_so_the_poller_skips_that_feed(self, monkeypatch):
        from app.ongoing import poller
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
        real = httpx.Client
        monkeypatch.setattr(poller.httpx, "Client", lambda **kw: real(transport=transport, **kw))
        with pytest.raises(httpx.HTTPStatusError):
            poller._fetch_feed("https://s/feed")


class TestSchedulerJobMap:
    def test_groups_books_sharing_a_url(self):
        books = [Book(id=1, source_url="u"), Book(id=2, source_url="u"), Book(id=3, source_url="v")]
        assert scheduler._group_by_url(books) == {"u": [1, 2], "v": [3]}

    def test_legacy_single_id_values_still_read(self):
        assert scheduler._book_ids(7) == [7]
        assert scheduler._book_ids([1, 2]) == [1, 2]


class TestRequeue:
    def test_tracked_story_stays_active_and_respects_cursor_floor(self, in_memory_db):
        from app.routers.admin import requeue_book
        cid = in_memory_db.query(Channel.id).limit(1).scalar()
        b = Book(title="t", tracked=True, channel_id=cid, status=BookStatus.dropped,
                 cursor_chapter_index=9, cursor_floor=4, fresh_from_index=7, paused=True, quota_weight=0)
        in_memory_db.add(b)
        in_memory_db.commit()
        requeue_book(b.id, db=in_memory_db)
        in_memory_db.refresh(b)
        assert b.status == BookStatus.active and b.slot_index is not None
        assert b.cursor_chapter_index == 4 and b.fresh_from_index is None and b.paused is False

    def test_backlog_book_goes_back_to_queue(self, in_memory_db):
        from app.routers.admin import requeue_book
        cid = in_memory_db.query(Channel.id).limit(1).scalar()
        b = Book(title="t", channel_id=cid, status=BookStatus.dropped, cursor_chapter_index=3)
        in_memory_db.add(b)
        in_memory_db.commit()
        requeue_book(b.id, db=in_memory_db)
        in_memory_db.refresh(b)
        assert b.status == BookStatus.queued and b.cursor_chapter_index == 0


class TestPlannerPending:
    def test_story_without_calibre_id_has_no_units_and_no_warning(self, in_memory_db, caplog):
        from app.calibre.adapter import CalibreAdapter
        from app.planner.planner import _remaining_units
        cid = in_memory_db.query(Channel.id).limit(1).scalar()
        b = Book(title="t", tracked=True, channel_id=cid, calibre_id=None)
        with caplog.at_level("WARNING"):
            assert _remaining_units(b, CalibreAdapter(settings.calibre_library_path)) == []
        assert not caplog.records


class TestBackgroundPublish:
    def test_publish_runs_on_a_worker_thread(self, monkeypatch):
        from app.models import Drop
        from app.websub import publisher
        started = {}

        class _T:
            def __init__(self, target, args, name, daemon):
                started.update(target=target, args=args, daemon=daemon)

            def start(self):
                started["started"] = True
        monkeypatch.setattr(publisher.threading, "Thread", _T)
        publisher.publish_in_background([Drop(id=5), Drop(id=6)])
        assert started["target"] is publisher._publish_ids and started["args"] == ([5, 6],)
        assert started["daemon"] and started["started"]

    def test_nothing_to_publish_starts_no_thread(self, monkeypatch):
        from app.websub import publisher
        monkeypatch.setattr(publisher.threading, "Thread",
                            lambda *a, **k: pytest.fail("no thread expected"))
        publisher.publish_in_background([])
