"""Amazon EventBridge Scheduler adapter for the Relay escalation timer.

Creates one-time EventBridge Scheduler schedules to implement the
EscalationTimerPort protocol.  Each schedule fires once after *delay_minutes*
and invokes the node Lambda with a ``relay_event: escalation_timeout`` payload.
The schedule is configured with ``ActionAfterCompletion=DELETE`` so AWS cleans
it up automatically; ``cancel_timeout`` calls ``DeleteSchedule`` and treats
``ResourceNotFoundException`` as a no-op (the schedule already fired/deleted).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Characters allowed in EventBridge Scheduler schedule names:
# letters, digits, hyphens, underscores, periods.  Everything else is replaced.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9\-_.]")
_MAX_NAME_LEN = 64  # hard limit imposed by the Scheduler API


def _sanitize_name(raw: str) -> str:
    """Replace disallowed characters with underscores and truncate to 64 chars.

    Args:
        raw: Unsanitised candidate schedule name.

    Returns:
        A schedule name that satisfies the EventBridge Scheduler naming rules.
    """
    safe = _SAFE_NAME_RE.sub("_", raw)
    return safe[:_MAX_NAME_LEN]


class SchedulerTimerPort:
    """EscalationTimerPort backed by Amazon EventBridge Scheduler.

    Creates one-time schedules that invoke the Relay node Lambda after the
    requested delay.  The Python EscalationEngine remains the single source
    of truth for escalation state; this class is purely a "call me back in
    N minutes" primitive.

    Schedule names are derived deterministically from ``(incident_id,
    step_index)`` so that a duplicate ``schedule_timeout`` call for the same
    step overwrites (or collides with) the existing schedule rather than
    creating a phantom timer.

    Args:
        target_lambda_arn:   ARN of the Relay node Lambda that Scheduler should
                             invoke when the timer fires.
        scheduler_role_arn:  ARN of the IAM role that EventBridge Scheduler
                             assumes to call ``lambda:InvokeFunction``.
        scheduler_client:    Optional pre-built boto3 ``scheduler`` client.
                             Pass a fake/mock for unit tests; defaults to a
                             fresh ``boto3.client("scheduler")``.
        group_name:          EventBridge Scheduler schedule group name.
                             Defaults to ``"default"``.  Create a dedicated
                             group in CDK to scope IAM policies tightly.
        clock:               Zero-argument callable returning the current
                             timezone-aware UTC datetime.  Inject a fixed
                             ``lambda: datetime(...)`` in tests to make
                             schedule expressions deterministic.
    """

    def __init__(
        self,
        target_lambda_arn: str,
        scheduler_role_arn: str,
        scheduler_client: Any | None = None,
        group_name: str = "default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target_lambda_arn = target_lambda_arn
        self._scheduler_role_arn = scheduler_role_arn
        self._client = scheduler_client or boto3.client("scheduler")
        self._group_name = group_name
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(UTC)
        )

    # ------------------------------------------------------------------
    # EscalationTimerPort implementation
    # ------------------------------------------------------------------

    def schedule_timeout(
        self,
        incident_id: str,
        step_index: int,
        delay_minutes: int,
    ) -> str:
        """Create a one-time EventBridge Scheduler schedule.

        The schedule fires at ``now + delay_minutes`` and invokes the node
        Lambda with a JSON payload of the form::

            {
                "relay_event": "escalation_timeout",
                "incident_id": "<incident_id>",
                "step_index": <step_index>
            }

        ``ActionAfterCompletion`` is set to ``DELETE`` so the schedule is
        automatically removed after firing; the Lambda does not need to clean
        it up.

        Args:
            incident_id:    Unique incident identifier.
            step_index:     Zero-based escalation step index; included in the
                            payload so the EscalationEngine can detect stale
                            (already-superseded) callbacks.
            delay_minutes:  How far in the future (in whole minutes) the
                            schedule should fire.

        Returns:
            The schedule name, which serves as the opaque timer handle passed
            back to ``cancel_timeout``.

        Raises:
            ClientError: On unrecoverable Scheduler API errors.
        """
        name = self._build_name(incident_id, step_index)
        fire_at: datetime = self._clock().replace(second=0, microsecond=0) + timedelta(
            minutes=delay_minutes
        )

        schedule_expression = (
            f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})"
        )

        payload: dict[str, Any] = {
            "relay_event": "escalation_timeout",
            "incident_id": incident_id,
            "step_index": step_index,
        }

        try:
            self._client.create_schedule(
                Name=name,
                GroupName=self._group_name,
                ScheduleExpression=schedule_expression,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": self._target_lambda_arn,
                    "RoleArn": self._scheduler_role_arn,
                    "Input": json.dumps(payload),
                },
                ActionAfterCompletion="DELETE",
            )
        except ClientError:
            logger.exception(
                "Failed to create Scheduler schedule %r for incident=%s step=%s",
                name,
                incident_id,
                step_index,
            )
            raise

        logger.info(
            "Scheduler schedule created",
            extra={
                "schedule_name": name,
                "incident_id": incident_id,
                "step_index": step_index,
                "fire_at": fire_at.isoformat(),
                "delay_minutes": delay_minutes,
            },
        )
        return name

    def cancel_timeout(self, timer_handle: str) -> None:
        """Delete an EventBridge Scheduler schedule by its handle (name).

        Treats ``ResourceNotFoundException`` as a no-op: the schedule may have
        already fired (and been auto-deleted via ``ActionAfterCompletion=DELETE``)
        or may have been deleted by a previous call.

        Args:
            timer_handle: The schedule name returned by ``schedule_timeout``.

        Raises:
            ClientError: On unrecoverable Scheduler API errors other than
                         ``ResourceNotFoundException``.
        """
        if not timer_handle:
            logger.debug("cancel_timeout called with empty handle — skipping")
            return

        try:
            self._client.delete_schedule(
                Name=timer_handle,
                GroupName=self._group_name,
            )
            logger.info(
                "Scheduler schedule deleted",
                extra={"schedule_name": timer_handle},
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                logger.debug(
                    "Scheduler schedule %r not found — already fired or deleted, ignoring",
                    timer_handle,
                )
                return
            logger.exception(
                "Failed to delete Scheduler schedule %r", timer_handle
            )
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_name(self, incident_id: str, step_index: int) -> str:
        """Build a deterministic, API-safe schedule name.

        Args:
            incident_id: Unique incident identifier.
            step_index:  Escalation step index.

        Returns:
            A schedule name composed of a ``relay-esc-`` prefix, the sanitised
            incident ID, and the step index; truncated to 64 characters.
        """
        raw = f"relay-esc-{incident_id}-{step_index}"
        return _sanitize_name(raw)
