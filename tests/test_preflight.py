"""Tests for relay.config.preflight — evaluate_metadata and generate_placeholder."""

from __future__ import annotations

from datetime import UTC, datetime

from relay.adapters.registry import AdapterManifest
from relay.config.preflight import (
    MetadataCheck,
    PreflightReport,
    evaluate_metadata,
    generate_placeholder,
)
from relay.config.schema import (
    CatalogConfig,
    DeploymentDefaults,
    EscalationConfig,
    HierarchyConfig,
    RelayConfig,
    RoutingConfig,
)
from relay.core.model import OrgNode, OrgTree

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(
    *,
    nodes: list[OrgNode] | None = None,
    leaf_level: str = "deployment",
    tag_map: dict[str, str] | None = None,
) -> RelayConfig:
    """Return a minimal RelayConfig with optional catalog + hierarchy."""
    base = RelayConfig(
        escalation=EscalationConfig(policies=[]),
        routing=RoutingConfig(
            rules=[],
            default_escalation_policy_id="",
            default_streams=[],
        ),
        loaded_at=datetime.now(UTC),
    )
    if nodes is not None:
        catalog = CatalogConfig(nodes=nodes)
        org_tree = OrgTree(nodes=nodes, leaf_level=leaf_level)
        dd = DeploymentDefaults(tag_map=tag_map or {})
        hier = HierarchyConfig(
            levels=["component", leaf_level],
            leaf_level=leaf_level,
            deployment_defaults=dd,
        )
        return base.model_copy(
            update={"catalog": catalog, "org_tree": org_tree, "hierarchy": hier}
        )
    return base


def _leaf(node_id: str, metadata: dict | None = None, parent: str | None = None) -> OrgNode:
    return OrgNode(
        id=node_id,
        name=node_id,
        level="deployment",
        parent=parent,
        metadata=metadata or {},
    )


def _manifest(
    name: str,
    required_metadata: tuple[str, ...] = (),
    suggested_tag_map: dict[str, str] | None = None,
) -> AdapterManifest:
    return AdapterManifest(
        name=name,
        build=lambda ctx: None,
        required_metadata=required_metadata,
        suggested_tag_map=suggested_tag_map or {},
    )


# ---------------------------------------------------------------------------
# MetadataCheck / PreflightReport helpers
# ---------------------------------------------------------------------------


class TestPreflightReport:
    def test_ok_when_no_missing(self) -> None:
        report = PreflightReport(
            checks=[
                MetadataCheck("dep-1", "gitlab", "gitlab_project", "literal"),
                MetadataCheck("dep-2", "gitlab", "gitlab_project", "tag_map"),
            ]
        )
        assert report.ok is True
        assert report.missing == []

    def test_not_ok_when_missing(self) -> None:
        report = PreflightReport(
            checks=[
                MetadataCheck("dep-1", "gitlab", "gitlab_project", "missing", suggestion="hint"),
            ]
        )
        assert report.ok is False
        assert len(report.missing) == 1

    def test_format_contains_adapter_and_status(self) -> None:
        report = PreflightReport(
            checks=[
                MetadataCheck("dep-1", "gitlab", "gitlab_project", "literal"),
            ]
        )
        text = report.format()
        assert "gitlab" in text
        assert "literal" in text
        assert "dep-1" in text

    def test_format_pass_when_ok(self) -> None:
        report = PreflightReport(
            checks=[MetadataCheck("d", "a", "k", "literal")]
        )
        assert "PASS" in report.format()

    def test_format_fail_when_missing(self) -> None:
        report = PreflightReport(
            checks=[MetadataCheck("d", "a", "k", "missing")]
        )
        assert "FAIL" in report.format()

    def test_format_no_checks(self) -> None:
        text = PreflightReport().format()
        assert "nothing to check" in text


# ---------------------------------------------------------------------------
# evaluate_metadata — core status cases
# ---------------------------------------------------------------------------


class TestEvaluateMetadata:

    def test_literal_hit(self) -> None:
        """A plain string in node metadata → status 'literal'."""
        node = _leaf("dep-1", {"gitlab_project": "team/auth"})
        config = _make_config(nodes=[node])
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert len(report.checks) == 1
        assert report.checks[0].status == "literal"
        assert report.checks[0].suggestion is None
        assert report.ok is True

    def test_template_hit(self) -> None:
        """A ${tag:NAME} value → status 'template'."""
        node = _leaf("dep-1", {"gitlab_project": "${tag:GITLAB_PROJECT_ID}"})
        config = _make_config(nodes=[node])
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert report.checks[0].status == "template"
        assert report.ok is True

    def test_tag_map_hit(self) -> None:
        """Key present in deployment_defaults.tag_map → status 'tag_map'."""
        node = _leaf("dep-1")  # no per-node metadata
        config = _make_config(nodes=[node], tag_map={"gitlab_project": "GITLAB_PROJECT_ID"})
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert report.checks[0].status == "tag_map"
        assert report.ok is True

    def test_missing_with_suggestion(self) -> None:
        """Key absent, suggested_tag_map present → suggestion text included."""
        node = _leaf("dep-1")
        config = _make_config(nodes=[node])
        manifest = _manifest(
            "gitlab",
            required_metadata=("gitlab_project",),
            suggested_tag_map={"gitlab_project": "GITLAB_PROJECT_ID"},
        )
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        check = report.checks[0]
        assert check.status == "missing"
        assert check.suggestion is not None
        assert "GITLAB_PROJECT_ID" in check.suggestion
        assert report.ok is False

    def test_missing_without_suggestion(self) -> None:
        """Key absent, no suggested_tag_map entry → generic suggestion."""
        node = _leaf("dep-1")
        config = _make_config(nodes=[node])
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        check = report.checks[0]
        assert check.status == "missing"
        assert check.suggestion is not None
        assert "SOME_TAG" in check.suggestion

    def test_enabled_vs_not_gating(self) -> None:
        """Adapter not in enabled_adapters is excluded from checks entirely."""
        node = _leaf("dep-1")
        config = _make_config(nodes=[node])
        gitlab = _manifest("gitlab", required_metadata=("gitlab_project",))
        teams = _manifest("teams", required_metadata=("teams_channel",))
        report = evaluate_metadata(
            config=config,
            manifests=[gitlab, teams],
            enabled_adapters={"teams"},  # gitlab excluded
        )
        adapters_in_report = {c.adapter for c in report.checks}
        assert "gitlab" not in adapters_in_report
        assert "teams" in adapters_in_report

    def test_null_safe_no_org_tree(self) -> None:
        """Config with no org_tree returns an empty report without raising."""
        config = _make_config()  # no nodes → no org_tree
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert report.checks == []
        assert report.ok is True

    def test_null_safe_no_hierarchy(self) -> None:
        """Config with no hierarchy falls back to all nodes as leaves."""
        node = _leaf("dep-1")
        # Build config without hierarchy manually to exercise the no-leaf_level branch
        base = RelayConfig(
            escalation=EscalationConfig(policies=[]),
            routing=RoutingConfig(
                rules=[], default_escalation_policy_id="", default_streams=[]
            ),
            loaded_at=datetime.now(UTC),
        )
        org_tree = OrgTree(nodes=[node], leaf_level="deployment")
        config = base.model_copy(update={"org_tree": org_tree, "hierarchy": None})
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        # Should not raise; should return a check
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert len(report.checks) == 1

    def test_multiple_leaves_multiple_checks(self) -> None:
        """One check per (leaf, key) pair."""
        nodes = [_leaf("dep-1"), _leaf("dep-2"), _leaf("dep-3")]
        config = _make_config(nodes=nodes)
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert len(report.checks) == 3
        dep_ids = {c.deployment_id for c in report.checks}
        assert dep_ids == {"dep-1", "dep-2", "dep-3"}

    def test_inherited_metadata_counts(self) -> None:
        """Metadata inherited from a parent node satisfies the requirement."""
        parent = OrgNode(
            id="comp-1",
            name="comp-1",
            level="component",
            metadata={"gitlab_project": "inherited/project"},
        )
        child = OrgNode(
            id="dep-1",
            name="dep-1",
            level="deployment",
            parent="comp-1",
            metadata={},
        )
        # hierarchy with two levels
        catalog = CatalogConfig(nodes=[parent, child])
        org_tree = OrgTree(nodes=[parent, child], leaf_level="deployment")
        hier = HierarchyConfig(
            levels=["component", "deployment"],
            leaf_level="deployment",
            deployment_defaults=DeploymentDefaults(tag_map={}),
        )
        base = RelayConfig(
            escalation=EscalationConfig(policies=[]),
            routing=RoutingConfig(
                rules=[], default_escalation_policy_id="", default_streams=[]
            ),
            loaded_at=datetime.now(UTC),
        )
        config = base.model_copy(
            update={"catalog": catalog, "org_tree": org_tree, "hierarchy": hier}
        )
        manifest = _manifest("gitlab", required_metadata=("gitlab_project",))
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"gitlab"}
        )
        assert report.checks[0].status == "literal"
        assert report.ok is True

    def test_adapter_without_required_metadata_skipped(self) -> None:
        """Manifests with empty required_metadata produce no checks."""
        node = _leaf("dep-1")
        config = _make_config(nodes=[node])
        manifest = _manifest("servicenow")  # no required_metadata
        report = evaluate_metadata(
            config=config, manifests=[manifest], enabled_adapters={"servicenow"}
        )
        assert report.checks == []


# ---------------------------------------------------------------------------
# generate_placeholder
# ---------------------------------------------------------------------------


class TestGeneratePlaceholder:

    def test_basic_shape(self) -> None:
        """Stub starts with '- id:' and contains match/deployment_tag."""
        manifest = _manifest(
            "gitlab",
            required_metadata=("gitlab_project",),
            suggested_tag_map={"gitlab_project": "GITLAB_PROJECT_ID"},
        )
        stub = generate_placeholder(
            deployment_id="dep-my-svc-prod",
            component_tag="my-svc-prod",
            manifests=[manifest],
            enabled_adapters={"gitlab"},
        )
        assert stub.strip().startswith("- id: dep-my-svc-prod")
        assert "deployment_tag: my-svc-prod" in stub
        assert "metadata:" in stub
        assert "GITLAB_PROJECT_ID" in stub

    def test_disabled_adapter_omitted(self) -> None:
        """Metadata hints for disabled adapters are not included."""
        manifest = _manifest(
            "gitlab",
            required_metadata=("gitlab_project",),
            suggested_tag_map={"gitlab_project": "GITLAB_PROJECT_ID"},
        )
        stub = generate_placeholder(
            deployment_id="dep-1",
            component_tag="svc",
            manifests=[manifest],
            enabled_adapters=set(),  # gitlab not enabled
        )
        assert "GITLAB_PROJECT_ID" not in stub
        assert "metadata:" not in stub

    def test_no_required_metadata_no_block(self) -> None:
        """When no adapter has required_metadata, no metadata block is emitted."""
        manifest = _manifest("servicenow")
        stub = generate_placeholder(
            deployment_id="dep-1",
            component_tag="svc",
            manifests=[manifest],
            enabled_adapters={"servicenow"},
        )
        assert "metadata:" not in stub

    def test_deduplicates_shared_keys(self) -> None:
        """When two adapters share a required_metadata key, it appears once."""
        m1 = _manifest(
            "a",
            required_metadata=("shared_key",),
            suggested_tag_map={"shared_key": "TAG_A"},
        )
        m2 = _manifest(
            "b",
            required_metadata=("shared_key",),
            suggested_tag_map={"shared_key": "TAG_B"},
        )
        stub = generate_placeholder(
            deployment_id="dep-1",
            component_tag="svc",
            manifests=[m1, m2],
            enabled_adapters={"a", "b"},
        )
        # key should appear exactly once
        assert stub.count("shared_key") == 1

    def test_deterministic_output(self) -> None:
        """Repeated calls with the same inputs produce identical output."""
        manifests = [
            _manifest("b", required_metadata=("b_key",), suggested_tag_map={"b_key": "B_TAG"}),
            _manifest("a", required_metadata=("a_key",), suggested_tag_map={"a_key": "A_TAG"}),
        ]
        stub1 = generate_placeholder(
            deployment_id="dep-1",
            component_tag="svc",
            manifests=manifests,
            enabled_adapters={"a", "b"},
        )
        stub2 = generate_placeholder(
            deployment_id="dep-1",
            component_tag="svc",
            manifests=manifests,
            enabled_adapters={"a", "b"},
        )
        assert stub1 == stub2
        # a should come before b (sorted by adapter name)
        assert stub1.index("a_key") < stub1.index("b_key")
