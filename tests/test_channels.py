"""Channel create/edit — slug generation, editing, and uniqueness.

Endpoints are called directly, so the FastAPI `Form(...)` defaults must be passed explicitly.
"""
from app.models import Channel
from app.routers.admin import create_channel, edit_channel


def _create(db, name):
    create_channel(name=name, genre_match="", parallel_slots=2,
                   budget_mode="words", budget=5000, db=db)
    return db.query(Channel).filter(Channel.name == name).order_by(Channel.id.desc()).first()


def _edit(db, channel_id, name, slug):
    edit_channel(channel_id, name=name, slug=slug, genre_match="", parallel_slots=1,
                 budget_mode="words", budget=5000, db=db)


class TestCreateChannel:
    def test_slugifies_name(self, in_memory_db):
        ch = _create(in_memory_db, "Sci-Fi & Fantasy")
        assert ch.slug == "sci-fi-fantasy"

    def test_collision_gets_suffix(self, in_memory_db):
        _create(in_memory_db, "Fantasy")
        _create(in_memory_db, "Fantasy")
        slugs = sorted(c.slug for c in in_memory_db.query(Channel).filter(Channel.name == "Fantasy"))
        assert slugs == ["fantasy", "fantasy-2"]


class TestEditChannel:
    def test_edits_slug(self, in_memory_db):
        ch = _create(in_memory_db, "Fantasy")
        _edit(in_memory_db, ch.id, name="Fantasy", slug="epic-reads")
        assert in_memory_db.get(Channel, ch.id).slug == "epic-reads"

    def test_blank_slug_keeps_current(self, in_memory_db):
        ch = _create(in_memory_db, "Fantasy")
        _edit(in_memory_db, ch.id, name="Fantasy Renamed", slug="")
        ch = in_memory_db.get(Channel, ch.id)
        assert ch.name == "Fantasy Renamed"
        assert ch.slug == "fantasy"  # unchanged

    def test_slug_collision_with_other_channel_gets_suffix(self, in_memory_db):
        # General already exists (seeded). Try to take its slug.
        ch = _create(in_memory_db, "Fantasy")
        _edit(in_memory_db, ch.id, name="Fantasy", slug="general")
        assert in_memory_db.get(Channel, ch.id).slug == "general-2"

    def test_keeping_own_slug_is_allowed(self, in_memory_db):
        ch = _create(in_memory_db, "Fantasy")
        _edit(in_memory_db, ch.id, name="Fantasy", slug="fantasy")  # unchanged
        assert in_memory_db.get(Channel, ch.id).slug == "fantasy"  # not fantasy-2
