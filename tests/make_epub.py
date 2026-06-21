"""Utility to create minimal test EPUBs for chapterizer tests.

Usage:
    from tests.make_epub import make_epub, make_fff_epub

make_epub() returns a pathlib.Path to a temporary EPUB file (standard, EPUB3 nav TOC).
make_fff_epub() returns one structured like FanFicFare output (EPUB2 ncx, dc:source set).
"""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path


def make_epub(
    chapters: list[tuple[str, str]] | None = None,
    source_url: str | None = None,
    epub_version: int = 3,
) -> Path:
    """Create a minimal EPUB in a temp file and return its path.

    chapters: list of (title, html_body_content)
    """
    if chapters is None:
        chapters = [
            ("Chapter 1: The Beginning", "<p>It was a dark and stormy night.</p>" * 50),
            ("Chapter 2: Rising Action", "<p>Things got complicated quickly.</p>" * 60),
            ("Chapter 3: The Climax", "<p>Everything came to a head.</p>" * 55),
        ]

    tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp.close()

    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _container_xml())
        zf.writestr("OEBPS/content.opf", _opf(chapters, source_url, epub_version))

        chapter_items = []
        for i, chap in enumerate(chapters, 1):
            title, body = chap[0], chap[1]
            chapter_url = chap[2] if len(chap) > 2 else None
            name = f"chapter{i:03d}.xhtml"
            zf.writestr(f"OEBPS/{name}", _chapter_xhtml(title, body, chapter_url))
            chapter_items.append((name, title))

        if epub_version == 3:
            zf.writestr("OEBPS/nav.xhtml", _nav_xhtml(chapter_items))
        else:
            zf.writestr("OEBPS/toc.ncx", _toc_ncx(chapter_items))

    return Path(tmp.name)


# Alias for FanFicFare-style EPUB (EPUB2 + dc:source)
def make_fff_epub(
    chapters: list[tuple[str, str]] | None = None,
    source_url: str = "https://archiveofourown.org/works/12345",
) -> Path:
    return make_epub(chapters=chapters, source_url=source_url, epub_version=2)


def _container_xml() -> str:
    return """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def _opf(
    chapters: list[tuple[str, str]],
    source_url: str | None,
    epub_version: int,
) -> str:
    items = "\n    ".join(
        f'<item id="ch{i:03d}" href="chapter{i:03d}.xhtml" media-type="application/xhtml+xml"/>'
        for i, _ in enumerate(chapters, 1)
    )
    spine = "\n    ".join(
        f'<itemref idref="ch{i:03d}"/>' for i, _ in enumerate(chapters, 1)
    )
    source_tag = f"<dc:source>{source_url}</dc:source>" if source_url else ""

    if epub_version == 3:
        nav_item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        version_attr = 'version="3.0"'
        ncx_ref = ""
    else:
        nav_item = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        version_attr = 'version="2.0"'
        ncx_ref = 'toc="ncx"'

    return f"""<?xml version="1.0" encoding="utf-8"?>
<package {version_attr} xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:identifier id="uid">urn:uuid:test-1234</dc:identifier>
    {source_tag}
  </metadata>
  <manifest>
    {items}
    {nav_item}
  </manifest>
  <spine {ncx_ref}>
    {spine}
  </spine>
</package>"""


def _chapter_xhtml(title: str, body: str, chapter_url: str | None = None) -> str:
    meta = f'<meta name="chapterurl" content="{chapter_url}" />' if chapter_url else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title>{meta}</head>
<body>
  <h1>{title}</h1>
  {body}
</body>
</html>"""


def _nav_xhtml(items: list[tuple[str, str]]) -> str:
    lis = "\n      ".join(
        f'<li><a href="{name}">{title}</a></li>' for name, title in items
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Navigation</title></head>
<body>
  <nav epub:type="toc">
    <ol>
      {lis}
    </ol>
  </nav>
</body>
</html>"""


def _toc_ncx(items: list[tuple[str, str]]) -> str:
    navpoints = "\n  ".join(
        f"""<navPoint id="np{i}" playOrder="{i}">
    <navLabel><text>{title}</text></navLabel>
    <content src="{name}"/>
  </navPoint>"""
        for i, (name, title) in enumerate(items, 1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"
  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:test-1234"/>
  </head>
  <docTitle><text>Test Book</text></docTitle>
  <navMap>
  {navpoints}
  </navMap>
</ncx>"""
