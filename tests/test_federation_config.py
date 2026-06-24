"""Tests for config-driven federation forwarding (routing.yaml `federation:` block).

Covers:
  1. FederationConfig parsing from routing.yaml (present + absent/optional).
  2. decide_forward() global severity threshold.
  3. decide_forward() forward_states filter.
  4. FederationOverride matching (app_name / alarm_name_prefix / environment / tags).
  5. Override actions: forward=never, forward=always, slice min_severity.
  6. Override + state filter interaction (always still respects forward_states).
  7. HubProcessor prefers config policy over env-var gate; falls back when absent.
"""

from __future__ import annotations

import yaml

from relay.config.schema import (
    FederationConfig,
    FederationOverride,
    RoutingConfig,
)
from relay.core.model import Incident, IncidentState, Severity, SignalSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _incident(
    *,
    severity: Severity = Severity.SEV2,
    state: IncidentState = IncidentState.TRIGGERED,
    app_name: str = "test-app",
    alarm_name: str = "test-alarm",
    environment: str = "production",
    tags: dict[str, str] | None = None,
) -> Incident:
    return Incident(
        account_id="123456789012",
        region="us-east-1",
        app_name=app_name,
        severity=severity,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name=alarm_name,
        state=state,
        environment=environment,
        tags=tags or {},
    )


# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------


def test_routing_config_federation_is_optional():
    """routing.yaml with no `federation:` block parses with federation=None."""
    cfg = RoutingConfig.model_validate(
        {
            "rules": [],
            "default_escalation_policy_id": "pol-standard",
            "default_streams": ["TEAM"],
        }
    )
    assert cfg.federation is None


def test_federation_block_parses_from_yaml():
    raw = """
rules: []
default_escalation_policy_id: pol-standard
default_streams: [TEAM]
federation:
  min_severity: SEV1
  forward_states: [TRIGGERED, ESCALATED]
  overrides:
    - name: noisy-batch
      app_name: batch-reports
      forward: never
    - name: db-aggressive
      alarm_name_prefix: rds-
      min_severity: SEV3
"""
    cfg = RoutingConfig.model_validate(yaml.safe_load(raw))
    assert cfg.federation is not None
    fed = cfg.federation
    assert fed.min_severity == Severity.SEV1
    assert fed.forward_states == [IncidentState.TRIGGERED, IncidentState.ESCALATED]
    assert len(fed.overrides) == 2
    assert fed.overrides[0].forward == "never"
    assert fed.overrides[1].min_severity == Severity.SEV3


def test_federation_defaults():
    """An empty federation block defaults to SEV2 / all-states / no overrides."""
    fed = FederationConfig()
    assert fed.min_severity == Severity.SEV2
    assert fed.forward_states == []
    assert fed.overrides == []


# ---------------------------------------------------------------------------
# 2. Global severity threshold
# ---------------------------------------------------------------------------


def test_global_threshold_forwards_at_or_above():
    fed = FederationConfig(min_severity=Severity.SEV2)
    assert fed.decide_forward(_incident(severity=Severity.SEV1)) is True
    assert fed.decide_forward(_incident(severity=Severity.SEV2)) is True


def test_global_threshold_blocks_below():
    fed = FederationConfig(min_severity=Severity.SEV2)
    assert fed.decide_forward(_incident(severity=Severity.SEV3)) is False
    assert fed.decide_forward(_incident(severity=Severity.SEV4)) is False


# ---------------------------------------------------------------------------
# 3. forward_states filter
# ---------------------------------------------------------------------------


def test_forward_states_blocks_other_states():
    fed = FederationConfig(
        min_severity=Severity.SEV4, forward_states=[IncidentState.TRIGGERED]
    )
    assert fed.decide_forward(_incident(state=IncidentState.TRIGGERED)) is True
    assert fed.decide_forward(_incident(state=IncidentState.RESOLVED)) is False


def test_empty_forward_states_allows_all():
    fed = FederationConfig(min_severity=Severity.SEV4, forward_states=[])
    assert fed.decide_forward(_incident(state=IncidentState.RESOLVED)) is True


# ---------------------------------------------------------------------------
# 4. Override matching
# ---------------------------------------------------------------------------


def test_override_matches_app_name():
    ov = FederationOverride(app_name="payments")
    assert ov.matches(_incident(app_name="payments")) is True
    assert ov.matches(_incident(app_name="other")) is False


def test_override_matches_alarm_name_prefix():
    ov = FederationOverride(alarm_name_prefix="rds-")
    assert ov.matches(_incident(alarm_name="rds-cpu-high")) is True
    assert ov.matches(_incident(alarm_name="lambda-errors")) is False


def test_override_matches_environment():
    ov = FederationOverride(environment="non-production")
    assert ov.matches(_incident(environment="non-production")) is True
    assert ov.matches(_incident(environment="production")) is False


def test_override_matches_tags_anded():
    ov = FederationOverride(tags={"team": "core", "tier": "1"})
    assert ov.matches(_incident(tags={"team": "core", "tier": "1"})) is True
    # missing one tag → no match
    assert ov.matches(_incident(tags={"team": "core"})) is False


def test_override_fields_anded_together():
    ov = FederationOverride(app_name="payments", environment="production")
    assert ov.matches(_incident(app_name="payments", environment="production")) is True
    assert (
        ov.matches(_incident(app_name="payments", environment="non-production"))
        is False
    )


# ---------------------------------------------------------------------------
# 5. Override actions
# ---------------------------------------------------------------------------


def test_override_forward_never_blocks_even_high_severity():
    fed = FederationConfig(
        min_severity=Severity.SEV4,  # would otherwise forward everything
        overrides=[FederationOverride(app_name="batch", forward="never")],
    )
    assert fed.decide_forward(_incident(app_name="batch", severity=Severity.SEV1)) is False
    # a different app is unaffected by the override
    assert fed.decide_forward(_incident(app_name="api", severity=Severity.SEV4)) is True


def test_override_forward_always_bypasses_threshold():
    fed = FederationConfig(
        min_severity=Severity.SEV1,  # normally only SEV1 forwards
        overrides=[FederationOverride(app_name="vip", forward="always")],
    )
    assert fed.decide_forward(_incident(app_name="vip", severity=Severity.SEV4)) is True
    # other apps still gated to SEV1
    assert fed.decide_forward(_incident(app_name="api", severity=Severity.SEV4)) is False


def test_override_slice_min_severity():
    fed = FederationConfig(
        min_severity=Severity.SEV1,
        overrides=[FederationOverride(alarm_name_prefix="rds-", min_severity=Severity.SEV3)],
    )
    # rds- alarms forward down to SEV3
    assert fed.decide_forward(_incident(alarm_name="rds-cpu", severity=Severity.SEV3)) is True
    assert fed.decide_forward(_incident(alarm_name="rds-cpu", severity=Severity.SEV4)) is False
    # non-rds alarms still need SEV1
    assert fed.decide_forward(_incident(alarm_name="api-5xx", severity=Severity.SEV2)) is False


def test_first_matching_override_wins():
    fed = FederationConfig(
        overrides=[
            FederationOverride(app_name="x", forward="never"),
            FederationOverride(app_name="x", forward="always"),
        ],
    )
    # File order: the `never` is first, so it wins.
    assert fed.decide_forward(_incident(app_name="x", severity=Severity.SEV1)) is False


# ---------------------------------------------------------------------------
# 6. Override + state filter interaction
# ---------------------------------------------------------------------------


def test_forward_always_still_respects_state_filter():
    fed = FederationConfig(
        forward_states=[IncidentState.TRIGGERED],
        overrides=[FederationOverride(app_name="vip", forward="always")],
    )
    assert (
        fed.decide_forward(_incident(app_name="vip", state=IncidentState.TRIGGERED))
        is True
    )
    # even forward=always does not re-forward a RESOLVED redelivery
    assert (
        fed.decide_forward(_incident(app_name="vip", state=IncidentState.RESOLVED))
        is False
    )


# ---------------------------------------------------------------------------
# 7. HubProcessor wiring: config policy preferred, env fallback otherwise
# ---------------------------------------------------------------------------


def _make_processor(*, federation=None):
    from unittest.mock import MagicMock

    from relay.hub.app import HubProcessor, HubState, SSEPublisher

    hub_state = MagicMock(spec=HubState)
    hub_state.update_app.return_value = MagicMock()
    proc = HubProcessor(
        incident_store=MagicMock(),
        notifier=MagicMock(),
        hub_state=hub_state,
        sse_publisher=MagicMock(spec=SSEPublisher),
        forwarder=MagicMock(forward=MagicMock(return_value=True)),
        federation=federation,
        listeners=[],
    )
    return proc


def test_processor_uses_federation_config_when_present():
    """A config policy overrides the env-var gate: forward=never wins over min_sev."""
    fed = FederationConfig(
        min_severity=Severity.SEV4,
        overrides=[FederationOverride(app_name="batch", forward="never")],
    )
    proc = _make_processor(federation=fed)
    assert proc._should_forward(_incident(app_name="batch", severity=Severity.SEV1)) is False
    assert proc._should_forward(_incident(app_name="api", severity=Severity.SEV4)) is True


def test_processor_falls_back_to_env_gate_without_config():
    """No federation config → FederationConfig() defaults apply (SEV2 threshold)."""
    proc = _make_processor(federation=FederationConfig(min_severity=Severity.SEV2))
    assert proc._should_forward(_incident(severity=Severity.SEV2)) is True
    assert proc._should_forward(_incident(severity=Severity.SEV3)) is False
