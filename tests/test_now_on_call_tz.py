"""Tests for schedule-backed 'who's on call now', incl. timezone handling.

The schedule is authored in the team's local wall-clock; resolution must
convert UTC -> RELAY_TZ before picking the date + shift. (Regression: at
22:15 US/Eastern the UTC hour is ~02:15 next day, which wrongly resolved to
the NIGHT shift of the wrong day.)
"""

from __future__ import annotations

from datetime import UTC, datetime

from relay.hub.app import _resolve_now_on_call


class _FakeScheduleStore:
    def __init__(self, schedules: dict):
        self._schedules = schedules

    def get_schedule(self, week_start: str):
        return self._schedules.get(week_start)


# Week of Mon 2026-06-15 .. Sun 2026-06-21. Friday is 2026-06-19.
# Fri night(00-08)=bchen, day(08-16)=arlowe, evening(16-24)=okafor.
_FRIDAY_SCHEDULE = {
    "2026-06-15": {
        "week_start": "2026-06-15",
        "slots": [
            {"date": "2026-06-19", "shift": "night", "contact_id": "bchen"},
            {"date": "2026-06-19", "shift": "day", "contact_id": "arlowe"},
            {"date": "2026-06-19", "shift": "evening", "contact_id": "okafor"},
        ],
    }
}

_NAMES = {"bchen": "Bao Chen", "arlowe": "Avery Lowe", "okafor": "Daniel Okafor"}


def test_eastern_evening_resolves_to_evening_shift(monkeypatch):
    monkeypatch.setenv("RELAY_TZ", "America/New_York")
    store = _FakeScheduleStore(_FRIDAY_SCHEDULE)
    # 2026-06-19 22:15 US/Eastern == 2026-06-20 02:15 UTC.
    now_utc = datetime(2026, 6, 20, 2, 15, tzinfo=UTC)
    res = _resolve_now_on_call(store, now_utc, _NAMES)
    assert res is not None
    assert res["shift"] == "evening"
    assert res["contact_id"] == "okafor"


def test_utc_default_uses_utc_hour(monkeypatch):
    monkeypatch.delenv("RELAY_TZ", raising=False)
    store = _FakeScheduleStore(_FRIDAY_SCHEDULE)
    # Same instant, but with no RELAY_TZ -> UTC -> 02:15 Sat -> no slot Sat.
    now_utc = datetime(2026, 6, 20, 2, 15, tzinfo=UTC)
    res = _resolve_now_on_call(store, now_utc, _NAMES)
    assert res is None  # Saturday has no slots in this schedule


def test_eastern_morning_resolves_to_day_shift(monkeypatch):
    monkeypatch.setenv("RELAY_TZ", "America/New_York")
    store = _FakeScheduleStore(_FRIDAY_SCHEDULE)
    # 2026-06-19 10:00 US/Eastern == 2026-06-19 14:00 UTC.
    now_utc = datetime(2026, 6, 19, 14, 0, tzinfo=UTC)
    res = _resolve_now_on_call(store, now_utc, _NAMES)
    assert res is not None
    assert res["shift"] == "day"
    assert res["contact_id"] == "arlowe"


def test_invalid_tz_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv("RELAY_TZ", "Not/AZone")
    store = _FakeScheduleStore(_FRIDAY_SCHEDULE)
    # 2026-06-19 10:00 UTC -> day shift directly.
    now_utc = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
    res = _resolve_now_on_call(store, now_utc, _NAMES)
    assert res is not None
    assert res["shift"] == "day"
