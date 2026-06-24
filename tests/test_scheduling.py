"""Tests for the role-driven on-call scheduling algorithm (relay.core.scheduling)."""

from __future__ import annotations

from datetime import date, datetime

from relay.core.scheduling import (
    DEFAULT_ROLES,
    Availability,
    OutOfOffice,
    Role,
    Shift,
    apply_overrides,
    auto_schedule,
    monday_of,
    schedule_from_stored,
    shift_for_hour,
    week_slots,
)

WEEK = date(2026, 6, 22)  # a Monday

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_SHIFTS = [Shift.NIGHT, Shift.DAY, Shift.EVENING]
ALL_ROLES = {Role.PRIMARY, Role.SECONDARY, Role.MANAGER}

# 21 (day,shift) slots x 3 roles = 63 role-slots per week.
TOTAL_ROLE_SLOTS = 63


def _avail(cid, days, shifts, available=True, ooo=None, roles=ALL_ROLES):
    return Availability(
        contact_id=cid,
        available=available,
        slots={d: set(shifts) for d in days},
        ooo=ooo,
        roles=set(roles),
    )


def test_week_has_21_slots():
    assert len(week_slots(WEEK)) == 21


def test_shift_for_hour():
    assert shift_for_hour(2) == Shift.NIGHT
    assert shift_for_hour(9) == Shift.DAY
    assert shift_for_hour(20) == Shift.EVENING


def test_monday_of():
    # 2026-06-24 is a Wednesday -> Monday is the 22nd.
    assert monday_of(date(2026, 6, 24)) == WEEK


def test_total_role_slots():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a])
    assert len(sched.slots) == TOTAL_ROLE_SLOTS


def test_full_coverage_two_people_all_roles():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    b = _avail("b", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a, b])
    covered, total = sched.coverage
    assert (covered, total) == (TOTAL_ROLE_SLOTS, TOTAL_ROLE_SLOTS)
    assert not sched.gaps


def test_primary_and_secondary_never_same_person():
    # Two people, both eligible for primary+secondary everywhere.
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    b = _avail("b", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a, b])
    by_slot: dict[tuple, dict] = {}
    for s in sched.slots:
        by_slot.setdefault((s.date, s.shift), {})[s.role] = s.contact_id
    for assignments in by_slot.values():
        assert assignments[Role.PRIMARY] is not None
        assert assignments[Role.SECONDARY] is not None
        assert assignments[Role.PRIMARY] != assignments[Role.SECONDARY]


def test_secondary_is_gap_when_only_one_person():
    # One person can't be both primary and secondary in the same slot.
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a])
    cov = sched.coverage_by_role()
    assert cov[Role.PRIMARY] == (21, 21)
    assert cov[Role.SECONDARY] == (0, 21)  # every secondary slot is a gap
    assert cov[Role.MANAGER] == (21, 21)   # manager may overlap with primary


def test_manager_role_requires_eligibility():
    # People eligible only for primary/secondary => all manager slots are gaps.
    a = _avail("a", ALL_DAYS, ALL_SHIFTS, roles={Role.PRIMARY, Role.SECONDARY})
    b = _avail("b", ALL_DAYS, ALL_SHIFTS, roles={Role.PRIMARY, Role.SECONDARY})
    sched = auto_schedule(WEEK, [a, b])
    cov = sched.coverage_by_role()
    assert cov[Role.PRIMARY] == (21, 21)
    assert cov[Role.SECONDARY] == (21, 21)
    assert cov[Role.MANAGER] == (0, 21)


def test_gap_when_nobody_available_for_a_slot():
    # Only available Mon Day; everything else is a gap. Mon-day has 3 role-slots
    # but one person can only fill primary + manager (not secondary).
    a = _avail("a", ["mon"], [Shift.DAY])
    sched = auto_schedule(WEEK, [a])
    covered, total = sched.coverage
    assert total == TOTAL_ROLE_SLOTS
    assert covered == 2  # primary + manager for Mon day
    assert len(sched.gaps) == TOTAL_ROLE_SLOTS - 2


def test_unavailable_master_toggle_excludes_person():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS, available=False)
    sched = auto_schedule(WEEK, [a])
    assert sched.coverage == (0, TOTAL_ROLE_SLOTS)


def test_ooo_blocks_assignment_in_range():
    ooo = OutOfOffice(start=date(2026, 6, 22), end=date(2026, 6, 28))
    a = _avail("a", ALL_DAYS, ALL_SHIFTS, ooo=ooo)
    sched = auto_schedule(WEEK, [a])
    assert sched.coverage == (0, TOTAL_ROLE_SLOTS)


def test_balanced_three_people():
    people = [_avail(c, ALL_DAYS, ALL_SHIFTS) for c in ("a", "b", "c")]
    sched = auto_schedule(WEEK, people)
    counts = sched.counts_by_contact()
    # 63 role-slots over 3 people => 21 each.
    assert sorted(counts.values()) == [21, 21, 21]


def test_deterministic():
    people = [_avail(c, ALL_DAYS, ALL_SHIFTS) for c in ("a", "b")]
    s1 = auto_schedule(WEEK, people)
    s2 = auto_schedule(WEEK, people)
    assert [(s.date, s.shift, s.role, s.contact_id) for s in s1.slots] == [
        (s.date, s.shift, s.role, s.contact_id) for s in s2.slots
    ]


def test_assignment_at_resolves_current_shift_per_role():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    b = _avail("b", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a, b])
    when = datetime(2026, 6, 22, 10, 0)  # Monday DAY shift
    primary = sched.assignment_at(when, Role.PRIMARY)
    secondary = sched.assignment_at(when, Role.SECONDARY)
    assert primary in ("a", "b")
    assert secondary in ("a", "b")
    assert primary != secondary


def test_assignments_at_returns_all_roles():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    b = _avail("b", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a, b])
    got = sched.assignments_at(datetime(2026, 6, 22, 10, 0))
    assert set(got.keys()) == set(DEFAULT_ROLES)


def test_schedule_from_stored_roundtrips_with_roles():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    b = _avail("b", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a, b])
    stored = {
        "week_start": sched.week_start.isoformat(),
        "roles": [str(r) for r in sched.roles],
        "slots": [
            {
                "date": s.date.isoformat(),
                "shift": str(s.shift),
                "role": str(s.role),
                "contact_id": s.contact_id,
            }
            for s in sched.slots
        ],
    }
    rebuilt = schedule_from_stored(stored)
    assert rebuilt.week_start == WEEK
    assert len(rebuilt.slots) == TOTAL_ROLE_SLOTS
    when = datetime(2026, 6, 22, 10, 0)
    assert rebuilt.assignment_at(when, Role.PRIMARY) == sched.assignment_at(when, Role.PRIMARY)


def test_schedule_from_stored_defaults_missing_role_to_primary():
    # Legacy single-assignee rows had no 'role' key.
    stored = {
        "week_start": WEEK.isoformat(),
        "slots": [
            {"date": "2026-06-22", "shift": "day", "contact_id": "a"},
            {"date": "2026-06-22", "shift": "night", "contact_id": None},  # gap
            {"date": "2026-06-22", "shift": "bogus", "contact_id": "x"},   # dropped
        ],
    }
    rebuilt = schedule_from_stored(stored)
    assert len(rebuilt.slots) == 2
    assert all(s.role == Role.PRIMARY for s in rebuilt.slots)
    assert len(rebuilt.gaps) == 1


# ---------------------------------------------------------------------------
# Ad-hoc overrides (cover-me)
# ---------------------------------------------------------------------------
def _stored_one_person():
    a = _avail("a", ALL_DAYS, ALL_SHIFTS)
    sched = auto_schedule(WEEK, [a])
    return {
        "week_start": WEEK.isoformat(),
        "roles": [str(r) for r in sched.roles],
        "slots": [
            {"date": s.date.isoformat(), "shift": str(s.shift),
             "role": str(s.role), "contact_id": s.contact_id}
            for s in sched.slots
        ],
    }


def test_apply_overrides_replaces_assignee():
    stored = _stored_one_person()
    ov = [{"date": "2026-06-22", "shift": "day", "role": "primary", "contact_id": "b"}]
    out = apply_overrides(stored, ov)
    hit = [s for s in out["slots"]
           if s["date"] == "2026-06-22" and s["shift"] == "day" and s["role"] == "primary"]
    assert hit[0]["contact_id"] == "b"
    assert hit[0]["overridden"] is True
    # Non-matching slots untouched (no overridden flag).
    other = [s for s in out["slots"] if s.get("overridden")]
    assert len(other) == 1


def test_apply_overrides_can_fill_a_gap():
    stored = _stored_one_person()
    # secondary slots are gaps for one person; override fills one.
    ov = [{"date": "2026-06-22", "shift": "day", "role": "secondary", "contact_id": "b"}]
    out = apply_overrides(stored, ov)
    hit = [s for s in out["slots"]
           if s["date"] == "2026-06-22" and s["shift"] == "day" and s["role"] == "secondary"]
    assert hit[0]["contact_id"] == "b"


def test_apply_overrides_empty_is_noop():
    stored = _stored_one_person()
    assert apply_overrides(stored, []) is stored


def test_apply_overrides_unknown_slot_ignored():
    stored = _stored_one_person()
    ov = [{"date": "2099-01-01", "shift": "day", "role": "primary", "contact_id": "b"}]
    out = apply_overrides(stored, ov)
    assert out["slots"] == stored["slots"]
