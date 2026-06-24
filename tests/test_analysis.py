"""Tests for AI augmentation: briefing + AAR (relay.core.analysis)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from relay.core.analysis import build_context, generate_aar, generate_brief
from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

T0 = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _incident():
    return Incident(
        correlation_id="c-1", account_id="111111111111", region="us-east-1",
        app_name="query-api", severity=Severity.SEV1,
        signal_source=SignalSource.CLOUDWATCH_ALARM, state=IncidentState.RESOLVED,
        alarm_name="prod-query-5xx", environment="prod",
        service_path=["Patents", "Search", "QueryAPI", "query-api"],
        created_at=T0, updated_at=T0 + timedelta(minutes=20),
        acknowledged_at=T0 + timedelta(minutes=2), acknowledged_by="op",
        timeline=[
            TimelineEvent(event_id="t", incident_id="c-1", stream=Stream.TEAM,
                          occurred_at=T0, actor="system", event_type="triggered",
                          detail={"reason": "alarm ALARM"}),
            TimelineEvent(event_id="r", incident_id="c-1", stream=Stream.CENTRAL,
                          occurred_at=T0 + timedelta(minutes=20), actor="op",
                          event_type="resolved", detail={}),
        ],
    )


class _FakeAssistant:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, *, system, prompt, max_tokens=1024):
        self.calls.append((system, prompt, max_tokens))
        return self.text


class _FailingAssistant:
    def complete(self, *, system, prompt, max_tokens=1024):
        return None


def test_build_context_has_key_facts():
    ctx = build_context(_incident())
    assert "query-api" in ctx
    assert "SEV1" in ctx
    assert "prod-query-5xx" in ctx
    assert "Patents > Search > QueryAPI > query-api" in ctx
    assert "Timeline:" in ctx


def test_aar_fallback_without_assistant():
    res = generate_aar(_incident(), None)
    assert res["ai_generated"] is False
    md = res["markdown"]
    assert "After-action report" in md
    assert "## Timeline" in md
    assert "## Action items" in md


def test_aar_uses_assistant_when_present():
    fake = _FakeAssistant("# AI AAR\nlooks good")
    res = generate_aar(_incident(), fake)
    assert res["ai_generated"] is True
    assert res["markdown"] == "# AI AAR\nlooks good"
    # system prompt steers a blameless AAR
    assert "after-action" in fake.calls[0][0].lower()


def test_aar_falls_back_when_assistant_returns_none():
    res = generate_aar(_incident(), _FailingAssistant())
    assert res["ai_generated"] is False
    assert "After-action report" in res["markdown"]


def test_brief_fallback_and_ai():
    res = generate_brief(_incident(), None)
    assert res["ai_generated"] is False
    assert "SEV1" in res["markdown"]

    fake = _FakeAssistant("brief!")
    res2 = generate_brief(_incident(), fake)
    assert res2["ai_generated"] is True
    assert res2["markdown"] == "brief!"
    assert fake.calls[0][2] == 600  # brief uses a small token budget
