"""DetectionPipeline — in-process detection library.

Wraps a NodeHandler to provide a clean, typed entry-point for alarm
processing inside the always-on container.  This is the §8 no-drift seam
described in the collapsed-single-container design doc:

  parse → classify → persist → page → escalate → on_incident

The pipeline delegates to ``NodeHandler.process()`` for Step 1.  Typed
sub-methods (parse, classify, …) will be extracted here in later steps; for
now the single ``handle_alarm()`` method covers the full pipeline and the
``on_incident`` sink is wired through the handler's ``_on_incident`` hook so
the embedded Hub receives the Incident in-process rather than via an
EventBridge hop.

Usage (container composition)::

    from relay.node.handler import NodeHandler
    from relay.node.pipeline import DetectionPipeline

    handler = NodeHandler(
        _incident_store=incident_store,
        _on_incident=hub_processor.on_local_incident,
        ...
    )
    pipeline = DetectionPipeline(handler)
    result = pipeline.handle_alarm(alarm_event)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relay.node.handler import NodeHandler


class DetectionPipeline:
    """In-process alarm detection pipeline (parse→classify→persist→page→escalate→on_incident).

    Thin typed wrapper around :class:`~relay.node.handler.NodeHandler`.  Callers
    should construct a ``NodeHandler`` with the desired injected collaborators
    (at minimum ``_incident_store`` and ``_on_incident``) and pass it here.
    """

    def __init__(self, handler: NodeHandler) -> None:
        self._handler = handler

    def handle_alarm(self, event: dict[str, Any]) -> dict[str, Any]:
        """Run the full detection pipeline for a raw alarm event dict.

        Delegates to ``NodeHandler.process()``.  Typed sub-method extraction
        (parse, classify, persist, page) is deferred to a later step; the
        single-call delegation keeps the Step 1 diff minimal while establishing
        the typed seam.

        Args:
            event: Raw alarm event dict (CloudWatch Alarm State Change or
                   Relay internal control event).

        Returns:
            Status dict from the handler (``statusCode``, ``correlation_id``,
            ``severity``, ``team_ok``, ``central_ok``).
        """
        return self._handler.process(event)

    def handle_timeout(self, incident_id: str, step_index: int) -> dict[str, Any]:
        """Fire an escalation timeout for *incident_id* at *step_index*.

        This is the in-process replacement for the EventBridge Scheduler →
        Node Lambda callback: the container's sweep loop discovers a due
        DynamoDB deadline (collapsed-single-container plan §3 / Step 2) and
        calls here.  Advances the escalation state machine and re-runs the
        in-process Node→Hub effects (page next step, tile → ESCALATED) via the
        same ``_on_incident`` sink the alarm path uses.

        Args:
            incident_id: The incident whose escalation deadline fired.
            step_index:  The step the deadline belonged to; the engine ignores
                         the callback if escalation has already moved past it.

        Returns:
            Status dict from the handler's timeout path.
        """
        return self._handler.process(
            {
                "relay_event": "escalation_timeout",
                "incident_id": incident_id,
                "step_index": step_index,
            }
        )
