"""Tests for #status classification (app/calibre/status.py)."""
from app.calibre.status import classify_status, is_done, is_updating


class TestClassifyStatus:
    def test_updating_values(self):
        for v in ("In-Progress", "in progress", "Incomplete", "Hiatus", "Ongoing", "Active"):
            assert classify_status(v) == "updating", v

    def test_done_values(self):
        for v in ("Completed", "Complete", "Abandoned", "Dropped", "Published"):
            assert classify_status(v) == "done", v

    def test_unknown_values(self):
        for v in (None, "", "—", "Whatever"):
            assert classify_status(v) == "unknown", v

    def test_case_and_whitespace_insensitive(self):
        assert classify_status("  COMPLETED  ") == "done"

    def test_helpers(self):
        assert is_updating("In-Progress") and not is_done("In-Progress")
        assert is_done("Completed") and not is_updating("Completed")
