"""Resolve on-call roles to contact_ids via the generated schedule.

Pure-ish domain glue: given something that can fetch a stored schedule for a
week (the ``ScheduleStorePort`` below — DynamoScheduleStore satisfies it), turn
a list of role names at a moment in time into the on-call contact_ids.

Escalation policies page *roles* (primary/secondary/manager); this is what
converts those roles to people at page time. The Hub is the schedule-backed
paging authority, so this resolver lives in core and is wired wherever a
schedule store is available.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from relay.core.scheduling import Role, monday_of, schedule_from_stored, shift_for_hour

logger = logging.getLogger(__name__)


class ScheduleStorePort(Protocol):
    """The slice of a schedule store this resolver needs."""

    def get_schedule(self, week_start: str) -> dict[str, Any] | None:
        """Return the stored schedule dict for an ISO week_start, or None."""
        ...


def _team_timezone() -> ZoneInfo:
    """Team wall-clock zone (RELAY_TZ); schedules are authored in local time."""
    from zoneinfo import ZoneInfoNotFoundError

    name = os.environ.get("RELAY_TZ", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("invalid RELAY_TZ=%r; falling back to UTC", name)
        return ZoneInfo("UTC")


class ScheduleRoleResolver:
    """Resolves role names to on-call contact_ids using a schedule store.

    Callable: ``resolver(roles, when)`` -> ``list[contact_id]`` so it can be
    injected directly as the Node handler's ``_role_resolver``.
    """

    def __init__(self, schedule_store: ScheduleStorePort) -> None:
        self._store = schedule_store

    def resolve(self, roles: list[str], when: datetime) -> list[str]:
        """Resolve ``roles`` to deduped contact_ids on-call at ``when`` (UTC).

        ``when`` is converted to the team timezone before resolving date+shift,
        matching how the schedule was authored. Unknown role strings and
        coverage gaps simply contribute no contact. Never raises — returns [].
        """
        if not roles:
            return []
        try:
            # Normalize to Role enum, dropping unknown names.
            valid: list[Role] = []
            for r in roles:
                try:
                    valid.append(Role(r))
                except ValueError:
                    logger.warning("unknown escalation role %r; ignoring", r)
            if not valid:
                return []

            local = when.astimezone(_team_timezone())
            ws = monday_of(local.date())
            stored = self._store.get_schedule(ws.isoformat())
            if not stored:
                return []
            sched = schedule_from_stored(stored)
            naive_local = local.replace(tzinfo=None)
            # Guard: only resolve if a slot exists for this date+shift.
            shift = shift_for_hour(local.hour)
            if not any(s.date == local.date() and s.shift == shift for s in sched.slots):
                return []
            return sched.contacts_for_roles(naive_local, valid)
        except Exception:
            logger.warning("role resolution failed for roles=%s", roles, exc_info=True)
            return []

    def __call__(self, roles: list[str], when: datetime) -> list[str]:
        return self.resolve(roles, when)


__all__ = ["ScheduleRoleResolver", "ScheduleStorePort"]
