"""Shared security helpers: feed-secret checks, signed image URLs, admin authentication.

The **DB `config.feed_secret` is the source of truth** (it is what the admin page shows and
rotates); `BEACON_FEED_SECRET` only seeds it on first run (see `database._ensure_config`).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
from collections.abc import Sequence
from html import unescape
from urllib.parse import unquote

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Config


def current_feed_secret(session: Session) -> str:
    cfg = session.get(Config, 1)
    return cfg.feed_secret if cfg else settings.feed_secret


def secrets_equal(a: str | None, b: str | None) -> bool:
    """Constant-time string compare that tolerates None / non-ASCII input."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── signed in-EPUB image URLs ─────────────────────────────────────────────────

_SIG_LEN = 32  # hex chars of the HMAC-SHA256 kept in the URL (128 bits)


def image_signature(secret: str, calibre_id: int, path: str) -> str:
    msg = f"img:{calibre_id}:{path}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:_SIG_LEN]


def verify_image_signature(secret: str, calibre_id: int, path: str, sig: str | None) -> bool:
    return secrets_equal(image_signature(secret, calibre_id, path), sig)


def sign_image_urls(
    html: str, secret: str, base_url: str, alias_bases: Sequence[str] = (), out_base: str | None = None,
) -> str:
    """Append `?sig=` to every `{base_url}/img/{id}/{path}` URL in rendered chapter HTML.

    `alias_bases` are other origins that are really ours — the feedback origin, and origins a
    stored drop was materialised under before the base URL moved. Their `/img/` URLs are rewritten
    onto `base_url` (the one origin guaranteed to serve /img) and signed too. Hosts that are not
    listed are never touched: chapters legitimately embed third-party images whose path may look
    like `/img/123/x.jpg`.

    Signing happens at *render* time, not when a drop is materialised: stored `content_html`
    stays byte-stable, legacy (unsigned) drops keep working, and rotating the feed secret
    re-signs everything automatically.

    `out_base` is the origin written into the signed URLs (default `base_url`); `""` makes them
    root-relative, for pages that may be served on another of our origins (the /read page).
    """
    if not html or not secret:
        return html
    base = base_url.rstrip("/")
    origins = [base] + [a.rstrip("/") for a in alias_bases if a and a.rstrip("/") != base]
    out = base if out_base is None else out_base.rstrip("/")
    prefix = "(?:" + "|".join(re.escape(o) for o in origins) + ")/img/"
    pattern = re.compile(rf"{prefix}(\d+)/([^\"'\s<>,)?#]+)")

    def _sub(m: re.Match) -> str:
        raw = m.group(2)
        # The route verifies the percent-decoded, HTML-unescaped path.
        path = unquote(unescape(raw))
        return f"{out}/img/{m.group(1)}/{raw}?sig={image_signature(secret, int(m.group(1)), path)}"

    return pattern.sub(_sub, html)


# ── proxy-token gate (deny by default) ───────────────────────────────────────

logger = logging.getLogger(__name__)

PROXY_TOKEN_HEADER = "x-beacon-proxy-token"
REMOTE_USER_HEADER = "x-remote-user"

# Paths reachable without the proxy token — mirrors nginx-public.conf. Everything else
# (/admin, /admin/ongoing, any mutating POST, /docs, …) requires a valid token.
# There is no KOReader-sync route in this app; if one is added, list it here.
_PUBLIC_PREFIXES = ("/feed", "/read", "/fb", "/static", "/img")
_PUBLIC_EXACT = ("/robots.txt",)


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    prefixes = _PUBLIC_PREFIXES + (("/websub",) if settings.public_websub else ())
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def bind_is_loopback(host: str | None = None) -> bool:
    host = (host if host is not None else settings.bind_host).strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def gate_is_open() -> bool:
    """No token configured AND (bound to loopback only, or explicitly opted out) → nothing to
    protect: local development / a deployment whose only exposure is a trusted network."""
    return not settings.proxy_token and (bind_is_loopback() or settings.admin_open)


def log_startup_posture() -> None:
    if settings.proxy_token:
        logger.info("Proxy-token gate active: non-public routes require %s", PROXY_TOKEN_HEADER)
    elif settings.admin_open:
        logger.warning("BEACON_ADMIN_OPEN=true: admin has NO authentication. Only use this on a "
                       "network you trust (never expose the port publicly).")
    elif bind_is_loopback():
        logger.info("BEACON_PROXY_TOKEN unset, loopback bind (%s): admin open for local development",
                    settings.bind_host)
    else:
        logger.error(
            "BEACON_PROXY_TOKEN is unset and BEACON_BIND_HOST=%s is not loopback: admin and all "
            "non-public routes are REFUSED. Set BEACON_PROXY_TOKEN (same value as the proxy sends), "
            "or BEACON_ADMIN_OPEN=true for a trusted-network setup.",
            settings.bind_host,
        )


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _origin_ok(headers: dict[str, str]) -> bool:
    """CSRF guard for state-changing admin requests. The proxy token is added by the proxy to
    *every* request it forwards, so a cross-site form POST would otherwise pass the gate.
    Browsers always send `Sec-Fetch-Site` and/or `Origin` on such requests; non-browser clients
    (curl, scripts) send neither and are allowed."""
    site = headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is not None:
        from urllib.parse import urlsplit
        host = urlsplit(origin).netloc.lower()
        allowed = {urlsplit(settings.base_url).netloc.lower(), headers.get("host", "").lower(),
                   headers.get("x-forwarded-host", "").lower()}
        return host in allowed - {""}
    return True


class ProxyTokenMiddleware:
    """Pure-ASGI deny-by-default gate. `X-Remote-User` is exposed as `scope['state']['remote_user']`
    **only** when the proxy token verified — it is for display/audit, never the auth check."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token_ok = secrets_equal(headers.get(PROXY_TOKEN_HEADER), settings.proxy_token)
        path = scope["path"]
        if (scope["type"] == "http" and scope["method"] not in _SAFE_METHODS
                and not is_public_path(path) and not _origin_ok(headers)):
            return await _reject(scope, receive, send, 403, "Cross-site request refused")
        if token_ok:
            user = headers.get(REMOTE_USER_HEADER, "").strip()[:128]
            scope.setdefault("state", {})["remote_user"] = user or None
            if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS") \
                    and not is_public_path(path):
                logger.info("admin audit: user=%s %s %s", user or "-", scope["method"], path)
        elif not is_public_path(path) and not gate_is_open():
            if settings.proxy_token:
                status, detail = 401, "Missing or invalid proxy token"
            else:
                status, detail = 503, "BEACON_PROXY_TOKEN is not configured; refusing non-public routes"
            return await _reject(scope, receive, send, status, detail)
        return await self.app(scope, receive, send)


async def _reject(scope, receive, send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        return await send({"type": "websocket.close", "code": 1008})
    body = f'{{"detail":"{detail}"}}'.encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
    ]})
    await send({"type": "http.response.body", "body": body})
