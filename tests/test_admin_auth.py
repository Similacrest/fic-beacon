"""Deny-by-default proxy-token gate (app/security.py:ProxyTokenMiddleware)."""
import re

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import admin, feed, feedback, media, reader, websub
from app.security import bind_is_loopback

TOKEN = "proxy-shared-secret"
HDR = "X-Beacon-Proxy-Token"


@pytest.fixture
def configure(monkeypatch):
    def _cfg(token=TOKEN, bind="0.0.0.0", public_websub=False):
        monkeypatch.setattr(settings, "proxy_token", token)
        monkeypatch.setattr(settings, "bind_host", bind)
        monkeypatch.setattr(settings, "public_websub", public_websub)
        # No lifespan (scheduler/DB): the gate runs before any handler anyway.
        return TestClient(app, raise_server_exceptions=False)
    return _cfg


def _non_public_routes():
    """Every (method, concrete path) the app serves outside the public prefixes."""
    from app.security import is_public_path
    out = []
    # (FastAPI defers included routers, so read them from the modules.)
    for r in [r for mod in (admin, feed, feedback, reader, media, websub) for r in mod.router.routes]:
        if isinstance(r, APIRoute) and not is_public_path(re.sub(r"\{[^}]+\}", "1", r.path)):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out.append((m, re.sub(r"\{[^}]+\}", "1", r.path)))
    return out


def test_route_inventory_covers_admin_and_mutations():
    routes = _non_public_routes()
    assert any(p.startswith("/admin/books") for _, p in routes)
    assert any(m == "POST" and p.startswith("/admin") for m, p in routes)
    assert not any(p.startswith(("/feed", "/read", "/fb", "/img")) for _, p in routes)


@pytest.mark.parametrize("headers", [{}, {HDR: "wrong"}, {HDR: ""}, {"X-Remote-User": "admin"},
                                     {HDR: "wrong", "X-Remote-User": "admin"}])
def test_every_non_public_route_401_without_valid_token(configure, headers):
    c = configure()
    for method, path in _non_public_routes():
        resp = c.request(method, path, headers=headers, follow_redirects=False)
        assert resp.status_code == 401, (method, path, resp.status_code)


def test_docs_and_unknown_paths_also_denied(configure):
    c = configure()
    for path in ("/docs", "/openapi.json", "/nope", "/websub/hub"):
        assert c.get(path).status_code == 401, path


def test_valid_token_passes_the_gate(configure):
    c = configure()
    # Past the gate (the handler itself may then fail for lack of a DB — that's not 401/503).
    assert c.get("/admin/", headers={HDR: TOKEN}).status_code not in (401, 503)


@pytest.mark.parametrize("path", ["/feed/x/1", "/read/abc", "/fb/tok", "/fb/confirm/tok",
                                  "/img/1/a.png", "/static/missing.css", "/robots.txt"])
def test_public_paths_need_no_token(configure, path):
    assert configure().get(path).status_code not in (401, 503)


def test_prefix_lookalikes_are_not_public(configure):
    c = configure()
    for path in ("/feedback", "/reader", "/images", "/static-admin", "/admin/feed/x"):
        assert c.get(path).status_code == 401, path


def test_websub_public_only_when_enabled(configure):
    assert configure().post("/websub/hub").status_code == 401
    assert configure(public_websub=True).post("/websub/hub").status_code not in (401, 503)


def test_forged_remote_user_is_never_trusted(configure):
    resp = configure().get("/admin/", headers={"X-Remote-User": "admin"})
    assert resp.status_code == 401


def test_remote_user_exposed_only_with_valid_token(configure):
    seen = {}

    @app.get("/_who", include_in_schema=False)
    def _who(request: Request):
        seen["u"] = request.state.remote_user if hasattr(request.state, "remote_user") else None
        return {}
    try:
        c = configure()
        c.get("/_who", headers={HDR: TOKEN, "X-Remote-User": "alice"})
        assert seen["u"] == "alice"
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/_who"]


def test_unset_token_non_loopback_bind_refuses_admin(configure):
    c = configure(token=None, bind="0.0.0.0")
    for method, path in _non_public_routes():
        assert c.request(method, path, follow_redirects=False).status_code == 503, (method, path)
    # …and an arbitrary header value can't open it.
    assert c.get("/admin/", headers={HDR: ""}).status_code == 503
    # Public paths still work.
    assert c.get("/robots.txt").status_code == 200


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_unset_token_loopback_bind_stays_open(configure, bind):
    assert configure(token=None, bind=bind).get("/admin/").status_code not in (401, 503)


def test_bind_is_loopback():
    assert bind_is_loopback("127.0.0.1") and bind_is_loopback("::1")
    assert not bind_is_loopback("0.0.0.0") and not bind_is_loopback("192.168.1.5")
    assert not bind_is_loopback("")
