"""Story identity for tracked-story URLs.

The same story shows up under several spellings: what the user pasted, what FanFicFare
canonicalises it to (and writes into the EPUB / the Calibre `url:` identifier), and what a feed
links to. Exact-string comparison between those silently breaks duplicate detection and library
lookups, so identity is compared with `story_key`, and URLs are tidied on the way in with
`normalize_story_url`.

`story_key` is mirrored in `fetcher/app.py:_story_key` — the fetcher is a separate image that
can't import the app package, so keep the two in step (tests/test_story_key.py asserts they agree).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_ROYALROAD_RE = re.compile(r"^https?://(?:www\.)?royalroad\.com/fiction/(\d+)", re.IGNORECASE)
# XenForo thread: the numeric id is the number after the last dot of the slug. Anything after the
# thread (`/page-3`, `/reader/`, `/threadmarks`, `/post-123`) is a view of the same story.
_XENFORO_RE = re.compile(r"^(https?://[^/?#]+/threads/[^/?#]*\.\d+)(?:[/?#].*)?$", re.IGNORECASE)


def story_key(url: str | None) -> str:
    """A site-stable identity for a story URL: `host|<numeric story id or normalised path>`.

    Drops `www.`/`m.`, scheme, trailing slash, slug/title suffixes (FFN `/s/123` ↔
    `/s/123/1/Title`), XenForo `/page-N`, and the query string. Empty for a blank URL.
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = parsed.path
    xf = re.search(r"/threads/[^/]*?\.(\d+)(?:/|$)", path)
    if xf:
        return f"{host}|{xf.group(1)}"
    num = re.search(r"\d{3,}", path)
    if num:
        return f"{host}|{num.group(0)}"
    return f"{host}|{path.rstrip('/').lower()}"


def normalize_story_url(url: str) -> str:
    """Tidy a pasted story URL to the form FanFicFare records as the Calibre `url:` identifier.

    Conservative on purpose — only sites whose canonical form is known are rewritten; everything
    else is returned stripped and fragment-free. Lookups no longer depend on this being exact
    (they go through `story_key`), it just keeps the stored `source_url` and the inferred feed URL
    stable regardless of which page of the story was pasted.
    """
    url = (url or "").strip()
    url = url.split("#", 1)[0]
    m = _ROYALROAD_RE.match(url)
    if m:
        return f"https://www.royalroad.com/fiction/{m.group(1)}"
    m = _XENFORO_RE.match(url)
    if m:
        return m.group(1) + "/"
    return url


_CHAPTER_ID_PATTERNS = (
    (re.compile(r"/chapter/(\d+)", re.IGNORECASE), "chapter"),            # Royal Road
    (re.compile(r"/chapters/(\d+)", re.IGNORECASE), "chapter"),           # AO3
    (re.compile(r"/post-(\d+)", re.IGNORECASE), "post"),                  # XenForo
)
_FFN_CHAPTER_RE = re.compile(r"^/s/(\d+)/(\d+)", re.IGNORECASE)


def chapter_key(url: str | None) -> str | None:
    """A chapter's identity that survives a renamed story/chapter slug.

    Royal Road renames the slug when an author retitles a story, so the raw chapter URLs of an old
    EPUB and the live one never match although the numeric chapter id does. Keyed on the site's
    chapter id where it has one (`host|chapter|<id>`, XenForo `post-<id>`, FFN story+chapter number),
    else the normalised URL (no scheme, `www.`/`m.`, query, fragment or trailing slash).
    """
    if not url or not url.strip():
        return None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = parsed.path
    ffn = _FFN_CHAPTER_RE.match(path)
    if ffn:
        return f"{host}|s|{ffn.group(1)}|{ffn.group(2)}"
    for pattern, kind in _CHAPTER_ID_PATTERNS:
        m = pattern.search(path) or (pattern.search("/" + parsed.fragment) if parsed.fragment else None)
        if m:
            return f"{host}|{kind}|{m.group(1)}"
    return f"{host}|{path.rstrip('/').lower()}"
