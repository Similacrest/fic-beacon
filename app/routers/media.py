"""Serve images embedded in Calibre EPUBs (read-only).

Chapter HTML in feeds/reader pages carries relative <img> paths that live *inside* the
EPUB zip; nothing else serves those bytes, so readers resolve them against the beacon
origin and 404. The chapterizer rewrites each in-EPUB image to
"{base_url}/img/{calibre_id}/{epub-internal-path}" (see app/epub/chapterizer.py), and
this route streams the matching zip entry straight out of the library — never writing it.
"""
from __future__ import annotations

import mimetypes
import posixpath
import re
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.calibre.adapter import CalibreAdapter
from app.config import settings

router = APIRouter()

# The chapterizer emits image paths relative to the OPF (ebooklib's get_name()
# namespace), so we resolve them against the EPUB's OPF directory to hit the zip entry.
_OPF_RE = re.compile(rb'full-path=["\']([^"\']+)["\']')


def _opf_dir(zf: zipfile.ZipFile) -> str:
    """Return the directory holding the OPF (e.g. "OEBPS"), or "" if at the zip root."""
    try:
        m = _OPF_RE.search(zf.read("META-INF/container.xml"))
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""
    return posixpath.dirname(m.group(1).decode("utf-8", "replace")) if m else ""


@router.get("/img/{calibre_id}/{path:path}", include_in_schema=False)
def epub_image(calibre_id: int, path: str) -> Response:
    # Normalise and refuse anything that climbs out of the zip root.
    rel = posixpath.normpath(path).lstrip("/")
    if not rel or rel.startswith(".."):
        raise HTTPException(status_code=404)

    adapter = CalibreAdapter(settings.calibre_library_path)
    book = adapter.get_book(calibre_id)
    if book is None:
        raise HTTPException(status_code=404)
    epub_path = adapter.epub_path(book)
    if not epub_path.exists():
        raise HTTPException(status_code=404)

    try:
        with zipfile.ZipFile(epub_path) as zf:
            entry = posixpath.normpath(posixpath.join(_opf_dir(zf), rel)).lstrip("/")
            if entry.startswith(".."):
                raise HTTPException(status_code=404)
            try:
                data = zf.read(entry)
            except KeyError:
                raise HTTPException(status_code=404)
    except (zipfile.BadZipFile, OSError):
        raise HTTPException(status_code=404)

    media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    # Bytes are immutable for a given (book, path); let readers/proxies cache hard.
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )
