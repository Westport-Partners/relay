"""Tests for the DynamoDB-deadline escalation timer (collapse Step 2).

Covers:
  1. DynamoDeadlineTimer — schedule writes a PENDING deadline row; cancel
     deletes it; query_due_deadlines respects fire_at + status; claim_deadline
     is a one-shot PENDING→FIRED guard.
  2. EscalationEngine wired with DynamoDeadlineTimer — start() then a sweep at a
     future clock advances to the next step.
  3. DeadlineSweeper.sweep_once — claims + fires due deadlines, isolates errors.

Uses moto to mock DynamoDB — no real AWS calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest

from relay.adapters.aws.dynamo_stores import (
    DynamoDeadlineTimer,
    DynamoEscalationStateStore,
)

TABLE_NAME = "relay-deadline-test"


@pytest.fixture
def dynamo_table():
    """Create a fresh mocked DynamoDB table per test."""
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
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield session


def _timer(session, clock=None) -> DynamoDeadlineTimer:
    return DynamoDeadlineTimer(
        table_name=TABLE_NAME, boto3_session=session, clock=clock
    )


# ---------------------------------------------------------------------------
# DynamoDeadlineTimer — schedule / cancel / query / claim
# ---------------------------------------------------------------------------


class TestDynamoDeadlineTimer:
    def test_schedule_writes_pending_deadline_row(self, dynamo_table):
        timer = _timer(dynamo_table)
        handle = timer.schedule_timeout("inc-1", step_index=0, delay_minutes=5)
        assert handle == "inc-1"

        item = (
            dynamo_table.resource("dynamodb")
            .Table(TABLE_NAME)
            .get_item(Key={"pk": "ESC#inc-1", "sk": "DEADLINE"})["Item"]
        )
        assert item["status"] == "PENDING"
        assert int(item["step_index"]) == 0
        assert item["incident_id"] == "inc-1"
        assert item["fire_at"]  # ISO string present

    def test_cancel_deletes_deadline_row(self, dynamo_table):
        timer = _timer(dynamo_table)
        timer.schedule_timeout("inc-1", step_index=0, delay_minutes=5)
        timer.cancel_timeout("inc-1")
        resp = (
            dynamo_table.resource("dynamodb")
            .Table(TABLE_NAME)
            .get_item(Key={"pk": "ESC#inc-1", "sk": "DEADLINE"})
        )
        assert "Item" not in resp

    def test_cancel_missing_handle_is_noop(self, dynamo_table):
        timer = _timer(dynamo_table)
        timer.cancel_timeout("never-existed")  # must not raise
        timer.cancel_timeout("")  # empty handle

    def test_query_due_returns_only_past_pending(self, dynamo_table):
        base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
        # Schedule with the clock fixed at base: due deadline fires +1m, future +10m.
        timer = _timer(dynamo_table, clock=lambda: base)
        timer.schedule_timeout("inc-due", step_index=0, delay_minutes=1)
        timer.schedule_timeout("inc-future", step_index=0, delay_minutes=10)

        # Now is base + 5m: inc-due is past, inc-future is not.
        now = base + timedelta(minutes=5)
        due = timer.query_due_deadlines(now=now)
        ids = {d.incident_id for d in due}
        assert ids == {"inc-due"}
        assert due[0].step_index == 0

    def test_query_due_uses_injected_clock_by_default(self, dynamo_table):
        base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
        timer = _timer(dynamo_table, clock=lambda: base)
        timer.schedule_timeout("inc-due", step_index=2, delay_minutes=1)
        # Advance the clock past fire_at; no explicit `now`.
        timer._clock = lambda: base + timedelta(minutes=2)
        due = timer.query_due_deadlines()
        assert [(d.incident_id, d.step_index) for d in due] == [("inc-due", 2)]

    def test_claim_is_one_shot(self, dynamo_table):
        timer = _timer(dynamo_table)
        timer.schedule_timeout("inc-1", step_index=0, delay_minutes=1)
        assert timer.claim_deadline("inc-1", 0) is True
        # Second claim fails — already FIRED.
        assert timer.claim_deadline("inc-1", 0) is False

    def test_claim_fails_on_step_mismatch(self, dynamo_table):
        timer = _timer(dynamo_table)
        timer.schedule_timeout("inc-1", step_index=1, delay_minutes=1)
        # Stale claim for an old step index must not win.
        assert timer.claim_deadline("inc-1", 0) is False

    def test_fired_deadline_not_returned_as_due(self, dynamo_table):
        base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
        timer = _timer(dynamo_table, clock=lambda: base)
        timer.schedule_timeout("inc-1", step_index=0, delay_minutes=1)
        timer.claim_deadline("inc-1", 0)
        due = timer.query_due_deadlines(now=base + timedelta(minutes=5))
        assert due == []

    def test_advance_overwrites_deadline(self, dynamo_table):
        """A second schedule_timeout for the next step supersedes the first."""
        base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
        timer = _timer(dynamo_table, clock=lambda: base)
        timer.schedule_timeout("inc-1", step_index=0, delay_minutes=1)
        timer.schedule_timeout("inc-1", step_index=1, delay_minutes=10)
        due = timer.query_due_deadlines(now=base + timedelta(minutes=5))
        # step 0 deadline is gone; step 1 deadline is in the future → nothing due.
        assert due == []


# ---------------------------------------------------------------------------
# EscalationEngine + DynamoDeadlineTimer — round trip
# ---------------------------------------------------------------------------


def test_engine_with_deadline_timer_advances_on_sweep(dynamo_table):
    from relay.core.escalation import EscalationEngine, EscalationPhase
    from relay.core.model import EscalationPolicy, EscalationStep
    from tests.test_dynamo_state import _minimal_incident  # reuse helper

    base = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)
    timer = _timer(dynamo_table, clock=lambda: base)
    state_store = DynamoEscalationStateStore(
        table_name=TABLE_NAME, boto3_session=dynamo_table
    )
    engine = EscalationEngine(timer=timer, state_store=state_store)

    policy = EscalationPolicy(
        policy_id="pol-1",
        name="default",
        team="team-1",
        steps=[
            EscalationStep(step_index=0, contact_ids=["a"], timeout_minutes=5),
            EscalationStep(step_index=1, contact_ids=["b"], timeout_minutes=5),
        ],
    )
    incident = _minimal_incident("inc-esc-1")
    engine.start(incident, policy)

    # Deadline for step 0 is due 5m later.
    now = base + timedelta(minutes=6)
    due = timer.query_due_deadlines(now=now)
    assert [(d.incident_id, d.step_index) for d in due] == [("inc-esc-1", 0)]
    assert timer.claim_deadline("inc-esc-1", 0) is True

    transition = engine.on_timeout("inc-esc-1", 0, policy)
    assert transition.new_phase == EscalationPhase.ESCALATING
    assert transition.contact_ids_to_page == ["b"]

    # A fresh deadline for step 1 now exists (the prior FIRED row was overwritten).
    later = now + timedelta(minutes=6)
    due2 = timer.query_due_deadlines(now=later)
    assert [(d.incident_id, d.step_index) for d in due2] == [("inc-esc-1", 1)]


# ---------------------------------------------------------------------------
# DeadlineSweeper — claims + fires, isolates failures
# ---------------------------------------------------------------------------


class _FakeTimer:
    def __init__(self, due):
        self._due = list(due)
        self.claimed: list[tuple[Any, ...]] = []
        self._claim_returns: dict[tuple[Any, ...], bool] = {}

    def set_claim(self, key, value):
        self._claim_returns[key] = value

    def query_due_deadlines(self, now=None):
        return list(self._due)

    def claim_deadline(self, incident_id, step_index):
        self.claimed.append((incident_id, step_index))
        return self._claim_returns.get((incident_id, step_index), True)


def _due(incident_id, step_index):
    from relay.adapters.aws.dynamo_stores import DueDeadline

    return DueDeadline(incident_id=incident_id, step_index=step_index)


def test_sweeper_fires_claimed_deadlines():
    from relay.hub.app import DeadlineSweeper

    timer = _FakeTimer([_due("inc-1", 0), _due("inc-2", 1)])
    fired: list[tuple[Any, ...]] = []
    sweeper = DeadlineSweeper(timer=timer, fire=lambda i, s: fired.append((i, s)))

    count = sweeper.sweep_once()
    assert count == 2
    assert fired == [("inc-1", 0), ("inc-2", 1)]


def test_sweeper_skips_unclaimed():
    from relay.hub.app import DeadlineSweeper

    timer = _FakeTimer([_due("inc-1", 0)])
    timer.set_claim(("inc-1", 0), False)  # lost the race
    fired: list[tuple[Any, ...]] = []
    sweeper = DeadlineSweeper(timer=timer, fire=lambda i, s: fired.append((i, s)))

    assert sweeper.sweep_once() == 0
    assert fired == []


def test_sweeper_isolates_fire_failure():
    from relay.hub.app import DeadlineSweeper

    timer = _FakeTimer([_due("inc-bad", 0), _due("inc-ok", 0)])
    fired: list[tuple[Any, ...]] = []

    def _fire(incident_id, step_index):
        if incident_id == "inc-bad":
            raise RuntimeError("boom")
        fired.append((incident_id, step_index))

    sweeper = DeadlineSweeper(timer=timer, fire=_fire)
    # The bad one raises but is isolated; the good one still fires.
    assert sweeper.sweep_once() == 1
    assert fired == [("inc-ok", 0)]


def test_sweeper_handles_query_failure():
    from relay.hub.app import DeadlineSweeper

    class _BoomTimer:
        def query_due_deadlines(self, now=None):
            raise RuntimeError("dynamo down")

    sweeper = DeadlineSweeper(timer=_BoomTimer(), fire=lambda i, s: None)
    assert sweeper.sweep_once() == 0  # must not raise
