"""⏸ Pause releases / ⏭ Skip next release, and the time-ordered System status."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import scheduler
from app.models import Schedule
from app.state import LAST_RUN_RESULT, RELEASES_PAUSED, SKIP_NEXT_RELEASE, get_value


@pytest.fixture
def sched(in_memory_db, monkeypatch):
    """The scheduler module over the test DB, with a started-but-paused APScheduler (real job ids
    and next fire times, nothing actually fires) and the cycle's real work stubbed out."""
    @contextmanager
    def fake_session():
        yield in_memory_db

    from apscheduler.schedulers.background import BackgroundScheduler
    fake = BackgroundScheduler()
    fake.start(paused=True)
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    monkeypatch.setattr(scheduler, "db_session", fake_session)
    monkeypatch.setattr(scheduler, "_active_op", None)
    monkeypatch.setattr(scheduler, "_check_feeds_for_cycle", lambda s: (2, 1))
    scheduler.reload_schedules()
    yield scheduler
    fake.shutdown(wait=False)


def _default(db) -> Schedule:
    return db.query(Schedule).order_by(Schedule.id).first()


def _fire(sched, db, schedule_id, at):
    """Fire the cron entry point as if APScheduler ran it at `at`; return what it recorded."""
    with patch.object(sched, "_now", lambda: at), \
         patch.object(sched, "_run_release_cycle", return_value={"chapters": 3}) as run:
        sched._scheduled_release_cycle(schedule_id)
    return run, json.loads(get_value(db, f"{LAST_RUN_RESULT}:release"))


class TestSkipNextRelease:
    def test_skip_holds_exactly_the_next_firing(self, sched, in_memory_db):
        sc = _default(in_memory_db)
        target = sched.skip_next_release(in_memory_db)
        assert target["schedule_id"] == sc.id
        assert target["at"] == sched.next_release_times()[sc.id]

        run, rec = _fire(sched, in_memory_db, sc.id, target["at"])
        assert not run.called                                  # nothing released…
        assert rec["held"] == "skipped" and rec["chapters"] == 0
        assert rec["feeds_changed"] == 2 and rec["downloads"] == 1  # …but feeds were checked
        assert get_value(in_memory_db, SKIP_NEXT_RELEASE) is None   # used up

        run, rec = _fire(sched, in_memory_db, sc.id, target["at"] + timedelta(days=1))
        assert run.called and "held" not in rec                # the one after runs as usual

    def test_other_schedules_firing_first_are_not_skipped(self, sched, in_memory_db):
        other = Schedule(name="Other", cron="0 5 * * *", budget=100, sort_order=9)
        in_memory_db.add(other)
        in_memory_db.flush()
        sched.reload_schedules()
        target = sched.skip_next_release(in_memory_db)
        not_target = other.id if target["schedule_id"] != other.id else _default(in_memory_db).id
        run, _ = _fire(sched, in_memory_db, not_target, target["at"] - timedelta(hours=1))
        assert run.called
        assert get_value(in_memory_db, SKIP_NEXT_RELEASE) is not None  # still waiting for its firing

    def test_stale_skip_is_void_and_cleared(self, sched, in_memory_db):
        sc = _default(in_memory_db)
        target = sched.skip_next_release(in_memory_db)
        later = target["at"] + timedelta(hours=3)                   # app was down at the target
        with patch.object(sched, "_now", lambda: later):
            assert sched.skip_target(in_memory_db) is None
        run, _ = _fire(sched, in_memory_db, sc.id, later)
        assert run.called
        assert get_value(in_memory_db, SKIP_NEXT_RELEASE) is None

    def test_cancel(self, sched, in_memory_db):
        sched.skip_next_release(in_memory_db)
        sched.cancel_skip(in_memory_db)
        in_memory_db.flush()
        assert sched.skip_target(in_memory_db) is None

    def test_nothing_scheduled_sets_nothing(self, sched, in_memory_db):
        _default(in_memory_db).enabled = False
        in_memory_db.flush()
        sched.reload_schedules()
        assert sched.skip_next_release(in_memory_db) is None
        assert get_value(in_memory_db, SKIP_NEXT_RELEASE) is None

    def test_following_fire_time(self, sched, in_memory_db):
        sc = _default(in_memory_db)
        nxt = sched.next_release_times()[sc.id]
        after = sched.fire_time_after(sc.id, nxt)
        assert after is not None and after > nxt


class TestPauseReleases:
    def test_paused_cron_checks_feeds_only_until_resumed(self, sched, in_memory_db):
        sc = _default(in_memory_db)
        sched.pause_releases(in_memory_db)
        since = sched.releases_paused_since(in_memory_db)
        sched.pause_releases(in_memory_db)                          # idempotent: keeps the first time
        assert sched.releases_paused_since(in_memory_db) == since

        now = datetime.now(timezone.utc)
        run, rec = _fire(sched, in_memory_db, sc.id, now)
        assert not run.called and rec["held"] == "paused" and rec["feeds_changed"] == 0  # no fetching

        sched.resume_releases(in_memory_db)
        in_memory_db.flush()
        assert get_value(in_memory_db, RELEASES_PAUSED) is None
        run, rec = _fire(sched, in_memory_db, sc.id, now)
        assert run.called

    def test_pausing_drops_a_pending_skip(self, sched, in_memory_db):
        sched.skip_next_release(in_memory_db)
        sched.pause_releases(in_memory_db)
        in_memory_db.flush()
        assert get_value(in_memory_db, SKIP_NEXT_RELEASE) is None

    def test_manual_run_is_never_held(self, sched, in_memory_db):
        sched.pause_releases(in_memory_db)
        with patch.object(sched, "_run_release_cycle", return_value={"chapters": 3}) as run:
            sched._manual_release_cycle(_default(in_memory_db).id)
        assert run.called


@pytest.fixture
def ctx(monkeypatch):
    """(client, db) for the admin routes over a StaticPool in-memory DB."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.config import settings
    from app.database import ensure_default_channel, ensure_default_schedule, get_db
    from app.main import app
    from app.models import Base, Config
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Config(id=1, wpm=250, feed_secret="test-secret"))
    db.flush()
    ensure_default_channel(db)
    ensure_default_schedule(db)
    db.commit()
    monkeypatch.setattr(settings, "admin_open", True)
    monkeypatch.setattr(settings, "proxy_token", None)
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app), db
    app.dependency_overrides.pop(get_db, None)
    db.close()
    engine.dispose()


class TestHoldRoutes:
    def test_pause_and_resume_from_the_dashboard(self, ctx):
        client, db = ctx
        assert "⏸ Pause releases" in client.get("/admin/").text
        r = client.post("/admin/releases/pause", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/"
        assert get_value(db, RELEASES_PAUSED) is not None
        text = client.get("/admin/").text
        assert "▶ Resume releases" in text and "Scheduled releases are paused" in text
        client.post("/admin/releases/resume")
        assert get_value(db, RELEASES_PAUSED) is None

    def test_skip_and_undo_from_the_dashboard(self, ctx):
        client, db = ctx
        target = {"schedule_id": 1, "at": datetime.now(timezone.utc) + timedelta(hours=2)}
        with patch("app.scheduler.skip_next_release", return_value=target) as skip:
            client.post("/admin/releases/skip")
        assert skip.called
        client.post("/admin/releases/unskip")
        assert get_value(db, SKIP_NEXT_RELEASE) is None

    def test_unknown_op_404(self, ctx):
        client, _ = ctx
        assert client.post("/admin/releases/explode").status_code == 404

    def test_held_run_is_not_shown_as_the_last_release(self, ctx):
        from app.state import set_value
        client, db = ctx
        set_value(db, f"{LAST_RUN_RESULT}:release", json.dumps({
            "op": "release", "at": datetime.now(timezone.utc).isoformat(), "seconds": 1, "error": None,
            "schedule": "Default", "held": "skipped", "chapters": 0, "words": 0}))
        db.commit()
        text = " ".join(client.get("/admin/").text.split())
        assert "Last run: Default — skipped, nothing released" in text
        assert "Last release <strong>never</strong>" in text


class TestStatusRowsInTimeOrder:
    def test_sorted_past_first_then_soonest_untimed_last(self):
        from app.routers.admin import _build_status_rows
        now = datetime.now(timezone.utc)
        rows = [
            {"schedule": Schedule(name="Evening", enabled=True), "next": now + timedelta(hours=9),
             "skipped": False, "then": None},
            {"schedule": Schedule(name="Off", enabled=False), "next": None, "skipped": False, "then": None},
            {"schedule": Schedule(name="Morning", enabled=True), "next": now + timedelta(hours=1),
             "skipped": True, "then": now + timedelta(days=1)},
        ]
        out = _build_status_rows(rows, last_poll=now - timedelta(hours=2),
                                 next_sweep=now + timedelta(hours=5), paused_since=None)
        assert [r["label"] for r in out] == [
            "Last feed check", "Next: Morning", "Next feedless sweep", "Next: Evening", "Next: Off"]
        assert out[1]["note"] == "skipped" and out[-1]["note"] == "disabled"

    def test_paused_note(self):
        from app.routers.admin import _build_status_rows
        now = datetime.now(timezone.utc)
        rows = [{"schedule": Schedule(name="M", enabled=True), "next": now, "skipped": False, "then": None}]
        out = _build_status_rows(rows, None, None, paused_since=now)
        assert out[0]["note"].startswith("paused")
        assert out[1]["label"] == "Last feed check" and out[1]["note"] == "never"


def test_feedless_sweep_skipped_while_paused(sched, in_memory_db):
    sched.pause_releases(in_memory_db)
    with patch("app.ongoing.poller.sweep_feedless") as sweep:
        sched._run_feedless_sweep()
    assert not sweep.called
