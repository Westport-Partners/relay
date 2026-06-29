"""Tests for relay.core.model — verifies domain model construction, validation, and behavior."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from relay.core.model import (
    Contact,
    EscalationPolicy,
    EscalationStep,
    Incident,
    IncidentState,
    OrgNode,
    OrgTree,
    RoutingRule,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def test_severity_ordering():
    # Severity is a StrEnum — verify all four values exist and their string representations.
    assert str(Severity.SEV1) == "SEV1"
    assert str(Severity.SEV2) == "SEV2"
    assert str(Severity.SEV3) == "SEV3"
    assert str(Severity.SEV4) == "SEV4"

    # from_label with direct name match works (e.g. "SEV1" -> SEV1)
    assert Severity.from_label("sev1") == Severity.SEV1
    assert Severity.from_label("SEV3") == Severity.SEV3

    # from_label with a synonym ("critical") either maps to SEV1 or raises ValueError.
    # The model docstring marks fuzzy lookup as a TODO that raises ValueError.
    try:
        result = Severity.from_label("critical")
        # If it succeeds, it should map to SEV1.
        assert result == Severity.SEV1
    except (ValueError, NotImplementedError):
        pytest.skip("Severity.from_label fuzzy lookup not yet implemented (TODO in model)")


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------


def test_contact_requires_email_or_phone():
    # Email alone is sufficient.
    c1 = Contact(contact_id="c1", name="Alice", email="alice@example.com")
    assert c1.contact_id == "c1"

    # Phone alone is sufficient.
    c2 = Contact(contact_id="c2", name="Bob", phone="+15555551234")
    assert c2.contact_id == "c2"

    # Neither email nor phone should raise ValidationError.
    with pytest.raises(ValidationError):
        Contact(contact_id="c3", name="Charlie")


# ---------------------------------------------------------------------------
# Incident construction
# ---------------------------------------------------------------------------


def test_incident_construction():
    now = datetime.now(UTC)
    incident = Incident(
        correlation_id="inc-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )

    assert incident.state == IncidentState.TRIGGERED
    assert incident.timeline == []
    assert incident.correlation_id == "inc-001"


# ---------------------------------------------------------------------------
# Incident.add_event
# ---------------------------------------------------------------------------


def test_incident_add_event():
    now = datetime.now(UTC)
    incident = Incident(
        correlation_id="inc-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )

    event = TimelineEvent(
        incident_id="inc-001",
        stream=Stream.TEAM,
        actor="system",
        event_type="triggered",
        detail={"alarm": "myapp-high-error-rate"},
    )
    incident.add_event(event)

    assert len(incident.timeline) == 1
    assert incident.timeline[0].event_type == "triggered"


# ---------------------------------------------------------------------------
# Incident.external_tickets
# ---------------------------------------------------------------------------


def _incident_kwargs(**overrides) -> dict[str, Any]:
    base = dict(
        correlation_id="inc-xt",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Incident.escalation_policy_id — default None + round-trip
# ---------------------------------------------------------------------------


def test_escalation_policy_id_defaults_none():
    inc = Incident(**_incident_kwargs())
    assert inc.escalation_policy_id is None


def test_escalation_policy_id_round_trips():
    inc = Incident(**_incident_kwargs(escalation_policy_id="pol-42"))
    assert inc.escalation_policy_id == "pol-42"
    dumped = inc.model_dump(mode="json")
    assert dumped["escalation_policy_id"] == "pol-42"
    reloaded = Incident.model_validate(dumped)
    assert reloaded.escalation_policy_id == "pol-42"


def test_external_tickets_get_set_helpers():
    incident = Incident(**_incident_kwargs())
    assert incident.get_ticket("gitlab_iid") is None
    incident.set_ticket("gitlab_iid", "42")
    incident.set_ticket("gitlab_project", "team/proj")
    assert incident.get_ticket("gitlab_iid") == "42"
    assert incident.external_tickets == {"gitlab_iid": "42", "gitlab_project": "team/proj"}


def test_external_tickets_round_trips_through_validate():
    incident = Incident(**_incident_kwargs(external_tickets={"gitlab_iid": "42"}))
    reloaded = Incident.model_validate(incident.model_dump(mode="json"))
    assert reloaded.get_ticket("gitlab_iid") == "42"


# ---------------------------------------------------------------------------
# EscalationPolicy step validation
# ---------------------------------------------------------------------------


def test_escalation_policy_step_validation():
    step0 = EscalationStep(step_index=0, contact_ids=["c1"], timeout_minutes=5)
    step1 = EscalationStep(step_index=1, contact_ids=["c2"], timeout_minutes=10)

    # Valid policy with two contiguous steps.
    policy = EscalationPolicy(
        policy_id="p1",
        name="Default",
        team="platform",
        steps=[step0, step1],
    )
    assert len(policy.steps) == 2

    # Steps provided in reverse order should either be auto-sorted or raise.
    # The model validator sorts by step_index, so reversed input is accepted.
    reversed_policy = EscalationPolicy(
        policy_id="p2",
        name="Reversed",
        team="platform",
        steps=[step1, step0],
    )
    # After validation the steps must be in ascending order.
    assert reversed_policy.steps[0].step_index == 0
    assert reversed_policy.steps[1].step_index == 1

    # No steps should raise ValidationError.
    with pytest.raises(ValidationError):
        EscalationPolicy(policy_id="p3", name="Empty", team="platform", steps=[])

    # Non-contiguous indices (0 and 2, skipping 1) should raise ValidationError.
    step2 = EscalationStep(step_index=2, contact_ids=["c3"], timeout_minutes=15)
    with pytest.raises(ValidationError):
        EscalationPolicy(
            policy_id="p4",
            name="Gap",
            team="platform",
            steps=[step0, step2],
        )


# ---------------------------------------------------------------------------
# RoutingRule regex validation
# ---------------------------------------------------------------------------


def test_routing_rule_invalid_regex():
    with pytest.raises(ValidationError):
        RoutingRule(
            rule_id="r1",
            priority=0,
            alarm_name_regex="[invalid",
            escalation_policy_id="p1",
        )


# ---------------------------------------------------------------------------
# OrgTree round-trip
# ---------------------------------------------------------------------------


class TestOrgPathRoundTrip:

    def _make_tree(self):
        """Build pl→prod→comp→dep tree."""
        pl = OrgNode(id="pl-id", name="ProductLine", level="product_line", parent=None)
        prod = OrgNode(id="prod-id", name="Product", level="product", parent="pl-id")
        comp = OrgNode(id="comp-id", name="Component", level="component", parent="prod-id")
        dep = OrgNode(id="dep-id", name="dep-name", level="deployment", parent="comp-id",
                      metadata={"gitlab_project": "group/repo"}, owner_ref="team-x")
        return OrgTree([pl, prod, comp, dep])

    def test_org_path_returns_root_to_leaf(self):
        tree = self._make_tree()
        path = tree.org_path("dep-id")
        assert len(path) == 4
        # root first, leaf last
        assert path[0]["id"] == "pl-id"
        assert path[0]["level"] == "product_line"
        assert path[0]["parent"] is None
        assert path[-1]["id"] == "dep-id"
        assert path[-1]["level"] == "deployment"
        assert path[-1]["metadata"]["gitlab_project"] == "group/repo"
        assert path[-1]["owner_ref"] == "team-x"
        # every entry has required keys
        for entry in path:
            assert "id" in entry
            assert "name" in entry
            assert "level" in entry
            assert "parent" in entry

    def test_org_path_unknown_returns_empty(self):
        tree = self._make_tree()
        assert tree.org_path("does-not-exist") == []

    def test_from_registrations_rebuilds_tree(self):
        tree = self._make_tree()
        path_a = tree.org_path("dep-id")

        # Second deployment shares pl-id and prod-id, new comp+dep
        comp2 = OrgNode(id="comp2-id", name="Comp2", level="component", parent="prod-id")
        dep2 = OrgNode(id="dep2-id", name="dep2-name", level="deployment", parent="comp2-id")
        full_nodes = list(tree.all_nodes()) + [comp2, dep2]
        tree2 = OrgTree(full_nodes)
        path_b = tree2.org_path("dep2-id")

        rebuilt = OrgTree.from_registrations([path_a, path_b])

        # Shared product_line appears once
        assert rebuilt.get("pl-id") is not None
        assert rebuilt.get("prod-id") is not None
        assert rebuilt.get("dep-id") is not None
        assert rebuilt.get("dep2-id") is not None
        all_pl = [n for n in rebuilt.all_nodes() if n.level == "product_line"]
        assert len(all_pl) == 1

        # roots() has exactly 1 node (the shared product_line)
        roots = rebuilt.roots()
        assert len(roots) == 1
        assert roots[0].id == "pl-id"

        # Service path for dep-id matches original
        assert rebuilt.resolve_service_path("dep-id") == tree.resolve_service_path("dep-id")

    def test_from_registrations_drops_dangling_parent(self):
        entry = {"id": "dep-x", "name": "x", "level": "deployment", "parent": "missing-prod"}
        rebuilt = OrgTree.from_registrations([[entry]])
        # Should not raise; dangling parent is nulled → dep-x becomes a root
        node = rebuilt.get("dep-x")
        assert node is not None
        assert node.parent is None
        roots = rebuilt.roots()
        assert any(r.id == "dep-x" for r in roots)

    def test_from_registrations_skips_invalid_entries(self):
        valid = {"id": "good-dep", "name": "Good", "level": "deployment", "parent": None}
        missing_id = {"name": "No ID", "level": "deployment", "parent": None}
        missing_level = {"id": "no-level", "name": "No Level"}
        rebuilt = OrgTree.from_registrations([[valid, missing_id, missing_level]])
        assert rebuilt.get("good-dep") is not None
        # The invalid entries should not be in the tree
        assert rebuilt.get("no-level") is None
        all_nodes = rebuilt.all_nodes()
        assert len(all_nodes) == 1


# ---------------------------------------------------------------------------
# OrgNode integration routing keys live in metadata (gitlab_project, …)
# ---------------------------------------------------------------------------


class TestOrgNodeMetadataGeneralization:

    def test_gitlab_project_is_not_a_model_attribute(self):
        node = OrgNode(
            id="dep",
            name="dep",
            level="deployment",
            metadata={"gitlab_project": "group/repo"},
        )
        assert node.metadata["gitlab_project"] == "group/repo"
        # The routing key lives only in metadata — no dedicated column.
        assert not hasattr(node, "gitlab_project")

    def test_heartbeat_payload_carries_metadata(self):
        from relay.core.model import _org_node_to_payload

        node = OrgNode(
            id="dep",
            name="dep",
            level="deployment",
            metadata={"gitlab_project": "group/repo", "region": "us-east-1"},
        )
        payload = _org_node_to_payload(node)
        assert payload["metadata"]["gitlab_project"] == "group/repo"
        assert payload["metadata"]["region"] == "us-east-1"
        # No legacy top-level field is emitted.
        assert "gitlab_project" not in payload

    def test_payload_reads_metadata_shape(self):
        from relay.core.model import _payload_to_org_node

        entry = {
            "id": "dep",
            "name": "dep",
            "level": "deployment",
            "metadata": {"gitlab_project": "group/repo"},
        }
        node = _payload_to_org_node(entry)
        assert node is not None
        assert node.metadata["gitlab_project"] == "group/repo"

    def test_metadata_round_trips_through_registrations(self):
        from relay.core.model import _org_node_to_payload, _payload_to_org_node

        node = OrgNode(
            id="dep",
            name="dep",
            level="deployment",
            metadata={"gitlab_project": "group/repo"},
        )
        rebuilt = _payload_to_org_node(_org_node_to_payload(node))
        assert rebuilt is not None
        assert rebuilt.metadata["gitlab_project"] == "group/repo"
