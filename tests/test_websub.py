"""Tests for the WebSub hub (subscribe/verify) and the push publisher."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

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


class TestHub:
    async def test_subscribe_verifies_and_stores(self, in_memory_db, monkeypatch):
        topic = f"{websub.settings.base_url}/feed/fantasy/1"
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb",
            "hub.lease_seconds": "3600", "hub.secret": "s3cr3t",
        })

        async def _ok(*a, **k):
            return True
        monkeypatch.setattr(websub, "_verify_intent", _ok)

        resp = await websub.hub(req, db=in_memory_db)
        assert resp.status_code == 202
        sub = in_memory_db.query(WebSubSubscription).filter_by(topic_url=topic).first()
        assert sub is not None and sub.verified is True and sub.secret == "s3cr3t"
        assert sub.lease_expires_at is not None

    async def test_rejects_foreign_topic(self, in_memory_db):
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": "https://evil.example/feed",
            "hub.callback": "https://reader.example/cb",
        })
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, db=in_memory_db)
        assert ei.value.status_code == 404

    async def test_failed_verification_is_409(self, in_memory_db, monkeypatch):
        topic = f"{websub.settings.base_url}/feed"
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb",
        })

        async def _fail(*a, **k):
            return False
        monkeypatch.setattr(websub, "_verify_intent", _fail)
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, db=in_memory_db)
        assert ei.value.status_code == 409


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
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=1, budget_words=100)
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

    def test_pushes_to_channel_and_union_subscribers(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed/fantasy/1", callback_url="https://r/cb", verified=True))
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed", callback_url="https://r/union", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r/cb" in urls and "https://r/union" in urls

    def test_skips_unverified_and_expired(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = publisher.settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed", callback_url="https://r/unverified", verified=False))
        in_memory_db.add(WebSubSubscription(
            topic_url=f"{base}/feed", callback_url="https://r/expired", verified=True,
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
            topic_url=f"{base}/feed", callback_url="https://r/signed", verified=True, secret="k"))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        signed = [h for u, h in _FakeClient.posts if u == "https://r/signed"]
        assert signed and signed[0].get("X-Hub-Signature", "").startswith("sha1=")
