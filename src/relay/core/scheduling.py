"""On-call scheduling — pure domain (no AWS).

Role-driven iteration (see docs/SCHEDULING.md and
docs/plans/scheduling-config-rework.md):

- The day is three fixed 8-hour shifts: NIGHT 00-08, DAY 08-16, EVENING 16-24.
- Each (day, shift) is covered by one person **per on-call role**. v1 roles are
  PRIMARY (first responder), SECONDARY (escalation target) and MANAGER (late
  escalation). A week therefore has 7 days x 3 shifts x N roles slots.
- Per person: an availability grid (which day x shift slots they'll take) + a
  single out-of-office range + a master "available" toggle + the set of roles
  they're eligible to serve.
- ``auto_schedule`` greedily fills each (slot, role) with the least-loaded
  eligible person. PRIMARY and SECONDARY for the same slot are never the same
  person (hard constraint); MANAGER may overlap. Unfilled (slot, role) pairs
  are reported as GAPS — surfaced, never hidden.

This module is the single source of truth for "who is on call"; the old
round-robin rotation model (relay.core.oncall) has been removed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum


class Shift(StrEnum):
    """The three fixed 8-hour shifts in a day."""

    NIGHT = "night"      # 00:00 - 08:00
    DAY = "day"          # 08:00 - 16:00
    EVENING = "evening"  # 16:00 - 24:00


class Role(StrEnum):
    """An on-call role. Escalation policies page roles, not people, and the
    schedule resolves role -> person at page time."""

    PRIMARY = "primary"      # first responder
    SECONDARY = "secondary"  # escalation target
    MANAGER = "manager"      # late escalation / backstop


# Default role set + the order escalation walks them. Modeled as a list so the
# role set can grow without code changes elsewhere.
DEFAULT_ROLES: list[Role] = [Role.PRIMARY, Role.SECONDARY, Role.MANAGER]

# Roles where one person may not hold two such roles in the same slot. MANAGER
# is intentionally excluded — it's a small stable group that may overlap.
EXCLUSIVE_ROLES: frozenset[Role] = frozenset({Role.PRIMARY, Role.SECONDARY})

# Local start hour for each shift (used to resolve "who's on call now").
SHIFT_START_HOUR: dict[Shift, int] = {Shift.NIGHT: 0, Shift.DAY: 8, Shift.EVENING: 16}

# Ordered weekday keys (Mon..Sun) matching date.weekday() 0..6.
WEEKDAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def shift_for_hour(hour: int) -> Shift:
    """Return the shift covering a given hour-of-day (0-23)."""
    if hour < 8:
        return Shift.NIGHT
    if hour < 16:
        return Shift.DAY
    return Shift.EVENING


@dataclass
class OutOfOffice:
    """A single inclusive out-of-office date range."""

    start: date
    end: date

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass
class Availability:
    """One person's on-call availability.

    Args:
        contact_id:  the person.
        available:   master toggle — if False they're never scheduled.
        slots:       map of weekday key -> set of Shifts they will take.
        ooo:         optional single out-of-office range.
        roles:       set of Roles this person is eligible to serve. Defaults to
                     PRIMARY + SECONDARY (MANAGER is opt-in — a smaller group).
    """

    contact_id: str
    available: bool = False
    slots: dict[str, set[Shift]] = field(default_factory=dict)
    ooo: OutOfOffice | None = None
    roles: set[Role] = field(default_factory=lambda: {Role.PRIMARY, Role.SECONDARY})

    def can_take(self, d: date, shift: Shift) -> bool:
        """Whether this person can take a given date+shift (ignores role)."""
        if not self.available:
            return False
        if self.ooo is not None and self.ooo.covers(d):
            return False
        weekday = WEEKDAYS[d.weekday()]
        return shift in self.slots.get(weekday, set())

    def can_serve(self, d: date, shift: Shift, role: Role) -> bool:
        """Whether this person can take a given date+shift for a given role."""
        return role in self.roles and self.can_take(d, shift)


@dataclass
class ScheduledSlot:
    """One assigned (or unfilled) role-slot in a generated schedule."""

    date: date
    shift: Shift
    role: Role
    contact_id: str | None  # None => coverage gap

    @property
    def is_gap(self) -> bool:
        return self.contact_id is None


@dataclass
class Schedule:
    """A generated week schedule: one slot per (day, shift, role)."""

    week_start: date
    slots: list[ScheduledSlot]
    roles: list[Role] = field(default_factory=lambda: list(DEFAULT_ROLES))

    @property
    def gaps(self) -> list[ScheduledSlot]:
        return [s for s in self.slots if s.is_gap]

    @property
    def coverage(self) -> tuple[int, int]:
        """(covered, total) across all role-slots."""
        total = len(self.slots)
        return total - len(self.gaps), total

    def coverage_by_role(self) -> dict[Role, tuple[int, int]]:
        """Per-role (covered, total)."""
        out: dict[Role, list[int]] = defaultdict(lambda: [0, 0])
        for s in self.slots:
            out[s.role][1] += 1
            if not s.is_gap:
                out[s.role][0] += 1
        return {role: (c, t) for role, (c, t) in out.items()}

    def counts_by_contact(self) -> dict[str, int]:
        """Total assigned role-slots per contact (across all roles)."""
        out: dict[str, int] = defaultdict(int)
        for s in self.slots:
            if s.contact_id:
                out[s.contact_id] += 1
        return dict(out)

    def assignment_at(self, when: datetime, role: Role = Role.PRIMARY) -> str | None:
        """Who holds ``role`` at ``when`` (None if gap/out of range)."""
        d = when.date()
        shift = shift_for_hour(when.hour)
        for s in self.slots:
            if s.date == d and s.shift == shift and s.role == role:
                return s.contact_id
        return None

    def assignments_at(self, when: datetime) -> dict[Role, str | None]:
        """All roles' assignees at ``when`` (role -> contact_id|None)."""
        d = when.date()
        shift = shift_for_hour(when.hour)
        out: dict[Role, str | None] = {}
        for s in self.slots:
            if s.date == d and s.shift == shift:
                out[s.role] = s.contact_id
        return out

    def contacts_for_roles(self, when: datetime, roles: list[Role]) -> list[str]:
        """Resolve the given roles to on-call contact_ids at ``when``.

        Order-preserving and de-duplicated (a person holding two requested
        roles is paged once). Roles that are gaps/unassigned contribute nothing.
        """
        assigned = self.assignments_at(when)
        out: list[str] = []
        for role in roles:
            cid = assigned.get(role)
            if cid and cid not in out:
                out.append(cid)
        return out


def week_slots(week_start: date) -> list[tuple[date, Shift]]:
    """The 21 (date, shift) pairs of a week starting at ``week_start``."""
    out: list[tuple[date, Shift]] = []
    for day_offset in range(7):
        d = week_start + timedelta(days=day_offset)
        for shift in (Shift.NIGHT, Shift.DAY, Shift.EVENING):
            out.append((d, shift))
    return out


def auto_schedule(
    week_start: date,
    availabilities: list[Availability],
    roles: list[Role] | None = None,
) -> Schedule:
    """Greedy balanced fill of a week's role-slots across available people.

    For each (slot, role) in chronological / role order, assign the eligible
    person with the fewest total shifts so far (stable tie-break by
    contact_id). PRIMARY and SECONDARY for the same slot are never the same
    person; MANAGER may overlap. A (slot, role) with no eligible person becomes
    a gap (contact_id=None) — surfaced, never hidden.
    """
    role_list = list(roles) if roles else list(DEFAULT_ROLES)
    counts: dict[str, int] = defaultdict(int)
    slots: list[ScheduledSlot] = []
    for d, shift in week_slots(week_start):
        taken_exclusive: set[str] = set()  # people already holding an exclusive role this slot
        for role in role_list:
            eligible = [a for a in availabilities if a.can_serve(d, shift, role)]
            if role in EXCLUSIVE_ROLES:
                eligible = [a for a in eligible if a.contact_id not in taken_exclusive]
            if not eligible:
                slots.append(ScheduledSlot(date=d, shift=shift, role=role, contact_id=None))
                continue
            # Least-loaded first; tie-break stable by contact_id for determinism.
            eligible.sort(key=lambda a: (counts[a.contact_id], a.contact_id))
            chosen = eligible[0]
            counts[chosen.contact_id] += 1
            if role in EXCLUSIVE_ROLES:
                taken_exclusive.add(chosen.contact_id)
            slots.append(
                ScheduledSlot(date=d, shift=shift, role=role, contact_id=chosen.contact_id)
            )
    return Schedule(week_start=week_start, slots=slots, roles=role_list)


def monday_of(d: date) -> date:
    """The Monday on/before ``d`` (week start)."""
    return d - timedelta(days=d.weekday())


def apply_overrides(stored: dict, overrides: list[dict]) -> dict:
    """Overlay ad-hoc overrides onto a stored schedule dict (non-mutating).

    Each override is ``{date, shift, role, contact_id, ...}`` and replaces the
    assignee for that exact (date, shift, role) slot. Overridden slots are
    flagged ``overridden: true`` so the UI can show them. An override for a
    (date, shift, role) with no matching generated slot is ignored (the auto
    schedule defines which role-slots exist).
    """
    if not overrides:
        return stored
    index = {
        (o.get("date"), o.get("shift"), o.get("role")): o
        for o in overrides
    }
    new_slots = []
    for s in stored.get("slots", []):
        key = (s.get("date"), s.get("shift"), s.get("role", Role.PRIMARY.value))
        ov = index.get(key)
        if ov is not None:
            s = {**s, "contact_id": ov.get("contact_id"), "overridden": True}
        new_slots.append(s)
    return {**stored, "slots": new_slots}


def schedule_from_stored(data: dict) -> Schedule:
    """Rebuild a ``Schedule`` from a persisted dict (DynamoScheduleStore form).

    Tolerant of missing/extra keys; bad slots are skipped rather than raising.
    A slot with no ``role`` defaults to PRIMARY (legacy single-assignee rows).
    """
    week_start = date.fromisoformat(data["week_start"])
    slots: list[ScheduledSlot] = []
    for s in data.get("slots", []):
        try:
            slots.append(
                ScheduledSlot(
                    date=date.fromisoformat(s["date"]),
                    shift=Shift(s["shift"]),
                    role=Role(s.get("role", Role.PRIMARY)),
                    contact_id=s.get("contact_id") or None,
                )
            )
        except (KeyError, ValueError):
            continue
    stored_roles = data.get("roles")
    roles = [Role(r) for r in stored_roles] if stored_roles else list(DEFAULT_ROLES)
    return Schedule(week_start=week_start, slots=slots, roles=roles)


__all__ = [
    "Shift",
    "Role",
    "DEFAULT_ROLES",
    "EXCLUSIVE_ROLES",
    "SHIFT_START_HOUR",
    "WEEKDAYS",
    "shift_for_hour",
    "OutOfOffice",
    "Availability",
    "ScheduledSlot",
    "Schedule",
    "week_slots",
    "auto_schedule",
    "monday_of",
    "schedule_from_stored",
    "apply_overrides",
]
