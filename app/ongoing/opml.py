"""Parse OPML subscription lists into (title, feed_url) pairs."""
from __future__ import annotations

from xml.etree import ElementTree


def parse_opml(content: bytes) -> list[tuple[str, str]]:
    """Return [(title, xmlUrl)] for every RSS/Atom outline in the OPML document."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid OPML: {exc}") from exc

    results: list[tuple[str, str]] = []
    for outline in root.iter("outline"):
        url = outline.get("xmlUrl") or outline.get("xmlurl")
        if not url:
            continue
        title = (
            outline.get("title")
            or outline.get("text")
            or url
        )
        results.append((title.strip(), url.strip()))
    return results
