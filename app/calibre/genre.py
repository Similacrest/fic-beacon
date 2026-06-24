"""Genre classification + channel matching.

Channel membership is driven by the Calibre custom column **#genre_manual** (a curated,
hierarchical genre like ``Fantasy.Rational`` or ``Classical.Ukrainian``). When a book has
no #genre_manual value, we *auto-classify* it into one of five buckets by keyword-grepping
its raw **#genre** tags (RoyalRoad/AO3 style). A channel declares a ``genre_match`` prefix;
a book lands in the first channel whose prefix matches one of the book's (manual or derived)
genres — otherwise it falls back to the General channel.
"""
from __future__ import annotations

# The five auto-classification buckets, spelled to match the user's #genre_manual values
# so derived genres prefix-match channels created with those names.
FANFICTION = "Fanfiction"
SCIFI = "Sci-Fi"
FANTASY = "Fantasy"
CLASSICAL = "Classical"
NONFICTION = "Non-fiction"

# Ordered (first hit wins) case-insensitive substring rules over the raw #genre tags.
# Order matters: e.g. "Historical Fantasy" / "Science Fiction" resolve to Fantasy / Sci-Fi
# before the broad "historical" / non-fiction "science" keywords are considered.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    (FANFICTION, ("fanfic", "fan fiction")),
    # Note: keep these specific (hyphenated/spaced) so they don't catch popular-science
    # ("sci-pop"), which is non-fiction and handled below.
    (SCIFI, (
        "sci-fi", "scifi", "sci fi", "science fiction", "space opera", "cyberpunk",
        "steampunk", "post apocalyptic", "post-apocalyptic", "dystopia",
        "artificial intelligence", "first contact", "alien", "time travel", "time loop",
    )),
    (FANTASY, (
        "fantasy", "magic", "litrpg", "gamelit", "cultivation", "xianxia", "wuxia",
        "isekai", "portal fantasy", "progression", "dungeon", "superhero", "super heroes",
        "supernatural", "mythos", "lovecraft",
    )),
    (CLASSICAL, ("classic",)),
    (NONFICTION, (
        "non-fiction", "nonfiction", "non fiction", "sci-pop", "sci pop", "popular science",
        "pop science", "self-improvement", "self improvement", "self-help", "self help",
        "productivity", "psychology", "philosophy", "programming", "computer programming",
        "physics", "ethics", "metaphysics", "existentialism", "educational", "essay",
        "biography", "biographical", "history", "historical", "politics", "reference",
        "science",
    )),
]


def classify_genre(genre_tags: list[str]) -> str | None:
    """Map raw #genre tags to one of the five buckets, or None if nothing matches."""
    haystack = " ".join(genre_tags).lower()
    for bucket, keywords in _RULES:
        if any(kw in haystack for kw in keywords):
            return bucket
    return None


def effective_genres(genres: list[str], genre_tags: list[str]) -> list[str]:
    """The genres used for channel matching: the manual #genre_manual values if any,
    else a single auto-classified bucket (or [] when even that fails)."""
    if genres:
        return genres
    bucket = classify_genre(genre_tags)
    return [bucket] if bucket else []


def _matches(genre: str, prefix: str) -> bool:
    """Hierarchical prefix match: 'Fantasy' matches 'Fantasy' and 'Fantasy.Rational'."""
    return genre == prefix or genre.startswith(prefix + ".")


def pick_channel_id(book_genres: list[str], channels, default_id: int) -> int:
    """Return the id of the first channel whose genre_match matches a book genre.

    ``channels`` is any iterable of objects with ``.id`` and ``.genre_match``. Channels are
    tried in their given order; the first prefix hit wins. Falls back to ``default_id``.
    """
    for genre in book_genres:
        for channel in channels:
            if channel.genre_match and _matches(genre, channel.genre_match):
                return channel.id
    return default_id
