"""Phase B: escalation pages roles, resolved to people via the schedule."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from relay.core.escalation import EscalationEngine, EscalationPhase
from relay.core.model import EscalationPolicy, EscalationStep
from relay.core.role_resolver import ScheduleRoleResolver
from relay.core.scheduling import (
    Availability,
    Role,
    Shift,
    auto_schedule,
)

WEEK = "2026-06-22"  # Monday
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_SHIFTS = [Shift.NIGHT, Shift.DAY, Shift.EVENING]


# --------------------------------------------------------------------------
# Model: EscalationStep role/contact validation
# --------------------------------------------------------------------------
def test_step_accepts_roles_only():
    s = EscalationStep(step_index=0, roles=["primary"], timeout_minutes=5)
    assert s.roles == ["primary"]
    assert s.contact_ids == []


def test_step_accepts_contacts_only():
    s = EscalationStep(step_index=0, contact_ids=["cnt-a"], timeout_minutes=5)
    assert s.contact_ids == ["cnt-a"]
    assert s.roles == []


def test_step_requires_a_paging_target():
    with pytest.raises(ValueError, match="at least one of 'roles' or 'contact_ids'"):
        EscalationStep(step_index=0, timeout_minutes=5)


# --------------------------------------------------------------------------
# Engine: transition carries roles_to_page (stays pure)
# --------------------------------------------------------------------------
class _FakeTimer:
    def schedule_timeout(self, incident_id, step_index, delay_minutes):
        return f"timer-{step_index}"

    def cancel_timeout(self, timer_handle):
        pass


class _FakeStateStore:
    def __init__(self):
        self._ctx = {}

    def load(self, incident_id):
        return self._ctx.get(incident_id)

    def save(self, ctx):
        self._ctx[ctx.incident_id] = ctx


class _Incident:
    correlation_id = "inc-1"


def test_engine_start_carries_roles_to_page():
    policy = EscalationPolicy(
        policy_id="pol", name="P", team="t",
        steps=[EscalationStep(step_index=0, roles=["primary"], timeout_minutes=5)],
    )
    eng = EscalationEngine(timer=_FakeTimer(), state_store=_FakeStateStore())
    t = eng.start(_Incident(), policy)
    assert t.new_phase == EscalationPhase.WAITING_ACK
    assert t.roles_to_page == ["primary"]
    assert t.contact_ids_to_page == []


# --------------------------------------------------------------------------
# Schedule.contacts_for_roles
# --------------------------------------------------------------------------
def _sched_two_people():
    a = Availability(contact_id="a", available=True,
                     slots={d: set(ALL_SHIFTS) for d in ALL_DAYS},
                     roles={Role.PRIMARY, Role.SECONDARY})
    b = Availability(contact_id="b", available=True,
                     slots={d: set(ALL_SHIFTS) for d in ALL_DAYS},
                     roles={Role.PRIMARY, Role.SECONDARY})
    from datetime import date
    return auto_schedule(date.fromisoformat(WEEK), [a, b])


def test_contacts_for_roles_resolves_and_dedupes():
    sched = _sched_two_people()
    when = datetime(2026, 6, 22, 10, 0)  # Monday DAY
    primary = sched.assignment_at(when, Role.PRIMARY)
    secondary = sched.assignment_at(when, Role.SECONDARY)
    got = sched.contacts_for_roles(when, [Role.PRIMARY, Role.SECONDARY])
    assert got == [primary, secondary]
    # Requesting the same role twice doesn't duplicate.
    assert sched.contacts_for_roles(when, [Role.PRIMARY, Role.PRIMARY]) == [primary]


def test_contacts_for_roles_skips_gaps():
    # One person => secondary is a gap.
    a = Availability(contact_id="a", available=True,
                     slots={d: set(ALL_SHIFTS) for d in ALL_DAYS},
                     roles={Role.PRIMARY, Role.SECONDARY})
    from datetime import date
    sched = auto_schedule(date.fromisoformat(WEEK), [a])
    when = datetime(2026, 6, 22, 10, 0)
    assert sched.contacts_for_roles(when, [Role.SECONDARY]) == []
    assert sched.contacts_for_roles(when, [Role.PRIMARY]) == ["a"]


# --------------------------------------------------------------------------
# ScheduleRoleResolver (store-backed, timezone-aware)
# --------------------------------------------------------------------------
class _FakeScheduleStore:
    def __init__(self, stored):
        self._stored = stored

    def get_schedule(self, week_start):
        return self._stored if week_start == WEEK else None


def _stored_from(sched):
    return {
        "week_start": sched.week_start.isoformat(),
        "roles": [str(r) for r in sched.roles],
        "slots": [
            {"date": s.date.isoformat(), "shift": str(s.shift),
             "role": str(s.role), "contact_id": s.contact_id}
            for s in sched.slots
        ],
    }


def test_resolver_resolves_roles_in_utc(monkeypatch):
    monkeypatch.delenv("RELAY_TZ", raising=False)
    sched = _sched_two_people()
    store = _FakeScheduleStore(_stored_from(sched))
    resolver = ScheduleRoleResolver(store)
    when = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)  # Monday DAY in UTC
    primary = sched.assignment_at(datetime(2026, 6, 22, 10, 0), Role.PRIMARY)
    assert resolver(["primary"], when) == [primary]


def test_resolver_timezone_shifts_resolution(monkeypatch):
    # 02:15 UTC Saturday == 22:15 Friday US/Eastern (evening shift, Friday).
    monkeypatch.setenv("RELAY_TZ", "America/New_York")
    sched = _sched_two_people()
    store = _FakeScheduleStore(_stored_from(sched))
    resolver = ScheduleRoleResolver(store)
    when = datetime(2026, 6, 27, 2, 15, tzinfo=UTC)  # Sat 02:15 UTC
    # Friday evening primary
    fri_evening = sched.assignment_at(datetime(2026, 6, 26, 20, 0), Role.PRIMARY)
    assert resolver(["primary"], when) == [fri_evening]


def test_resolver_unknown_role_and_no_schedule():
    resolver = ScheduleRoleResolver(_FakeScheduleStore(None))
    assert resolver(["primary"], datetime(2026, 6, 22, 10, 0, tzinfo=UTC)) == []
    sched = _sched_two_people()
    resolver2 = ScheduleRoleResolver(_FakeScheduleStore(_stored_from(sched)))
    assert resolver2(["bogus"], datetime(2026, 6, 22, 10, 0, tzinfo=UTC)) == []


# --------------------------------------------------------------------------
# Handler glue: _contacts_for_transition merges explicit + role-resolved
# --------------------------------------------------------------------------
class _Transition:
    def __init__(self, contact_ids, roles):
        self.contact_ids_to_page = contact_ids
        self.roles_to_page = roles


class _HandlerShell:
    """Minimal stand-in exposing _contacts_for_transition without full ctor."""

    from relay.node.handler import NodeHandler
    _contacts_for_transition = NodeHandler._contacts_for_transition

    def __init__(self, resolver):
        self.role_resolver = resolver


def test_handler_merges_explicit_and_resolved_contacts():
    sched = _sched_two_people()
    resolver = ScheduleRoleResolver(_FakeScheduleStore(_stored_from(sched)))
    # Resolve at a fixed time by freezing role_resolver via a wrapper.
    h = _HandlerShell(lambda roles, when: resolver(roles, datetime(2026, 6, 22, 10, 0, tzinfo=UTC)))
    primary = sched.assignment_at(datetime(2026, 6, 22, 10, 0), Role.PRIMARY)
    t = _Transition(contact_ids=["cnt-vendor"], roles=["primary"])
    got = h._contacts_for_transition(t)
    assert got[0] == "cnt-vendor"           # explicit first
    assert primary in got                    # role-resolved appended
    assert len(got) == len(set(got))         # no dupes


def test_handler_falls_back_to_explicit_when_no_resolver():
    h = _HandlerShell(None)  # no resolver wired (current Node default)
    t = _Transition(contact_ids=["cnt-arlowe"], roles=["primary"])
    # Roles can't resolve without a resolver; explicit contacts still page.
    assert h._contacts_for_transition(t) == ["cnt-arlowe"]
