"""Derives Severity and routing decisions from CloudWatch alarm metadata.

Pure functions — no I/O, no AWS calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from relay.config.schema import RoutingConfig
from relay.core.model import RoutingRule, Severity, SignalSource, Stream


@dataclass
class ClassificationResult:
    severity: Severity
    signal_source: SignalSource
    streams: list[Stream]
    escalation_policy_id: str
    matched_rule_id: str | None = None
    reasoning: str = ""  # human-readable explanation of why this rule matched


def classify_alarm(
    alarm_name: str,
    alarm_arn: str,
    namespace: str,
    tags: dict[str, str],
    routing_config: RoutingConfig,
) -> ClassificationResult:
    """Walk routing rules by priority. Return ClassificationResult for the first
    matching rule. Falls back to routing_config defaults."""

    # Determine signal source
    lower_tags = {k.lower(): v for k, v in tags.items()}
    if namespace.startswith("CloudWatchSynthetics") or tags.get("source") == "synthetic":
        signal_source = SignalSource.SYNTHETIC
    elif "otel" in lower_tags or "opentelemetry" in lower_tags:
        signal_source = SignalSource.OTEL
    else:
        signal_source = SignalSource.CLOUDWATCH_ALARM

    # Walk rules sorted by priority ascending
    sorted_rules = sorted(routing_config.rules, key=lambda r: r.priority)
    for rule in sorted_rules:
        if _rule_matches(rule, alarm_name, namespace, tags):
            severity = (
                rule.severity_override
                if rule.severity_override is not None
                else _derive_severity_from_name(alarm_name, tags)
            )
            return ClassificationResult(
                severity=severity,
                signal_source=signal_source,
                streams=list(rule.streams),
                escalation_policy_id=rule.escalation_policy_id,
                matched_rule_id=rule.rule_id,
                reasoning=f"Matched rule '{rule.rule_id}' (priority={rule.priority})",
            )

    # No rule matched — use defaults
    return ClassificationResult(
        severity=_derive_severity_from_name(alarm_name, tags),
        signal_source=signal_source,
        streams=list(routing_config.default_streams),
        escalation_policy_id=routing_config.default_escalation_policy_id,
        matched_rule_id=None,
        reasoning="No routing rule matched; using routing_config defaults",
    )


def _rule_matches(
    rule: RoutingRule,
    alarm_name: str,
    namespace: str,
    tags: dict[str, str],
) -> bool:
    """Check whether *rule* matches the given alarm attributes.

    All set conditions must match (AND logic).

    TODO: compile regexes once at startup for performance.
    """
    if rule.alarm_name_prefix is not None:
        if not alarm_name.startswith(rule.alarm_name_prefix):
            return False

    if rule.alarm_name_regex is not None:
        if not re.fullmatch(rule.alarm_name_regex, alarm_name):
            return False

    if rule.tag_filters:
        for key, value in rule.tag_filters.items():
            if tags.get(key) != value:
                return False

    if rule.namespace_prefix is not None:
        if not namespace.startswith(rule.namespace_prefix):
            return False

    return True


def _derive_severity_from_name(alarm_name: str, tags: dict[str, str]) -> Severity:
    """Best-effort severity derivation from naming conventions and tags.

    TODO: expand with team-specific conventions.
    """
    # Check tags first
    tag_severity = tags.get("severity", "").lower()
    if tag_severity in ("critical", "sev1"):
        return Severity.SEV1
    if tag_severity in ("high", "sev2"):
        return Severity.SEV2
    if tag_severity in ("medium", "warning", "sev3"):
        return Severity.SEV3
    if tag_severity in ("low", "sev4"):
        return Severity.SEV4

    # Check alarm name patterns (case-insensitive)
    lower_name = alarm_name.lower()
    if any(pat in lower_name for pat in ("critical", "p1", "sev1")):
        return Severity.SEV1
    if any(pat in lower_name for pat in ("high", "p2", "sev2")):
        return Severity.SEV2
    if any(pat in lower_name for pat in ("warning", "warn", "p3", "sev3")):
        return Severity.SEV3

    return Severity.SEV3
