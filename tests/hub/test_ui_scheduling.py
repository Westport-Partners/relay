"""Tests for the Hub UI scheduling endpoints (availability/auto-schedule/contacts scheduling)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


class _FakeScheduleStore:
    def __init__(self):
        self.avail: dict[str, dict[str, Any]] = {}
        self.sched: dict[str, dict[str, Any]] = {}

    def list_availability(self):
        return list(self.avail.values())

    def get_availability(self, cid):
        return self.avail.get(cid)

    def put_availability(self, cid, data):
        d = dict(data)
        d["contact_id"] = cid
        self.avail[cid] = d

    def get_schedule(self, ws):
        return self.sched.get(ws)

    def put_schedule(self, ws, data):
        self.sched[ws] = data


def _client_sched(monkeypatch, store=None):
    app_obj = HubApp.__new__(HubApp)
    app_obj._schedule_store = store if store is not None else _FakeScheduleStore()
    app_obj._settings_store = None
    app_obj._incident_store = None
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


_ALL = {d: ["night", "day", "evening"]
        for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


def test_put_availability_requires_auth(monkeypatch):
    c = _client_sched(monkeypatch)
    assert c.put("/availability/cnt-a",
                 json={"available": True, "slots": _ALL, "ooo": None}).status_code == 403


def test_availability_roundtrip_and_auto_schedule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    for cid in ("cnt-a", "cnt-b"):
        assert c.put(f"/availability/{cid}",
                     json={"available": True, "slots": _ALL, "ooo": None}).status_code == 200
    assert len(c.get("/availability").json()) == 2
    r = c.post("/schedule/auto?week=2026-06-22")
    assert r.status_code == 200
    body = r.json()
    # 21 (day,shift) x 3 roles = 63 role-slots; two people eligible for
    # primary+secondary (default), nobody for manager => all manager slots gap.
    assert body["coverage"] == [42, 63]
    assert body["gaps"] == 21
    assert body["coverage_by_role"]["primary"] == [21, 21]
    assert body["coverage_by_role"]["secondary"] == [21, 21]
    assert body["coverage_by_role"]["manager"] == [0, 21]
    # primary+secondary balanced across the two people: 42 / 2 = 21 each
    assert sorted(body["counts"].values()) == [21, 21]
    # stored + retrievable
    g = c.get("/schedule?week=2026-06-22").json()
    assert len(g["slots"]) == 63


def test_put_availability_explicit_empty_roles_stays_empty(monkeypatch):
    """An explicit empty roles list means 'eligible for no roles' and must be
    honored (a contact can be created with none) — not defaulted."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-none",
              json={"available": False, "slots": {}, "ooo": None, "roles": []})
    assert r.status_code == 200
    assert store.avail["cnt-none"]["roles"] == []


def test_put_availability_omitted_roles_defaults(monkeypatch):
    """A MISSING roles key falls back to the primary+secondary default."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-def",
              json={"available": True, "slots": _ALL, "ooo": None})
    assert r.status_code == 200
    assert store.avail["cnt-def"]["roles"] == ["primary", "secondary"]


def test_put_availability_explicit_roles_filtered_to_valid(monkeypatch):
    """An explicit list keeps only valid roles (invalid values dropped)."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-mgr",
              json={"available": True, "slots": _ALL, "ooo": None,
                    "roles": ["manager", "bogus"]})
    assert r.status_code == 200
    assert store.avail["cnt-mgr"]["roles"] == ["manager"]


def test_auto_schedule_requires_auth():
    c = _client_sched(None)
    assert c.post("/schedule/auto?week=2026-06-22").status_code == 403


def test_auto_schedule_with_no_availability_is_all_gaps(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_sched(monkeypatch)
    body = c.post("/schedule/auto?week=2026-06-22").json()
    assert body["coverage"] == [0, 63]
    assert body["gaps"] == 63


def test_get_schedule_read_open():
    c = _client_sched(None)
    assert c.get("/schedule?week=2026-06-22").status_code == 200
