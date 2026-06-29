"""Tests for relay.node.enrichment.TagEnricher.

Covers:
  - catalog-derived metadata fold (owner / free-form node meta incl. gitlab_project)
  - live AWS tag fetch disabled by default (no AWS calls)
  - aws_tags fold when enabled (fake tagging client)
  - graceful degradation: any AWS error yields {} (never raises)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from relay.node.enrichment import TagEnricher


def test_catalog_metadata_fold_without_aws():
    enricher = TagEnricher(enabled=False)
    leaf = {
        "id": "dep-1",
        "name": "checkout-api",
        "level": "deployment",
        "owner_ref": "team-pay",
        # Integration routing keys ride inside metadata (the heartbeat shape).
        "metadata": {
            "gitlab_project": "payments/checkout-api",
            "region": "us-east-1",
            "runbook": "https://wiki/checkout",
        },
    }
    meta = enricher.build_metadata(
        deployment_id="dep-1", app_name="checkout-api", org_node=leaf
    )
    assert meta["owner"] == "team-pay"
    assert meta["gitlab_project"] == "payments/checkout-api"
    assert meta["region"] == "us-east-1"
    assert meta["runbook"] == "https://wiki/checkout"
    # No live fetch when disabled.
    assert "aws_tags" not in meta


def test_no_org_node_yields_empty_when_disabled():
    enricher = TagEnricher(enabled=False)
    assert enricher.build_metadata(deployment_id="d", app_name="a", org_node=None) == {}


def test_aws_tags_folded_when_enabled():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_resources.return_value = {
        "ResourceTagMappingList": [
            {"ResourceARN": "arn:...:service/x",
             "Tags": [{"Key": "env", "Value": "prod"}, {"Key": "team", "Value": "pay"}]}
        ],
        "PaginationToken": "",
    }
    enricher = TagEnricher(enabled=True, boto3_session=session, region="us-east-1")
    meta = enricher.build_metadata(deployment_id="dep-1", app_name="checkout-api")
    assert meta["aws_tags"] == {"env": "prod", "team": "pay"}
    # First filter matched, so only one get_resources call.
    assert client.get_resources.call_count == 1


def test_falls_back_to_app_tag_when_deployment_tag_misses():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    # First call (relay:deployment) empty; second call (relay:app) hits.
    client.get_resources.side_effect = [
        {"ResourceTagMappingList": [], "PaginationToken": ""},
        {"ResourceTagMappingList": [
            {"Tags": [{"Key": "app", "Value": "checkout"}]}], "PaginationToken": ""},
    ]
    enricher = TagEnricher(enabled=True, boto3_session=session)
    meta = enricher.build_metadata(deployment_id="dep-1", app_name="checkout-api")
    assert meta["aws_tags"] == {"app": "checkout"}
    assert client.get_resources.call_count == 2


def test_aws_error_degrades_to_empty():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_resources.side_effect = RuntimeError("AccessDenied")
    enricher = TagEnricher(enabled=True, boto3_session=session)
    meta = enricher.build_metadata(deployment_id="dep-1", app_name="checkout-api")
    # No tags, but no raise — and catalog meta (none here) still merges cleanly.
    assert meta == {}


def test_results_cached_for_warm_lambda():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_resources.return_value = {
        "ResourceTagMappingList": [{"Tags": [{"Key": "env", "Value": "prod"}]}],
        "PaginationToken": "",
    }
    enricher = TagEnricher(enabled=True, boto3_session=session)
    enricher.build_metadata(deployment_id="dep-1", app_name="a")
    enricher.build_metadata(deployment_id="dep-1", app_name="a")
    # Second call served from cache — only one underlying fetch.
    assert client.get_resources.call_count == 1


def test_env_flag_enables(monkeypatch):
    monkeypatch.setenv("RELAY_ENRICH_TAGS", "true")
    assert TagEnricher()._enabled is True
    monkeypatch.setenv("RELAY_ENRICH_TAGS", "")
    assert TagEnricher()._enabled is False


def test_build_metadata_with_tag_map_resolves_deployment_keys():
    """When enabled + aws_tags + tag_map provided, resolved deployment metadata merges in."""
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_resources.return_value = {
        "ResourceTagMappingList": [
            {
                "ResourceARN": "arn:...:service/x",
                "Tags": [
                    {"Key": "git_sha", "Value": "deadbeef"},
                    {"Key": "component", "Value": "checkout"},
                ],
            }
        ],
        "PaginationToken": "",
    }
    enricher = TagEnricher(enabled=True, boto3_session=session, region="us-east-1")
    # tag_map: metadata key → tag name
    tag_map = {"git_sha": "git_sha", "component_id": "component"}
    meta = enricher.build_metadata(
        deployment_id="dep-1",
        app_name="checkout-api",
        tag_map=tag_map,
    )
    # Raw tags still present.
    assert meta["aws_tags"] == {"git_sha": "deadbeef", "component": "checkout"}
    # Resolved keys merged in.
    assert meta["git_sha"] == "deadbeef"
    assert meta["component_id"] == "checkout"


def test_build_metadata_tag_map_no_aws_no_crash():
    """Providing tag_map but enrichment disabled yields no crash and no resolved keys."""
    enricher = TagEnricher(enabled=False)
    meta = enricher.build_metadata(
        deployment_id="dep-1",
        app_name="checkout-api",
        tag_map={"git_sha": "git_sha"},
    )
    # Disabled enricher: no aws_tags and no resolved deployment metadata.
    assert "aws_tags" not in meta
    assert "git_sha" not in meta


def test_build_metadata_tag_map_none_is_safe():
    """tag_map=None is equivalent to not passing it — no crash."""
    enricher = TagEnricher(enabled=False)
    meta = enricher.build_metadata(
        deployment_id="dep-1",
        app_name="checkout-api",
        tag_map=None,
    )
    assert meta == {}
