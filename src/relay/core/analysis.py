"""Incident AI augmentation — briefing packs and after-action reports.

Pure domain: builds the *prompts* + context packets from incident data and
provides a deterministic, model-free **fallback** rendering so the feature works
even with no AI assistant wired (graceful degradation per docs/AI.md §5).

An ``AIAssistant`` (see relay.adapters.base) is optional. When present, these
helpers ask it to draft prose from the context; when absent — or when the model
fails — they fall back to a structured, deterministic summary built from the
timeline. Either way the caller labels the output AI-generated vs. auto-generated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from relay.core.metrics import humanize_seconds

if TYPE_CHECKING:
    from relay.adapters.base import AIAssistant
    from relay.core.model import Incident

# Keep the model honest + cheap: it drafts narrative from facts we supply, it
# does not invent metrics or root causes presented as fact.
_AAR_SYSTEM = (
    "You are an SRE writing a blameless post-incident review (after-action "
    "report). Use ONLY the facts provided. Be concise and factual. Mark genuine "
    "uncertainty as such. Output GitHub-flavored markdown with these sections: "
    "Summary, Impact, Timeline, Contributing factors, Action items. Do not invent "
    "metrics, causes, or people not present in the input."
)

_BRIEF_SYSTEM = (
    "You are an SRE assembling a t=0 incident briefing for a responder who just "
    "got paged. Use ONLY the facts provided. Be terse — a responder is scanning "
    "under stress. Output short markdown: what fired, where (service path / "
    "environment), what we know, and concrete next checks to run. Frame causes as "
    "hypotheses, never verdicts. Do not invent data."
)


def _timeline_lines(incident: Incident) -> list[str]:
    """Render the incident timeline as readable lines (oldest first)."""
    events = sorted(incident.timeline, key=lambda e: e.occurred_at)
    out: list[str] = []
    for e in events:
        ts = e.occurred_at.isoformat() if e.occurred_at else "?"
        detail = ""
        if e.detail:
            bits = ", ".join(f"{k}={v}" for k, v in e.detail.items() if k != "note")
            note = e.detail.get("note")
            detail = f" ({bits})" if bits else ""
            if note:
                detail += f" — {note}"
        out.append(f"- {ts} · {e.event_type} · by {e.actor}{detail}")
    return out


def build_context(incident: Incident) -> str:
    """A compact, factual context packet shared by briefing + AAR prompts."""
    sp = " > ".join(incident.service_path) if incident.service_path else incident.app_name
    lines = [
        f"Incident: {incident.correlation_id}",
        f"App: {incident.app_name}",
        f"Service path: {sp}",
        f"Environment: {incident.environment}",
        f"Severity: {incident.severity}",
        f"State: {incident.state}",
        f"Signal source: {incident.signal_source}",
        f"Alarm: {incident.alarm_name}",
        f"Account/Region: {incident.account_id} / {incident.region}",
        f"Created: {incident.created_at.isoformat() if incident.created_at else '?'}",
    ]
    if incident.acknowledged_at:
        lines.append(
            f"Acknowledged: {incident.acknowledged_at.isoformat()} "
            f"by {incident.acknowledged_by}"
        )
    if incident.tags:
        lines.append("Tags: " + ", ".join(f"{k}={v}" for k, v in incident.tags.items()))
    tl = _timeline_lines(incident)
    if tl:
        lines.append("Timeline:")
        lines.extend(tl)
    return "\n".join(lines)


def _completion_text(result: object) -> str | None:
    """Normalize an assistant result to text.

    Accepts an ``AICompletion`` (reads ``.text``) or a bare string — the latter
    keeps lightweight test doubles and any legacy adapter working. Returns the
    non-empty text, or None when the model declined.
    """
    if result is None:
        return None
    text = getattr(result, "text", result)
    if isinstance(text, str) and text.strip():
        return text
    return None


def _resolved_seconds(incident: Incident) -> float | None:
    from relay.core.metrics import _resolved_at, _seconds

    r = _resolved_at(incident)
    return _seconds(incident.created_at, r) if r else None


def _fallback_aar(incident: Incident) -> str:
    """Deterministic AAR when no AI assistant is available."""
    dur = humanize_seconds(_resolved_seconds(incident))
    ack = humanize_seconds(
        (incident.acknowledged_at - incident.created_at).total_seconds()
        if incident.acknowledged_at else None
    )
    sp = " > ".join(incident.service_path) if incident.service_path else incident.app_name
    tl = "\n".join(_timeline_lines(incident)) or "- (no timeline events recorded)"
    return (
        f"# After-action report — {incident.app_name}\n\n"
        f"_Auto-generated from the incident timeline (no AI model configured). "
        f"Edit before sharing._\n\n"
        f"## Summary\n"
        f"A {incident.severity} incident on **{sp}** ({incident.environment}) "
        f"triggered by `{incident.alarm_name}` via {incident.signal_source}. "
        f"Current state: {incident.state}.\n\n"
        f"## Impact\n"
        f"- Severity: {incident.severity}\n"
        f"- Time to acknowledge: {ack}\n"
        f"- Time to resolve: {dur}\n\n"
        f"## Timeline\n{tl}\n\n"
        f"## Contributing factors\n"
        f"_To be completed by the responder — the timeline above is the raw "
        f"material; record what actually caused this._\n\n"
        f"## Action items\n"
        f"- [ ] _Add follow-ups identified during review._\n"
    )


def _fallback_brief(incident: Incident) -> str:
    """Deterministic t=0 briefing when no AI assistant is available."""
    sp = " > ".join(incident.service_path) if incident.service_path else incident.app_name
    return (
        f"**{incident.severity} · {incident.app_name}** ({incident.environment})\n\n"
        f"- Fired: `{incident.alarm_name}` via {incident.signal_source}\n"
        f"- Where: {sp}\n"
        f"- Account/Region: {incident.account_id} / {incident.region}\n"
        f"- Next checks: recent deploys to this component, sibling alarms, "
        f"canary history, prior incidents on this deployment.\n\n"
        f"_Auto-generated (no AI model configured)._"
    )


def generate_aar(incident: Incident, assistant: AIAssistant | None = None) -> dict[str, Any]:
    """Draft an after-action report.

    Returns ``{"markdown": str, "ai_generated": bool}``. Uses ``assistant`` when
    provided and successful; otherwise falls back to the deterministic report.
    """
    if assistant is not None:
        prompt = (
            "Write a post-incident after-action report from these facts:\n\n"
            + build_context(incident)
        )
        text = _completion_text(
            assistant.complete(system=_AAR_SYSTEM, prompt=prompt, max_tokens=1500)
        )
        if text:
            return {"markdown": text, "ai_generated": True}
    return {"markdown": _fallback_aar(incident), "ai_generated": False}


def generate_brief(incident: Incident, assistant: AIAssistant | None = None) -> dict[str, Any]:
    """Draft a t=0 responder briefing pack.

    Returns ``{"markdown": str, "ai_generated": bool}``.
    """
    if assistant is not None:
        prompt = (
            "Write a t=0 incident briefing from these facts:\n\n"
            + build_context(incident)
        )
        text = _completion_text(
            assistant.complete(system=_BRIEF_SYSTEM, prompt=prompt, max_tokens=600)
        )
        if text:
            return {"markdown": text, "ai_generated": True}
    return {"markdown": _fallback_brief(incident), "ai_generated": False}


__all__ = ["build_context", "generate_aar", "generate_brief"]
