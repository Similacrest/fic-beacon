"""Reader page: stable permalink for each drop.

Served at /read/{slug} — used as the guid and fallback link in the feed,
especially for books without a source URL or when a reader clips long content.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Drop

router = APIRouter()


@router.get("/read/{slug}", response_class=HTMLResponse)
def reader_page(slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    drop = db.query(Drop).filter(Drop.reader_slug == slug).first()
    if drop is None:
        raise HTTPException(status_code=404)

    book = drop.book
    chapter_label = drop.chapter_titles or f"Chapter {drop.chapter_start + 1}"
    title = f"{book.title} — {chapter_label}"

    return HTMLResponse(_reader_html(title, book.author, drop.word_count, drop.content_html))


def _reader_html(title: str, author: str, word_count: int, content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      font-family: Georgia, serif;
      max-width: 700px;
      margin: 2em auto;
      padding: 1em;
      line-height: 1.7;
      font-size: 1.1em;
    }}
    h1 {{ font-size: 1.4em; }}
    .meta {{ color: #666; font-size: .9em; margin-bottom: 2em; }}
    .chapter {{ margin-bottom: 3em; }}
    hr {{ border: none; border-top: 1px solid #ddd; margin: 2em 0; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">{author} &middot; {word_count:,} words</p>
  {content}
</body>
</html>"""
