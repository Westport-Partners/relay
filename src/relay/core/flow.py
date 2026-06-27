"""relay.core.flow — Process-flow timeline builder (pure domain, no AWS).

Computes a merged "expected escalation ladder vs. actual events" view for an
incident's process-flow timeline UI (issue #20).  The expected ladder comes
from an :class:`~relay.core.model.EscalationPolicy`; the actual events come
from the incident's timeline.

No boto3, no FastAPI, no imports from relay.adapters or relay.hub.
Only relay.core.model and stdlib are allowed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from relay.core.model import EscalationPolicy, Incident, TimelineEvent

# ---------------------------------------------------------------------------
# Event-type constants
# ---------------------------------------------------------------------------

_PAGE_SENT = "escalation.page_sent"
_STEP_ADVANCED = "escalation.step_advanced"
_EXHAUSTED = "escalation.exhausted"
_TRIGGERED = "incident.triggered"

# Hub writes both bare ("acknowledged", "resolved") and namespaced forms.
# metrics.py also recognises both resolved forms.  Include all four so the
# flow view works regardless of which writer produced the event.
_ACK_EVENT_TYPES: frozenset[str] = frozenset({"acknowledged", "incident.acknowledged"})
_RESOLVE_EVENT_TYPES: frozenset[str] = frozenset({"resolved", "incident.resolved"})

# All event_type strings surfaced in actual_events.
_ACTUAL_EVENT_TYPES: frozenset[str] = (
    frozenset({_PAGE_SENT, _STEP_ADVANCED, _EXHAUSTED, _TRIGGERED})
    | _ACK_EVENT_TYPES
    | _RESOLVE_EVENT_TYPES
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    """Return ISO-8601 string for *dt*, or None when *dt* is None."""
    if dt is None:
        return None
    return dt.isoformat()


def _policy_id_from_timeline(timeline: list[TimelineEvent]) -> str | None:
    """Extract policy_id from the first ``incident.triggered`` event, or None."""
    for ev in timeline:
        if ev.event_type == _TRIGGERED:
            pid = ev.detail.get("policy_id")
            if pid is not None:
                return str(pid)
    return None


def _page_sent_events(timeline: list[TimelineEvent]) -> list[TimelineEvent]:
    """Return all ``escalation.page_sent`` events, ordered by occurred_at."""
    events = [ev for ev in timeline if ev.event_type == _PAGE_SENT]
    return sorted(events, key=lambda e: e.occurred_at)


def _build_expected_from_policy(
    policy: EscalationPolicy,
    timeline: list[TimelineEvent],
) -> list[dict[str, Any]]:
    """Build expected_steps from a known EscalationPolicy (source='config').

    Matches each policy rung to the first ``escalation.page_sent`` event whose
    ``detail["step_index"]`` equals the rung's step_index.
    """
    # Index page_sent events by step_index for O(1) lookup.
    page_by_step: dict[int, TimelineEvent] = {}
    for ev in _page_sent_events(timeline):
        si = ev.detail.get("step_index")
        if si is not None:
            step_int = int(si)
            if step_int not in page_by_step:
                page_by_step[step_int] = ev

    result: list[dict[str, Any]] = []
    for step in policy.steps:  # already sorted by step_index per model validator
        page_ev = page_by_step.get(step.step_index)
        reached = page_ev is not None
        result.append(
            {
                "step_index": step.step_index,
                "roles": list(step.roles),
                "contact_ids": list(step.contact_ids),
                "notify_streams": [s.value for s in step.notify_streams],
                "timeout_minutes": step.timeout_minutes,
                "reached": reached,
                "reached_at": _iso(page_ev.occurred_at) if page_ev else None,
                "page_event_id": page_ev.event_id if page_ev else None,
            }
        )
    return result


def _build_expected_derived(
    timeline: list[TimelineEvent],
) -> list[dict[str, Any]]:
    """Build expected_steps from observed page_sent events (source='derived').

    Synthesises one rung per distinct step_index seen, pulling detail fields
    from that event.  Only reached rungs are knowable.
    """
    seen: dict[int, TimelineEvent] = {}
    for ev in _page_sent_events(timeline):
        si = ev.detail.get("step_index")
        if si is not None:
            step_int = int(si)
            if step_int not in seen:
                seen[step_int] = ev

    result: list[dict[str, Any]] = []
    for step_int in sorted(seen):
        ev = seen[step_int]
        detail = ev.detail
        streams_raw = detail.get("streams") or []
        streams: list[str] = [str(s) for s in streams_raw]
        result.append(
            {
                "step_index": step_int,
                "roles": list(detail.get("roles") or []),
                "contact_ids": list(detail.get("contact_ids") or []),
                "notify_streams": streams,
                "timeout_minutes": detail.get("timeout_minutes"),
                "reached": True,
                "reached_at": _iso(ev.occurred_at),
                "page_event_id": ev.event_id,
            }
        )
    return result


def _build_actual_events(timeline: list[TimelineEvent]) -> list[dict[str, Any]]:
    """Return escalation + ack/resolve events sorted by occurred_at ascending."""
    filtered = [ev for ev in timeline if ev.event_type in _ACTUAL_EVENT_TYPES]
    filtered.sort(key=lambda e: e.occurred_at)

    result: list[dict[str, Any]] = []
    for ev in filtered:
        step_index: int | None = None
        raw_si = ev.detail.get("step_index")
        if raw_si is not None:
            step_index = int(raw_si)
        result.append(
            {
                "event_id": ev.event_id,
                "event_type": ev.event_type,
                "occurred_at": _iso(ev.occurred_at),
                "actor": ev.actor,
                "stream": ev.stream.value,
                "step_index": step_index,
                "detail": dict(ev.detail),
            }
        )
    return result


def _restrict_contacts(
    contacts: dict[str, str],
    expected_steps: list[dict[str, Any]],
    actual_events: list[dict[str, Any]],
) -> dict[str, str]:
    """Return the subset of *contacts* that are referenced by the view.

    Scans contact_ids in expected_steps, contact_ids in actual_event details,
    and actor fields (when the actor looks like a contact_id present in the map).
    """
    referenced: set[str] = set()

    for step in expected_steps:
        for cid in step.get("contact_ids") or []:
            referenced.add(str(cid))

    for ev in actual_events:
        detail = ev.get("detail") or {}
        for cid in detail.get("contact_ids") or []:
            referenced.add(str(cid))
        actor = ev.get("actor")
        if actor and actor in contacts:
            referenced.add(actor)

    return {k: v for k, v in contacts.items() if k in referenced}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_flow(
    incident: Incident,
    policy: EscalationPolicy | None,
    contacts: dict[str, str],
) -> dict[str, Any]:
    """Compute the merged escalation-ladder / actual-events view for *incident*.

    Args:
        incident: The incident whose timeline is used for actual events.
        policy:   The EscalationPolicy associated with this incident, or None
                  when the policy is unknown (e.g. a legacy row or missing config).
        contacts: Full ``contact_id → name`` map for the team.  The returned
                  ``contacts`` key will be restricted to ids referenced by the
                  view; un-referenced ids are dropped and missing ids are omitted
                  rather than fabricated.

    Returns:
        A dict with the following keys:

        * ``correlation_id`` — the incident's correlation id.
        * ``policy_id`` — the policy id in effect (str or None).
        * ``source`` — ``"config"`` | ``"derived"`` | ``"none"``.
        * ``expected_steps`` — ordered list of escalation-rung dicts.
        * ``actual_events`` — ordered list of significant timeline event dicts.
        * ``contacts`` — restricted ``contact_id → name`` map.
        * ``fallback`` — True only when ``source == "none"``.
    """
    timeline = incident.timeline

    # ---- Determine policy_id ----
    policy_id: str | None = incident.escalation_policy_id
    if policy_id is None:
        policy_id = _policy_id_from_timeline(timeline)
    # If policy object carries an id that differs from what we derived, prefer
    # the passed-in policy object's id (it is the authoritative source).
    if policy is not None:
        policy_id = policy.policy_id

    # ---- Select source + build expected_steps ----
    page_sent = _page_sent_events(timeline)

    if policy is not None:
        source = "config"
        expected_steps = _build_expected_from_policy(policy, timeline)
        fallback = False
    elif page_sent:
        source = "derived"
        expected_steps = _build_expected_derived(timeline)
        fallback = False
    else:
        source = "none"
        expected_steps = []
        fallback = True

    # ---- Build actual_events ----
    actual_events = _build_actual_events(timeline)

    # ---- Restrict contacts ----
    restricted_contacts = _restrict_contacts(contacts, expected_steps, actual_events)

    return {
        "correlation_id": incident.correlation_id,
        "policy_id": policy_id,
        "source": source,
        "expected_steps": expected_steps,
        "actual_events": actual_events,
        "contacts": restricted_contacts,
        "fallback": fallback,
    }


__all__ = ["build_flow"]
