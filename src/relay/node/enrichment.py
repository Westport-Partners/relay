"""relay.node.enrichment — best-effort deployment metadata enrichment.

The Node runs inside the team's own AWS account, so unlike the Hub it can read
real resource state. This module turns that access into a free-form ``metadata``
dict that rides the heartbeat (and is surfaced in the tile-detail drawer and
consumed by the AI investigation skills).

Two sources, merged (later wins on key collision):
  1. Catalog-derived facts already known to the Node: owner_ref and any
     free-form ``metadata`` (e.g. gitlab_project) on the deployment's org node.
  2. Live AWS resource tags (Resource Groups Tagging API), folded under the
     ``aws_tags`` key. Only fetched when tag enrichment is explicitly enabled.

Everything here is best-effort: any failure yields ``{}`` (or the catalog-only
subset) and is logged, never raised. Enrichment must never break the heartbeat.

Enable live tag fetching with RELAY_ENRICH_TAGS=true (context relay:enrich_tags).
The Node's IAM must then allow tag:GetResources / ecs:Describe* — granted by
node_stack.py behind the same flag, so the default deploy's permissions are
unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class TagEnricher:
    """Builds the heartbeat ``metadata`` dict for a deployment.

    Args:
        account_id: The team account (recorded under metadata for traceability).
        region: AWS region for the tagging client.
        enabled: When False (default), live AWS calls are skipped entirely and
            only catalog-derived metadata is returned. Driven by RELAY_ENRICH_TAGS.
        boto3_session: Optional injected session for tests.
    """

    def __init__(
        self,
        account_id: str = "",
        region: str | None = None,
        enabled: bool | None = None,
        boto3_session: Any | None = None,
    ) -> None:
        self._account_id = account_id
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        if enabled is None:
            enabled = os.environ.get("RELAY_ENRICH_TAGS", "").lower() in (
                "1",
                "true",
                "yes",
            )
        self._enabled = enabled
        self._session = boto3_session
        # Process-lifetime cache: tags rarely change within a warm Lambda, and we
        # do not want to hit the tagging API on every heartbeat. Keyed by the
        # tag-filter tuple used for the lookup.
        self._cache: dict[tuple, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_metadata(
        self,
        *,
        deployment_id: str,
        app_name: str,
        org_node: dict[str, Any] | None = None,
        tag_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Assemble the metadata dict for one deployment. Never raises.

        ``org_node`` is the leaf org node dict (the last entry of org_path) when
        available — its ``owner_ref`` and free-form ``metadata`` (which holds
        integration routing keys like ``gitlab_project``) are folded in. Returns
        ``{}`` only if there is genuinely nothing to report.

        ``tag_map`` is the org-wide ``deployment_defaults.tag_map`` from
        hierarchy.yaml; when provided alongside live AWS tags, resolved
        deployment metadata keys are merged into ``meta`` (resolved wins).
        """
        meta: dict[str, Any] = {}
        try:
            if org_node:
                if org_node.get("owner_ref"):
                    meta["owner"] = org_node["owner_ref"]
                node_meta = org_node.get("metadata")
                if isinstance(node_meta, dict):
                    # Free-form catalog metadata (gitlab_project, region,
                    # runbook, cost_center…).
                    meta.update(node_meta)
        except Exception:
            logger.warning("catalog metadata fold failed", exc_info=True)

        if self._enabled:
            aws_tags = self._fetch_aws_tags(deployment_id=deployment_id, app_name=app_name)
            if aws_tags:
                # Raw tags for the tile drawer's Resource-tags chips.
                meta["aws_tags"] = aws_tags
                # Also resolve deployment metadata templates against the live tags.
                try:
                    from relay.config.tag_mapping import resolve_deployment_metadata

                    node_meta_for_resolve = (
                        org_node.get("metadata") if org_node else None
                    )
                    resolved = resolve_deployment_metadata(
                        node_metadata=node_meta_for_resolve,
                        tag_map=tag_map,
                        tags=aws_tags,
                    )
                    if resolved:
                        # Resolved deployment metadata wins on key collision.
                        meta.update(resolved)
                except Exception:
                    logger.warning(
                        "deployment metadata resolution failed in enrichment; continuing",
                        exc_info=True,
                    )

        return meta

    # ------------------------------------------------------------------
    # Live AWS tag fetch (best-effort)
    # ------------------------------------------------------------------

    def _fetch_aws_tags(self, *, deployment_id: str, app_name: str) -> dict[str, str]:
        """Fetch resource tags via the Resource Groups Tagging API.

        Matches resources carrying a ``relay:deployment`` tag equal to this
        deployment (falling back to ``relay:app``). Returns a flat tag dict, or
        ``{}`` on any error or no match. Cached per filter for the warm Lambda.
        """
        cache_key = ("deployment", deployment_id, app_name)
        if cache_key in self._cache:
            return self._cache[cache_key]

        tags: dict[str, str] = {}
        try:
            import boto3  # local import: only when enrichment is enabled

            session = self._session or boto3.session.Session()
            client = session.client("resourcegroupstaggingapi", region_name=self._region)
            # Prefer an explicit relay:deployment tag; many teams tag by app.
            tag_filters = [
                {"Key": "relay:deployment", "Values": [deployment_id]},
            ]
            resources = self._get_resources(client, tag_filters)
            if not resources:
                resources = self._get_resources(
                    client, [{"Key": "relay:app", "Values": [app_name]}]
                )
            for res in resources:
                for t in res.get("Tags", []):
                    key = t.get("Key")
                    if key:
                        tags[key] = t.get("Value", "")
        except Exception:
            logger.warning(
                "AWS tag enrichment failed for %s/%s; continuing without tags",
                deployment_id,
                app_name,
                exc_info=True,
            )
            tags = {}

        self._cache[cache_key] = tags
        return tags

    @staticmethod
    def _get_resources(client: Any, tag_filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Page through GetResources for the given tag filters."""
        out: list[dict[str, Any]] = []
        token = ""
        while True:
            kwargs: dict[str, Any] = {"TagFilters": tag_filters, "ResourcesPerPage": 50}
            if token:
                kwargs["PaginationToken"] = token
            resp = client.get_resources(**kwargs)
            out.extend(resp.get("ResourceTagMappingList", []))
            token = resp.get("PaginationToken", "")
            if not token:
                break
        return out
