"""Tests for CloudWatchAlarmSource._derive_deployment_id project-tag matching.

Covers the generic ``relay:project`` tag matched against a node's
``metadata["gitlab_project"]``.
"""

from __future__ import annotations

from relay.adapters.aws.cloudwatch_source import CloudWatchAlarmSource
from relay.core.model import OrgNode, OrgTree


def _tree() -> OrgTree:
    return OrgTree(
        [
            OrgNode(id="pl", name="Platform", level="product_line"),
            OrgNode(
                id="dep-auth",
                name="auth-api-prod",
                level="deployment",
                parent="pl",
                metadata={"gitlab_project": "identity/auth-api"},
            ),
        ]
    )


def _source() -> CloudWatchAlarmSource:
    return CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")


def test_relay_project_tag_matches_node_metadata():
    src = _source()
    dep_id, path = src._derive_deployment_id(
        "some-unrelated-alarm", {"relay:project": "identity/auth-api"}, _tree()
    )
    assert dep_id == "dep-auth"
    assert path[-1] == "auth-api-prod"


def test_no_match_falls_through_to_unknown():
    src = _source()
    dep_id, path = src._derive_deployment_id(
        "zzz", {"relay:project": "not/a-project"}, _tree()
    )
    assert dep_id == "unknown"
    assert path == []


# ---------------------------------------------------------------------------
# COMPONENT_ID join-key tests
# ---------------------------------------------------------------------------


def _tree_with_component_id() -> OrgTree:
    return OrgTree(
        [
            OrgNode(id="pl", name="Platform", level="product_line"),
            OrgNode(
                id="dep-payments",
                name="payments-api-prod",
                level="deployment",
                parent="pl",
                metadata={"component_id": "comp-payments-123"},
            ),
        ]
    )


def test_component_id_matches_node_by_id():
    """COMPONENT_ID that equals a node id resolves directly."""
    tree = OrgTree(
        [
            OrgNode(id="pl", name="Platform", level="product_line"),
            OrgNode(
                id="comp-direct-id",
                name="direct-service",
                level="deployment",
                parent="pl",
            ),
        ]
    )
    src = _source()
    dep_id, path = src._derive_deployment_id(
        "some-alarm", {"COMPONENT_ID": "comp-direct-id"}, tree
    )
    assert dep_id == "comp-direct-id"
    assert path[-1] == "direct-service"


def test_component_id_matches_node_by_metadata():
    """COMPONENT_ID that matches metadata["component_id"] resolves to that node."""
    src = _source()
    dep_id, path = src._derive_deployment_id(
        "some-alarm", {"COMPONENT_ID": "comp-payments-123"}, _tree_with_component_id()
    )
    assert dep_id == "dep-payments"
    assert path[-1] == "payments-api-prod"


def test_relay_deployment_wins_over_component_id():
    """relay:deployment tag takes precedence over COMPONENT_ID."""
    tree = OrgTree(
        [
            OrgNode(id="pl", name="Platform", level="product_line"),
            OrgNode(
                id="dep-auth",
                name="auth-api-prod",
                level="deployment",
                parent="pl",
                metadata={"component_id": "comp-auth-999"},
            ),
            OrgNode(
                id="dep-other",
                name="other-service",
                level="deployment",
                parent="pl",
            ),
        ]
    )
    src = _source()
    dep_id, path = src._derive_deployment_id(
        "some-alarm",
        {"relay:deployment": "dep-other", "COMPONENT_ID": "comp-auth-999"},
        tree,
    )
    # relay:deployment should win
    assert dep_id == "dep-other"
    assert path[-1] == "other-service"


# ---------------------------------------------------------------------------
# parse_event + tag_resolver integration tests
# ---------------------------------------------------------------------------



def _minimal_alarm_event(alarm_name: str = "team-app-cpu-alarm") -> dict:
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": alarm_name,
            "alarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:" + alarm_name,
            "state": {"value": "ALARM"},
            "configuration": {
                "metrics": [
                    {
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/Lambda",
                                "dimensions": [{"name": "FunctionName", "value": "my-func"}],
                            }
                        }
                    }
                ]
            },
        },
    }


class _StubTagResolver:
    def __init__(self, tags: dict):
        self._tags = tags

    def resolve(self, *, alarm_arn, detail):
        return self._tags


def test_parse_event_populates_tags_from_resolver():
    stub = _StubTagResolver({"relay:app": "my-payments", "COMPONENT_ID": "comp-123", "GIT_SHA": "deadbeef"})
    src = CloudWatchAlarmSource(
        account_id="123456789012",
        region="us-east-1",
        tag_resolver=stub,
    )
    incident = src.parse_event(_minimal_alarm_event())
    assert incident.tags == {"relay:app": "my-payments", "COMPONENT_ID": "comp-123", "GIT_SHA": "deadbeef"}


def test_parse_event_relay_app_tag_becomes_app_name():
    stub = _StubTagResolver({"relay:app": "my-payments"})
    src = CloudWatchAlarmSource(
        account_id="123456789012",
        region="us-east-1",
        tag_resolver=stub,
    )
    incident = src.parse_event(_minimal_alarm_event("team-svc-cpu-alarm"))
    # relay:app tag should override the name-convention derivation
    assert incident.app_name == "my-payments"


# ---------------------------------------------------------------------------
# synthetic marker tests
# ---------------------------------------------------------------------------


def _alarm_event_with_top_level_marker() -> dict:
    """Event with relay_synthetic=True at the EventBridge envelope level."""
    ev = _minimal_alarm_event()
    ev["relay_synthetic"] = True
    return ev


def _alarm_event_with_detail_marker() -> dict:
    """Event with relay_synthetic=True inside the detail dict."""
    ev = _minimal_alarm_event()
    ev["detail"]["relay_synthetic"] = True
    return ev


def test_parse_event_synthetic_false_by_default():
    """Incidents from normal alarm events are not flagged synthetic."""
    src = CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")
    incident = src.parse_event(_minimal_alarm_event())
    assert incident.synthetic is False


def test_parse_event_synthetic_true_from_top_level_marker():
    """relay_synthetic=True on the envelope sets Incident.synthetic=True."""
    src = CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")
    incident = src.parse_event(_alarm_event_with_top_level_marker())
    assert incident.synthetic is True


def test_parse_event_synthetic_true_from_detail_marker():
    """relay_synthetic=True inside detail dict sets Incident.synthetic=True."""
    src = CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")
    incident = src.parse_event(_alarm_event_with_detail_marker())
    assert incident.synthetic is True


def test_parse_event_synthetic_not_set_by_truthy_value():
    """relay_synthetic truthy but not exactly True should NOT set synthetic=True."""
    src = CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")
    ev = _minimal_alarm_event()
    ev["relay_synthetic"] = "yes"  # truthy string, not exactly True
    incident = src.parse_event(ev)
    assert incident.synthetic is False


def test_parse_event_synthetic_independent_of_signal_source():
    """synthetic flag is orthogonal to signal_source=SYNTHETIC (canary alarms).

    A canary alarm (SignalSource.SYNTHETIC) without the relay_synthetic marker
    should still have incident.synthetic=False.
    """
    src = CloudWatchAlarmSource(account_id="123456789012", region="us-east-1")
    ev = _minimal_alarm_event("prod-canary-heartbeat")
    incident = src.parse_event(ev)
    # It's a canary (signal_source=SYNTHETIC) but not a test incident.
    from relay.core.model import SignalSource
    assert incident.signal_source == SignalSource.SYNTHETIC
    assert incident.synthetic is False
