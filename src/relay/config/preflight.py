"""relay.config.preflight — Catalog metadata preflight gate and placeholder generator.

Checks every catalog leaf node against each enabled adapter's ``required_metadata``
and reports which keys are satisfied (literal, tag_map, or ${tag:} template) and
which are missing.  Also generates paste-ready YAML stubs for unconfigured leaves.

Design decisions
----------------
* **Never raise.**  All evaluation functions catch exceptions internally so a bad
  catalog or partial config never prevents a deployment from starting.
* **Pure / no AWS.**  No AWS SDK calls; fully testable offline.
* **Stateless.**  No caches; callers control how often they run the check.

CLI usage::

    relay-preflight --config-dir config/
    relay-preflight --config-dir config/ --json
    relay-preflight --config-dir config/ --all
    relay-preflight --config-dir config/ --adapter gitlab
    relay-preflight --config-dir config/ --generate-placeholders dep-auth-api-prod=auth-api-prod

Exit code is 1 when any ``missing`` checks exist for enabled adapters; 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relay.adapters.registry import AdapterManifest
    from relay.config.schema import RelayConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetadataCheck:
    """Result of checking one required-metadata key for one deployment/adapter pair.

    Attributes:
        deployment_id: Catalog leaf node id being checked.
        adapter:       Adapter name (e.g. ``"gitlab"``).
        key:           The required-metadata key (e.g. ``"gitlab_project"``).
        status:        One of ``"literal"``, ``"tag_map"``, ``"template"``, ``"missing"``.
        suggestion:    For ``"missing"`` checks: a ready-to-paste YAML snippet showing
                       how to satisfy the requirement (e.g. via a ``${tag:}`` template
                       or a literal placeholder).
    """

    deployment_id: str
    adapter: str
    key: str
    status: str  # "literal" | "tag_map" | "template" | "missing"
    suggestion: str | None = None


@dataclass
class PreflightReport:
    """Aggregate result of a preflight evaluation run.

    Attributes:
        checks: All checks, both passing and failing.
    """

    checks: list[MetadataCheck] = field(default_factory=list)

    @property
    def missing(self) -> list[MetadataCheck]:
        """Return only checks with status ``"missing"``."""
        return [c for c in self.checks if c.status == "missing"]

    @property
    def ok(self) -> bool:
        """True when no checks are missing."""
        return len(self.missing) == 0

    def format(self) -> str:
        """Return a human-readable preflight report string."""
        if not self.checks:
            return "preflight: no adapters with required_metadata enabled — nothing to check.\n"

        lines: list[str] = []
        lines.append(f"preflight: {len(self.checks)} check(s), {len(self.missing)} missing\n")

        # Group by adapter for readability
        by_adapter: dict[str, list[MetadataCheck]] = {}
        for c in self.checks:
            by_adapter.setdefault(c.adapter, []).append(c)

        for adapter in sorted(by_adapter):
            checks = by_adapter[adapter]
            missing_count = sum(1 for c in checks if c.status == "missing")
            lines.append(f"  [{adapter}]  {len(checks)} check(s), {missing_count} missing")
            for c in sorted(checks, key=lambda x: (x.deployment_id, x.key)):
                marker = "OK  " if c.status != "missing" else "MISS"
                line = f"    {marker}  {c.deployment_id}  {c.key}  ({c.status})"
                if c.suggestion:
                    line += f"\n          suggestion: {c.suggestion}"
                lines.append(line)

        lines.append("")
        if self.ok:
            lines.append("  result: PASS")
        else:
            lines.append(f"  result: FAIL — {len(self.missing)} missing metadata key(s)")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------


def evaluate_metadata(
    *,
    config: RelayConfig,
    manifests: list[AdapterManifest],
    enabled_adapters: set[str],
) -> PreflightReport:
    """Check every catalog leaf node against each enabled adapter's required_metadata.

    Args:
        config:           A :class:`~relay.config.schema.RelayConfig` (may have
                          ``None`` org_tree / hierarchy / catalog — all null-safe).
        manifests:        List of :class:`~relay.adapters.registry.AdapterManifest`
                          objects from the registry.
        enabled_adapters: Set of adapter names to gate on.  Adapters not in this set
                          are still reported (so operators can see the full picture)
                          but their ``"missing"`` checks do NOT count toward
                          :attr:`PreflightReport.ok` / exit code.  The ``missing``
                          property on the returned report contains only the checks
                          that are in *enabled_adapters*.

    Returns:
        A :class:`PreflightReport` with one :class:`MetadataCheck` per
        (deployment, adapter, key) triple that was evaluated.
    """
    report = PreflightReport()

    # Gather adapters that actually declare required_metadata.
    active = [m for m in manifests if m.required_metadata and m.name in enabled_adapters]
    if not active:
        return report

    # Null-safe access to tree and tag_map.
    org_tree = config.org_tree if config is not None else None
    hierarchy = config.hierarchy if config is not None else None
    tag_map: dict[str, str] = {}
    leaf_level: str | None = None
    if hierarchy is not None:
        leaf_level = hierarchy.leaf_level
        if hierarchy.deployment_defaults is not None:
            tag_map = hierarchy.deployment_defaults.tag_map

    # Determine which nodes to iterate.  Prefer leaf-level nodes when we know
    # the leaf_level; fall back to all nodes so we never silently skip anything
    # when hierarchy.yaml is absent.
    if org_tree is not None:
        all_nodes = org_tree.all_nodes()
        if leaf_level:
            leaf_nodes = [n for n in all_nodes if n.level == leaf_level]
        else:
            # No hierarchy — treat nodes with no children as leaves.
            leaf_nodes = [n for n in all_nodes if not org_tree.children(n.id)]
        if not leaf_nodes:
            # Hierarchy says there's a leaf_level but catalog has no nodes at that
            # level yet — fall back to all nodes so partial catalogs still report.
            leaf_nodes = all_nodes
    else:
        # No org_tree — nothing to check against.
        return report

    for manifest in active:
        for node in leaf_nodes:
            try:
                _check_node(node, manifest, org_tree, tag_map, report)
            except Exception:
                logger.warning(
                    "preflight: unexpected error checking node %r / adapter %r; skipping",
                    node.id,
                    manifest.name,
                    exc_info=True,
                )

    return report


def _check_node(
    node: object,
    manifest: AdapterManifest,
    org_tree: object,
    tag_map: dict[str, str],
    report: PreflightReport,
) -> None:
    """Evaluate one node against one manifest's required_metadata and append checks."""
    # resolve_metadata merges raw strings (including ${tag:} templates) root->leaf.
    # It does NOT resolve the templates — it only dict-merges the raw values, so
    # we can still detect the "${tag:" marker for the "template" status.
    try:
        resolved_raw: dict[str, object] = org_tree.resolve_metadata(node.id)  # type: ignore[union-attr]
    except Exception:
        resolved_raw = {}

    for key in manifest.required_metadata:
        status: str
        suggestion: str | None = None

        if key in tag_map:
            # Key is sourced org-wide from a resource tag — always covered.
            status = "tag_map"
        elif key in resolved_raw:
            raw_val = resolved_raw[key]
            if isinstance(raw_val, str) and "${tag:" in raw_val:
                status = "template"
            else:
                status = "literal"
        else:
            status = "missing"
            tag_hint = manifest.suggested_tag_map.get(key)
            if tag_hint:
                suggestion = f'{key}: "${{tag:{tag_hint}}}"'
            else:
                suggestion = f'{key}: <value>  # or "${{tag:SOME_TAG}}"'

        report.checks.append(
            MetadataCheck(
                deployment_id=node.id,  # type: ignore[union-attr]
                adapter=manifest.name,
                key=key,
                status=status,
                suggestion=suggestion,
            )
        )


# ---------------------------------------------------------------------------
# Placeholder generator
# ---------------------------------------------------------------------------


def generate_placeholder(
    *,
    deployment_id: str,
    component_tag: str,
    manifests: list[AdapterManifest],
    enabled_adapters: set[str],
    suggested_only: bool = True,
) -> str:
    """Generate a paste-ready YAML stub for one catalog leaf node.

    The stub includes a ``match.deployment_tag`` block and commented metadata
    lines for each required_metadata key of each enabled adapter.

    Args:
        deployment_id:    The id to assign the stub node (e.g. ``"dep-my-service-prod"``).
        component_tag:    The AWS resource-tag value used to match this deployment
                          (e.g. the component name stamped on alarms).
        manifests:        List of adapter manifests to pull required_metadata from.
        enabled_adapters: Only include metadata hints for adapters in this set.
        suggested_only:   When True, only emit hints that have a ``suggested_tag_map``
                          entry; when False, emit a generic ``SOME_TAG`` hint for
                          every required key.

    Returns:
        A YAML string representing a single catalog list item (``- id: ...``).
    """
    # Collect metadata hints, sorted for determinism.
    meta_lines: list[str] = []
    seen_keys: set[str] = set()
    for manifest in sorted(manifests, key=lambda m: m.name):
        if manifest.name not in enabled_adapters or not manifest.required_metadata:
            continue
        for key in sorted(manifest.required_metadata):
            if key in seen_keys:
                continue
            seen_keys.add(key)
            tag_hint = manifest.suggested_tag_map.get(key)
            if tag_hint:
                meta_lines.append(f'      # {key}: "${{tag:{tag_hint}}}"')
            elif not suggested_only:
                meta_lines.append(f'      # {key}: "${{tag:SOME_TAG}}"  # replace SOME_TAG')

    meta_block = ""
    if meta_lines:
        meta_block = "\n    metadata:\n" + "\n".join(meta_lines)

    stub = (
        f"  - id: {deployment_id}\n"
        f"    name: {deployment_id}\n"
        f"    level: deployment\n"
        f"    parent: null  # set to the parent component id\n"
        f"    match:\n"
        f"      deployment_tag: {component_tag}"
        f"{meta_block}\n"
    )
    return stub


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _discover_enabled_adapters(
    manifests: list[AdapterManifest],
    *,
    force_all: bool,
    explicit: list[str] | None,
) -> set[str]:
    """Determine which adapter names are considered enabled.

    Enablement heuristic (when not overridden by flags):
    - An adapter with ``required_env`` is enabled when ALL its env vars are set.
    - An adapter with no ``required_env`` is treated as enabled (we can't cheaply
      determine settings-store keys without standing up the full app).
    """
    if explicit:
        return set(explicit)
    if force_all:
        return {m.name for m in manifests}

    enabled: set[str] = set()
    for m in manifests:
        if not m.required_env:
            # No env gate — treat as enabled (conservative: report it).
            enabled.add(m.name)
        elif all(os.environ.get(var) for var in m.required_env):
            enabled.add(m.name)
    return enabled


def _load_config(config_dir: str) -> RelayConfig:
    """Load RelayConfig from the given directory via LocalConfigLoader."""
    from relay.config.local_loader import LocalConfigLoader

    loader = LocalConfigLoader(config_dir)
    return loader.load()


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``relay-preflight`` CLI command."""
    parser = argparse.ArgumentParser(
        prog="relay-preflight",
        description=(
            "Check that every catalog leaf has the metadata each enabled adapter needs."
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=os.environ.get("RELAY_CONFIG_DIR", "config"),
        metavar="DIR",
        help="Directory containing Relay YAML config files (default: config/ or $RELAY_CONFIG_DIR).",
    )
    parser.add_argument(
        "--all",
        dest="force_all",
        action="store_true",
        help="Gate every discovered adapter regardless of environment variables.",
    )
    parser.add_argument(
        "--adapter",
        dest="adapters",
        action="append",
        metavar="NAME",
        help="Gate a specific adapter by name (repeatable; overrides --all and env detection).",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Print a JSON report instead of the human-readable text.",
    )
    parser.add_argument(
        "--generate-placeholders",
        dest="placeholders",
        metavar="ID=TAG[,ID=TAG...]",
        help=(
            "Print YAML stubs for the given deployment_id=component_tag pairs and exit 0. "
            "Example: --generate-placeholders dep-my-svc-prod=my-svc-prod"
        ),
    )

    args = parser.parse_args(argv)

    # --generate-placeholders: load manifests, print stubs, exit 0.
    if args.placeholders:
        from relay.adapters.registry import discover_manifests

        manifests = discover_manifests()
        enabled = _discover_enabled_adapters(
            manifests, force_all=args.force_all, explicit=args.adapters
        )
        pairs = [p.strip() for p in args.placeholders.split(",") if p.strip()]
        for pair in pairs:
            if "=" not in pair:
                print(f"# skipping malformed placeholder spec: {pair!r}", file=sys.stderr)
                continue
            dep_id, _, tag = pair.partition("=")
            print(
                generate_placeholder(
                    deployment_id=dep_id.strip(),
                    component_tag=tag.strip(),
                    manifests=manifests,
                    enabled_adapters=enabled,
                )
            )
        sys.exit(0)

    # Normal preflight flow.
    try:
        config = _load_config(args.config_dir)
    except Exception as exc:
        print(f"relay-preflight: failed to load config from {args.config_dir!r}: {exc}", file=sys.stderr)
        sys.exit(2)

    from relay.adapters.registry import discover_manifests

    manifests = discover_manifests()
    enabled = _discover_enabled_adapters(
        manifests, force_all=args.force_all, explicit=args.adapters
    )

    report = evaluate_metadata(config=config, manifests=manifests, enabled_adapters=enabled)

    if args.output_json:
        data = {
            "ok": report.ok,
            "missing_count": len(report.missing),
            "checks": [asdict(c) for c in report.checks],
        }
        print(json.dumps(data, indent=2))
    else:
        print(report.format(), end="")

    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    main()
