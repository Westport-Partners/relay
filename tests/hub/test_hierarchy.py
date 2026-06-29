"""Tests for OrgNode, OrgTree, environment derivation, deployment_id derivation,
fleet rollup, and config schema models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from relay.config.schema import (
    CatalogConfig,
    EnvironmentDef,
    EnvironmentsConfig,
)
from relay.core.model import OrgNode, OrgTree
from relay.hub.health import FleetTile, Liveness, compute_rollup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tree() -> OrgTree:
    nodes = [
        OrgNode(id="pl1", name="Platform", level="product_line", parent=None),
        OrgNode(id="prod1", name="Auth Service", level="product", parent="pl1"),
        OrgNode(id="comp1", name="API", level="component", parent="prod1"),
        OrgNode(id="dep1", name="auth-api-prod", level="deployment", parent="comp1",
                owner_ref="team-auth", metadata={"gitlab_project": "auth/api"}),
        OrgNode(id="dep2", name="auth-api-dev", level="deployment", parent="comp1"),
        OrgNode(id="comp2", name="Worker", level="component", parent="prod1",
                owner_ref="team-platform"),
        OrgNode(id="dep3", name="auth-worker-prod", level="deployment", parent="comp2"),
    ]
    return OrgTree(nodes=nodes, leaf_level="deployment")


def make_tile(deployment_id: str, status: str, environment: str = "prod") -> FleetTile:
    return FleetTile(
        account_id="123456789012",
        app_name=deployment_id,
        environment=environment,
        deployment_id=deployment_id,
        status=status,
        liveness=Liveness.LIVE,
        open_incidents=0,
        worst_severity=None,
        last_heartbeat_at=datetime.now(UTC),
        registered_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# OrgTree build/validate
# ---------------------------------------------------------------------------

class TestOrgTreeBuild:
    def test_build_valid_tree(self):
        tree = make_tree()
        assert len(tree.all_nodes()) == 7

    def test_unknown_parent_raises(self):
        nodes = [
            OrgNode(id="n1", name="A", level="product_line", parent="nonexistent"),
        ]
        with pytest.raises(ValueError, match="unknown parent"):
            OrgTree(nodes=nodes)

    def test_cycle_raises(self):
        nodes = [
            OrgNode(id="n1", name="A", level="product_line", parent="n2"),
            OrgNode(id="n2", name="B", level="product", parent="n1"),
        ]
        with pytest.raises(ValueError, match="Cycle"):
            OrgTree(nodes=nodes)

    def test_roots(self):
        tree = make_tree()
        roots = tree.roots()
        assert len(roots) == 1
        assert roots[0].id == "pl1"


class TestAncestors:
    def test_ancestors_leaf_to_root(self):
        tree = make_tree()
        ancestors = tree.ancestors("dep1")
        ancestor_ids = [a.id for a in ancestors]
        # Should be comp1, prod1, pl1 in that order (leaf->root)
        assert ancestor_ids == ["comp1", "prod1", "pl1"]

    def test_ancestors_root_returns_empty(self):
        tree = make_tree()
        assert tree.ancestors("pl1") == []


class TestDescendantDeployments:
    def test_descendant_deployments_from_root(self):
        tree = make_tree()
        deps = tree.descendant_deployments("pl1")
        dep_ids = {d.id for d in deps}
        assert dep_ids == {"dep1", "dep2", "dep3"}

    def test_descendant_deployments_from_component(self):
        tree = make_tree()
        deps = tree.descendant_deployments("comp1")
        dep_ids = {d.id for d in deps}
        assert dep_ids == {"dep1", "dep2"}

    def test_leaf_is_its_own_deployment(self):
        tree = make_tree()
        deps = tree.descendant_deployments("dep1")
        assert [d.id for d in deps] == ["dep1"]


class TestResolveOwnerRef:
    def test_leaf_owner_wins(self):
        tree = make_tree()
        # dep1 has owner_ref="team-auth"
        assert tree.resolve_owner_ref("dep1") == "team-auth"

    def test_inherits_from_component(self):
        tree = make_tree()
        # dep3 has no owner_ref, comp2 has owner_ref="team-platform"
        assert tree.resolve_owner_ref("dep3") == "team-platform"

    def test_no_owner_returns_none(self):
        tree = make_tree()
        # dep2 has no owner_ref, comp1 has no owner_ref, prod1 has none, pl1 has none
        assert tree.resolve_owner_ref("dep2") is None

    def test_leaf_wins_over_ancestor(self):
        # dep1 has owner_ref="team-auth", comp2 has "team-platform"
        tree = make_tree()
        assert tree.resolve_owner_ref("dep1") == "team-auth"  # leaf wins


class TestResolveMetadata:
    def test_leaf_wins_on_key_conflict(self):
        nodes = [
            OrgNode(id="pl1", name="PL", level="product_line", metadata={"env": "prod", "team": "platform"}),
            OrgNode(id="dep1", name="dep", level="deployment", parent="pl1", metadata={"team": "auth"}),
        ]
        tree = OrgTree(nodes=nodes, leaf_level="deployment")
        meta = tree.resolve_metadata("dep1")
        assert meta["env"] == "prod"
        assert meta["team"] == "auth"  # leaf wins

    def test_empty_metadata_on_unknown(self):
        tree = make_tree()
        assert tree.resolve_metadata("nonexistent") == {}


class TestServicePath:
    def test_service_path_root_to_leaf(self):
        tree = make_tree()
        path = tree.resolve_service_path("dep1")
        assert path == ["Platform", "Auth Service", "API", "auth-api-prod"]

    def test_service_path_unknown_returns_empty(self):
        tree = make_tree()
        assert tree.resolve_service_path("nonexistent") == []


# ---------------------------------------------------------------------------
# CatalogConfig validation
# ---------------------------------------------------------------------------

class TestCatalogConfig:
    def test_duplicate_id_raises(self):
        with pytest.raises(Exception, match="Duplicate"):
            CatalogConfig(nodes=[
                OrgNode(id="n1", name="A", level="product_line"),
                OrgNode(id="n1", name="B", level="product_line"),
            ])

    def test_valid_catalog(self):
        cfg = CatalogConfig(nodes=[
            OrgNode(id="n1", name="A", level="product_line"),
            OrgNode(id="n2", name="B", level="product", parent="n1"),
        ])
        assert len(cfg.nodes) == 2


# ---------------------------------------------------------------------------
# EnvironmentsConfig validation
# ---------------------------------------------------------------------------

class TestEnvironmentsConfig:
    def test_duplicate_name_raises(self):
        with pytest.raises(Exception, match="Duplicate"):
            EnvironmentsConfig(environments=[
                EnvironmentDef(name="prod"),
                EnvironmentDef(name="prod"),
            ])

    def test_valid_config(self):
        cfg = EnvironmentsConfig(
            environments=[
                EnvironmentDef(name="prod"),
                EnvironmentDef(name="dev"),
            ],
            default_environment="dev",
        )
        assert len(cfg.environments) == 2


# ---------------------------------------------------------------------------
# Fleet rollup
# ---------------------------------------------------------------------------

class TestFleetRollup:
    def test_rollup_worst_of_descendants(self):
        tree = make_tree()
        tiles = [
            make_tile("dep1", "red"),
            make_tile("dep2", "green"),
            make_tile("dep3", "degraded"),
        ]
        rollup = compute_rollup(tiles, tree)
        assert len(rollup) == 1
        pl_rollup = rollup[0]
        assert pl_rollup["id"] == "pl1"
        assert pl_rollup["status"] == "red"  # worst-of

    def test_rollup_counts(self):
        tree = make_tree()
        tiles = [
            make_tile("dep1", "red"),
            make_tile("dep2", "green"),
            make_tile("dep3", "degraded"),
        ]
        rollup = compute_rollup(tiles, tree)
        pl = rollup[0]
        assert pl["red_count"] == 1
        assert pl["green_count"] == 1
        assert pl["degraded_count"] == 1

    def test_rollup_all_green(self):
        tree = make_tree()
        tiles = [
            make_tile("dep1", "green"),
            make_tile("dep2", "green"),
            make_tile("dep3", "green"),
        ]
        rollup = compute_rollup(tiles, tree)
        assert rollup[0]["status"] == "green"

    def test_rollup_no_tiles_is_grey(self):
        tree = make_tree()
        rollup = compute_rollup([], tree)
        assert rollup[0]["status"] == "grey"

    def test_rollup_nested_children(self):
        tree = make_tree()
        tiles = [make_tile("dep1", "red"), make_tile("dep2", "green"), make_tile("dep3", "green")]
        rollup = compute_rollup(tiles, tree)
        prod1_child = rollup[0]["children"][0]  # prod1
        assert prod1_child["id"] == "prod1"
        assert prod1_child["status"] == "red"


# ---------------------------------------------------------------------------
# FleetTile key
# ---------------------------------------------------------------------------

class TestFleetTileKey:
    def test_key_is_env_slash_deployment(self):
        tile = make_tile("auth-api-prod", "green", environment="prod")
        assert tile.key == "prod/auth-api-prod"
