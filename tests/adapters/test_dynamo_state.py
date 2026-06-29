"""Tests for DynamoEscalationStateStore, DynamoIncidentStore (get/put/append).

Uses moto to mock DynamoDB — no real AWS calls are made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest

from relay.adapters.aws.dynamo_stores import (
    DynamoContactStore,
    DynamoDeadlineTimer,
    DynamoEscalationStateStore,
    DynamoIncidentStore,
)
from relay.core.escalation import EscalationContext, EscalationPhase
from relay.core.model import (
    Contact,
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

# ---------------------------------------------------------------------------
# moto fixture — create a mocked DynamoDB table once per module
# ---------------------------------------------------------------------------

TABLE_NAME = "relay-test"

# Incident-listing GSIs, mirroring infra/stacks/data_stack.py so moto Queries
# resolve against real indices. Shared with other moto fixtures via import.
INCIDENT_GSIS = [
    {
        "IndexName": "incident-status-index",
        "KeySchema": [
            {"AttributeName": "gsi_open_pk", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": "incident-all-index",
        "KeySchema": [
            {"AttributeName": "gsi_all_pk", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
]


@pytest.fixture(scope="module")
def dynamo_table():
    """Create a mocked DynamoDB table using moto and return the boto3 Session."""
    from moto import mock_aws

    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        table = ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi_open_pk", "AttributeType": "S"},
                {"AttributeName": "gsi_all_pk", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=INCIDENT_GSIS,
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _make_session_store(session):
    """Return a DynamoEscalationStateStore backed by the mocked session."""
    return DynamoEscalationStateStore(table_name=TABLE_NAME, boto3_session=session)


def _make_incident_store(session):
    return DynamoIncidentStore(table_name=TABLE_NAME, boto3_session=session)


def _minimal_incident(correlation_id: str = "inc-test-001") -> Incident:
    now = datetime.now(_UTC)
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="testapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="testapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# EscalationStateStore tests
# ---------------------------------------------------------------------------


class TestDynamoEscalationStateStore:
    """Round-trip and edge-case tests for DynamoEscalationStateStore."""

    def test_load_missing_returns_none(self, dynamo_table):
        store = _make_session_store(dynamo_table)
        result = store.load("nonexistent-incident-id")
        assert result is None

    def test_save_and_load_round_trip_basic(self, dynamo_table):
        """save() then load() returns equal field values."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-rt-001",
            policy_id="policy-alpha",
            current_step_index=0,
            phase=EscalationPhase.WAITING_ACK,
        )
        store.save(ctx)
        loaded = store.load("inc-rt-001")

        assert loaded is not None
        assert loaded.incident_id == ctx.incident_id
        assert loaded.policy_id == ctx.policy_id
        assert loaded.current_step_index == ctx.current_step_index
        assert loaded.phase == EscalationPhase.WAITING_ACK

    def test_phase_is_deserialized_as_enum(self, dynamo_table):
        """phase comes back as EscalationPhase, not a bare string."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-enum-001",
            policy_id="policy-beta",
            phase=EscalationPhase.ESCALATING,
        )
        store.save(ctx)
        loaded = store.load("inc-enum-001")

        assert loaded is not None
        assert loaded.phase is EscalationPhase.ESCALATING
        assert isinstance(loaded.phase, EscalationPhase)

    def test_datetimes_round_trip_with_timezone(self, dynamo_table):
        """Datetime fields survive serialisation as timezone-aware datetimes."""
        store = _make_session_store(dynamo_table)
        paged = datetime(2026, 1, 15, 10, 0, 0, tzinfo=_UTC)
        last_esc = datetime(2026, 1, 15, 10, 15, 0, tzinfo=_UTC)
        ack_ts = datetime(2026, 1, 15, 10, 20, 0, tzinfo=_UTC)

        ctx = EscalationContext(
            incident_id="inc-dt-001",
            policy_id="policy-gamma",
            phase=EscalationPhase.ACKNOWLEDGED,
            paged_at=paged,
            last_escalated_at=last_esc,
            ack_by="contact-xyz",
            ack_at=ack_ts,
        )
        store.save(ctx)
        loaded = store.load("inc-dt-001")

        assert loaded is not None
        assert loaded.paged_at == paged
        assert loaded.last_escalated_at == last_esc
        assert loaded.ack_by == "contact-xyz"
        assert loaded.ack_at == ack_ts
        # Must come back timezone-aware
        assert loaded.paged_at.tzinfo is not None
        assert loaded.ack_at.tzinfo is not None

    def test_none_datetimes_remain_none(self, dynamo_table):
        """Fields that were None before save come back as None after load."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-none-001",
            policy_id="policy-delta",
            phase=EscalationPhase.IDLE,
        )
        store.save(ctx)
        loaded = store.load("inc-none-001")

        assert loaded is not None
        assert loaded.paged_at is None
        assert loaded.last_escalated_at is None
        assert loaded.ack_by is None
        assert loaded.ack_at is None

    def test_timer_handle_persisted_and_loaded(self, dynamo_table):
        """_timer_handle is stored and returned correctly."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-timer-001",
            policy_id="policy-epsilon",
            phase=EscalationPhase.WAITING_ACK,
        )
        ctx._timer_handle = "relay-esc-inc-timer-001-0"
        store.save(ctx)
        loaded = store.load("inc-timer-001")

        assert loaded is not None
        assert loaded._timer_handle == "relay-esc-inc-timer-001-0"

    def test_timer_handle_none_survives_round_trip(self, dynamo_table):
        """_timer_handle=None comes back as None (not missing key error)."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-timer-none-001",
            policy_id="policy-zeta",
            phase=EscalationPhase.IDLE,
        )
        ctx._timer_handle = None
        store.save(ctx)
        loaded = store.load("inc-timer-none-001")

        assert loaded is not None
        assert loaded._timer_handle is None

    def test_save_overwrites_existing_context(self, dynamo_table):
        """Saving twice updates the stored context (upsert semantics)."""
        store = _make_session_store(dynamo_table)
        ctx = EscalationContext(
            incident_id="inc-overwrite-001",
            policy_id="policy-eta",
            current_step_index=0,
            phase=EscalationPhase.WAITING_ACK,
        )
        store.save(ctx)

        ctx.phase = EscalationPhase.ESCALATING
        ctx.current_step_index = 1
        store.save(ctx)

        loaded = store.load("inc-overwrite-001")
        assert loaded is not None
        assert loaded.phase == EscalationPhase.ESCALATING
        assert loaded.current_step_index == 1

    def test_all_phase_values_round_trip(self, dynamo_table):
        """All EscalationPhase enum values survive serialisation."""
        store = _make_session_store(dynamo_table)
        for i, phase in enumerate(EscalationPhase):
            ctx = EscalationContext(
                incident_id=f"inc-phase-{i}",
                policy_id="policy-phases",
                phase=phase,
            )
            store.save(ctx)
            loaded = store.load(f"inc-phase-{i}")
            assert loaded is not None
            assert loaded.phase == phase


# ---------------------------------------------------------------------------
# IncidentStore tests
# ---------------------------------------------------------------------------


class TestDynamoIncidentStore:
    """Tests for DynamoIncidentStore.get_incident, put_incident, append_timeline_event."""

    def test_get_incident_missing_returns_none(self, dynamo_table):
        store = _make_incident_store(dynamo_table)
        result = store.get_incident("nonexistent-correlation-id")
        assert result is None

    def test_put_and_get_round_trip(self, dynamo_table):
        """put_incident then get_incident returns the same incident."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-put-get-001")
        store.put_incident(incident)

        loaded = store.get_incident("inc-put-get-001")
        assert loaded is not None
        assert loaded.correlation_id == "inc-put-get-001"
        assert loaded.account_id == "123456789012"
        assert loaded.severity == Severity.SEV2
        assert loaded.signal_source == SignalSource.CLOUDWATCH_ALARM
        assert loaded.state == IncidentState.TRIGGERED

    def test_put_incident_overwrites(self, dynamo_table):
        """Calling put_incident twice updates the record (upsert)."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-overwrite-inc-001")
        store.put_incident(incident)

        incident.state = IncidentState.ACKNOWLEDGED
        store.put_incident(incident)

        loaded = store.get_incident("inc-overwrite-inc-001")
        assert loaded is not None
        assert loaded.state == IncidentState.ACKNOWLEDGED

    def test_append_timeline_event_appends(self, dynamo_table):
        """append_timeline_event adds an event to the timeline atomically."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-timeline-001")
        store.put_incident(incident)

        event = TimelineEvent(
            incident_id="inc-timeline-001",
            stream=Stream.TEAM,
            actor="system",
            event_type="incident.triggered",
            detail={"alarm_name": "testapp-high-error-rate"},
        )
        store.append_timeline_event("inc-timeline-001", event)

        loaded = store.get_incident("inc-timeline-001")
        assert loaded is not None
        assert len(loaded.timeline) == 1
        assert loaded.timeline[0].event_type == "incident.triggered"
        assert loaded.timeline[0].actor == "system"

    def test_append_timeline_event_multiple(self, dynamo_table):
        """Multiple appends accumulate events in order."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-timeline-multi-001")
        store.put_incident(incident)

        for i in range(3):
            ev = TimelineEvent(
                incident_id="inc-timeline-multi-001",
                stream=Stream.TEAM,
                actor="system",
                event_type=f"step.{i}",
            )
            store.append_timeline_event("inc-timeline-multi-001", ev)

        loaded = store.get_incident("inc-timeline-multi-001")
        assert loaded is not None
        assert len(loaded.timeline) == 3
        assert loaded.timeline[0].event_type == "step.0"
        assert loaded.timeline[2].event_type == "step.2"

    def test_append_creates_timeline_when_absent(self, dynamo_table):
        """append_timeline_event works even if 'timeline' key doesn't exist yet."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-timeline-fresh-001")
        # Put incident with empty timeline, then simulate the key being absent
        # by using an item that was put without the timeline field.
        store.put_incident(incident)

        # Now directly append without having put any timeline before.
        ev = TimelineEvent(
            incident_id="inc-timeline-fresh-001",
            stream=Stream.CENTRAL,
            actor="contact-001",
            event_type="incident.acknowledged",
        )
        store.append_timeline_event("inc-timeline-fresh-001", ev)

        loaded = store.get_incident("inc-timeline-fresh-001")
        assert loaded is not None
        # The existing timeline from put_incident is [] and the append adds 1.
        assert any(e.event_type == "incident.acknowledged" for e in loaded.timeline)

    def test_external_tickets_round_trip(self, dynamo_table):
        """external_tickets persists and reloads as a flat attribute map."""
        store = _make_incident_store(dynamo_table)
        incident = _minimal_incident("inc-xt-rt-001")
        incident.set_ticket("gitlab_project", "team/proj")
        incident.set_ticket("gitlab_iid", "42")
        store.put_incident(incident)

        loaded = store.get_incident("inc-xt-rt-001")
        assert loaded is not None
        assert loaded.get_ticket("gitlab_iid") == "42"
        assert loaded.get_ticket("gitlab_project") == "team/proj"


# ---------------------------------------------------------------------------
# Purge tests
# ---------------------------------------------------------------------------


def _make_deadline_timer(session):
    return DynamoDeadlineTimer(table_name=TABLE_NAME, boto3_session=session)


def _incident_at(correlation_id: str, dt: datetime, synthetic: bool = False) -> Incident:
    """Create a minimal incident with a specific created_at."""
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="testapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="testapp-alarm",
        created_at=dt,
        updated_at=dt,
        synthetic=synthetic,
    )


class TestPurgeIncidents:
    """Tests for DynamoIncidentStore.purge_incidents."""

    def test_purge_before_deletes_only_older(self, dynamo_table):
        """purge with before= removes incidents created <= that time, leaves newer."""
        store = _make_incident_store(dynamo_table)

        cutoff = datetime(2026, 1, 10, 12, 0, 0, tzinfo=_UTC)
        old = _incident_at("purge-before-old-001", datetime(2026, 1, 9, 0, 0, 0, tzinfo=_UTC))
        new = _incident_at("purge-before-new-001", datetime(2026, 1, 11, 0, 0, 0, tzinfo=_UTC))
        store.put_incident(old)
        store.put_incident(new)

        result = store.purge_incidents(before=cutoff)

        assert result["matched"] >= 1
        assert result["deleted"] == result["matched"]
        assert result["dry_run"] is False
        assert store.get_incident("purge-before-old-001") is None
        assert store.get_incident("purge-before-new-001") is not None

    def test_purge_after_deletes_only_newer(self, dynamo_table):
        """purge with after= removes incidents created >= that time, leaves older."""
        store = _make_incident_store(dynamo_table)

        cutoff = datetime(2026, 2, 10, 12, 0, 0, tzinfo=_UTC)
        old = _incident_at("purge-after-old-001", datetime(2026, 2, 9, 0, 0, 0, tzinfo=_UTC))
        new = _incident_at("purge-after-new-001", datetime(2026, 2, 11, 0, 0, 0, tzinfo=_UTC))
        store.put_incident(old)
        store.put_incident(new)

        result = store.purge_incidents(after=cutoff)

        assert result["matched"] >= 1
        assert result["deleted"] == result["matched"]
        assert store.get_incident("purge-after-new-001") is None
        assert store.get_incident("purge-after-old-001") is not None

    def test_purge_range_deletes_within_bounds(self, dynamo_table):
        """purge with both before= and after= deletes only incidents in [after, before]."""
        store = _make_incident_store(dynamo_table)

        after_dt = datetime(2026, 3, 5, 0, 0, 0, tzinfo=_UTC)
        before_dt = datetime(2026, 3, 15, 0, 0, 0, tzinfo=_UTC)

        too_old = _incident_at("purge-range-too-old-001", datetime(2026, 3, 1, 0, 0, 0, tzinfo=_UTC))
        in_range = _incident_at("purge-range-in-001", datetime(2026, 3, 10, 0, 0, 0, tzinfo=_UTC))
        too_new = _incident_at("purge-range-too-new-001", datetime(2026, 3, 20, 0, 0, 0, tzinfo=_UTC))
        store.put_incident(too_old)
        store.put_incident(in_range)
        store.put_incident(too_new)

        result = store.purge_incidents(after=after_dt, before=before_dt)

        assert result["matched"] >= 1
        assert store.get_incident("purge-range-in-001") is None
        assert store.get_incident("purge-range-too-old-001") is not None
        assert store.get_incident("purge-range-too-new-001") is not None

    def test_purge_inverted_range_returns_zero(self, dynamo_table):
        """An inverted range (after > before) returns zero without deleting anything."""
        store = _make_incident_store(dynamo_table)
        incident = _incident_at("purge-invert-001", datetime(2026, 4, 10, 0, 0, 0, tzinfo=_UTC))
        store.put_incident(incident)

        result = store.purge_incidents(
            after=datetime(2026, 4, 20, 0, 0, 0, tzinfo=_UTC),
            before=datetime(2026, 4, 1, 0, 0, 0, tzinfo=_UTC),
        )

        assert result["matched"] == 0
        assert result["deleted"] == 0
        assert store.get_incident("purge-invert-001") is not None

    def test_synthetic_only_deletes_only_synthetic(self, dynamo_table):
        """synthetic_only=True only removes incidents where synthetic=True."""
        store = _make_incident_store(dynamo_table)

        real = _incident_at("purge-synth-real-001", datetime(2026, 5, 1, 0, 0, 0, tzinfo=_UTC), synthetic=False)
        fake = _incident_at("purge-synth-fake-001", datetime(2026, 5, 1, 0, 0, 0, tzinfo=_UTC), synthetic=True)
        store.put_incident(real)
        store.put_incident(fake)

        result = store.purge_incidents(synthetic_only=True)

        assert result["matched"] >= 1
        assert result["synthetic"] == result["matched"]
        assert store.get_incident("purge-synth-fake-001") is None
        assert store.get_incident("purge-synth-real-001") is not None

    def test_dry_run_counts_but_does_not_delete(self, dynamo_table):
        """dry_run=True returns matched count but leaves all incidents intact."""
        store = _make_incident_store(dynamo_table)

        ts = datetime(2026, 6, 1, 0, 0, 0, tzinfo=_UTC)
        inc = _incident_at("purge-dryrun-001", ts)
        store.put_incident(inc)

        result = store.purge_incidents(
            before=ts + timedelta(hours=1),
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["deleted"] == 0
        assert result["companions_deleted"] == 0
        assert result["matched"] >= 1
        # Incident must still be present.
        assert store.get_incident("purge-dryrun-001") is not None

    def test_purge_reports_affected_tiles(self, dynamo_table):
        """purge_incidents reports the distinct fleet-tile keys it touched so the
        caller can recompute those FLEET# aggregates (issue #30)."""
        store = _make_incident_store(dynamo_table)

        ts = datetime(2026, 8, 1, 0, 0, 0, tzinfo=_UTC)
        # Two incidents on the same app (one tile) + one on a different app.
        inc_a1 = _incident_at("purge-tiles-a1", ts, synthetic=True)
        inc_a2 = _incident_at("purge-tiles-a2", ts, synthetic=True)
        inc_b = Incident(
            correlation_id="purge-tiles-b",
            account_id="123456789012",
            region="us-east-1",
            app_name="otherapp",
            severity=Severity.SEV3,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="otherapp-alarm",
            created_at=ts,
            updated_at=ts,
            synthetic=True,
        )
        store.put_incident(inc_a1)
        store.put_incident(inc_a2)
        store.put_incident(inc_b)

        result = store.purge_incidents(synthetic_only=True)

        tiles = result["affected_tiles"]
        # Deduped: testapp appears once despite two incidents.
        apps = sorted(t["app_name"] for t in tiles)
        assert apps == ["otherapp", "testapp"]
        for t in tiles:
            assert set(t) == {"account_id", "app_name", "environment", "deployment_id"}

    def test_purge_dry_run_still_reports_affected_tiles(self, dynamo_table):
        """A dry-run preview still reports which tiles would shift."""
        store = _make_incident_store(dynamo_table)
        ts = datetime(2026, 8, 2, 0, 0, 0, tzinfo=_UTC)
        store.put_incident(_incident_at("purge-tiles-dry", ts, synthetic=True))

        result = store.purge_incidents(synthetic_only=True, dry_run=True)

        assert result["dry_run"] is True
        assert result["deleted"] == 0
        assert any(t["app_name"] == "testapp" for t in result["affected_tiles"])

    def test_cascade_deletes_esc_rows(self, dynamo_table):
        """Purging an incident also removes its ESC#/STATE and ESC#/DEADLINE rows."""
        store = _make_incident_store(dynamo_table)
        esc_store = _make_session_store(dynamo_table)
        timer = _make_deadline_timer(dynamo_table)

        ts = datetime(2026, 7, 1, 0, 0, 0, tzinfo=_UTC)
        incident = _incident_at("purge-cascade-001", ts)
        store.put_incident(incident)

        # Write companion ESC rows.
        ctx = EscalationContext(
            incident_id="purge-cascade-001",
            policy_id="policy-x",
            phase=EscalationPhase.WAITING_ACK,
        )
        esc_store.save(ctx)
        # Write a deadline row via DynamoDeadlineTimer.
        timer.schedule_timeout("purge-cascade-001", step_index=0, delay_minutes=60)

        # Verify companion rows exist before purge.
        assert esc_store.load("purge-cascade-001") is not None

        result = store.purge_incidents(before=ts + timedelta(seconds=1))

        assert result["deleted"] >= 1
        assert result["companions_deleted"] >= 2  # STATE + DEADLINE
        # Incident gone.
        assert store.get_incident("purge-cascade-001") is None
        # ESC/STATE gone.
        assert esc_store.load("purge-cascade-001") is None
        # ESC/DEADLINE gone — verify by checking no DEADLINE row exists.
        # dynamo_table fixture yields the boto3 Session; use it to access the table directly.
        ddb_table = dynamo_table.resource("dynamodb").Table(TABLE_NAME)
        deadline_item = ddb_table.get_item(
            Key={"pk": "ESC#purge-cascade-001", "sk": "DEADLINE"}
        ).get("Item")
        assert deadline_item is None


# ---------------------------------------------------------------------------
# Incident listing (GSI-backed) — isolated per-test table so the module-scoped
# table's accumulated incidents don't pollute these assertions.
# ---------------------------------------------------------------------------

ISO_TABLE = "relay-listing-test"


@pytest.fixture
def listing_session():
    """A fresh moto table (with the incident GSIs) per test for clean listings."""
    from moto import mock_aws

    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        table = ddb.create_table(
            TableName=ISO_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi_open_pk", "AttributeType": "S"},
                {"AttributeName": "gsi_all_pk", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=INCIDENT_GSIS,
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        try:
            yield session
        finally:
            # The module-scoped dynamo_table fixture holds an outer mock_aws open,
            # so this nested mock shares its backend and the table would otherwise
            # leak into the next test. Drop it explicitly.
            table.delete()


def _listing_store(session):
    return DynamoIncidentStore(table_name=ISO_TABLE, boto3_session=session)


def _incident_state(correlation_id, dt, state, account_id="123456789012", synthetic=False):
    inc = _incident_at(correlation_id, dt, synthetic=synthetic)
    inc.account_id = account_id
    inc.state = state
    return inc


class TestListOpenIncidents:
    """list_open_incidents queries the sparse open GSI (US1)."""

    def test_returns_only_open_newest_first(self, listing_session):
        store = _listing_store(listing_session)
        base = datetime(2026, 5, 1, tzinfo=_UTC)
        # Three open (varying states + times), two terminal — terminal must be absent.
        store.put_incident(_incident_state("o1", base, IncidentState.TRIGGERED))
        store.put_incident(
            _incident_state("o2", base + timedelta(hours=2), IncidentState.ACKNOWLEDGED)
        )
        store.put_incident(
            _incident_state("o3", base + timedelta(hours=1), IncidentState.ESCALATED)
        )
        store.put_incident(
            _incident_state("t1", base + timedelta(hours=3), IncidentState.RESOLVED)
        )
        store.put_incident(
            _incident_state("t2", base + timedelta(hours=4), IncidentState.CLOSED)
        )
        # A non-incident row must never be read by the open query.
        DynamoContactStore(table_name=ISO_TABLE, boto3_session=listing_session).put_contact(
            Contact(contact_id="c1", name="Pat", email="pat@example.com")
        )

        result = store.list_open_incidents()
        ids = [i.correlation_id for i in result]
        assert ids == ["o2", "o3", "o1"]  # newest-first by created_at

    def test_account_filter(self, listing_session):
        store = _listing_store(listing_session)
        base = datetime(2026, 5, 2, tzinfo=_UTC)
        store.put_incident(_incident_state("a1", base, IncidentState.TRIGGERED, account_id="111"))
        store.put_incident(
            _incident_state("a2", base + timedelta(hours=1), IncidentState.TRIGGERED, account_id="222")
        )
        result = store.list_open_incidents(account_id="222")
        assert [i.correlation_id for i in result] == ["a2"]

    def test_empty_returns_empty(self, listing_session):
        store = _listing_store(listing_session)
        assert store.list_open_incidents() == []

    def test_resolve_evicts_from_open_index(self, listing_session):
        """Sparse eviction: re-putting an incident as RESOLVED drops it from the
        open index with no explicit REMOVE (FR-007)."""
        store = _listing_store(listing_session)
        inc = _incident_state("evict-1", datetime(2026, 5, 3, tzinfo=_UTC), IncidentState.TRIGGERED)
        store.put_incident(inc)
        assert [i.correlation_id for i in store.list_open_incidents()] == ["evict-1"]

        inc.state = IncidentState.RESOLVED
        store.put_incident(inc)
        assert store.list_open_incidents() == []
        # Still present in the all-index.
        assert [i.correlation_id for i in store.list_incidents()] == ["evict-1"]

    def test_pagination_returns_full_set(self, listing_session, monkeypatch):
        """A result set spanning multiple query pages returns in full (FR-003)."""
        store = _listing_store(listing_session)
        base = datetime(2026, 5, 4, tzinfo=_UTC)
        for n in range(25):
            store.put_incident(
                _incident_state(f"pg-{n:02d}", base + timedelta(minutes=n), IncidentState.TRIGGERED)
            )
        # Force tiny pages so the LastEvaluatedKey loop is exercised.
        real_query = store._table.query

        def paged_query(**kwargs):
            kwargs["Limit"] = 5
            return real_query(**kwargs)

        monkeypatch.setattr(store._table, "query", paged_query)
        result = store.list_open_incidents()
        assert len(result) == 25
        assert len({i.correlation_id for i in result}) == 25


class TestListIncidents:
    """list_incidents queries the all GSI (US2)."""

    def test_returns_open_and_terminal_excludes_non_incident(self, listing_session):
        store = _listing_store(listing_session)
        base = datetime(2026, 6, 1, tzinfo=_UTC)
        store.put_incident(_incident_state("all-open", base, IncidentState.TRIGGERED))
        store.put_incident(
            _incident_state("all-term", base + timedelta(hours=1), IncidentState.CLOSED)
        )
        DynamoContactStore(table_name=ISO_TABLE, boto3_session=listing_session).put_contact(
            Contact(contact_id="c2", name="Sam", email="sam@example.com")
        )
        ids = {i.correlation_id for i in store.list_incidents()}
        assert ids == {"all-open", "all-term"}

    def test_includes_synthetic(self, listing_session):
        store = _listing_store(listing_session)
        base = datetime(2026, 6, 2, tzinfo=_UTC)
        store.put_incident(
            _incident_state("syn-1", base, IncidentState.TRIGGERED, synthetic=True)
        )
        result = store.list_incidents()
        assert [i.correlation_id for i in result] == ["syn-1"]
        assert result[0].synthetic is True

    def test_pagination_returns_full_set(self, listing_session, monkeypatch):
        store = _listing_store(listing_session)
        base = datetime(2026, 6, 3, tzinfo=_UTC)
        for n in range(20):
            state = IncidentState.TRIGGERED if n % 2 else IncidentState.CLOSED
            store.put_incident(_incident_state(f"a-{n:02d}", base + timedelta(minutes=n), state))
        real_query = store._table.query

        def paged_query(**kwargs):
            kwargs["Limit"] = 4
            return real_query(**kwargs)

        monkeypatch.setattr(store._table, "query", paged_query)
        assert len(store.list_incidents()) == 20
