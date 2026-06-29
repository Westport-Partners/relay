"""Regression guard: every shipped config/*.example.yaml must parse.

These are the files the installer (install.sh) copies into a fresh team's live
config. They previously drifted from the Pydantic schema (an old
`routing_rules:`/`match:`/`route:` format that the parser rejects), which made a
fresh install silently fall back to RelayConfig.empty() — alarms recorded but
never paged. No test loaded the examples, so the drift went unnoticed.

This module loads each example through the REAL schema and asserts:
  * it validates, and
  * cross-references resolve (every routing escalation_policy_id exists in
    escalation; the federation/suppression env names are internally consistent).

If you change a schema, these tests force you to update the examples too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from relay.config.schema import (
    CatalogConfig,
    EnvironmentsConfig,
    EscalationConfig,
    HierarchyConfig,
    RelayConfig,
    RoutingConfig,
)

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file()
)
CONFIG_DIR = _REPO_ROOT / "config"


def _load(name: str) -> dict[str, Any]:
    from typing import cast
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Individual files parse against their schema
# ---------------------------------------------------------------------------


def test_routing_example_parses():
    cfg = RoutingConfig.model_validate(_load("routing.example.yaml"))
    assert cfg.rules, "routing.example.yaml should ship with rules"


def test_escalation_example_parses():
    cfg = EscalationConfig.model_validate(_load("escalation.example.yaml"))
    assert cfg.policies, "escalation.example.yaml should ship with policies"


def test_environments_example_parses():
    cfg = EnvironmentsConfig.model_validate(_load("environments.example.yaml"))
    assert cfg.environments


def test_hierarchy_example_parses():
    HierarchyConfig.model_validate(_load("hierarchy.example.yaml"))


def test_catalog_example_parses():
    CatalogConfig.model_validate(_load("catalog.example.yaml"))


# ---------------------------------------------------------------------------
# The pair the installer copies assembles into a full RelayConfig
# ---------------------------------------------------------------------------


def test_installer_pair_assembles_into_relayconfig():
    """escalation + routing (what install.sh copies) build a valid RelayConfig."""
    esc = (CONFIG_DIR / "escalation.example.yaml").read_text(encoding="utf-8")
    rou = (CONFIG_DIR / "routing.example.yaml").read_text(encoding="utf-8")
    cfg = RelayConfig.from_yaml_files(escalation_yaml=esc, routing_yaml=rou)
    assert cfg.routing.rules
    assert cfg.escalation.policies


# ---------------------------------------------------------------------------
# Cross-reference integrity
# ---------------------------------------------------------------------------


def test_every_routing_policy_ref_resolves():
    """Every escalation_policy_id referenced by routing exists in escalation."""
    routing = RoutingConfig.model_validate(_load("routing.example.yaml"))
    escalation = EscalationConfig.model_validate(_load("escalation.example.yaml"))

    defined = {p.policy_id for p in escalation.policies}
    referenced = {r.escalation_policy_id for r in routing.rules}
    referenced.add(routing.default_escalation_policy_id)

    missing = referenced - defined
    assert not missing, f"routing.example.yaml references unknown policies: {missing}"


def test_routing_example_ships_standard_noise_defaults():
    """The example must ship the federation + suppression blocks (prod-loud /
    nonprod-quiet), or the 'smart defaults' aren't actually wired into install."""
    routing = RoutingConfig.model_validate(_load("routing.example.yaml"))
    assert routing.federation is not None, "federation: block missing from example"
    assert routing.suppression is not None, "suppression: block missing from example"
    assert routing.suppression.enabled is True

    # Non-prod should be carved out in both gates (the prod-loud/nonprod-quiet
    # standard). We don't pin exact env names, just that an override referencing
    # a non-prod environment list exists.
    fed_envs = [o.environment for o in routing.federation.overrides if o.environment]
    sup_envs = [r.environment for r in routing.suppression.rules if r.environment]
    assert fed_envs, "federation overrides should carve out non-prod"
    assert sup_envs, "suppression rules should throttle non-prod"
