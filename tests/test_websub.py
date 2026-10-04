"""Tests for the WebSub hub (subscribe/verify) and the push publisher."""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.models import (
    Book, BookStatus, Channel, Drop, WebSubSubscription, utcnow,
)
from app.config import settings
from app.routers import websub
from app.websub import publisher, safety

SECRET = "test-secret"  # seeded into Config by the in_memory_db fixture


def _topic(slug="fantasy", key=1, token=SECRET):
    base = f"{settings.base_url}/feed/{slug}/{key}"
    return f"{base}?token={token}" if token is not None else base


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """No real DNS in tests: hostnames resolve to a public address, IP literals to themselves."""
    import ipaddress

    def _fake(host, port):
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return ["93.184.216.34"]
    monkeypatch.setattr(safety, "_resolve", _fake)


@pytest.fixture
def hub_db(in_memory_db, monkeypatch):
    monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))
    return in_memory_db


def _form_request(form: dict):
    from starlette.datastructures import FormData
    data = FormData(form)
    req = MagicMock()
    async def _form():
        return data
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
    async def test_accepts_and_schedules_verification(self, hub_db, monkeypatch):
        """A valid request returns 202 immediately and defers verification (spec §5.3)."""
        topic = _topic()
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb",
            "hub.lease_seconds": "3600", "hub.secret": "s3cr3t",
            "hub.verify_token": "vt0ken",
        })
        bg = BackgroundTasks()
        resp = await websub.hub(req, background=bg)
        assert resp.status_code == 202
        # Verification is deferred, not done inline.
        assert len(bg.tasks) == 1
        assert bg.tasks[0].func is websub._verify_and_store
        # The subscriber-supplied verify_token is carried into the deferred verification
        # (PuSH 0.3 subscribers match on it before echoing the challenge).
        assert bg.tasks[0].args[-1] == "vt0ken"

    async def test_verify_intent_merges_into_callback_query(self, monkeypatch):
        """The verification GET must carry hub.* AND preserve the callback's own query.

        httpx (≥0.28) *replaces* a URL's query when you pass params=, so we merge into the
        callback URL instead. Readers (Inoreader) key their pending verification on params
        already in the callback (e.g. feed_id/hub_id); dropping them → bare empty 200.
        """
        captured = {}

        class _Resp:
            status_code = 200

            async def aiter_bytes(self):
                yield b"challenge123"

        class _Stream:
            def __init__(self, url, headers, extensions):
                captured.update(url=str(url), headers=headers, extensions=extensions)

            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        class _AsyncClient:
            def __init__(self, *a, **k):
                captured["follow_redirects"] = k.get("follow_redirects")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, headers=None, extensions=None):
                return _Stream(url, headers, extensions)

        monkeypatch.setattr(websub.httpx, "AsyncClient", _AsyncClient)
        ok, _ = await websub._verify_intent(
            "https://incoming.example/cb?feed_id=42&hub_id=7",
            "subscribe", "https://t/1", "challenge123", 3600, "vt0ken")
        assert ok is True
        url = captured["url"]
        # SSRF hardening: connect to the validated IP, keep Host/SNI, never follow redirects.
        assert url.startswith("https://93.184.216.34/")
        assert captured["headers"]["Host"] == "incoming.example"
        assert captured["extensions"]["sni_hostname"] == "incoming.example"
        assert captured["follow_redirects"] is False
        # The subscriber's own params survive the merge…
        assert "feed_id=42" in url and "hub_id=7" in url
        # …alongside our verification params.
        assert "hub.verify_token=vt0ken" in url
        assert "hub.challenge=challenge123" in url

    async def test_sync_verify_stores_and_returns_204(self, in_memory_db, monkeypatch):
        """A hub.verify=sync subscriber (Inoreader) is verified inline → 204 + stored."""
        topic = _topic()
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb",
            "hub.verify": "sync", "hub.verify_token": "vt",
        })

        async def _ok(*a, **k):
            return True, "ok"
        monkeypatch.setattr(websub, "_verify_intent", _ok)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        resp = await websub.hub(req, background=BackgroundTasks())
        assert resp.status_code == 204
        sub = in_memory_db.query(WebSubSubscription).filter_by(topic_url=topic).first()
        assert sub is not None and sub.verified is True

    async def test_sync_verify_failure_is_409(self, in_memory_db, monkeypatch):
        topic = _topic()
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": topic,
            "hub.callback": "https://reader.example/cb", "hub.verify": "sync",
        })

        async def _fail(*a, **k):
            return False, "status=200 echo=False"
        monkeypatch.setattr(websub, "_verify_intent", _fail)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, background=BackgroundTasks())
        assert ei.value.status_code == 409
        assert in_memory_db.query(WebSubSubscription).count() == 0

    async def test_rejects_foreign_topic(self, hub_db):
        req = _form_request({
            "hub.mode": "subscribe", "hub.topic": "https://evil.example/feed",
            "hub.callback": "https://reader.example/cb",
        })
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, background=BackgroundTasks())
        assert ei.value.status_code == 404

    async def test_rejects_bad_mode(self, hub_db):
        req = _form_request({
            "hub.mode": "bogus", "hub.topic": _topic(),
            "hub.callback": "https://reader.example/cb",
        })
        with pytest.raises(HTTPException) as ei:
            await websub.hub(req, background=BackgroundTasks())
        assert ei.value.status_code == 400

    async def test_verify_and_store_persists_on_success(self, in_memory_db, monkeypatch):
        topic = _topic()
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
        topic = _topic()
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
        topic = _topic()
        monkeypatch.setattr(websub, "_VERIFY_DELAYS", (0.0, 0.0))

        async def _fail(*a, **k):
            return False, "status=200 echo=False"
        monkeypatch.setattr(websub, "_verify_intent", _fail)
        monkeypatch.setattr(websub, "db_session", _fake_db_session(in_memory_db))

        await websub._verify_and_store("subscribe", topic, "https://reader.example/cb",
                                       3600, None)

        assert in_memory_db.query(WebSubSubscription).count() == 0



class TestHubSecurity:
    async def _hub(self, **form):
        form.setdefault("hub.mode", "subscribe")
        form.setdefault("hub.callback", "https://reader.example/cb")
        return await websub.hub(_form_request(form), background=BackgroundTasks())

    @pytest.mark.parametrize("topic", [_topic(token=None), _topic(token="wrong"), _topic(token="")])
    async def test_token_free_or_wrong_token_topic_is_4xx(self, hub_db, topic):
        with pytest.raises(HTTPException) as ei:
            await self._hub(**{"hub.topic": topic})
        assert 400 <= ei.value.status_code < 500

    async def test_duplicate_token_params_rejected(self, hub_db):
        with pytest.raises(HTTPException) as ei:
            await self._hub(**{"hub.topic": _topic() + "&token=wrong"})
        assert ei.value.status_code == 403

    async def test_valid_token_topic_accepted(self, hub_db):
        resp = await self._hub(**{"hub.topic": _topic()})
        assert resp.status_code == 202

    @pytest.mark.parametrize("callback", [
        "http://reader.example/cb",          # not https
        "http://127.0.0.1/cb",
        "http://10.1.2.3/cb",
        "http://[::1]/cb",
        "https://127.0.0.1/cb",
        "https://localhost./cb",             # resolved below → loopback
        "https://10.45.113.5:8080/cb",
        "https://192.168.1.10/cb",
        "https://169.254.169.254/latest",    # link-local / cloud metadata
        "https://100.64.0.1/cb",             # CGNAT (Tailscale)
        "https://[fd00::1]/cb",              # ULA
        "https://[::ffff:127.0.0.1]/cb",     # v4-mapped loopback
        "https://user:pw@reader.example/cb", # embedded credentials
        "ftp://reader.example/cb",
    ])
    async def test_unsafe_callbacks_rejected(self, hub_db, monkeypatch, callback):
        monkeypatch.setattr(safety, "_resolve", lambda host, port: ["127.0.0.1"] if host.startswith("localhost") else [host])
        with pytest.raises(HTTPException) as ei:
            await self._hub(**{"hub.topic": _topic(), "hub.callback": callback})
        assert ei.value.status_code == 400

    async def test_hostname_resolving_to_private_address_rejected(self, hub_db, monkeypatch):
        monkeypatch.setattr(safety, "_resolve", lambda host, port: ["93.184.216.34", "10.0.0.9"])
        with pytest.raises(HTTPException) as ei:
            await self._hub(**{"hub.topic": _topic()})
        assert ei.value.status_code == 400

    async def test_callback_host_allowlist(self, hub_db, monkeypatch):
        monkeypatch.setattr(safety.settings, "websub_callback_hosts", "incoming.inoreader.com")
        with pytest.raises(HTTPException):
            await self._hub(**{"hub.topic": _topic()})  # reader.example not allowed
        resp = await self._hub(**{"hub.topic": _topic(),
                                  "hub.callback": "https://incoming.inoreader.com/x"})
        assert resp.status_code == 202

    async def test_verify_intent_does_not_connect_to_unsafe_target(self, monkeypatch):
        monkeypatch.setattr(safety, "_resolve", lambda host, port: ["10.0.0.9"])

        class _Boom:
            def __init__(self, *a, **k):
                raise AssertionError("must not open a connection")
        monkeypatch.setattr(websub.httpx, "AsyncClient", _Boom)
        ok, detail = await websub._verify_intent(
            "https://rebind.example/cb", "subscribe", _topic(), "c", 60)
        assert ok is False and "unsafe callback" in detail


class TestPrune:
    def test_prunes_stale_and_token_free_subscriptions(self, in_memory_db):
        base = settings.base_url
        keep = WebSubSubscription(topic_url=_topic(), callback_url="https://r.example/a", verified=True)
        for topic in (_topic(token=None), _topic(token="old"),
                      f"https://old-domain.example/feed/fantasy/1?token={SECRET}"):
            in_memory_db.add(WebSubSubscription(topic_url=topic, callback_url="https://r.example/b",
                                                verified=True))
        in_memory_db.add(keep)
        in_memory_db.flush()
        assert safety.prune_subscriptions(in_memory_db) == 3
        in_memory_db.commit()
        assert [s.topic_url for s in in_memory_db.query(WebSubSubscription)] == [_topic()]
        assert base  # base_url used by _topic


class _FakeClient:
    posts: list = []
    bodies: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, content=None, headers=None, extensions=None):
        # `url` is the IP-pinned URL; the original callback is recoverable from Host/SNI.
        _FakeClient.posts.append((f"https://{extensions['sni_hostname']}{url.path}", headers))
        _FakeClient.bodies.append(content)
        return self


class TestPublisher:
    def _setup_drop(self, db):
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=1, weight=1)
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
        base = settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=_topic(), callback_url="https://r.example/cb", verified=True))
        # A subscriber to a *different* slot must not be notified for this drop.
        in_memory_db.add(WebSubSubscription(
            topic_url=_topic(key=2), callback_url="https://r.example/other", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r.example/cb" in urls
        assert "https://r.example/other" not in urls

    def test_only_pushes_to_subscriptions_carrying_the_valid_token(self, in_memory_db, monkeypatch):
        """Token-free / wrong-token rows (legacy, or planted) never receive feed content."""
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        for cb, topic in (
            ("https://r.example/bare", _topic(token=None)),
            ("https://r.example/wrong", _topic(token="nope")),
            ("https://r.example/good", _topic()),
            ("https://r.example/good-extra-param", _topic() + "&fmt=atom"),
        ):
            in_memory_db.add(WebSubSubscription(topic_url=topic, callback_url=cb, verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r.example/good" in urls and "https://r.example/good-extra-param" in urls
        assert "https://r.example/bare" not in urls and "https://r.example/wrong" not in urls

    def test_push_refuses_unsafe_callback(self, in_memory_db, monkeypatch):
        """A stored callback is re-validated at push time (e.g. DNS now points inside)."""
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        in_memory_db.add(WebSubSubscription(
            topic_url=_topic(), callback_url="https://rebind.example/cb", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(safety, "_resolve", lambda host, port: ["10.0.0.7"])
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        assert _FakeClient.posts == []

    def test_skips_unverified_and_expired(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = settings.base_url
        topic = _topic()
        in_memory_db.add(WebSubSubscription(
            topic_url=topic, callback_url="https://r.example/unverified", verified=False))
        in_memory_db.add(WebSubSubscription(
            topic_url=topic, callback_url="https://r.example/expired", verified=True,
            lease_expires_at=utcnow() - timedelta(hours=1)))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        urls = [u for u, _ in _FakeClient.posts]
        assert "https://r.example/unverified" not in urls
        assert "https://r.example/expired" not in urls

    def test_signs_when_secret_present(self, in_memory_db, monkeypatch):
        _FakeClient.posts = []
        drop = self._setup_drop(in_memory_db)
        base = settings.base_url
        in_memory_db.add(WebSubSubscription(
            topic_url=_topic(), callback_url="https://r.example/signed",
            verified=True, secret="k"))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drop])

        signed = [h for u, h in _FakeClient.posts if u == "https://r.example/signed"]
        assert signed and signed[0].get("X-Hub-Signature", "").startswith("sha1=")


class TestTrimToBudget:
    """The push body is trimmed to the newest drops that fit the byte budget — Inoreader
    silently drops oversized fat pings (see publisher._trim_to_budget)."""

    def _drops(self, sizes):
        return [Drop(content_html="x" * n) for n in sizes]

    def test_keeps_only_what_fits_newest_first(self):
        # size per drop = len + _ENTRY_OVERHEAD (2000); envelope = _FEED_OVERHEAD (1500).
        drops = self._drops([10_000, 10_000, 10_000])  # 12k each
        kept = publisher._trim_to_budget(drops, 20_000)
        assert kept == drops[:1]  # 1500 + 12000 fits; a second would exceed 20k

    def test_always_keeps_at_least_the_newest(self):
        drops = self._drops([500_000])  # far over budget
        kept = publisher._trim_to_budget(drops, 1_000)
        assert kept == drops  # never split / drop the sole newest item

    def test_zero_budget_disables_trimming(self):
        drops = self._drops([10_000, 10_000, 10_000])
        assert publisher._trim_to_budget(drops, 0) == drops

    def test_push_body_shrinks_under_budget(self, in_memory_db, monkeypatch):
        """End-to-end: a small Config budget makes the pushed body carry fewer items than the
        full slot feed would."""
        from app.models import Config
        _FakeClient.posts = []
        _FakeClient.bodies = []
        ch = Channel(name="Fantasy", slug="fantasy", parallel_slots=1, weight=1,
                     feed_item_limit=50)
        in_memory_db.add(ch)
        cfg = in_memory_db.get(Config, 1) or Config(id=1)
        cfg.feed_secret = "tok"
        cfg.websub_max_push_bytes = 8_000
        in_memory_db.add(cfg)
        in_memory_db.flush()
        book = Book(calibre_id=1, title="B", author="A", status=BookStatus.active,
                    channel_id=ch.id, slot_index=1)
        in_memory_db.add(book)
        in_memory_db.flush()
        drops = []
        for i in range(5):
            d = Drop(book_id=book.id, channel_id=ch.id, feed_key="1", word_count=10,
                     chapter_start=i, chapter_end=i, chapter_titles=f"C{i}",
                     content_html="y" * 5_000, feedback_token=f"t{i}", reader_slug=f"s{i}")
            in_memory_db.add(d)
            drops.append(d)
        in_memory_db.add(WebSubSubscription(
            topic_url=_topic(token="tok"),
            callback_url="https://r.example/cb", verified=True))
        in_memory_db.commit()
        monkeypatch.setattr(publisher.httpx, "Client", _FakeClient)

        publisher.publish_updates(in_memory_db, [drops[-1]])

        assert _FakeClient.bodies, "expected a push"
        body = _FakeClient.bodies[-1]
        # With a 5k content_html + 2k overhead per item and an 8k budget, only 1 item fits.
        assert body.count(b"<entry") == 1
