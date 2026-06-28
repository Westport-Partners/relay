"""Tests for the Relay Hub fleet big-board dashboard.

Covers:
  1. Liveness derivation (live / stale / lost / unknown) across a fake clock
  2. worst_of() resolution truth table (§2.4)
  3. Registry shows grey for registered-never-reported (unknown liveness)
  4. FleetStore heartbeat self-registration + apply_incident (moto)
  5. Hydrate-from-Dynamo rebuilds cache
  6. SSE delta emission (tests the delta-computation logic directly)
  7. datetime.utcnow() fix — HubState uses timezone-aware UTC
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import boto3
import pytest

# ---------------------------------------------------------------------------
# moto setup
# ---------------------------------------------------------------------------

try:
    from moto import mock_aws

    _HAS_MOTO = True
except ImportError:
    _HAS_MOTO = False

pytestmark = pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed")

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from relay.core.model import Incident, IncidentState, Severity, SignalSource
from relay.hub.app import HubState, SSEPublisher, SweepTimer
from relay.hub.fleet_store import FleetStore
from relay.hub.health import (
    DEFAULT_CADENCE_SECONDS,
    FleetTile,
    Liveness,
    liveness_from_heartbeat,
    worst_of,
)

# ---------------------------------------------------------------------------
# Fake clock helper
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable clock for deterministic tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now: datetime = start or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_NAME = "relay-fleet-test"


@pytest.fixture
def aws_session():
    """Return a moto-mocked boto3 session with the fleet table created."""
    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fleet_store(aws_session, clock) -> FleetStore:
    return FleetStore(
        table_name=TABLE_NAME,
        boto3_session=aws_session,
        clock=clock,
    )


@pytest.fixture
def hub_state(fleet_store, clock) -> HubState:
    return HubState(fleet_store=fleet_store, clock=clock)


@pytest.fixture
def base_incident() -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id="test-inc-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )


# ===========================================================================
# 1. Liveness derivation
# ===========================================================================


class TestLivenessDerivation:
    def test_never_seen_is_unknown(self):
        assert liveness_from_heartbeat(None) == Liveness.UNKNOWN

    def test_fresh_heartbeat_is_live(self, clock):
        hb = clock()
        clock.advance(60)  # exactly 1× cadence — still live (≤2× = 120s)
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.LIVE

    def test_at_2x_boundary_is_live(self, clock):
        hb = clock()
        clock.advance(120)  # exactly 2× cadence — boundary, still live
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.LIVE

    def test_just_past_2x_is_stale(self, clock):
        hb = clock()
        clock.advance(121)  # 2×+1 → stale
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.STALE

    def test_at_5x_boundary_is_stale(self, clock):
        hb = clock()
        clock.advance(300)  # exactly 5× cadence — boundary, still stale
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.STALE

    def test_just_past_5x_is_lost(self, clock):
        hb = clock()
        clock.advance(301)  # >5× → lost
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.LOST

    def test_long_silence_is_lost(self, clock):
        hb = clock()
        clock.advance(3600)  # 1 hour → lost
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == Liveness.LOST

    def test_liveness_progression_live_to_stale_to_lost(self, clock):
        hb = clock()
        assert liveness_from_heartbeat(hb, clock=clock) == Liveness.LIVE
        clock.advance(150)
        assert liveness_from_heartbeat(hb, clock=clock) == Liveness.STALE
        clock.advance(200)
        assert liveness_from_heartbeat(hb, clock=clock) == Liveness.LOST

    def test_naive_datetime_treated_as_utc(self, clock):
        # Naive datetimes should be coerced to UTC without raising.
        naive_hb = datetime(2024, 1, 1, 12, 0, 0)  # no tzinfo
        clock.advance(60)
        result = liveness_from_heartbeat(naive_hb, clock=clock)
        assert result in (Liveness.LIVE, Liveness.STALE, Liveness.LOST)


# ===========================================================================
# 2. worst_of() truth table
# ===========================================================================


class TestWorstOf:
    # --- red conditions ---
    def test_lost_liveness_is_red(self):
        assert worst_of(Liveness.LOST) == "red"

    def test_lost_liveness_with_no_incidents_is_red(self):
        assert worst_of(Liveness.LOST, open_incidents=0) == "red"

    def test_live_sev1_is_red(self):
        assert worst_of(Liveness.LIVE, open_incidents=1, worst_severity=Severity.SEV1) == "red"

    def test_live_sev2_is_red(self):
        assert worst_of(Liveness.LIVE, open_incidents=1, worst_severity=Severity.SEV2) == "red"

    def test_stale_sev1_is_red(self):
        # liveness==lost wins over stale, but we test that SEV1 independently forces red.
        assert worst_of(Liveness.STALE, open_incidents=1, worst_severity=Severity.SEV1) == "red"

    # --- degraded conditions ---
    def test_stale_no_incidents_is_degraded(self):
        assert worst_of(Liveness.STALE, open_incidents=0) == "degraded"

    def test_live_sev3_is_degraded(self):
        assert worst_of(Liveness.LIVE, open_incidents=1, worst_severity=Severity.SEV3) == "degraded"

    def test_live_sev4_is_degraded(self):
        assert worst_of(Liveness.LIVE, open_incidents=1, worst_severity=Severity.SEV4) == "degraded"

    def test_live_acked_is_degraded(self):
        assert worst_of(Liveness.LIVE, open_incidents=1, has_acked=True) == "degraded"

    # --- grey condition ---
    def test_unknown_no_incidents_is_grey(self):
        assert worst_of(Liveness.UNKNOWN) == "grey"

    def test_unknown_no_incidents_is_not_red(self):
        assert worst_of(Liveness.UNKNOWN, open_incidents=0) == "grey"

    # --- green condition ---
    def test_live_no_incidents_is_green(self):
        assert worst_of(Liveness.LIVE) == "green"

    def test_live_no_incidents_explicit_is_green(self):
        assert worst_of(Liveness.LIVE, open_incidents=0, worst_severity=None) == "green"

    # --- unknown with incidents: SEV1 -> red (incident beats unknown liveness for red) ---
    def test_unknown_sev1_is_red(self):
        # SEV1/SEV2 → red regardless of liveness being unknown.
        assert worst_of(Liveness.UNKNOWN, open_incidents=1, worst_severity=Severity.SEV1) == "red"

    def test_unknown_sev3_is_degraded(self):
        # SEV3 → degraded; unknown liveness → grey — but degraded check runs first.
        assert worst_of(Liveness.UNKNOWN, open_incidents=1, worst_severity=Severity.SEV3) == "degraded"


# ===========================================================================
# 3. Registry: grey for registered-never-reported
# ===========================================================================


class TestRegistryGrey:
    def test_tile_with_no_heartbeat_is_unknown_liveness(self, fleet_store, clock):
        """An app registered via an incident but never heartbeating should be UNKNOWN/grey."""
        # Insert a fleet tile with no heartbeat (simulates fleet.yaml registration
        # or an app that reported an incident but never sent a heartbeat).
        item = {
            "pk": "FLEET#unrouted#noheartbeat",
            "sk": "STATE",
            "account_id": "999",
            "app_name": "noheartbeat",
            "environment": "unrouted",
            "deployment_id": "noheartbeat",
            "open_incident_count": 0,
            "registered_at": clock().isoformat(),
            "has_acked": False,
            # No last_heartbeat_at
        }
        fleet_store._table.put_item(Item=item)
        tile = fleet_store.get_tile("999", "noheartbeat")
        assert tile is not None
        assert tile.liveness == Liveness.UNKNOWN
        assert tile.status == "grey"
        assert tile.last_heartbeat_at is None

    def test_hub_state_shows_grey_for_never_reported(self, hub_state, fleet_store, clock):
        """HubState.get_tile returns grey for a registered-never-reported app."""
        item = {
            "pk": "FLEET#unrouted#silent",
            "sk": "STATE",
            "account_id": "444",
            "app_name": "silent",
            "environment": "unrouted",
            "deployment_id": "silent",
            "open_incident_count": 0,
            "registered_at": clock().isoformat(),
            "has_acked": False,
        }
        fleet_store._table.put_item(Item=item)
        hub_state.hydrate()
        tile = hub_state.get_tile("444", "silent")
        assert tile is not None
        assert tile.status == "grey"
        assert tile.liveness == Liveness.UNKNOWN


# ===========================================================================
# 4. FleetStore: heartbeat self-registration + apply_incident (moto)
# ===========================================================================


class TestFleetStoreHeartbeat:
    def test_first_heartbeat_registers_app(self, fleet_store, clock):
        ts = clock()
        tile = fleet_store.record_heartbeat("111", "newapp", ts)
        assert tile.account_id == "111"
        assert tile.app_name == "newapp"
        assert tile.liveness == Liveness.LIVE  # just registered, age = 0
        assert tile.last_heartbeat_at == ts
        assert tile.registered_at is not None

    def test_heartbeat_updates_existing(self, fleet_store, clock):
        ts1 = clock()
        fleet_store.record_heartbeat("111", "newapp", ts1)
        clock.advance(60)
        ts2 = clock()
        tile = fleet_store.record_heartbeat("111", "newapp", ts2)
        assert tile.last_heartbeat_at == ts2

    def test_heartbeat_persists_to_dynamo(self, fleet_store, clock):
        ts = clock()
        fleet_store.record_heartbeat("222", "persistapp", ts)
        fetched = fleet_store.get_tile("222", "persistapp")
        assert fetched is not None
        assert fetched.last_heartbeat_at is not None
        assert abs((fetched.last_heartbeat_at - ts).total_seconds()) < 1

    def test_missing_app_returns_none(self, fleet_store):
        assert fleet_store.get_tile("000", "ghost") is None


class TestFleetTileDetailFields:
    """metadata / on_call / org_path ride the heartbeat and round-trip the store."""

    def test_heartbeat_persists_metadata_and_oncall(self, fleet_store, clock):
        ts = clock()
        meta = {"owner": "team-auth", "gitlab_project": "id/auth", "aws_tags": {"env": "prod"}}
        oncall = {
            "as_of": ts.isoformat(),
            "shift": "day",
            "source": "team_snapshot",
            "roles": {"primary": {"contact_id": "c1", "name": "Alice"}},
        }
        # deployment_id == app_name and default environment so the
        # get_tile()-derived key matches (get_tile keys on app_name/unrouted).
        fleet_store.record_heartbeat(
            "777", "detailapp", ts, metadata=meta, on_call=oncall,
        )
        fetched = fleet_store.get_tile("777", "detailapp")
        assert fetched is not None
        assert fetched.metadata == meta
        assert fetched.on_call == oncall
        # to_dict surfaces them for the drawer.
        d = fetched.to_dict()
        assert d["metadata"]["owner"] == "team-auth"
        assert d["on_call"]["roles"]["primary"]["name"] == "Alice"

    def test_absent_metadata_does_not_clobber_existing(self, fleet_store, clock):
        ts = clock()
        meta = {"owner": "team-auth"}
        fleet_store.record_heartbeat(
            "888", "stickyapp", ts, deployment_id="stickyapp",
            metadata=meta, on_call={"roles": {}},
        )
        # A later heartbeat with no metadata/on_call (older Node / enrichment off)
        # must not wipe the previously-stored richer values.
        clock.advance(60)
        fleet_store.record_heartbeat("888", "stickyapp", clock(), deployment_id="stickyapp")
        fetched = fleet_store.get_tile("888", "stickyapp")
        assert fetched.metadata == meta

    def test_org_path_surfaced_on_tile(self, fleet_store, clock):
        ts = clock()
        org_path = [
            {"id": "pl", "name": "PL", "level": "product_line", "parent": None},
            {"id": "orgapp", "name": "orgapp", "level": "deployment", "parent": "pl"},
        ]
        fleet_store.record_heartbeat(
            "999", "orgapp", ts, org_path=org_path,
        )
        fetched = fleet_store.get_tile("999", "orgapp")
        assert fetched.org_path == org_path
        assert fetched.to_dict()["org_path"][1]["level"] == "deployment"


class TestFleetStoreApplyIncident:
    def _make_incident(
        self,
        account_id: str = "123",
        app_name: str = "app1",
        state: IncidentState = IncidentState.TRIGGERED,
        severity: Severity = Severity.SEV2,
    ) -> Incident:
        now = datetime.now(UTC)
        return Incident(
            correlation_id=f"test-{state}-{severity}",
            account_id=account_id,
            region="us-east-1",
            app_name=app_name,
            severity=severity,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="test-alarm",
            state=state,
            created_at=now,
            updated_at=now,
        )

    def test_triggered_increments_count(self, fleet_store, clock):
        inc = self._make_incident(state=IncidentState.TRIGGERED, severity=Severity.SEV2)
        tile = fleet_store.apply_incident(inc)
        assert tile.open_incidents == 1
        assert tile.worst_severity == Severity.SEV2
        assert tile.status == "red"  # SEV2 → red

    def test_acknowledged_sets_has_acked(self, fleet_store, clock):
        # First trigger
        inc = self._make_incident(state=IncidentState.TRIGGERED, severity=Severity.SEV3)
        fleet_store.apply_incident(inc)
        # Then acknowledge
        ack = self._make_incident(state=IncidentState.ACKNOWLEDGED, severity=Severity.SEV3)
        tile = fleet_store.apply_incident(ack)
        # acknowledged → degraded (has_acked=True, count > 0)
        assert tile.status in ("degraded", "red")

    def test_resolved_decrements_count(self, fleet_store, clock):
        # Trigger two incidents.
        inc1 = self._make_incident(state=IncidentState.TRIGGERED, severity=Severity.SEV3)
        fleet_store.apply_incident(inc1)
        # Use a second incident with a unique correlation_id.
        now = datetime.now(UTC)
        inc2 = Incident(
            correlation_id="inc2",
            account_id="123",
            region="us-east-1",
            app_name="app1",
            severity=Severity.SEV3,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="alarm2",
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
        )
        fleet_store.apply_incident(inc2)
        # Resolve one.
        res = self._make_incident(state=IncidentState.RESOLVED, severity=Severity.SEV3)
        tile = fleet_store.apply_incident(res)
        assert tile.open_incidents == 1

    def test_resolve_clears_severity_when_last(self, fleet_store, clock):
        inc = self._make_incident(state=IncidentState.TRIGGERED, severity=Severity.SEV2)
        fleet_store.apply_incident(inc)
        res = self._make_incident(state=IncidentState.RESOLVED, severity=Severity.SEV2)
        tile = fleet_store.apply_incident(res)
        assert tile.open_incidents == 0
        assert tile.worst_severity is None
        assert tile.status == "grey"  # no heartbeat + no incidents = unknown → grey

    def test_sev1_forces_red(self, fleet_store, clock):
        # First give a heartbeat so liveness is LIVE.
        fleet_store.record_heartbeat("123", "app1", clock())
        inc = self._make_incident(state=IncidentState.TRIGGERED, severity=Severity.SEV1)
        tile = fleet_store.apply_incident(inc)
        assert tile.status == "red"

    def test_worst_severity_tightens_to_more_severe(self, fleet_store, clock):
        """A second, more-severe incident raises worst_severity (Step 3 conditional SET)."""
        fleet_store.apply_incident(
            self._make_incident(app_name="sevapp", severity=Severity.SEV3)
        )
        tile = fleet_store.apply_incident(
            Incident(
                correlation_id="sev-2",
                account_id="123",
                region="us-east-1",
                app_name="sevapp",
                severity=Severity.SEV1,
                signal_source=SignalSource.CLOUDWATCH_ALARM,
                alarm_name="a2",
                state=IncidentState.TRIGGERED,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        assert tile.worst_severity == Severity.SEV1

    def test_worst_severity_not_loosened_by_less_severe(self, fleet_store, clock):
        """A later, less-severe incident must NOT lower worst_severity.

        This is the conditional-SET guard: the read-modify-write version could
        clobber the stored worst with the milder value under concurrency.
        """
        fleet_store.apply_incident(
            self._make_incident(app_name="sevapp2", severity=Severity.SEV1)
        )
        tile = fleet_store.apply_incident(
            Incident(
                correlation_id="sev-mild",
                account_id="123",
                region="us-east-1",
                app_name="sevapp2",
                severity=Severity.SEV4,
                signal_source=SignalSource.CLOUDWATCH_ALARM,
                alarm_name="a3",
                state=IncidentState.TRIGGERED,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        assert tile.worst_severity == Severity.SEV1

    def test_triggered_stamps_deployment_metadata_and_tags(self, fleet_store, clock):
        """apply_incident TRIGGERED merges deployment_metadata + tags into tile metadata."""
        now = datetime.now(UTC)
        inc = Incident(
            correlation_id="meta-stamp-001",
            account_id="123",
            region="us-east-1",
            app_name="app-meta",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="alarm-meta",
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
            deployment_metadata={"gitlab_project": "pay/api", "git_sha": "abc123"},
            tags={"env": "prod", "team": "payments"},
        )
        tile = fleet_store.apply_incident(inc)
        assert tile.metadata is not None
        assert tile.metadata.get("gitlab_project") == "pay/api"
        assert tile.metadata.get("git_sha") == "abc123"
        assert tile.metadata.get("resource_tags") == {"env": "prod", "team": "payments"}

    def test_triggered_merges_keeps_heartbeat_metadata(self, fleet_store, clock):
        """Heartbeat-supplied metadata keys survive the incident merge."""
        ts = clock()
        fleet_store.record_heartbeat(
            "123",
            "merge-app",
            ts,
            deployment_id="merge-app-dep",
            metadata={"owner": "team-auth", "runbook": "https://wiki/merge"},
        )
        now = datetime.now(UTC)
        inc = Incident(
            correlation_id="meta-merge-001",
            account_id="123",
            region="us-east-1",
            app_name="merge-app",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="alarm-merge",
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
            # deployment_id matches the heartbeat's dep_id so both hit the same item.
            deployment_id="merge-app-dep",
            deployment_metadata={"gitlab_project": "corp/merge-app"},
        )
        tile = fleet_store.apply_incident(inc)
        # Heartbeat-only key survives.
        assert tile.metadata.get("owner") == "team-auth"
        assert tile.metadata.get("runbook") == "https://wiki/merge"
        # Incident-supplied key is present.
        assert tile.metadata.get("gitlab_project") == "corp/merge-app"

    def test_triggered_empty_tags_and_metadata_no_merge(self, fleet_store, clock):
        """Empty deployment_metadata + tags leave tile metadata unchanged (no spurious write)."""
        ts = clock()
        fleet_store.record_heartbeat(
            "123",
            "nochange-app",
            ts,
            deployment_id="nochange-app-dep",
            metadata={"owner": "stable-team"},
        )
        now = datetime.now(UTC)
        inc = Incident(
            correlation_id="meta-empty-001",
            account_id="123",
            region="us-east-1",
            app_name="nochange-app",
            severity=Severity.SEV3,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="alarm-empty",
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
            # deployment_id matches the heartbeat's dep_id.
            deployment_id="nochange-app-dep",
            # deployment_metadata and tags are both empty (default)
        )
        tile = fleet_store.apply_incident(inc)
        # Pre-existing metadata key is untouched.
        assert tile.metadata.get("owner") == "stable-team"
        # No resource_tags key injected.
        assert "resource_tags" not in (tile.metadata or {})


# ===========================================================================
# 4b. FleetStore.recompute — repair aggregate after a direct delete (purge)
# ===========================================================================


class TestFleetStoreRecompute:
    def _make_incident(
        self,
        correlation_id: str,
        severity: Severity = Severity.SEV2,
        app_name: str = "app1",
        account_id: str = "123",
        environment: str = "unrouted",
        deployment_id: str = "unknown",
    ) -> Incident:
        now = datetime.now(UTC)
        return Incident(
            correlation_id=correlation_id,
            account_id=account_id,
            region="us-east-1",
            app_name=app_name,
            environment=environment,
            deployment_id=deployment_id,
            severity=severity,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="test-alarm",
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
        )

    def test_recompute_to_zero_clears_red_tile(self, fleet_store, clock):
        # A SEV2 incident drives the tile red, count=1.
        inc = self._make_incident("c1", severity=Severity.SEV2)
        tile = fleet_store.apply_incident(inc)
        assert tile.open_incidents == 1
        assert tile.status == "red"

        # Simulate a purge that deleted the incident row: recompute from the now-
        # empty survivor list. The tile must clear (count 0, no severity).
        tile = fleet_store.recompute("123", "app1", [], deployment_id="unknown")
        assert tile is not None
        assert tile.open_incidents == 0
        assert tile.worst_severity is None
        assert tile.status != "red"

    def test_recompute_recounts_surviving_incidents(self, fleet_store, clock):
        # Three incidents open (SEV1 worst).
        fleet_store.apply_incident(self._make_incident("a", severity=Severity.SEV3))
        fleet_store.apply_incident(self._make_incident("b", severity=Severity.SEV1))
        tile = fleet_store.apply_incident(
            self._make_incident("c", severity=Severity.SEV3)
        )
        assert tile.open_incidents == 3
        assert tile.worst_severity == Severity.SEV1

        # Purge removed the SEV1 + one SEV3; one SEV3 survives.
        survivor = self._make_incident("a", severity=Severity.SEV3)
        tile = fleet_store.recompute("123", "app1", [survivor], deployment_id="unknown")
        assert tile is not None
        assert tile.open_incidents == 1
        assert tile.worst_severity == Severity.SEV3

    def test_recompute_unregistered_tile_returns_none(self, fleet_store):
        assert fleet_store.recompute("nope", "ghost", []) is None

    def test_recompute_respects_environment_and_deployment(self, fleet_store, clock):
        inc = self._make_incident(
            "p1", severity=Severity.SEV2, environment="prod", deployment_id="dep-1"
        )
        fleet_store.apply_incident(inc)
        # Wrong env/dep is a different tile — recompute there is a no-op (None).
        assert fleet_store.recompute(
            "123", "app1", [], environment="dev", deployment_id="dep-1"
        ) is None
        # Correct key clears the prod tile.
        tile = fleet_store.recompute(
            "123", "app1", [], environment="prod", deployment_id="dep-1"
        )
        assert tile is not None
        assert tile.open_incidents == 0


# ===========================================================================
# 5. Hydrate-from-DynamoDB rebuilds cache
# ===========================================================================


class TestHydrate:
    def test_hydrate_loads_all_tiles(self, fleet_store, hub_state, clock):
        # Seed two apps via heartbeat.
        fleet_store.record_heartbeat("aaa", "app-a", clock())
        fleet_store.record_heartbeat("bbb", "app-b", clock())

        hub_state.hydrate()

        tiles = hub_state.get_tiles()
        keys = {t.key for t in tiles}
        assert any("app-a" in k for k in keys)
        assert any("app-b" in k for k in keys)

    def test_hydrate_empty_store_gives_empty_cache(self, hub_state):
        hub_state.hydrate()
        assert hub_state.get_tiles() == []

    def test_hydrate_overwrites_stale_cache(self, fleet_store, hub_state, clock):
        # Put something in the cache manually.
        fake_tile = FleetTile(
            account_id="stale",
            app_name="stale-app",
            status="red",
            liveness=Liveness.LOST,
            open_incidents=5,
            worst_severity=Severity.SEV1,
            last_heartbeat_at=None,
            registered_at=clock(),
        )
        hub_state.upsert_tile(fake_tile)
        assert len(hub_state.get_tiles()) == 1

        # Now hydrate from empty DynamoDB — cache should be empty.
        hub_state.hydrate()
        assert hub_state.get_tiles() == []


# ===========================================================================
# 6. SSE delta emission
# ===========================================================================


class TestSSEPublisher:
    def test_subscribe_receives_delta(self):
        pub = SSEPublisher()
        q = pub.subscribe()

        tile = FleetTile(
            account_id="123",
            app_name="myapp",
            status="red",
            liveness=Liveness.LOST,
            open_incidents=1,
            worst_severity=Severity.SEV2,
            last_heartbeat_at=None,
            registered_at=datetime.now(UTC),
        )
        pub.publish_delta(tile)

        msg = q.get(timeout=1.0)
        assert msg.startswith("event: delta\n")
        data = json.loads(msg.split("data: ", 1)[1].strip())
        assert data["account_id"] == "123"
        assert data["app_name"] == "myapp"
        assert data["status"] == "red"

    def test_unsubscribed_client_does_not_receive(self):
        pub = SSEPublisher()
        q = pub.subscribe()
        pub.unsubscribe(q)

        tile = FleetTile(
            account_id="x",
            app_name="y",
            status="green",
            liveness=Liveness.LIVE,
            open_incidents=0,
            worst_severity=None,
            last_heartbeat_at=datetime.now(UTC),
            registered_at=datetime.now(UTC),
        )
        pub.publish_delta(tile)
        assert q.empty()

    def test_ping_is_named_event(self):
        # Named 'ping' event (not a ': ' comment) so the browser EventSource
        # actually fires a JS event the dashboard can use for liveness.
        pub = SSEPublisher()
        q = pub.subscribe()
        pub.publish_ping()
        msg = q.get(timeout=1.0)
        assert msg.startswith("event: ping")
        assert msg.endswith("\n\n")

    def test_multiple_subscribers_all_receive(self):
        pub = SSEPublisher()
        q1 = pub.subscribe()
        q2 = pub.subscribe()

        tile = FleetTile(
            account_id="acc",
            app_name="svc",
            status="green",
            liveness=Liveness.LIVE,
            open_incidents=0,
            worst_severity=None,
            last_heartbeat_at=datetime.now(UTC),
            registered_at=datetime.now(UTC),
        )
        pub.publish_delta(tile)

        assert not q1.empty()
        assert not q2.empty()


# ===========================================================================
# 7. Sweep timer emits deltas when liveness changes
# ===========================================================================


class TestSweepTimer:
    def test_sweep_emits_delta_when_liveness_degrades(self, fleet_store, hub_state, clock):
        """When the clock advances past the stale threshold, the sweep should emit a delta."""
        # Register and heartbeat app.
        hb_ts = clock()
        tile = fleet_store.record_heartbeat("sweep-acc", "sweep-app", hb_ts)
        hub_state.upsert_tile(tile)

        # Advance clock past stale threshold (2× cadence = 120s).
        clock.advance(150)

        pub = SSEPublisher()
        q = pub.subscribe()
        shutdown = threading.Event()

        sweep = SweepTimer(
            hub_state=hub_state,
            sse_publisher=pub,
            shutdown_event=shutdown,
            sweep_interval=0,   # run immediately
            ping_interval=9999, # suppress pings
        )

        # Run one sweep directly (not in a thread).
        sweep._do_sweep()

        # After sweep, tile should be stale → degraded.
        refreshed = hub_state.get_tile("sweep-acc", "sweep-app")
        assert refreshed is not None
        assert refreshed.liveness == Liveness.STALE
        assert refreshed.status == "degraded"

        # A delta should have been published.
        assert not q.empty()
        msg = q.get_nowait()
        assert "delta" in msg

    def test_sweep_no_delta_when_status_unchanged(self, fleet_store, hub_state, clock):
        """If liveness has not changed, no delta should be emitted."""
        hb_ts = clock()
        tile = fleet_store.record_heartbeat("stable-acc", "stable-app", hb_ts)
        hub_state.upsert_tile(tile)
        # Do NOT advance clock — app is still live.

        pub = SSEPublisher()
        q = pub.subscribe()
        shutdown = threading.Event()

        sweep = SweepTimer(
            hub_state=hub_state,
            sse_publisher=pub,
            shutdown_event=shutdown,
            sweep_interval=0,
            ping_interval=9999,
        )
        sweep._do_sweep()

        # No delta expected (status has not changed).
        assert q.empty()


# ===========================================================================
# 8. HubState.update_app uses timezone-aware UTC (regression test)
# ===========================================================================


class TestHubStateTimezone:
    def test_update_app_uses_aware_datetime(self, hub_state, base_incident):
        """update_app must produce timezone-aware last_updated (not naive UTC)."""
        tile = hub_state.update_app(base_incident)
        assert tile.last_updated.tzinfo is not None

    def test_record_heartbeat_uses_aware_datetime(self, hub_state):
        ts = datetime.now(UTC)
        tile = hub_state.record_heartbeat("tz-acc", "tz-app", ts)
        assert tile.last_heartbeat_at is not None
        assert tile.last_heartbeat_at.tzinfo is not None


# ===========================================================================
# 9. HubProcessor heartbeat dispatch
# ===========================================================================


class TestHubProcessorHeartbeat:
    def test_handles_heartbeat_event_via_detail_type(self, fleet_store, hub_state):
        from relay.hub.app import HubProcessor

        pub = SSEPublisher()
        mock_store = MagicMock()
        mock_store.put_incident = MagicMock()
        processor = HubProcessor(
            incident_store=mock_store,
            notifier=MagicMock(),
            hub_state=hub_state,
            sse_publisher=pub,
            listeners=[],
        )
        q = pub.subscribe()

        now_str = datetime.now(UTC).isoformat()
        event = {
            "detail-type": "relay.heartbeat",
            "detail": {
                "account_id": "hb-acc",
                "app_name": "hb-app",
                "timestamp": now_str,
            },
        }
        processor.handle_event(event)

        # A delta should have been published.
        assert not q.empty()
        # Incident store should NOT have been touched.
        mock_store.put_incident.assert_not_called()

    def test_handles_heartbeat_event_via_relay_event_marker(self, fleet_store, hub_state):
        from relay.hub.app import HubProcessor

        pub = SSEPublisher()
        mock_store = MagicMock()
        mock_store.put_incident = MagicMock()
        processor = HubProcessor(
            incident_store=mock_store,
            notifier=MagicMock(),
            hub_state=hub_state,
            sse_publisher=pub,
            listeners=[],
        )
        q = pub.subscribe()

        now_str = datetime.now(UTC).isoformat()
        event = {
            "relay_event": "heartbeat",
            "account_id": "hb-acc2",
            "app_name": "hb-app2",
            "timestamp": now_str,
        }
        processor.handle_event(event)

        assert not q.empty()
        mock_store.put_incident.assert_not_called()


# ===========================================================================
# Dynamic catalog (org tree from heartbeat registrations)
# ===========================================================================


class TestDynamicCatalog:
    """Tests for FleetStore.build_org_tree() and HubState dynamic catalog from registrations."""

    _ORG_PATH = [
        {"id": "pl-id", "name": "Identity", "level": "product_line", "parent": None},
        {"id": "prod-auth", "name": "Auth", "level": "product", "parent": "pl-id"},
        {"id": "dep-auth-prod", "name": "auth-api-prod", "level": "deployment", "parent": "prod-auth"},
    ]

    def test_record_heartbeat_persists_and_builds_org_tree(self, fleet_store, clock):
        ts = clock()
        fleet_store.record_heartbeat(
            "111", "auth-api", ts,
            environment="prod",
            deployment_id="dep-auth-prod",
            service_path=["Identity", "Auth", "dep-auth-prod"],
            org_path=self._ORG_PATH,
        )
        tree = fleet_store.build_org_tree()
        assert tree.get("dep-auth-prod") is not None
        assert tree.get("pl-id") is not None
        roots = tree.roots()
        assert any(r.level == "product_line" for r in roots)

    def test_build_org_tree_empty_when_no_registrations(self, fleet_store):
        tree = fleet_store.build_org_tree()
        assert tree.all_nodes() == []

    def test_build_org_tree_dedupes_shared_ancestors(self, fleet_store, clock):
        ts = clock()
        # Two deployments sharing pl-id + prod-auth
        path_a = self._ORG_PATH  # ends at dep-auth-prod
        path_b = [
            {"id": "pl-id", "name": "Identity", "level": "product_line", "parent": None},
            {"id": "prod-auth", "name": "Auth", "level": "product", "parent": "pl-id"},
            {"id": "dep-auth-dev", "name": "auth-api-dev", "level": "deployment", "parent": "prod-auth"},
        ]
        fleet_store.record_heartbeat(
            "111", "auth-api-prod", ts,
            environment="prod",
            deployment_id="dep-auth-prod",
            org_path=path_a,
        )
        clock.advance(1)
        fleet_store.record_heartbeat(
            "111", "auth-api-dev", clock(),
            environment="dev",
            deployment_id="dep-auth-dev",
            org_path=path_b,
        )
        tree = fleet_store.build_org_tree()
        # Shared product node appears once
        pl_nodes = [n for n in tree.all_nodes() if n.id == "pl-id"]
        assert len(pl_nodes) == 1
        prod_nodes = [n for n in tree.all_nodes() if n.id == "prod-auth"]
        assert len(prod_nodes) == 1
        roots = tree.roots()
        assert len(roots) == 1

    def test_hub_state_record_heartbeat_updates_get_org_tree(self, hub_state, clock):
        ts = clock()
        hub_state.record_heartbeat(
            "222", "auth-svc", ts,
            environment="prod",
            deployment_id="dep-auth-prod",
            service_path=["Identity", "Auth", "dep-auth-prod"],
            org_path=self._ORG_PATH,
        )
        tree = hub_state.get_org_tree()
        assert tree is not None
        assert tree.get("dep-auth-prod") is not None


# ===========================================================================
# Dashboard fragment assembly (#28 phase 2)
# ===========================================================================


class TestDashboardAssembly:
    """The dashboard markup/CSS is authored as ordered fragments under
    dashboard_parts/ and assembled at serve time; behavior is authored as native
    ES modules under dashboard_modules/ and served read-only at /static/dashboard/.
    These lock the contract: the manifest's fragments exist, the assembled shell
    is a single well-formed HTML page with one <style> pair and one module-script
    tag (no inline JS), the entry module exists, and every relative import between
    modules resolves to a real exported symbol."""

    def test_manifest_and_named_fragments_exist(self):
        from relay.hub.app import _DASHBOARD_PARTS_DIR

        manifest = _DASHBOARD_PARTS_DIR / "manifest.txt"
        assert manifest.is_file(), "dashboard_parts/manifest.txt must exist"
        names = [
            ln.strip()
            for ln in manifest.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        assert names, "manifest lists no fragments"
        for name in names:
            assert (_DASHBOARD_PARTS_DIR / name).is_file(), f"missing fragment: {name}"

    def test_assembled_html_is_well_formed_single_document(self):
        from relay.hub.app import _render_dashboard_html

        html = _render_dashboard_html()
        # The shell carries no inline JS — behavior loads as ES modules. There is
        # exactly one <style> pair and exactly one module script tag pointing at
        # the static entry module; no bare inline <script> remains.
        assert html.count("<script>") == 0, "no inline <script> — JS is ES modules"
        assert html.count('<script type="module"') == 1
        assert html.count("</script>") == 1
        assert '/static/dashboard/main.js' in html
        assert html.count("<style>") == 1
        assert html.count("</style>") == 1
        assert html.lstrip().startswith("<!"), "must start with a doctype"
        assert "</html>" in html
        # Substantial — guards against a truncated/empty assembly (CSS-dominated
        # now that the JS lives in modules).
        assert len(html) > 30_000

    def test_assembly_is_concatenation_in_manifest_order(self):
        from relay.hub.app import _DASHBOARD_PARTS_DIR, _render_dashboard_html

        names = [
            ln.strip()
            for ln in (_DASHBOARD_PARTS_DIR / "manifest.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        expected = "".join(
            (_DASHBOARD_PARTS_DIR / name).read_text(encoding="utf-8") for name in names
        )
        assert _render_dashboard_html() == expected

    def test_module_dir_and_entry_exist(self):
        from relay.hub.app import _DASHBOARD_MODULES_DIR

        assert _DASHBOARD_MODULES_DIR.is_dir(), "dashboard_modules/ must ship in the package"
        assert (_DASHBOARD_MODULES_DIR / "main.js").is_file(), "entry module main.js missing"

    def test_module_imports_resolve_to_real_exports(self):
        """Every `import { … } from './x.js'` must target a sibling module that
        actually exports each named symbol — catches a broken refactor that would
        only surface as a runtime error in the browser."""
        import re

        from relay.hub.app import _DASHBOARD_MODULES_DIR

        mods = {p.name: p.read_text(encoding="utf-8") for p in _DASHBOARD_MODULES_DIR.glob("*.js")}
        assert mods, "no ES modules found"

        def exported_names(text: str) -> set[str]:
            names: set[str] = set()
            for m in re.finditer(
                r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z0-9_]+)",
                text,
                re.M,
            ):
                names.add(m.group(1))
            for m in re.finditer(r"^export\s*\{([^}]*)\}", text, re.M):
                for part in m.group(1).split(","):
                    nm = part.strip().split(" as ")[-1].strip()
                    if nm:
                        names.add(nm)
            return names

        exports = {name: exported_names(text) for name, text in mods.items()}

        problems: list[str] = []
        for name, text in mods.items():
            for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*'\./([^']+)'", text):
                syms = [s.strip().split(" as ")[0].strip() for s in m.group(1).split(",") if s.strip()]
                target = m.group(2)
                if target not in mods:
                    problems.append(f"{name}: imports from missing module {target}")
                    continue
                for s in syms:
                    if s not in exports[target]:
                        problems.append(f"{name}: imports {{{s}}} from {target}, which does not export it")
        assert not problems, "broken ES-module imports:\n" + "\n".join(problems)


# ===========================================================================
# GET /incidents/{id}/flow  — process-flow endpoint (issue #20)
# ===========================================================================
#
# These tests use the same HubApp.__new__ + build_fastapi_app() pattern
# established by the other endpoint-test modules in this suite.
# They exercise the four behaviours: config-backed, derived, none/fallback,
# 404, and policy_id from the triggered event.
# ===========================================================================

import threading as _threading  # noqa: E402  (already at module top but kept explicit)
from types import SimpleNamespace  # noqa: E402

from relay.core.model import (  # noqa: E402
    EscalationPolicy,
    EscalationStep,
    Stream,
    TimelineEvent,
)

try:
    from fastapi.testclient import TestClient as _TestClient  # noqa: E402
except ImportError:
    _TestClient = None  # type: ignore[assignment,misc]

from relay.hub.app import HubApp  # noqa: E402 (HubState/SSEPublisher already imported)

# --------------------------------------------------------------------------
# Shared helpers (flow-only scope)
# --------------------------------------------------------------------------

_FLOW_T0 = datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC)


def _fev(
    cid: str,
    step_index: int,
    occurred_at: datetime,
    contact_ids: list | None = None,
    event_id: str | None = None,
) -> TimelineEvent:
    """Build an escalation.page_sent TimelineEvent."""
    return TimelineEvent(
        event_id=event_id or f"fev-{step_index}",
        incident_id=cid,
        stream=Stream.TEAM,
        occurred_at=occurred_at,
        actor="system",
        event_type="escalation.page_sent",
        detail={
            "step_index": step_index,
            "contact_ids": contact_ids or [],
            "roles": [],
            "streams": ["TEAM"],
            "timeout_minutes": 5,
        },
    )


def _flow_incident(
    cid: str = "flow-inc-001",
    timeline: list | None = None,
    escalation_policy_id: str | None = None,
) -> Incident:
    return Incident(
        correlation_id=cid,
        account_id="123456789012",
        region="us-east-1",
        app_name="svc",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm",
        state=IncidentState.TRIGGERED,
        timeline=timeline or [],
        escalation_policy_id=escalation_policy_id,
    )


class _FlowFakeIncidentStore:
    def __init__(self, incidents: list[Incident]) -> None:
        self._db = {i.correlation_id: i for i in incidents}

    def get_incident(self, cid: str) -> Incident | None:
        return self._db.get(cid)

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        # Mirror the real store: only non-terminal incidents are "open".
        terminal = {IncidentState.RESOLVED, IncidentState.CLOSED}
        return [i for i in self._db.values() if i.state not in terminal]

    def list_incidents(self) -> list[Incident]:
        return list(self._db.values())

    def put_incident(self, inc: Incident) -> None:
        self._db[inc.correlation_id] = inc


class _FlowFakeContactStore:
    def __init__(self, contacts: dict[str, str]) -> None:
        # contacts is contact_id -> name
        from relay.core.model import Contact
        self._db = [
            Contact(contact_id=cid, name=name, email=f"{cid}@example.com")
            for cid, name in contacts.items()
        ]

    def list_contacts(self) -> list:
        return list(self._db)


def _flow_policy(policy_id: str = "flow-pol-1") -> EscalationPolicy:
    return EscalationPolicy(
        policy_id=policy_id,
        name="Flow Policy",
        team="team-flow",
        steps=[
            EscalationStep(
                step_index=0,
                contact_ids=["fc1"],
                timeout_minutes=5,
                notify_streams=[Stream.TEAM],
            ),
            EscalationStep(
                step_index=1,
                contact_ids=["fc2"],
                timeout_minutes=10,
                notify_streams=[Stream.TEAM],
            ),
        ],
    )


def _flow_client(
    incident: Incident | None = None,
    hub_config: object | None = None,
    contact_store: object | None = None,
) -> _TestClient:
    """Build a minimal HubApp TestClient wired for /incidents/{id}/flow tests."""
    if _TestClient is None:
        pytest.skip("fastapi/httpx not installed")

    store = _FlowFakeIncidentStore([incident] if incident is not None else [])

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = store
    app_obj._contact_store = contact_store
    app_obj._config = hub_config
    app_obj._schedule_store = None
    app_obj._settings_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None

    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = _threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    return _TestClient(app_obj.build_fastapi_app())


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestFlowEndpoint:
    """GET /incidents/{id}/flow — process-flow endpoint."""

    def test_404_for_unknown_incident(self):
        c = _flow_client()
        r = c.get("/incidents/no-such-id/flow")
        assert r.status_code == 404

    def test_config_backed_source(self):
        """Config has a matching policy → source=='config', expected_steps from policy."""
        policy = _flow_policy("flow-pol-1")
        # Fake hub config with escalation.policies
        hub_config = SimpleNamespace(
            escalation=SimpleNamespace(policies=[policy]),
            routing=None,
        )
        timeline = [_fev("fci-config", 0, _FLOW_T0, contact_ids=["fc1"])]
        inc = _flow_incident(
            "fci-config",
            timeline=timeline,
            escalation_policy_id="flow-pol-1",
        )
        c = _flow_client(
            incident=inc,
            hub_config=hub_config,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-config/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "config"
        assert len(body["expected_steps"]) == 2
        assert body["expected_steps"][0]["reached"] is True
        assert body["expected_steps"][1]["reached"] is False
        assert body["fallback"] is False

    def test_derived_when_no_config_escalation(self):
        """No policy in config → source=='derived' (ladder inferred from page_sent events)."""
        timeline = [
            _fev("fci-derived", 0, _FLOW_T0, contact_ids=["fc1"]),
            _fev("fci-derived", 1, _FLOW_T0 + timedelta(seconds=60), contact_ids=["fc2"]),
        ]
        inc = _flow_incident("fci-derived", timeline=timeline)
        # Config with no escalation attr → policy lookup is skipped
        c = _flow_client(
            incident=inc,
            hub_config=None,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-derived/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "derived"
        assert len(body["expected_steps"]) == 2
        assert all(s["reached"] for s in body["expected_steps"])
        assert body["fallback"] is False

    def test_none_fallback_no_policy_no_page_sent(self):
        """No policy + no page_sent events → source=='none', fallback True."""
        inc = _flow_incident("fci-none")
        c = _flow_client(incident=inc, hub_config=None)
        r = c.get("/incidents/fci-none/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "none"
        assert body["expected_steps"] == []
        assert body["fallback"] is True

    def test_policy_id_from_triggered_event(self):
        """incident.escalation_policy_id is None but triggered event carries policy_id."""
        policy = _flow_policy("flow-pol-trig")
        hub_config = SimpleNamespace(
            escalation=SimpleNamespace(policies=[policy]),
            routing=None,
        )
        timeline = [
            TimelineEvent(
                event_id="trig-1",
                incident_id="fci-trig",
                stream=Stream.TEAM,
                occurred_at=_FLOW_T0,
                actor="system",
                event_type="incident.triggered",
                detail={"policy_id": "flow-pol-trig", "alarm_name": "alarm"},
            ),
            _fev("fci-trig", 0, _FLOW_T0 + timedelta(seconds=5), contact_ids=["fc1"]),
        ]
        inc = _flow_incident(
            "fci-trig",
            timeline=timeline,
            escalation_policy_id=None,  # the field is None
        )
        c = _flow_client(
            incident=inc,
            hub_config=hub_config,
            contact_store=_FlowFakeContactStore({"fc1": "Alice", "fc2": "Bob"}),
        )
        r = c.get("/incidents/fci-trig/flow")
        assert r.status_code == 200
        body = r.json()
        # Route resolved the policy from the triggered event → config source
        assert body["source"] == "config"
        assert body["policy_id"] == "flow-pol-trig"


# ===========================================================================
# 10. Tile open_incident_count drift: resolve endpoint + sweep reconciliation
# ===========================================================================


class TestTileDriftRepair:
    """Derive-and-self-heal approach: resolve/ack/ignore endpoints and sweep
    reconciliation must never leave open_incident_count drifted on a tile."""

    def _make_open_incident(
        self,
        correlation_id: str = "drift-inc-001",
        account_id: str = "123456789012",
        app_name: str = "drift-app",
        environment: str = "unrouted",
        deployment_id: str = "drift-app",
        state: IncidentState = IncidentState.TRIGGERED,
    ) -> Incident:
        now = datetime.now(UTC)
        return Incident(
            correlation_id=correlation_id,
            account_id=account_id,
            region="us-east-1",
            app_name=app_name,
            environment=environment,
            deployment_id=deployment_id,
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="drift-alarm",
            state=state,
            created_at=now,
            updated_at=now,
        )

    def _build_client_and_sse(
        self,
        incidents: list[Incident],
        hub_state: HubState,
    ) -> tuple[_TestClient, SSEPublisher]:
        if _TestClient is None:
            pytest.skip("fastapi/httpx not installed")

        store = _FlowFakeIncidentStore(incidents)

        app_obj = HubApp.__new__(HubApp)
        app_obj._incident_store = store
        app_obj._contact_store = None
        app_obj._config = None
        app_obj._schedule_store = None
        app_obj._settings_store = None
        app_obj._notifier = None
        app_obj._paging_topic_arn = None
        app_obj._hub_state = hub_state
        pub = SSEPublisher()
        app_obj._sse_publisher = pub

        return _TestClient(app_obj.build_fastapi_app()), pub

    def test_resolve_endpoint_recomputes_tile_count(
        self, fleet_store, hub_state, clock, monkeypatch
    ):
        """Resolving an incident via /incidents/{id}/resolve must decrement the
        tile's open_incident_count to the correct derived value and emit an SSE
        delta — proving the tile never stays phantom-red after a UI resolve."""
        # dev auth gives the endpoint a fixed writer identity so the write path
        # actually runs (otherwise require_writer 403s and the recompute is
        # never reached — a vacuous pass).
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_DEV_USER", "tester")

        # Prime fleet tile with one open incident via the ingest path.
        inc = self._make_open_incident(state=IncidentState.TRIGGERED)
        hub_state.update_app(inc)
        tile_before = hub_state.get_tile(inc.account_id, inc.app_name)
        assert tile_before is not None
        assert tile_before.open_incidents == 1

        # Transition incident to RESOLVED in the fake store (simulating what the
        # endpoint will do via put_incident).
        resolved = self._make_open_incident(state=IncidentState.RESOLVED)

        # Build a fake store that starts with the resolved incident so that
        # list_open_incidents() returns zero open after the resolve.
        store = _FlowFakeIncidentStore([resolved])

        app_obj = HubApp.__new__(HubApp)
        app_obj._incident_store = store
        app_obj._contact_store = None
        app_obj._config = None
        app_obj._schedule_store = None
        app_obj._settings_store = None
        app_obj._notifier = None
        app_obj._paging_topic_arn = None
        app_obj._hub_state = hub_state
        pub = SSEPublisher()
        app_obj._sse_publisher = pub

        client = _TestClient(app_obj.build_fastapi_app())
        q = pub.subscribe()

        # Call the resolve endpoint — must succeed under dev auth.
        r = client.post("/incidents/drift-inc-001/resolve")
        assert r.status_code == 200, r.text

        # Tile must now reflect zero open incidents.
        tile_after = hub_state.get_tile(inc.account_id, inc.app_name)
        assert tile_after is not None
        assert tile_after.open_incidents == 0

        # An SSE delta must have been emitted.
        assert not q.empty()
        msg = q.get_nowait()
        assert "delta" in msg

    def test_sweep_reconciles_drifted_tile_count(self, fleet_store, hub_state, clock):
        """Sweep reconciliation must correct a tile whose open_incident_count is
        artificially high (simulating drift from UI writes that bypassed the ingest
        bus decrement)."""
        # Register a heartbeat so the tile exists in DynamoDB.
        hb_ts = clock()
        fleet_store.record_heartbeat("drift-acct", "drift-svc", hb_ts)

        # Apply one TRIGGERED incident to get the tile into the cache with count=1.
        inc_open = Incident(
            correlation_id="sweep-drift-001",
            account_id="drift-acct",
            region="us-east-1",
            app_name="drift-svc",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="sweep-alarm",
            state=IncidentState.TRIGGERED,
            created_at=clock(),
            updated_at=clock(),
        )
        hub_state.update_app(inc_open)

        # Artificially inflate the cached tile count to 5 (simulating drift).
        with hub_state.lock:
            key = list(hub_state._tiles.keys())[0]
            original = hub_state._tiles[key]
            drifted = FleetTile(
                account_id=original.account_id,
                app_name=original.app_name,
                environment=original.environment,
                deployment_id=original.deployment_id,
                service_path=original.service_path,
                org_path=original.org_path,
                metadata=original.metadata,
                on_call=original.on_call,
                status="red",
                liveness=original.liveness,
                open_incidents=5,
                worst_severity=Severity.SEV2,
                last_heartbeat_at=original.last_heartbeat_at,
                registered_at=original.registered_at,
                last_updated=clock(),
            )
            hub_state._tiles[key] = drifted

        # Verify the inflation is in place.
        assert hub_state.cached_tile(key).open_incidents == 5

        # Build a fake incident store that reports only the one real open incident.
        incident_store_fake = _FlowFakeIncidentStore([inc_open])

        pub = SSEPublisher()
        q = pub.subscribe()
        shutdown = threading.Event()

        sweep = SweepTimer(
            hub_state=hub_state,
            sse_publisher=pub,
            shutdown_event=shutdown,
            sweep_interval=0,
            ping_interval=9999,
            incident_store=incident_store_fake,
        )

        # Run one sweep — reconciliation must fire.
        sweep._do_sweep()

        # After sweep the tile count must match the actual open incident count.
        tile_after = hub_state.cached_tile(key)
        assert tile_after is not None
        assert tile_after.open_incidents == 1

        # An SSE delta must have been emitted for the corrected tile.
        assert not q.empty()
        msg = q.get_nowait()
        assert "delta" in msg
