"""Release schedules: whole-release budgets split across channels by weight, per-schedule cron
jobs, and the schedule admin routes."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models import Book, BookStatus, BudgetMode, Channel, Config, Schedule
from app.planner.planner import resolve_schedule, run_release_cycle, schedule_budget, split_budget
from tests.make_epub import make_epub
from tests.test_planner import _make_book, _mock_adapter, _set_release_budget


@pytest.fixture
def epub_path():
    """Five ~500-word chapters (2,500 words in all)."""
    path = make_epub(chapters=[(f"Chapter {i}", f"<p>{'word ' * 500}</p>") for i in range(1, 6)])
    yield Path(path)
    os.unlink(path)


def _channel(db, name, weight, slots=1):
    ch = Channel(name=name, slug=name.lower(), parallel_slots=slots, weight=weight)
    db.add(ch)
    db.flush()
    return ch


def _cycle(db, epub, schedule=None):
    db.commit()
    with patch("app.planner.planner.CalibreAdapter") as MockAdapter:
        MockAdapter.return_value = _mock_adapter(1, epub)
        return run_release_cycle(db, Path("/fake"), schedule)


def _chapters(drops, channel_id=None):
    """Whole chapters released (each is ~502 words, so budgets below are sized in multiples)."""
    return sum(d.chapter_end - d.chapter_start + 1 for d in drops
               if channel_id is None or d.channel_id == channel_id)


class TestSplitBudget:
    def test_proportional_to_weight(self):
        assert split_budget(10_000, {1: 3.0, 2: 1.0}) == {1: 7500.0, 2: 2500.0}

    def test_single_channel_takes_everything(self):
        assert split_budget(10_000, {7: 3.0}) == {7: 10_000.0}

    def test_no_channels(self):
        assert split_budget(10_000, {}) == {}

    def test_zero_weights_fall_back_to_even(self):
        assert split_budget(900, {1: 0.0, 2: 0.0, 3: 0.0}) == {1: 300.0, 2: 300.0, 3: 300.0}


class TestScheduleBudget:
    def test_minutes_use_wpm(self, in_memory_db):
        cfg = in_memory_db.get(Config, 1)
        cfg.wpm = 300
        sc = Schedule(name="m", cron="0 7 * * *", budget=10, budget_mode=BudgetMode.minutes)
        assert schedule_budget(sc, cfg) == 3000

    def test_resolve_prefers_given_then_first_enabled(self, in_memory_db):
        first = in_memory_db.query(Schedule).first()
        first.enabled = False
        second = Schedule(name="b", cron="0 8 * * *", budget=1, sort_order=5)
        in_memory_db.add(second)
        in_memory_db.flush()
        assert resolve_schedule(in_memory_db, None) is second          # first *enabled*
        assert resolve_schedule(in_memory_db, first.id) is first        # explicit id wins
        assert resolve_schedule(in_memory_db, first) is first


class TestReleaseSplit:
    # Chapters are ~502 words. The first N chapters of a release are deterministic while the
    # remaining budget covers a whole chapter (p = 1); only the boundary chapter is a dice roll.

    def test_channel_without_content_yields_its_share(self, in_memory_db, epub_path):
        """Two channels weighted 1:3 but only the light one has content → it gets the whole
        budget, not its 25% share (which would be ≤ 2 chapters)."""
        in_memory_db.query(Channel).filter(Channel.name == "General").update({"weight": 3})
        busy = _channel(in_memory_db, "Busy", weight=1)
        _make_book(in_memory_db, calibre_id=1, channel_id=busy.id)
        _set_release_budget(in_memory_db, 2100)

        assert _chapters(_cycle(in_memory_db, epub_path), busy.id) >= 4

    def test_budget_splits_by_weight_when_both_have_content(self, in_memory_db, epub_path):
        a = _channel(in_memory_db, "A", weight=1)
        b = _channel(in_memory_db, "B", weight=3)
        _make_book(in_memory_db, calibre_id=1, title="a", channel_id=a.id)
        _make_book(in_memory_db, calibre_id=2, title="b", channel_id=b.id)
        _set_release_budget(in_memory_db, 4400)          # A: 1100 · B: 3300

        drops = _cycle(in_memory_db, epub_path)

        assert _chapters(drops, a.id) in (2, 3)          # ≈1100 words
        assert _chapters(drops, b.id) == 5               # 3300 covers the whole 5-chapter EPUB

    def test_different_schedules_release_different_volumes(self, in_memory_db, epub_path):
        small = in_memory_db.query(Schedule).first()
        small.budget = 1004                              # exactly two chapters
        big = Schedule(name="weekend", cron="0 10 * * 6,0", budget=3000, sort_order=9)
        in_memory_db.add(big)
        book = _make_book(in_memory_db, calibre_id=1)

        assert _chapters(_cycle(in_memory_db, epub_path, small)) == 2
        book.cursor_chapter_index = 0                    # rewind for a like-for-like comparison
        in_memory_db.flush()
        assert _chapters(_cycle(in_memory_db, epub_path, big)) == 5

    def test_carried_credit_is_reclamped_to_this_releases_scale(self, in_memory_db, epub_path):
        """A big release's leftover credit must not inflate the next, smaller release: 20k of
        credit under a 1,004-word release is re-clamped to one base → 2,008 available → 4
        chapters. (Unclamped it would have drained the whole 5-chapter EPUB.)"""
        _make_book(in_memory_db, calibre_id=1)
        in_memory_db.query(Channel).order_by(Channel.id).first().budget_credit = 20_000
        _set_release_budget(in_memory_db, 1004)

        assert _chapters(_cycle(in_memory_db, epub_path)) == 4

    def test_disabled_schedule_can_still_be_run_manually(self, in_memory_db, epub_path):
        sc = in_memory_db.query(Schedule).first()
        sc.enabled = False
        sc.budget = 1004
        _make_book(in_memory_db, calibre_id=1)
        assert _chapters(_cycle(in_memory_db, epub_path, sc)) == 2


class TestSchedulerJobs:
    @pytest.fixture
    def sched(self, in_memory_db, monkeypatch):
        from app import scheduler

        @contextmanager
        def fake_session():
            yield in_memory_db

        # A paused, started scheduler behaves like the real one (job ids are de-duplicated) without
        # actually firing anything.
        from apscheduler.schedulers.background import BackgroundScheduler
        fake = BackgroundScheduler()
        fake.start(paused=True)
        monkeypatch.setattr(scheduler, "_scheduler", fake)
        monkeypatch.setattr(scheduler, "db_session", fake_session)
        yield scheduler
        fake.shutdown(wait=False)

    def test_one_job_per_enabled_schedule(self, sched, in_memory_db):
        in_memory_db.add(Schedule(name="wk", cron="0 10 * * 6,0", budget=20000, sort_order=2))
        in_memory_db.add(Schedule(name="off", cron="0 5 * * *", budget=1, sort_order=3, enabled=False))
        in_memory_db.flush()

        sched.reload_schedules()

        ids = {j.id for j in sched._release_jobs()}
        first = in_memory_db.query(Schedule).order_by(Schedule.id).first()
        wk = in_memory_db.query(Schedule).filter_by(name="wk").one()
        assert ids == {f"release_cycle:{first.id}", f"release_cycle:{wk.id}"}

    def test_reload_drops_removed_and_disabled_schedules(self, sched, in_memory_db):
        extra = Schedule(name="x", cron="0 5 * * *", budget=1, sort_order=2)
        in_memory_db.add(extra)
        in_memory_db.flush()
        sched.reload_schedules()
        assert len(sched._release_jobs()) == 2

        extra.enabled = False
        in_memory_db.flush()
        sched.reload_schedules()
        assert len(sched._release_jobs()) == 1

    def test_invalid_cron_is_skipped_not_fatal(self, sched, in_memory_db):
        in_memory_db.add(Schedule(name="bad", cron="not a cron", budget=1, sort_order=2))
        in_memory_db.flush()
        sched.reload_schedules()
        assert len(sched._release_jobs()) == 1           # the good Default still scheduled

    def test_validate_cron(self, sched):
        assert sched.validate_cron("0 7 * * 1-5") is None
        assert sched.validate_cron("0 7 * *") is not None
        assert sched.validate_cron("banana") is not None


class TestScheduleRoutes:
    @pytest.fixture(autouse=True)
    def _no_reload(self):
        with patch("app.scheduler.reload_schedules") as reload:
            self.reload = reload
            yield

    def test_create_valid(self, in_memory_db):
        from app.routers.admin import create_schedule
        resp = create_schedule(name="Weekend", cron="0 10 * * 6,0", budget=20000,
                               budget_mode="words", db=in_memory_db)
        assert resp.headers["location"] == "/admin/schedules"
        sc = in_memory_db.query(Schedule).filter_by(name="Weekend").one()
        assert sc.budget == 20000 and sc.cron == "0 10 * * 6,0" and sc.enabled
        self.reload.assert_called_once()

    def test_create_rejects_bad_cron_without_saving(self, in_memory_db):
        from app.routers.admin import create_schedule
        resp = create_schedule(name="Bad", cron="every day", budget=1, budget_mode="words",
                               db=in_memory_db)
        assert "error=" in resp.headers["location"]
        assert in_memory_db.query(Schedule).filter_by(name="Bad").count() == 0
        self.reload.assert_not_called()

    def test_edit_updates_and_unchecked_enabled_disables(self, in_memory_db):
        from app.routers.admin import edit_schedule
        sc = in_memory_db.query(Schedule).first()
        edit_schedule(sc.id, name="Renamed", cron="0 6 * * *", budget=10, budget_mode="minutes",
                      enabled=None, db=in_memory_db)
        assert (sc.name, sc.cron, sc.budget, sc.budget_mode, sc.enabled) == \
            ("Renamed", "0 6 * * *", 10, BudgetMode.minutes, False)

    def test_last_schedule_cannot_be_deleted(self, in_memory_db):
        from app.routers.admin import delete_schedule
        sc = in_memory_db.query(Schedule).one()
        delete_schedule(sc.id, db=in_memory_db)
        assert in_memory_db.query(Schedule).count() == 1
        self.reload.assert_not_called()

    def test_delete_when_another_exists(self, in_memory_db):
        from app.routers.admin import delete_schedule
        other = Schedule(name="o", cron="0 5 * * *", budget=1, sort_order=2)
        in_memory_db.add(other)
        in_memory_db.flush()
        delete_schedule(other.id, db=in_memory_db)
        assert in_memory_db.query(Schedule).count() == 1
        self.reload.assert_called_once()
