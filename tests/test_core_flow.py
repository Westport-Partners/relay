"""Unit tests for relay.core.flow.build_flow (issue #20, process-flow view).

Pure-core tests: no AWS, no FastAPI, no boto3. Follows the pattern in
test_metrics.py and test_analysis.py — hand-built Incident + TimelineEvent lists.

Ack/resolve event_type constants confirmed from flow.py:
  _ACK_EVENT_TYPES     = {"acknowledged", "incident.acknowledged"}
  _RESOLVE_EVENT_TYPES = {"resolved",      "incident.resolved"}
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from relay.core.flow import build_flow
from relay.core.model import (
    EscalationPolicy,
    EscalationStep,
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


def _t(offset_seconds: int) -> datetime:
    """Return T0 + offset_seconds (timezone-aware UTC)."""
    return T0 + timedelta(seconds=offset_seconds)


def _inc(
    cid: str = "inc-001",
    timeline: list[TimelineEvent] | None = None,
    escalation_policy_id: str | None = None,
) -> Incident:
    return Incident(
        correlation_id=cid,
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm",
        state=IncidentState.TRIGGERED,
        timeline=timeline or [],
        escalation_policy_id=escalation_policy_id,
    )


def _page_sent_ev(
    cid: str,
    step_index: int,
    occurred_at: datetime,
    contact_ids: list[str] | None = None,
    roles: list[str] | None = None,
    streams: list[str] | None = None,
    timeout_minutes: int = 5,
    event_id: str | None = None,
) -> TimelineEvent:
    return TimelineEvent(
        event_id=event_id or f"ps-{step_index}",
        incident_id=cid,
        stream=Stream.TEAM,
        occurred_at=occurred_at,
        actor="system",
        event_type="escalation.page_sent",
        detail={
            "step_index": step_index,
            "contact_ids": contact_ids or [],
            "roles": roles or [],
            "streams": streams or ["TEAM"],
            "timeout_minutes": timeout_minutes,
        },
    )


def _policy(
    policy_id: str = "pol-1",
    steps: list[tuple[int, list[str], list[str]]] | None = None,
) -> EscalationPolicy:
    """Build a policy. steps is a list of (step_index, contact_ids, roles)."""
    if steps is None:
        steps = [(0, ["c1"], []), (1, ["c2"], []), (2, ["c3"], [])]
    return EscalationPolicy(
        policy_id=policy_id,
        name="Test Policy",
        team="team-test",
        steps=[
            EscalationStep(
                step_index=si,
                contact_ids=cids if cids else [],
                roles=roles if roles else ["primary"],
                timeout_minutes=5,
                notify_streams=[Stream.TEAM],
            )
            for si, cids, roles in steps
        ],
    )


# ---------------------------------------------------------------------------
# 1. source="config", partial reach (steps 0+1 reached, step 2 not)
# ---------------------------------------------------------------------------


class TestConfigPartialReach:
    def _build(self) -> dict:
        policy = _policy(
            steps=[
                (0, ["c1"], []),
                (1, ["c2"], []),
                (2, ["c3"], []),
            ]
        )
        timeline = [
            _page_sent_ev("inc-p", 0, _t(0), contact_ids=["c1"], event_id="ps-0"),
            _page_sent_ev("inc-p", 1, _t(60), contact_ids=["c2"], event_id="ps-1"),
        ]
        inc = _inc("inc-p", timeline=timeline, escalation_policy_id="pol-1")
        return build_flow(inc, policy, {"c1": "Alice", "c2": "Bob", "c3": "Carol"})

    def test_source_is_config(self):
        r = self._build()
        assert r["source"] == "config"

    def test_three_expected_steps(self):
        r = self._build()
        assert len(r["expected_steps"]) == 3

    def test_step0_reached(self):
        r = self._build()
        s0 = r["expected_steps"][0]
        assert s0["step_index"] == 0
        assert s0["reached"] is True
        assert s0["reached_at"] is not None
        assert s0["page_event_id"] == "ps-0"

    def test_step1_reached(self):
        r = self._build()
        s1 = r["expected_steps"][1]
        assert s1["step_index"] == 1
        assert s1["reached"] is True
        assert s1["reached_at"] is not None
        assert s1["page_event_id"] == "ps-1"

    def test_step2_not_reached(self):
        r = self._build()
        s2 = r["expected_steps"][2]
        assert s2["step_index"] == 2
        assert s2["reached"] is False
        assert s2["reached_at"] is None
        assert s2["page_event_id"] is None

    def test_notify_streams_are_string_list(self):
        r = self._build()
        for step in r["expected_steps"]:
            streams = step["notify_streams"]
            assert isinstance(streams, list)
            assert all(isinstance(s, str) for s in streams)

    def test_fallback_is_false(self):
        assert self._build()["fallback"] is False


# ---------------------------------------------------------------------------
# 2. source="config", resolved early (step 0 page_sent + ack + resolve)
# ---------------------------------------------------------------------------


class TestConfigResolvedEarly:
    def _build(self) -> dict:
        policy = _policy(steps=[(0, ["c1"], []), (1, ["c2"], [])])
        timeline = [
            _page_sent_ev("inc-r", 0, _t(0), contact_ids=["c1"], event_id="ps-0"),
            TimelineEvent(
                event_id="ack-1",
                incident_id="inc-r",
                stream=Stream.CENTRAL,
                occurred_at=_t(30),
                actor="c1",
                event_type="acknowledged",
                detail={},
            ),
            TimelineEvent(
                event_id="res-1",
                incident_id="inc-r",
                stream=Stream.CENTRAL,
                occurred_at=_t(120),
                actor="c1",
                event_type="resolved",
                detail={},
            ),
        ]
        inc = _inc("inc-r", timeline=timeline, escalation_policy_id="pol-1")
        return build_flow(inc, policy, {"c1": "Alice", "c2": "Bob"})

    def test_step0_reached_step1_not(self):
        r = self._build()
        assert r["expected_steps"][0]["reached"] is True
        assert r["expected_steps"][1]["reached"] is False

    def test_actual_events_includes_ack_and_resolve(self):
        r = self._build()
        types = [ev["event_type"] for ev in r["actual_events"]]
        # "acknowledged" and "resolved" are the bare forms (see flow.py constants)
        assert "acknowledged" in types
        assert "resolved" in types

    def test_actual_events_sorted_by_occurred_at_ascending(self):
        r = self._build()
        times = [ev["occurred_at"] for ev in r["actual_events"]]
        assert times == sorted(times)

    def test_source_is_config(self):
        assert self._build()["source"] == "config"


# ---------------------------------------------------------------------------
# 3. source="config", exhausted (all steps reached + exhausted event)
# ---------------------------------------------------------------------------


class TestConfigExhausted:
    def _build(self) -> dict:
        policy = _policy(steps=[(0, ["c1"], []), (1, ["c2"], [])])
        timeline = [
            _page_sent_ev("inc-ex", 0, _t(0), contact_ids=["c1"], event_id="ps-0"),
            _page_sent_ev("inc-ex", 1, _t(60), contact_ids=["c2"], event_id="ps-1"),
            TimelineEvent(
                event_id="exh-1",
                incident_id="inc-ex",
                stream=Stream.TEAM,
                occurred_at=_t(120),
                actor="system",
                event_type="escalation.exhausted",
                detail={"last_step_index": 1},
            ),
        ]
        inc = _inc("inc-ex", timeline=timeline, escalation_policy_id="pol-1")
        return build_flow(inc, policy, {})

    def test_exhausted_event_in_actual_events(self):
        r = self._build()
        types = [ev["event_type"] for ev in r["actual_events"]]
        assert "escalation.exhausted" in types

    def test_all_steps_reached(self):
        r = self._build()
        assert all(s["reached"] for s in r["expected_steps"])

    def test_source_and_no_fallback(self):
        r = self._build()
        assert r["source"] == "config"
        assert r["fallback"] is False


# ---------------------------------------------------------------------------
# 4. source="derived" (no policy, page_sent events carry full detail)
# ---------------------------------------------------------------------------


class TestDerived:
    def _build(self) -> dict:
        timeline = [
            _page_sent_ev(
                "inc-d", 0, _t(0),
                contact_ids=["c1"], roles=["primary"],
                streams=["TEAM"], timeout_minutes=5, event_id="ps-0",
            ),
            _page_sent_ev(
                "inc-d", 1, _t(60),
                contact_ids=["c2"], roles=["secondary"],
                streams=["TEAM", "CENTRAL"], timeout_minutes=10, event_id="ps-1",
            ),
        ]
        inc = _inc("inc-d", timeline=timeline)
        return build_flow(inc, None, {"c1": "Alice", "c2": "Bob"})

    def test_source_is_derived(self):
        assert self._build()["source"] == "derived"

    def test_two_expected_steps(self):
        assert len(self._build()["expected_steps"]) == 2

    def test_both_reached(self):
        for step in self._build()["expected_steps"]:
            assert step["reached"] is True
            assert step["reached_at"] is not None
            assert step["page_event_id"] is not None

    def test_steps_ordered_by_step_index(self):
        r = self._build()
        indices = [s["step_index"] for s in r["expected_steps"]]
        assert indices == sorted(indices)

    def test_fallback_is_false(self):
        assert self._build()["fallback"] is False

    def test_contact_ids_carried_from_detail(self):
        r = self._build()
        s0 = r["expected_steps"][0]
        assert "c1" in s0["contact_ids"]


# ---------------------------------------------------------------------------
# 5. source="none" (no policy, no page_sent events)
# ---------------------------------------------------------------------------


def test_source_none_no_page_sent():
    inc = _inc("inc-n")
    r = build_flow(inc, None, {})
    assert r["source"] == "none"
    assert r["expected_steps"] == []
    assert r["fallback"] is True
    assert r["contacts"] == {}


# ---------------------------------------------------------------------------
# 6. policy_id resolution — three sub-cases
# ---------------------------------------------------------------------------


class TestPolicyIdResolution:
    def test_incident_field_wins_when_set(self):
        inc = _inc("inc-pid", escalation_policy_id="p-direct")
        r = build_flow(inc, None, {})
        assert r["policy_id"] == "p-direct"

    def test_timeline_triggered_event_fallback(self):
        timeline = [
            TimelineEvent(
                event_id="t1",
                incident_id="inc-trig",
                stream=Stream.TEAM,
                occurred_at=_t(0),
                actor="system",
                event_type="incident.triggered",
                detail={"policy_id": "p-x", "alarm_name": "alarm"},
            )
        ]
        inc = _inc("inc-trig", timeline=timeline, escalation_policy_id=None)
        r = build_flow(inc, None, {})
        assert r["policy_id"] == "p-x"

    def test_policy_object_id_wins(self):
        """When a policy object is passed its policy_id takes precedence."""
        policy = _policy(policy_id="p-from-obj", steps=[(0, ["c1"], [])])
        # Incident field and timeline point to something different
        timeline = [
            TimelineEvent(
                event_id="t2",
                incident_id="inc-obj",
                stream=Stream.TEAM,
                occurred_at=_t(0),
                actor="system",
                event_type="incident.triggered",
                detail={"policy_id": "p-from-timeline"},
            )
        ]
        inc = _inc("inc-obj", timeline=timeline, escalation_policy_id="p-from-field")
        r = build_flow(inc, policy, {})
        assert r["policy_id"] == "p-from-obj"


# ---------------------------------------------------------------------------
# 7. contacts restriction
# ---------------------------------------------------------------------------


class TestContactsRestriction:
    def _run(self) -> dict:
        """
        Contacts map has c1 (referenced by step 0 contact_ids),
        c2 (referenced only by page_sent detail in actual_events),
        c3 (actor on ack event), c_extra (unreferenced entirely).
        """
        policy = _policy(steps=[(0, ["c1"], []), (1, [], ["primary"])])
        timeline = [
            _page_sent_ev(
                "inc-ct", 0, _t(0),
                contact_ids=["c2"], event_id="ps-0",
            ),
            TimelineEvent(
                event_id="ack-c",
                incident_id="inc-ct",
                stream=Stream.CENTRAL,
                occurred_at=_t(30),
                actor="c3",
                event_type="acknowledged",
                detail={},
            ),
        ]
        inc = _inc("inc-ct", timeline=timeline, escalation_policy_id="pol-1")
        return build_flow(
            inc, policy,
            {
                "c1": "Alice",   # referenced by expected step 0
                "c2": "Bob",     # referenced by page_sent detail
                "c3": "Carol",   # actor on ack event (present in contacts map)
                "c_extra": "Zed",  # unreferenced — must be dropped
            },
        )

    def test_unreferenced_contact_is_dropped(self):
        r = self._run()
        assert "c_extra" not in r["contacts"]

    def test_referenced_contacts_are_kept(self):
        r = self._run()
        # c1 is in step 0 contact_ids (expected_steps)
        assert "c1" in r["contacts"]
        # c2 is in page_sent detail contact_ids (actual_events)
        assert "c2" in r["contacts"]
        # c3 is the actor on the ack event AND is present in the contacts map
        assert "c3" in r["contacts"]

    def test_missing_id_not_fabricated(self):
        """A contact_id referenced in events but absent from the map is omitted."""
        policy = _policy(steps=[(0, ["missing-c"], [])])
        inc = _inc("inc-miss", escalation_policy_id="pol-1")
        r = build_flow(inc, policy, {})  # empty contacts map
        assert "missing-c" not in r["contacts"]


# ---------------------------------------------------------------------------
# 8. actual_events sorted ascending by occurred_at + step_index lifted
# ---------------------------------------------------------------------------


class TestActualEventsSorting:
    def test_sorted_ascending(self):
        """Events inserted out-of-order in the timeline must come out sorted."""
        timeline = [
            TimelineEvent(
                event_id="res-e",
                incident_id="inc-sort",
                stream=Stream.CENTRAL,
                occurred_at=_t(300),
                actor="op",
                event_type="resolved",
                detail={},
            ),
            _page_sent_ev("inc-sort", 0, _t(0), event_id="ps-0"),
            TimelineEvent(
                event_id="ack-e",
                incident_id="inc-sort",
                stream=Stream.CENTRAL,
                occurred_at=_t(100),
                actor="op",
                event_type="acknowledged",
                detail={},
            ),
            _page_sent_ev("inc-sort", 1, _t(60), event_id="ps-1"),
        ]
        inc = _inc("inc-sort", timeline=timeline)
        r = build_flow(inc, None, {})
        times = [ev["occurred_at"] for ev in r["actual_events"]]
        assert times == sorted(times)

    def test_step_index_lifted_from_detail(self):
        timeline = [_page_sent_ev("inc-si", 2, _t(10), event_id="ps-2")]
        inc = _inc("inc-si", timeline=timeline)
        r = build_flow(inc, None, {})
        page_ev = next(
            e for e in r["actual_events"] if e["event_type"] == "escalation.page_sent"
        )
        assert page_ev["step_index"] == 2

    def test_step_index_none_for_non_escalation_events(self):
        timeline = [
            TimelineEvent(
                event_id="ack-si",
                incident_id="inc-si2",
                stream=Stream.CENTRAL,
                occurred_at=_t(10),
                actor="op",
                event_type="acknowledged",
                detail={},
            )
        ]
        inc = _inc("inc-si2", timeline=timeline)
        r = build_flow(inc, None, {})
        ack_ev = next(
            e for e in r["actual_events"] if e["event_type"] == "acknowledged"
        )
        assert ack_ev["step_index"] is None


# ---------------------------------------------------------------------------
# 9. Extra: both bare and namespaced ack/resolve forms are recognised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ack_type", ["acknowledged", "incident.acknowledged"])
def test_ack_event_types_both_recognised(ack_type):
    timeline = [
        TimelineEvent(
            event_id="ack-x",
            incident_id="inc-ack",
            stream=Stream.CENTRAL,
            occurred_at=_t(0),
            actor="op",
            event_type=ack_type,
            detail={},
        )
    ]
    inc = _inc("inc-ack", timeline=timeline)
    r = build_flow(inc, None, {})
    types = [ev["event_type"] for ev in r["actual_events"]]
    assert ack_type in types


@pytest.mark.parametrize("resolve_type", ["resolved", "incident.resolved"])
def test_resolve_event_types_both_recognised(resolve_type):
    timeline = [
        TimelineEvent(
            event_id="res-x",
            incident_id="inc-res",
            stream=Stream.CENTRAL,
            occurred_at=_t(0),
            actor="op",
            event_type=resolve_type,
            detail={},
        )
    ]
    inc = _inc("inc-res", timeline=timeline)
    r = build_flow(inc, None, {})
    types = [ev["event_type"] for ev in r["actual_events"]]
    assert resolve_type in types


# ---------------------------------------------------------------------------
# 10. correlation_id round-trips through return dict
# ---------------------------------------------------------------------------


def test_correlation_id_in_result():
    inc = _inc("inc-cid")
    r = build_flow(inc, None, {})
    assert r["correlation_id"] == "inc-cid"
