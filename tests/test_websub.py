"""Tests for the WebSub hub (subscribe/verify) and the push publisher."""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.models import (
    Book, BookStatus, Channel, Drop, WebSubSubscription, utcnow,
)
from app.routers import websub
from app.websub import publisher


def _form_request(form: dict):
    req = MagicMock()
    async def _form():
        return form
    req.form = _form
    return req


def _fake_db_session(session):
    """A db_session() stand-in that hands the background task the test's session
    without closing it (the in_memory_db fixture owns its lifecycle)."""
    @contextmanager
    def _factory():
        yield session
    return _factory


class TestHub:
    async def test_accepts_and_schedules_verification(self, monkeypatch):
        """A valid request returns 202 immediately and defers verification (spec §5.3)."""
        topic = f"{websub.settings.base_url}/feed/fantasy/1"
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb",
            "hub.lease_seconds": "3600", "hub.secret": "s3cr3t",
        })
        bg = BackgroundTasks()
        resp = await websub.hub(req, background=bg)
        assert resp.status_code == 202
        # Verification is deferred, not done inline.
        assert len(bg.tasks) == 1
        assert bg.tasks[0].func is websub._verify_and_store

    async def test_rejects_foreign_topic(self):
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": "https://evil.example/feed",
            "hub.callback": "https://reader.example/cb",
        })
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, background=BackgroundTasks())
        assert ei.value.status_code == 404

    async def test_rejects_bad_mode(self):
        req = _form_request({
            "hub.mode": "bogus", "hub.topic": f"{websub.settings.base_url}/feed/fantasy/1",
            "hub.callback": "https://reader.example/cb",
        })
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, background=BackgroundTasks())
        assert ei.value.status_code == 400

    async def test_verify_and_store_persists_on_success(self, in_memory_db, monkeypatch):
        topic = f"{websub.settings.base_url}/feed/fantasy/1"
        monkeypatch.setattr(websub, "_VERIFY_DELAYS", (0.0,))

        async def _ok(*a, **k):
            return True, "ok"
        monkeypatch.setattr(websub, "_verify_intent", _ok)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        await websub._verify_and_store("subscribe", topic, "https://reader.example/cb",
                                       3600, "s3cr3t")

        sub = in_memory_db.query(WebSubSubscription).filter_by(topic_url=topic).first()
        assert sub is not None and sub.verified is True and sub.secret == "s3cr3t"
        assert sub.lease_expires_at is not None

    async def test_verify_and_store_retries_then_succeeds(self, in_memory_db, monkeypatch):
        """A first verification miss (arming race) is retried, not dropped."""
        topic = f"{websub.settings.base_url}/feed/fantasy/1"
        monkeypatch.setattr(websub, "_VERIFY_DELAYS", (0.0, 0.0, 0.0))
        calls = {"n": 0}

        async def _flaky(*a, **k):
            calls["n"] += 1
            return calls["n"] >= 2, "detail"  # fails once, then succeeds
        monkeypatch.setattr(websub, "_verify_intent", _flaky)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        await websub._verify_and_store("subscribe", topic, "https://reader.example/cb",
                                       3600, None)

        assert calls["n"] == 2
        assert in_memory_db.query(WebSubSubscription).filter_by(topic_url=topic).count() == 1

    async def test_verify_and_store_skips_on_failed_verification(self, in_memory_db, monkeypatch):
        topic = f"{websub.settings.base_url}/feed/fantasy/1"
        monkeypatch.setattr(websub, "_VERIFY_DELAYS", (0.0, 0.0))

        async def _fail(*a, **k):
            return False, "status=200 echo=False"
        monkeypatch.setattr(websub, "_verify_intent", _fail)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        await websub._verify_and_store("subscribe", topic, "https://reader.example/cb",
                                       3600, None)

        assert in_memory_db.query(WebSubSubscription).count() == 0


class _FakeClient:
    posts: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, content=None, headers=None):
        _FakeClient.posts.append((url, headers))


class TestPublisher:
    def _setup_drop(self, db):
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=1, budget=100)
        db.add(ch)
        db.flush()
        book = Book(calibre_id=1, title="B", author="A", status=BookStatus.active,
                    channel_id=ch.id, slot_index=1)
        db.add(book)
        db.flush()
        drop = Drop(book_id=book.id, channel_id=ch.id, feed_key="1", word_count=10,
                    chapter_start=0, chapter_end=0, chapter_titles="C",
                    content_html="<p>x</p>", feedback_token="t", reader_slug="s")
        db.add(drop)
        db.flush()
        return drop

    def test_pushes_to_channel_slot_subscribers(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed/fantasy/1", callback_url="https://r/cb", verified=True))
        # A subscriber to a *different* slot must not be notified for this drop.
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed/fantasy/2", callback_url="https://r/other", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r/cb" in urls
        assert "https://r/other" not in urls

    def test_matches_tokened_and_bare_topic_forms(self, in_memory_db, monkeypatch):
        """rel=self is now tokened; subscribers registered with or without ?token= both match."""
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        topic = f"{base}/feed/fantasy/1"
        in_memory_db.add(WebSubSubscription(
            topic_url=topic, callback_url="https://r/bare", verified=True))
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{topic}?token=abc", callback_url="https://r/tokened", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r/bare" in urls and "https://r/tokened" in urls

    def test_skips_unverified_and_expired(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        topic = f"{base}/feed/fantasy/1"
        in_memory_db.add(WebSubSubscription(
            topic_url=topic, callback_url="https://r/unverified", verified=False))
        in_memory_db.add(WebSubSubscription(
            topic_url=topic, callback_url="https://r/expired", verified=True,
            lease_expires_at=utcnow() - timedelta(hours=1)))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r/unverified" not in urls
        assert "https://r/expired" not in urls

    def test_signs_when_secret_present(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed/fantasy/1", callback_url="https://r/signed",
            verified=True, secret="k"))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        signed = [h for u, h in _FakeClient.posts if u == "https://r/signed"]
        assert signed and signed[0].get("X-Hub-Signature", "").startswith("sha1=")
