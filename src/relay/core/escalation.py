"""Escalation state machine.

Timer firing and persistence are injected — no AWS calls here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from relay.core.model import EscalationPolicy, EscalationStep, Incident, Stream


class EscalationPhase(StrEnum):
    IDLE = "IDLE"
    WAITING_ACK = "WAITING_ACK"
    ESCALATING = "ESCALATING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    EXHAUSTED = "EXHAUSTED"


@dataclass
class EscalationContext:
    incident_id: str
    policy_id: str
    current_step_index: int = 0
    phase: EscalationPhase = EscalationPhase.IDLE
    paged_at: datetime | None = None
    last_escalated_at: datetime | None = None
    ack_by: str | None = None
    ack_at: datetime | None = None
    # Internal field for tracking the active timer handle
    _timer_handle: str | None = field(default=None, repr=False)


@dataclass
class EscalationTransition:
    old_phase: EscalationPhase
    new_phase: EscalationPhase
    contact_ids_to_page: list[str]
    streams: list[Stream]
    timeout_minutes: int | None = None
    note: str = ""
    # On-call roles this step pages (resolved to people via the schedule by the
    # caller). The engine stays pure and does not resolve roles itself.
    roles_to_page: list[str] = field(default_factory=list)


class EscalationTimerPort(Protocol):
    """Injected timer — concrete impl uses EventBridge Scheduler or Step Functions."""

    def schedule_timeout(
        self, incident_id: str, step_index: int, delay_minutes: int
    ) -> str:
        """Schedule a timeout callback. Returns a timer handle ID."""
        ...

    def cancel_timeout(self, timer_handle: str) -> None:
        """Cancel a previously scheduled timeout by its handle."""
        ...


class NoOpTimerPort:
    """EscalationTimerPort that records nothing and fires no callbacks.

    Used by the collapsed single-container runtime until the DynamoDB-deadline
    sweep timer lands (collapsed-single-container plan §3 / Step 2). In that
    interim the container has no EventBridge Scheduler, so escalation starts and
    pages step 0 but does not auto-advance on timeout. The handle is a stable
    synthetic string so ``cancel_timeout`` (e.g. on ack) is a clean no-op.
    """

    def schedule_timeout(
        self, incident_id: str, step_index: int, delay_minutes: int
    ) -> str:
        return f"noop-{incident_id}-{step_index}"

    def cancel_timeout(self, timer_handle: str) -> None:
        return None


class EscalationStatePort(Protocol):
    """Injected persistence — concrete impl uses DynamoDB."""

    def load(self, incident_id: str) -> EscalationContext | None:
        """Load escalation context for the given incident. Returns None if not found."""
        ...

    def save(self, ctx: EscalationContext) -> None:
        """Persist escalation context."""
        ...


class EscalationEngine:
    """State machine that drives an incident through escalation steps.

    Transitions:

        IDLE → WAITING_ACK       (start)
        WAITING_ACK → ESCALATING (on_timeout advances to next step)
        ESCALATING  → ESCALATING (further timeouts keep advancing)
        ESCALATING  → EXHAUSTED  (on_timeout when no more steps)
        WAITING_ACK → ACKNOWLEDGED  (acknowledge at step 0)
        ESCALATING  → ACKNOWLEDGED  (acknowledge at any step)

    Timer and persistence are injected via ports so the engine stays
    free of AWS SDK calls and is fully unit-testable.
    """

    def __init__(self, timer: EscalationTimerPort, state_store: EscalationStatePort) -> None:
        self._timer = timer
        self._state_store = state_store

    def start(self, incident: Incident, policy: EscalationPolicy) -> EscalationTransition:
        """Begin escalation for *incident* using *policy*.

        Creates a fresh EscalationContext (or resets an existing one), sets
        phase to WAITING_ACK, fires step 0, schedules a timeout, and persists
        the context.  Returns the transition describing which contacts to page.
        """
        ctx = EscalationContext(
            incident_id=incident.correlation_id,
            policy_id=policy.policy_id,
            current_step_index=0,
            phase=EscalationPhase.WAITING_ACK,
        )
        transition = self._transition_to_step(ctx, policy, 0)
        self._state_store.save(ctx)
        return transition

    def acknowledge(
        self, incident_id: str, contact_id: str, policy: EscalationPolicy
    ) -> EscalationTransition:
        """Record an acknowledgement from *contact_id*.

        Valid only when the incident is in WAITING_ACK or ESCALATING phase.
        Cancels the active timer and transitions the context to ACKNOWLEDGED.
        Raises ValueError if the incident is not in an acknowledgeable state.
        """
        ctx = self._state_store.load(incident_id)
        if ctx is None:
            raise ValueError(
                f"No escalation context found for incident '{incident_id}'."
            )

        if ctx.phase not in (EscalationPhase.WAITING_ACK, EscalationPhase.ESCALATING):
            raise ValueError(
                f"Incident '{incident_id}' is not in an acknowledgeable state "
                f"(current phase: {ctx.phase})."
            )

        old_phase = ctx.phase

        # Cancel active timer if one is tracked
        if ctx._timer_handle is not None:
            self._timer.cancel_timeout(ctx._timer_handle)
            ctx._timer_handle = None

        now = datetime.now(UTC)
        ctx.ack_by = contact_id
        ctx.ack_at = now
        ctx.phase = EscalationPhase.ACKNOWLEDGED

        self._state_store.save(ctx)

        return EscalationTransition(
            old_phase=old_phase,
            new_phase=EscalationPhase.ACKNOWLEDGED,
            contact_ids_to_page=[],
            streams=[],
            note=f"Acknowledged by {contact_id} at {now.isoformat()}",
        )

    def on_timeout(
        self, incident_id: str, step_index: int, policy: EscalationPolicy
    ) -> EscalationTransition:
        """Handle a timer expiry for *step_index*.

        Idempotent: if the incident has already been acknowledged (or the
        step_index doesn't match the current step) the method returns a no-op
        transition so that delayed or duplicate timer firings are harmless.

        Advances to the next escalation step, or transitions to EXHAUSTED
        if there are no further steps.
        """
        ctx = self._state_store.load(incident_id)
        if ctx is None:
            raise ValueError(
                f"No escalation context found for incident '{incident_id}'."
            )

        old_phase = ctx.phase

        # Idempotency guard: ignore if already acked or if step has moved on
        if ctx.phase not in (EscalationPhase.WAITING_ACK, EscalationPhase.ESCALATING):
            return EscalationTransition(
                old_phase=old_phase,
                new_phase=ctx.phase,
                contact_ids_to_page=[],
                streams=[],
                note=(
                    f"Timeout ignored — incident is already in phase {ctx.phase} "
                    f"(step_index={step_index})"
                ),
            )

        if ctx.current_step_index != step_index:
            return EscalationTransition(
                old_phase=old_phase,
                new_phase=ctx.phase,
                contact_ids_to_page=[],
                streams=[],
                note=(
                    f"Timeout ignored — stale step_index {step_index}, "
                    f"current is {ctx.current_step_index}"
                ),
            )

        next_index = step_index + 1
        if next_index >= len(policy.steps):
            # No more steps — mark exhausted
            ctx.phase = EscalationPhase.EXHAUSTED
            ctx.last_escalated_at = datetime.now(UTC)
            self._state_store.save(ctx)
            return EscalationTransition(
                old_phase=old_phase,
                new_phase=EscalationPhase.EXHAUSTED,
                contact_ids_to_page=[],
                streams=[],
                note="All escalation steps exhausted; no further contacts to page.",
            )

        ctx.phase = EscalationPhase.ESCALATING
        ctx.last_escalated_at = datetime.now(UTC)
        transition = self._transition_to_step(ctx, policy, next_index)
        self._state_store.save(ctx)
        return transition

    def _transition_to_step(
        self,
        ctx: EscalationContext,
        policy: EscalationPolicy,
        step_index: int,
    ) -> EscalationTransition:
        """Advance *ctx* to *step_index* and schedule the step's timeout.

        Looks up the step from policy.steps, updates the context in place,
        schedules a timer, and returns an EscalationTransition describing
        which contacts and streams to notify.
        """
        old_phase = ctx.phase
        step: EscalationStep = policy.steps[step_index]

        ctx.current_step_index = step_index
        ctx.paged_at = datetime.now(UTC)

        timer_handle = self._timer.schedule_timeout(
            incident_id=ctx.incident_id,
            step_index=step_index,
            delay_minutes=step.timeout_minutes,
        )
        ctx._timer_handle = timer_handle

        return EscalationTransition(
            old_phase=old_phase,
            new_phase=ctx.phase,
            contact_ids_to_page=list(step.contact_ids),
            roles_to_page=list(step.roles),
            streams=list(step.notify_streams),
            timeout_minutes=step.timeout_minutes,
            note=(
                f"Paging step {step_index}: roles={step.roles}, "
                f"contacts={step.contact_ids}, "
                f"timeout={step.timeout_minutes}m, "
                f"timer_handle={timer_handle}"
            ),
        )
