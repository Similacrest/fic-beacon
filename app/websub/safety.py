"""WebSub callback safety: SSRF guards and topic/token validation.

Subscribers are anonymous third parties, so every outbound request to a `hub.callback` must
(1) be https, (2) target a host that resolves **only** to public addresses, (3) connect to the
address we validated (no DNS-rebinding window), (4) never follow redirects, and (5) not read
unbounded responses. Topics must carry the feed secret, so the hub can't be used to read feeds
without it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import WebSubSubscription
from app.security import current_feed_secret, secrets_equal

MAX_VERIFY_BYTES = 64 * 1024
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeCallback(ValueError):
    """The callback URL is not an acceptable delivery target."""


def ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_public(ip.ipv4_mapped)
        if ip in _NAT64:
            return ip_is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None:
            return ip_is_public(ip.sixtofour)
    if ip in _CGNAT:
        return False
    return ip.is_global and not ip.is_multicast


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in settings.websub_callback_hosts.split(",") if h.strip()}


def parse_callback(url: str) -> httpx.URL:
    """Syntactic checks (no DNS): https only, a host, no embedded credentials."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise UnsafeCallback(f"invalid callback URL: {exc}") from exc
    if parsed.scheme != "https":
        raise UnsafeCallback("callback must be https://")
    host = parsed.host
    if not host:
        raise UnsafeCallback("callback has no host")
    if parsed.userinfo:
        raise UnsafeCallback("callback must not embed credentials")
    allowed = _allowed_hosts()
    if allowed and host.lower() not in allowed:
        raise UnsafeCallback("callback host is not in the allowlist")
    return parsed


def _resolve(host: str, port: int) -> list[str]:
    """Blocking DNS lookup → every address the host maps to (patched in tests)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


def public_addresses(url: httpx.URL) -> list[str]:
    """Resolve `url`'s host and return its addresses, raising if ANY is non-public."""
    host = url.host
    try:
        addrs = _resolve(host, url.port or 443)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnsafeCallback(f"cannot resolve callback host: {exc}") from exc
    if not addrs:
        raise UnsafeCallback("callback host did not resolve")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeCallback(f"unparseable address {addr!r}") from exc
        if not ip_is_public(ip):
            raise UnsafeCallback("callback resolves to a non-public address")
    return addrs


async def apublic_addresses(url: httpx.URL) -> list[str]:
    return await asyncio.to_thread(public_addresses, url)


def pin(url: httpx.URL, ip: str) -> tuple[httpx.URL, dict[str, str], dict]:
    """(url-with-IP, headers, extensions) so the connection goes to the address we validated
    while Host/SNI/cert verification still use the original hostname."""
    host = url.host
    default_port = url.port is None
    host_header = host if default_port else f"{host}:{url.port}"
    pinned = url.copy_with(host=ip)
    return pinned, {"Host": host_header}, {"sni_hostname": host}


def validate_callback(url: str) -> None:
    """Fast no-network-I/O-beyond-DNS check used by the hub before accepting a request."""
    public_addresses(parse_callback(url))


# ── topics ────────────────────────────────────────────────────────────────────

_TOPIC_PATH = re.compile(r"^[^/?#]+/\d+$")


def topic_status(topic: str, secret: str) -> str:
    """'ok' | 'foreign' (not one of our slot feeds) | 'bad-token' (ours, token missing/wrong)."""
    own = f"{settings.base_url.rstrip('/')}/feed/"
    parts = urlsplit(topic)
    bare = topic.split("?", 1)[0].split("#", 1)[0]
    if not bare.startswith(own) or not _TOPIC_PATH.match(bare[len(own):]):
        return "foreign"
    tokens = parse_qs(parts.query).get("token", [])
    if len(tokens) != 1 or not secrets_equal(tokens[0], secret):
        return "bad-token"
    return "ok"


def prune_subscriptions(session: Session) -> int:
    """Delete subscriptions whose topic is not one of our currently-valid, tokened feed URLs:
    stale after a `BEACON_BASE_URL` change or secret rotation, or legacy token-free rows."""
    secret = current_feed_secret(session)
    removed = 0
    for sub in session.query(WebSubSubscription).all():
        if topic_status(sub.topic_url, secret) != "ok":
            session.delete(sub)
            removed += 1
    return removed
