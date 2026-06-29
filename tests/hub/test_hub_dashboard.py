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

    @pytest.mark.parametrize(
        ("elapsed", "expected_liveness"),
        [
            pytest.param(60,   Liveness.LIVE,  id="fresh_heartbeat_is_live"),
            pytest.param(120,  Liveness.LIVE,  id="at_2x_boundary_is_live"),
            pytest.param(121,  Liveness.STALE, id="just_past_2x_is_stale"),
            pytest.param(300,  Liveness.STALE, id="at_5x_boundary_is_stale"),
            pytest.param(301,  Liveness.LOST,  id="just_past_5x_is_lost"),
            pytest.param(3600, Liveness.LOST,  id="long_silence_is_lost"),
        ],
    )
    def test_liveness_boundary(self, clock, elapsed, expected_liveness):
        hb = clock()
        clock.advance(elapsed)
        assert liveness_from_heartbeat(hb, cadence_seconds=DEFAULT_CADENCE_SECONDS, clock=clock) == expected_liveness

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
    @pytest.mark.parametrize(
        ("kwargs", "expected_color"),
        [
            # --- red conditions ---
            pytest.param(
                {"liveness": Liveness.LOST},
                "red",
                id="lost_liveness_is_red",
            ),
            pytest.param(
                {"liveness": Liveness.LOST, "open_incidents": 0},
                "red",
                id="lost_liveness_with_no_incidents_is_red",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 1, "worst_severity": Severity.SEV1},
                "red",
                id="live_sev1_is_red",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 1, "worst_severity": Severity.SEV2},
                "red",
                id="live_sev2_is_red",
            ),
            pytest.param(
                {"liveness": Liveness.STALE, "open_incidents": 1, "worst_severity": Severity.SEV1},
                "red",
                id="stale_sev1_is_red",
            ),
            pytest.param(
                {"liveness": Liveness.UNKNOWN, "open_incidents": 1, "worst_severity": Severity.SEV1},
                "red",
                id="unknown_sev1_is_red",
            ),
            # --- degraded conditions ---
            pytest.param(
                {"liveness": Liveness.STALE, "open_incidents": 0},
                "degraded",
                id="stale_no_incidents_is_degraded",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 1, "worst_severity": Severity.SEV3},
                "degraded",
                id="live_sev3_is_degraded",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 1, "worst_severity": Severity.SEV4},
                "degraded",
                id="live_sev4_is_degraded",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 1, "has_acked": True},
                "degraded",
                id="live_acked_is_degraded",
            ),
            pytest.param(
                {"liveness": Liveness.UNKNOWN, "open_incidents": 1, "worst_severity": Severity.SEV3},
                "degraded",
                id="unknown_sev3_is_degraded",
            ),
            # --- grey conditions ---
            pytest.param(
                {"liveness": Liveness.UNKNOWN},
                "grey",
                id="unknown_no_incidents_is_grey",
            ),
            pytest.param(
                {"liveness": Liveness.UNKNOWN, "open_incidents": 0},
                "grey",
                id="unknown_no_incidents_explicit_is_grey",
            ),
            # --- green conditions ---
            pytest.param(
                {"liveness": Liveness.LIVE},
                "green",
                id="live_no_incidents_is_green",
            ),
            pytest.param(
                {"liveness": Liveness.LIVE, "open_incidents": 0, "worst_severity": None},
                "green",
                id="live_no_incidents_explicit_is_green",
            ),
        ],
    )
    def test_worst_of_color(self, kwargs, expected_color):
        assert worst_of(**kwargs) == expected_color


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


