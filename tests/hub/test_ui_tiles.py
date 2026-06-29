"""Tests for the Hub UI tile detail / fleet tile drawer endpoints."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.core.model import Contact  # noqa: E402
from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402
from relay.hub.health import FleetTile, Liveness  # noqa: E402


def _incident(
    correlation_id: str = "c-123",
    state: IncidentState = IncidentState.TRIGGERED,
) -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="checkout-api",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        state=state,
        alarm_name="prod-checkout-5xx",
        environment="prod",
        deployment_id="dep-1",
        service_path=["Payments", "Checkout", "API", "checkout-api"],
        created_at=now,
        updated_at=now,
        timeline=[
            TimelineEvent(
                event_id="e1",
                incident_id="c-123",
                stream=Stream.TEAM,
                occurred_at=now,
                actor="relay",
                event_type="triggered",
                detail={"reason": "alarm ALARM"},
            )
        ],
    )


class _FakeIncidentStore:
    def __init__(self, incidents: list[Incident]) -> None:
        self._incidents = incidents

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        open_states = {
            IncidentState.TRIGGERED,
            IncidentState.ACKNOWLEDGED,
            IncidentState.ESCALATED,
        }
        incidents = [i for i in self._incidents if i.state in open_states]
        if account_id is None:
            return incidents
        return [i for i in incidents if i.account_id == account_id]

    def list_incidents(self) -> list[Incident]:
        return list(self._incidents)

    def get_incident(self, correlation_id: str) -> Incident | None:
        return next(
            (i for i in self._incidents if i.correlation_id == correlation_id), None
        )

    def put_incident(self, incident: Incident) -> None:
        self._incidents = [
            incident if i.correlation_id == incident.correlation_id else i
            for i in self._incidents
        ]
        if incident.correlation_id not in {i.correlation_id for i in self._incidents}:
            self._incidents.append(incident)


class _FakeContactStore:
    def __init__(self, contacts: list[Contact] | None = None) -> None:
        self._db = {c.contact_id: c for c in (contacts or [])}

    def list_contacts(self) -> list[Contact]:
        return list(self._db.values())

    def get_contact(self, cid: str) -> Contact | None:
        return self._db.get(cid)

    def put_contact(self, c: Contact) -> None:
        self._db[c.contact_id] = c

    def delete_contact(self, cid: str) -> None:
        self._db.pop(cid, None)


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


def _tile(**over):
    base = dict(
        account_id="123456789012",
        app_name="checkout-api",
        status="green",
        liveness=Liveness.LIVE,
        open_incidents=0,
        worst_severity=None,
        last_heartbeat_at=datetime.now(UTC),
        registered_at=datetime.now(UTC),
        environment="prod",
        deployment_id="dep-1",
        service_path=["Payments", "Checkout", "checkout-api"],
        org_path=[{"id": "dep-1", "name": "checkout-api", "level": "deployment", "parent": None}],
        metadata={"owner": "team-pay", "aws_tags": {"env": "prod"}},
        on_call={"source": "team_snapshot", "shift": "day",
                 "roles": {"primary": {"contact_id": "cnt-x", "name": "Carol"}}},
    )
    base.update(over)
    return FleetTile(**base)


def _client_tile(monkeypatch, tile, *, schedule_store=None, contacts=None):
    app_obj = HubApp.__new__(HubApp)
    app_obj._schedule_store = schedule_store
    app_obj._contact_store = _FakeContactStore(contacts or []) if contacts is not None else None
    app_obj._settings_store = None
    app_obj._incident_store = _FakeIncidentStore([])
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {tile.key: tile} if tile else {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


def _schedule_to_stored(sched):
    """Serialise a Schedule into the DynamoScheduleStore dict form."""
    return {
        "week_start": sched.week_start.isoformat(),
        "slots": [
            {"date": s.date.isoformat(), "shift": s.shift.value,
             "role": s.role.value, "contact_id": s.contact_id}
            for s in sched.slots
        ],
        "roles": [r.value for r in sched.roles],
    }


def test_tile_detail_serves_snapshot_when_no_schedule_store(monkeypatch):
    # Federated Hub: no local schedule → the pushed snapshot is returned as-is.
    c = _client_tile(monkeypatch, _tile(), schedule_store=None)
    r = c.get("/fleet/tile?account_id=123456789012&app_name=checkout-api")
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["owner"] == "team-pay"
    assert body["metadata"]["aws_tags"]["env"] == "prod"
    assert body["org_path"][0]["level"] == "deployment"
    assert body["on_call"]["source"] == "team_snapshot"
    assert body["on_call"]["roles"]["primary"]["name"] == "Carol"


def test_tile_detail_fills_oncall_live_on_team_hub(monkeypatch):
    # Team Hub: a real schedule covering "now" overrides the snapshot with a
    # live resolution. Build a schedule for this week so a slot exists now.
    from relay.core.scheduling import Availability, auto_schedule, monday_of

    now = datetime.now(UTC)
    ws = monday_of(now.date())
    everyone_all = Availability(
        contact_id="cnt-live",
        available=True,
        slots={d: ["night", "day", "evening"] for d in
               ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        ooo=None,
    )
    sched = auto_schedule(ws, [everyone_all])
    store = _FakeScheduleStore()
    store.put_schedule(ws.isoformat(), _schedule_to_stored(sched))

    c = _client_tile(
        monkeypatch, _tile(), schedule_store=store,
        contacts=[Contact(contact_id="cnt-live", name="Live Person", email="live@x.com")],
    )
    r = c.get("/fleet/tile?account_id=123456789012&app_name=checkout-api")
    assert r.status_code == 200
    oc = r.json()["on_call"]
    # Live resolution wins over the pushed snapshot.
    assert oc["source"] == "schedule"
    assert oc["roles"]["primary"]["name"] == "Live Person"


def test_tile_detail_404_for_unknown(monkeypatch):
    c = _client_tile(monkeypatch, _tile(), schedule_store=None)
    assert c.get("/fleet/tile?account_id=000&app_name=ghost").status_code == 404


def test_tile_detail_resolves_app_name_with_slash(monkeypatch):
    # An app_name derived from an ECS autoscaling alarm contains a "/", which a
    # path-segment route could never match. Query params represent it cleanly.
    slashed = _tile(account_id="652107191239", app_name="TargetTracking-service/relay")
    c = _client_tile(monkeypatch, slashed, schedule_store=None)
    r = c.get(
        "/fleet/tile",
        params={"account_id": "652107191239", "app_name": "TargetTracking-service/relay"},
    )
    assert r.status_code == 200
    assert r.json()["app_name"] == "TargetTracking-service/relay"
