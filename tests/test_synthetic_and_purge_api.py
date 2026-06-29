"""Tests for POST /synthetic/incident and POST /admin/purge endpoints.

Mirrors test_hub_ui_endpoints.py patterns:
- HubApp.__new__ + manual attribute injection
- RELAY_AUTH_MODE=dev via monkeypatch for writer-gated routes
- Fake pipeline / fake incident store — no real AWS calls
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Records every event passed to handle_alarm and returns a canned dict."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handle_alarm(self, event: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(event)
        return {"correlation_id": "synth-corr-001", "status": "triggered"}


class _FakeIncidentStore:
    """Minimal store that also supports purge_incidents."""

    def __init__(self) -> None:
        self.purge_calls: list[dict[str, Any]] = []

    def purge_incidents(
        self,
        *,
        before: datetime | None = None,
        after: datetime | None = None,
        synthetic_only: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        call = {
            "before": before,
            "after": after,
            "synthetic_only": synthetic_only,
            "dry_run": dry_run,
        }
        self.purge_calls.append(call)
        matched = 3
        deleted = 0 if dry_run else matched
        return {
            "matched": matched,
            "deleted": deleted,
            "synthetic": 1,
            "dry_run": dry_run,
            "companions_deleted": 0,
        }


def _make_client(
    pipeline=None,
    incident_store=None,
) -> tuple[TestClient, _FakePipeline, _FakeIncidentStore]:
    """Build a minimal HubApp TestClient with injected fakes."""
    fake_pipeline = pipeline if pipeline is not None else _FakePipeline()
    fake_store = incident_store if incident_store is not None else _FakeIncidentStore()

    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = fake_pipeline
    app_obj._incident_store = fake_store
    app_obj._runtime = "fargate"

    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    return TestClient(app_obj.build_fastapi_app()), fake_pipeline, fake_store


# ---------------------------------------------------------------------------
# Auth fixture — clear env before every test so no cross-test pollution
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


# ---------------------------------------------------------------------------
# POST /synthetic/incident
# ---------------------------------------------------------------------------


def test_synthetic_incident_empty_body_returns_correlation_id(monkeypatch):
    """Empty payload should use defaults and return a correlation_id."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, pipeline, _ = _make_client()

    r = client.post("/synthetic/incident", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "correlation_id" in body
    assert body["correlation_id"] == "synth-corr-001"


def test_synthetic_incident_empty_body_relay_synthetic_true(monkeypatch):
    """Pipeline must receive an event with relay_synthetic=True at top level and in detail."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, pipeline, _ = _make_client()

    client.post("/synthetic/incident", json={})

    assert len(pipeline.calls) == 1
    event = pipeline.calls[0]
    assert event.get("relay_synthetic") is True
    assert event["detail"].get("relay_synthetic") is True


def test_synthetic_incident_event_envelope_shape(monkeypatch):
    """Event envelope must match the CloudWatchAlarmSource.parse_event expectations."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, pipeline, _ = _make_client()

    client.post("/synthetic/incident", json={})

    event = pipeline.calls[0]
    assert event["source"] == "aws.cloudwatch"
    assert event["detail-type"] == "CloudWatch Alarm State Change"
    assert event["detail"]["state"]["value"] == "ALARM"
    # alarmName defaults to synthetic-smoke-test
    assert event["detail"]["alarmName"] == "synthetic-smoke-test"


def test_synthetic_incident_with_overrides_flows_into_event(monkeypatch):
    """Custom severity and app_name must appear in the synthetic event."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, pipeline, _ = _make_client()

    overrides = {
        "app_name": "my-service",
        "severity": "SEV1",
        "alarm_name": "custom-alarm",
        "account_id": "111122223333",
        "region": "eu-west-1",
    }
    r = client.post("/synthetic/incident", json=overrides)
    assert r.status_code == 200, r.text

    event = pipeline.calls[0]
    assert event["account"] == "111122223333"
    assert event["region"] == "eu-west-1"
    assert event["detail"]["alarmName"] == "custom-alarm"
    # Namespace encodes severity
    namespace = (
        event["detail"]["configuration"]["metrics"][0]["metricStat"]["metric"]["namespace"]
    )
    assert "SEV1" in namespace
    # app_name in dimensions
    dims = event["detail"]["configuration"]["metrics"][0]["metricStat"]["metric"]["dimensions"]
    assert dims["app"] == "my-service"


def test_synthetic_incident_pipeline_none_returns_503(monkeypatch):
    """503 when pipeline is not wired."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, _ = _make_client(pipeline=None)
    # Override pipeline to None after client creation
    # (HubApp.__new__ path — re-build)
    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = None
    app_obj._incident_store = _FakeIncidentStore()
    app_obj._runtime = "fargate"
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    no_pipe_client = TestClient(app_obj.build_fastapi_app())

    r = no_pipe_client.post("/synthetic/incident", json={})
    assert r.status_code == 503


def test_synthetic_incident_requires_writer_auth():
    """Without auth (RELAY_AUTH_MODE=none), must return 403."""
    client, _, _ = _make_client()
    r = client.post("/synthetic/incident", json={})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /admin/purge
# ---------------------------------------------------------------------------


def test_purge_dry_run_no_bounds_is_allowed(monkeypatch):
    """dry_run=True with no bounds is a safe preview — must not be rejected."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, store = _make_client()

    r = client.post("/admin/purge", json={"dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["matched"] == 3
    assert body["deleted"] == 0  # dry_run → nothing deleted


def test_purge_no_bounds_not_dry_run_not_synthetic_only_rejected(monkeypatch):
    """Blanket purge without safety valve must return 400."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, _ = _make_client()

    r = client.post("/admin/purge", json={"dry_run": False, "synthetic_only": False})
    assert r.status_code == 400
    assert "refusing" in r.json()["detail"].lower()


def test_purge_with_before_bound_calls_store_with_parsed_datetime(monkeypatch):
    """ISO before string must be parsed to a datetime and forwarded to the store."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, store = _make_client()

    r = client.post(
        "/admin/purge",
        json={"before": "2026-01-15T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text

    assert len(store.purge_calls) == 1
    call = store.purge_calls[0]
    assert call["before"] is not None
    assert isinstance(call["before"], datetime)
    assert call["before"].year == 2026
    assert call["before"].month == 1
    assert call["before"].day == 15
    assert call["before"].tzinfo is not None


def test_purge_with_after_bound_calls_store(monkeypatch):
    """ISO after string must be forwarded to the store."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, store = _make_client()

    r = client.post(
        "/admin/purge",
        json={"after": "2025-06-01T12:00:00+00:00", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    call = store.purge_calls[0]
    assert call["after"] is not None
    assert call["after"].year == 2025


def test_purge_synthetic_only_no_bounds_allowed(monkeypatch):
    """synthetic_only=True without time bounds is safe — must not be rejected."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, store = _make_client()

    r = client.post("/admin/purge", json={"synthetic_only": True, "dry_run": False})
    assert r.status_code == 200, r.text
    call = store.purge_calls[0]
    assert call["synthetic_only"] is True
    assert call["dry_run"] is False


def test_purge_malformed_before_returns_422(monkeypatch):
    """Malformed ISO date string must return 422."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, _ = _make_client()

    r = client.post(
        "/admin/purge",
        json={"before": "not-a-date", "dry_run": True},
    )
    assert r.status_code == 422
    assert "before" in r.json()["detail"].lower()


def test_purge_malformed_after_returns_422(monkeypatch):
    """Malformed ISO date for 'after' must return 422."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, _ = _make_client()

    r = client.post(
        "/admin/purge",
        json={"after": "2026/06/01", "dry_run": True},
    )
    assert r.status_code == 422


def test_purge_store_none_returns_503(monkeypatch):
    """503 when incident_store is None."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = _FakePipeline()
    app_obj._incident_store = None
    app_obj._runtime = "fargate"
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    no_store_client = TestClient(app_obj.build_fastapi_app())

    r = no_store_client.post("/admin/purge", json={"dry_run": True})
    assert r.status_code == 503


def test_purge_store_without_purge_method_returns_503(monkeypatch):
    """503 when incident_store lacks purge_incidents method."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    class _StoreNoPurge:
        pass

    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = _FakePipeline()
    app_obj._incident_store = _StoreNoPurge()
    app_obj._runtime = "fargate"
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    no_purge_client = TestClient(app_obj.build_fastapi_app())

    r = no_purge_client.post("/admin/purge", json={"dry_run": True})
    assert r.status_code == 503


def test_purge_requires_writer_auth():
    """Without auth (RELAY_AUTH_MODE=none), must return 403."""
    client, _, _ = _make_client()
    r = client.post("/admin/purge", json={"dry_run": True})
    assert r.status_code == 403


def test_purge_recomputes_affected_fleet_tiles_and_publishes(monkeypatch):
    """After a real purge, the endpoint must recompute each affected fleet tile
    (from the surviving open incidents) and push an SSE delta so the big board
    clears live — not wait for a heartbeat or restart (issue #30)."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    class _StoreWithTiles:
        def purge_incidents(self, **kwargs):
            return {
                "matched": 2,
                "deleted": 2,
                "synthetic": 2,
                "dry_run": False,
                "companions_deleted": 0,
                "affected_tiles": [
                    {
                        "account_id": "123",
                        "app_name": "app1",
                        "environment": "prod",
                        "deployment_id": "dep-1",
                    }
                ],
            }

        def list_open_incidents(self, account_id=None):
            return []  # everything purged; tile should clear

    recompute_calls: list[tuple[Any, ...]] = []

    class _RecordingHubState:
        def recompute_tile(
            self, account_id, app_name, open_incidents, environment, deployment_id
        ):
            recompute_calls.append(
                (account_id, app_name, len(open_incidents), environment, deployment_id)
            )
            from relay.hub.health import FleetTile, Liveness

            return FleetTile(
                account_id=account_id,
                app_name=app_name,
                environment=environment,
                deployment_id=deployment_id,
                status="green",
                liveness=Liveness.LIVE,
                open_incidents=0,
                worst_severity=None,
                last_heartbeat_at=None,
                registered_at=datetime.now(UTC),
            )

    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = _FakePipeline()
    app_obj._incident_store = _StoreWithTiles()
    app_obj._runtime = "fargate"
    app_obj._hub_state = _RecordingHubState()
    sse = SSEPublisher()
    app_obj._sse_publisher = sse
    q = sse.subscribe()
    client = TestClient(app_obj.build_fastapi_app())

    r = client.post("/admin/purge", json={"synthetic_only": True, "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tiles_recomputed"] == 1
    assert recompute_calls == [("123", "app1", 0, "prod", "dep-1")]
    # An SSE delta was published for the cleared tile.
    msg = q.get_nowait()
    assert "event: delta" in msg


def test_purge_dry_run_does_not_recompute_tiles(monkeypatch):
    """A dry-run preview must not recompute or publish — nothing was deleted."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    class _StoreWithTiles:
        def purge_incidents(self, **kwargs):
            return {
                "matched": 2,
                "deleted": 0,
                "synthetic": 2,
                "dry_run": True,
                "companions_deleted": 0,
                "affected_tiles": [
                    {"account_id": "123", "app_name": "app1",
                     "environment": "prod", "deployment_id": "dep-1"}
                ],
            }

        def list_open_incidents(self, account_id=None):
            raise AssertionError("must not be called on dry_run")

    app_obj = HubApp.__new__(HubApp)
    app_obj._pipeline = _FakePipeline()
    app_obj._incident_store = _StoreWithTiles()
    app_obj._runtime = "fargate"
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    client = TestClient(app_obj.build_fastapi_app())

    r = client.post("/admin/purge", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert "tiles_recomputed" not in r.json()


def test_purge_naive_datetime_gets_utc_tzinfo(monkeypatch):
    """Naive ISO strings (no tz offset) must be attached UTC tzinfo."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    client, _, store = _make_client()

    r = client.post(
        "/admin/purge",
        json={"before": "2026-03-01T00:00:00", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    call = store.purge_calls[0]
    assert call["before"].tzinfo is not None
    assert call["before"].tzinfo == UTC
