"""Neutralise active content in chapter HTML.

Chapter HTML comes from third-party EPUBs / scraped stories and is served from our own origin
(`/read/{slug}`, and inside feeds). Strip anything that can execute script or navigate the page:
script/embedding tags, `on*` handlers, and `javascript:`/`vbscript:`/`data:` (non-image) URLs.
Presentation markup (images, tables, `<details>`, inline styles…) is kept. The `/read` and `/fb`
pages additionally send a restrictive CSP (`CSP_HEADERS`) as a second layer.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_DROP_TAGS = ("script", "iframe", "frame", "frameset", "object", "embed", "applet", "base",
              "link", "noscript", "template")
_UNWRAP_TAGS = ("form", "input", "button", "select", "textarea")
_URL_ATTRS = ("href", "src", "xlink:href", "action", "formaction", "poster", "data", "background")
_DROP_ATTRS = ("srcdoc", "formaction")
_WS = re.compile(r"[\x00-\x20]+")

# Applied to HTML pages that render story text or story-derived titles.
CSP_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src * data:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _bad_url(attr: str, value: str) -> bool:
    v = _WS.sub("", value).lower()
    if v.startswith(("javascript:", "vbscript:")):
        return True
    if v.startswith("data:"):
        return not (attr in ("src", "poster") and v.startswith("data:image/") and "svg" not in v[:20])
    return False


def sanitize_tree(root) -> None:
    """Sanitise a BeautifulSoup tree/tag in place."""
    for tag in root.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in root.find_all("meta"):
        if "refresh" in (tag.get("http-equiv") or "").lower():
            tag.decompose()
    for tag in root.find_all(_UNWRAP_TAGS):
        tag.unwrap()
    for tag in root.find_all(True):
        for attr in list(tag.attrs):
            name = attr.lower()
            value = tag.attrs[attr]
            if name.startswith("on") or name in _DROP_ATTRS:
                del tag.attrs[attr]
            elif name in _URL_ATTRS and isinstance(value, str) and _bad_url(name, value):
                del tag.attrs[attr]


def sanitize_html(html: str) -> str:
    """Sanitise an HTML fragment (already-stored drop content, for example)."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    sanitize_tree(soup)
    return str(soup)
