"""Tests for scheduler timing logic in app/services/scheduler.py"""
from datetime import datetime, timedelta

import pytest

from app.services.scheduler import _should_run


# ---------------------------------------------------------------------------
# _should_run(last_run_str, time_str)
# Returns True if the scheduled time has passed today and hasn't run since
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_should_run_never_run_before(monkeypatch):
    # Scheduled for 00:00; now is 12:00; never run
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    assert _should_run("", "00:00") is True


def test_should_run_already_ran_today(monkeypatch):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    ran_at = now.replace(hour=4, minute=5)  # ran after scheduled time
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    assert _should_run(_iso(ran_at), "04:00") is False


def test_should_run_scheduled_time_not_yet_reached(monkeypatch):
    now = datetime.now().replace(hour=3, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    assert _should_run("", "04:00") is False


def test_should_run_ran_before_todays_scheduled_time(monkeypatch):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    # Task ran yesterday before the scheduled hour
    ran_at = (now - timedelta(days=1)).replace(hour=2, minute=0)
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    assert _should_run(_iso(ran_at), "04:00") is True


def test_should_run_invalid_time_string_defaults_to_0400(monkeypatch):
    now = datetime.now().replace(hour=5, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    # Bad time string → defaults to 04:00 → 05:00 > 04:00 → should run
    assert _should_run("", "not-a-time") is True


def test_should_run_invalid_last_run_treated_as_never(monkeypatch):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("app.services.scheduler.datetime", type("MockDT", (), {
        "now": staticmethod(lambda: now),
        "fromisoformat": datetime.fromisoformat,
        "utcnow": datetime.utcnow,
    }))
    assert _should_run("garbage-value", "04:00") is True
