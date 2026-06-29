"""Tests for relay.config.tag_mapping — resolve_template and resolve_deployment_metadata."""

from __future__ import annotations

from relay.config.tag_mapping import resolve_deployment_metadata, resolve_template

# ---------------------------------------------------------------------------
# resolve_template
# ---------------------------------------------------------------------------


class TestResolveTemplate:

    def test_literal_passthrough(self) -> None:
        """A plain string with no marker is returned unchanged."""
        assert resolve_template("identity/auth-api", {"X": "y"}) == "identity/auth-api"

    def test_single_substitution(self) -> None:
        """A single ${tag:NAME} is replaced with the tag value."""
        assert resolve_template("${tag:GITLAB_PROJECT_ID}", {"GITLAB_PROJECT_ID": "team/proj"}) == "team/proj"

    def test_multi_substitution(self) -> None:
        """Multiple distinct ${tag:NAME} markers in one string are all replaced."""
        result = resolve_template(
            "${tag:ORG}/${tag:REPO}",
            {"ORG": "acme", "REPO": "auth-api"},
        )
        assert result == "acme/auth-api"

    def test_composed_template(self) -> None:
        """A template with surrounding literal text and multiple tags."""
        result = resolve_template(
            "https://gitlab.example.com/${tag:GROUP}/${tag:PROJECT}",
            {"GROUP": "platform", "PROJECT": "relay"},
        )
        assert result == "https://gitlab.example.com/platform/relay"

    def test_missing_tag_returns_none(self) -> None:
        """If any referenced tag is absent the whole value is unresolvable → None."""
        assert resolve_template("${tag:MISSING}", {"OTHER": "x"}) is None

    def test_partial_missing_returns_none(self) -> None:
        """Even one missing tag among several makes the whole value None."""
        result = resolve_template(
            "${tag:PRESENT}/${tag:ABSENT}",
            {"PRESENT": "ok"},
        )
        assert result is None

    def test_non_str_passthrough(self) -> None:
        """Non-string values are returned unchanged regardless of tags."""
        assert resolve_template(42, {"X": "y"}) == 42
        assert resolve_template(True, {}) is True
        assert resolve_template({"key": "val"}, {"X": "y"}) == {"key": "val"}
        assert resolve_template(None, {}) is None

    def test_no_marker_fast_path(self) -> None:
        """A string without '${tag:' is returned without any regex work."""
        value = "plain-literal"
        assert resolve_template(value, None) is value

    def test_none_tags_treated_as_empty(self) -> None:
        """tags=None is safe — a template with a reference returns None."""
        assert resolve_template("${tag:X}", None) is None


# ---------------------------------------------------------------------------
# resolve_deployment_metadata
# ---------------------------------------------------------------------------


class TestResolveDeploymentMetadata:

    def test_all_none_args_returns_empty(self) -> None:
        """All-None args are safe and return {}."""
        assert resolve_deployment_metadata(None, None, None) == {}

    def test_tag_map_base_layer_only(self) -> None:
        """tag_map sets keys whose tag is present; absent tags are skipped."""
        result = resolve_deployment_metadata(
            node_metadata=None,
            tag_map={"component_id": "COMPONENT_ID", "git_sha": "GIT_SHA"},
            tags={"COMPONENT_ID": "auth-api"},
        )
        # GIT_SHA absent → only component_id set
        assert result == {"component_id": "auth-api"}

    def test_node_metadata_overrides_tag_map(self) -> None:
        """A literal node metadata value replaces the tag_map-provided one."""
        result = resolve_deployment_metadata(
            node_metadata={"component_id": "explicit-override"},
            tag_map={"component_id": "COMPONENT_ID"},
            tags={"COMPONENT_ID": "from-tag"},
        )
        assert result["component_id"] == "explicit-override"

    def test_node_template_resolves(self) -> None:
        """A node metadata template ${tag:X} is resolved against tags."""
        result = resolve_deployment_metadata(
            node_metadata={"gitlab_project": "${tag:GITLAB_PROJECT_ID}"},
            tag_map={},
            tags={"GITLAB_PROJECT_ID": "team/proj"},
        )
        assert result == {"gitlab_project": "team/proj"}

    def test_node_template_missing_tag_skipped_tag_map_survives(self) -> None:
        """If a node template can't resolve, the tag_map value is kept."""
        result = resolve_deployment_metadata(
            node_metadata={"gitlab_project": "${tag:MISSING_TAG}"},
            tag_map={"gitlab_project": "FALLBACK_TAG"},
            tags={"FALLBACK_TAG": "fallback/proj"},
        )
        # node template fails (MISSING_TAG absent), tag_map value survives
        assert result["gitlab_project"] == "fallback/proj"

    def test_node_template_missing_tag_no_fallback(self) -> None:
        """If a node template can't resolve and no tag_map entry, key is absent."""
        result = resolve_deployment_metadata(
            node_metadata={"gitlab_project": "${tag:MISSING}"},
            tag_map={},
            tags={},
        )
        assert "gitlab_project" not in result

    def test_non_str_node_value_set_as_is(self) -> None:
        """Non-string node metadata values are passed through directly."""
        result = resolve_deployment_metadata(
            node_metadata={"enabled": True, "count": 3},
            tag_map={},
            tags={},
        )
        assert result == {"enabled": True, "count": 3}

    def test_both_layers_combined(self) -> None:
        """tag_map provides base, node metadata extends + overrides it."""
        result = resolve_deployment_metadata(
            node_metadata={
                "gitlab_project": "${tag:GITLAB_PROJECT_ID}",
                "region": "us-west-2",  # literal override
            },
            tag_map={
                "component_id": "COMPONENT_ID",
                "git_sha": "GIT_SHA",
            },
            tags={
                "COMPONENT_ID": "auth-api",
                "GIT_SHA": "abc123",
                "GITLAB_PROJECT_ID": "team/auth",
            },
        )
        assert result["component_id"] == "auth-api"
        assert result["git_sha"] == "abc123"
        assert result["gitlab_project"] == "team/auth"
        assert result["region"] == "us-west-2"
