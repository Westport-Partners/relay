"""Tests for GitLabConfigLoader.

Strategy
--------
All network and AWS calls are replaced with lightweight test doubles:

* ``fake_sm_client`` — an object with a ``get_secret_value`` method that
  returns a fixed token string, tracking how many times it has been called.
* ``make_fetcher`` — a factory that returns a ``file_fetcher`` callable which,
  given a URL, returns the fixture YAML content for the corresponding filename.

The loader is constructed with these two injected dependencies so no real
network or AWS credentials are required.

Fixture YAML
------------
The YAML fixtures use the schema expected by the pydantic models (not the
human-friendly GitOps format in config/*.example.yaml, which is a
different representation of the same data).  See relay.config.schema and
relay.core.model for field names.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from relay.config.loader import GitLabConfigLoader
from relay.config.schema import RelayConfig

# ---------------------------------------------------------------------------
# Fixture YAML content (schema-compatible)
# ---------------------------------------------------------------------------

ESCALATION_YAML = textwrap.dedent("""\
    policies:
      - policy_id: pol-p1
        name: p1-critical
        team: team-platform
        steps:
          - step_index: 0
            contact_ids: [cnt_abc123]
            timeout_minutes: 5
            notify_streams: [TEAM, CENTRAL]
          - step_index: 1
            contact_ids: [cnt_def456]
            timeout_minutes: 10
            notify_streams: [TEAM, CENTRAL]
      - policy_id: pol-p3
        name: p3-low
        team: team-platform
        steps:
          - step_index: 0
            contact_ids: [cnt_abc123]
            timeout_minutes: 60
            notify_streams: [TEAM]
""")

ROUTING_YAML = textwrap.dedent("""\
    rules:
      - rule_id: rule-rds-p1
        priority: 10
        namespace_prefix: AWS/RDS
        escalation_policy_id: pol-p1
        streams: [TEAM, CENTRAL]
      - rule_id: rule-default
        priority: 999
        escalation_policy_id: pol-p3
        streams: [TEAM]
    default_escalation_policy_id: pol-p3
    default_streams: [TEAM, CENTRAL]
""")

# Malformed YAML (unbalanced bracket)
INVALID_YAML = "policies: [unclosed"

# Valid YAML but fails pydantic validation (duplicate policy_id)
DUPLICATE_ID_ESCALATION_YAML = textwrap.dedent("""\
    policies:
      - policy_id: pol-dup
        name: dup-1
        team: team-a
        steps:
          - step_index: 0
            contact_ids: [cnt_a]
            timeout_minutes: 5
      - policy_id: pol-dup
        name: dup-2
        team: team-a
        steps:
          - step_index: 0
            contact_ids: [cnt_b]
            timeout_minutes: 5
""")


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeSecretsManagerClient:
    """Mimics the subset of boto3 secretsmanager client used by the loader."""

    def __init__(self, token: str = "glpat-test-token-abc") -> None:
        self.token = token
        self.call_count = 0

    def get_secret_value(self, *, SecretId: str) -> dict:  # noqa: N803
        self.call_count += 1
        return {"SecretString": self.token}


def make_fetcher(
    escalation: str = ESCALATION_YAML,
    routing: str = ROUTING_YAML,
) -> tuple[list[str], callable]:
    """Return (calls_log, fetcher) where fetcher returns YAML by filename.

    ``calls_log`` is mutated in place as the fetcher is called, allowing tests
    to assert the number and order of fetches.
    """
    calls: list[str] = []

    def _fetcher(url: str, token: str) -> str:
        calls.append(url)
        if "escalation.yaml" in url:
            return escalation
        if "routing.yaml" in url:
            return routing
        # Optional files (environments.yaml, hierarchy.yaml, catalog.yaml) — return 404
        if any(f in url for f in ("environments.yaml", "hierarchy.yaml", "catalog.yaml")):
            raise RuntimeError(f"HTTP 404 fetching '{url}': Not Found")
        raise RuntimeError(f"Unexpected URL in test fetcher: {url!r}")

    return calls, _fetcher


def make_loader(
    escalation: str = ESCALATION_YAML,
    routing: str = ROUTING_YAML,
    sm_client: FakeSecretsManagerClient | None = None,
) -> tuple[GitLabConfigLoader, FakeSecretsManagerClient, list[str]]:
    """Convenience factory returning (loader, sm_client, fetch_calls)."""
    if sm_client is None:
        sm_client = FakeSecretsManagerClient()
    calls, fetcher = make_fetcher(escalation=escalation, routing=routing)
    loader = GitLabConfigLoader(
        gitlab_project_id="mygroup/myrepo",
        config_branch="main",
        secrets_manager_secret_name="relay/gitlab-token",
        secretsmanager_client=sm_client,
        file_fetcher=fetcher,
    )
    return loader, sm_client, calls


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


def test_load_returns_valid_relay_config() -> None:
    """load() returns a RelayConfig whose sub-configs match the fixture YAML."""
    loader, _, _ = make_loader()
    config = loader.load()

    assert isinstance(config, RelayConfig)

    # escalation
    pol_ids = {p.policy_id for p in config.escalation.policies}
    assert pol_ids == {"pol-p1", "pol-p3"}
    p1 = next(p for p in config.escalation.policies if p.policy_id == "pol-p1")
    assert len(p1.steps) == 2
    assert p1.steps[0].timeout_minutes == 5

    # routing
    rule_ids = {r.rule_id for r in config.routing.rules}
    assert rule_ids == {"rule-rds-p1", "rule-default"}
    assert config.routing.default_escalation_policy_id == "pol-p3"


def test_get_returns_cached_config_without_refetch() -> None:
    """get() should not hit the network after the first load."""
    loader, _, calls = make_loader()

    # First call loads and caches.
    first = loader.get()
    fetch_count_after_first = len(calls)

    # Second call must return the identical cached object with no new fetches.
    second = loader.get()
    assert second is first
    assert len(calls) == fetch_count_after_first


def test_get_triggers_load_on_first_call() -> None:
    """get() on a fresh loader should trigger a full load (2 required + 3 optional)."""
    loader, _, calls = make_loader()
    assert len(calls) == 0
    loader.get()
    assert len(calls) == 5  # escalation, routing + 3 optional files attempted


def test_refresh_re_fetches_config() -> None:
    """refresh() should fetch fresh YAML even when a valid cache exists."""
    loader, _, calls = make_loader()

    loader.load()
    calls_after_load = len(calls)

    second = loader.refresh()
    assert len(calls) == calls_after_load + 5  # 5 files fetched (2 required + 3 optional)
    # Both should be valid RelayConfig objects; they will differ only in loaded_at.
    assert isinstance(second, RelayConfig)


def test_refresh_invalidates_cache() -> None:
    """After refresh(), get() returns the newly loaded config."""
    loader, _, _ = make_loader()
    loader.get()  # populate cache

    refreshed = loader.refresh()
    cached = loader.get()

    # get() after refresh should return the same refreshed object, not the old one.
    assert cached is refreshed


# ---------------------------------------------------------------------------
# Tests — Secrets Manager token caching
# ---------------------------------------------------------------------------


def test_token_fetched_exactly_once_across_multiple_loads() -> None:
    """Secrets Manager must be called exactly once; subsequent loads reuse the cached token."""
    sm = FakeSecretsManagerClient()
    loader, _, _ = make_loader(sm_client=sm)

    loader.load()
    loader.load()
    loader.refresh()

    assert sm.call_count == 1, (
        f"Expected 1 Secrets Manager call, got {sm.call_count}"
    )


def test_token_cached_before_file_fetch() -> None:
    """Token is obtained from Secrets Manager and then reused for each file fetch."""
    sm = FakeSecretsManagerClient(token="glpat-cached-token")
    received_tokens: list[str] = []

    def _recording_fetcher(url: str, token: str) -> str:
        received_tokens.append(token)
        if "escalation.yaml" in url:
            return ESCALATION_YAML
        if "routing.yaml" in url:
            return ROUTING_YAML
        if any(f in url for f in ("environments.yaml", "hierarchy.yaml", "catalog.yaml")):
            raise RuntimeError(f"HTTP 404 fetching '{url}': Not Found")
        raise RuntimeError(f"Unexpected URL: {url!r}")

    loader = GitLabConfigLoader(
        gitlab_project_id="proj/repo",
        secretsmanager_client=sm,
        file_fetcher=_recording_fetcher,
    )
    loader.load()

    assert sm.call_count == 1
    assert all(t == "glpat-cached-token" for t in received_tokens)
    assert len(received_tokens) == 5  # 2 required + 3 optional files attempted


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


def test_load_raises_runtime_error_on_secrets_manager_failure() -> None:
    """A Secrets Manager error must propagate as RuntimeError with a clear message."""

    class FailingSecretsClient:
        def get_secret_value(self, *, SecretId: str) -> dict:  # noqa: N803
            raise Exception("Access denied")

    _, fetcher = make_fetcher()
    loader = GitLabConfigLoader(
        gitlab_project_id="proj/repo",
        secrets_manager_secret_name="relay/missing-secret",
        secretsmanager_client=FailingSecretsClient(),
        file_fetcher=fetcher,
    )

    with pytest.raises(RuntimeError, match="relay/missing-secret"):
        loader.load()


def test_load_raises_runtime_error_on_file_fetch_failure() -> None:
    """A network error on file fetch must raise RuntimeError with the filename."""

    def _failing_fetcher(url: str, token: str) -> str:
        raise RuntimeError("HTTP 404 fetching escalation.yaml: Not Found")

    loader = GitLabConfigLoader(
        gitlab_project_id="proj/repo",
        secretsmanager_client=FakeSecretsManagerClient(),
        file_fetcher=_failing_fetcher,
    )

    with pytest.raises(RuntimeError, match="escalation.yaml"):
        loader.load()


def test_load_raises_on_invalid_yaml() -> None:
    """Malformed YAML must raise yaml.YAMLError (propagated from from_yaml_files)."""
    loader, _, _ = make_loader(escalation=INVALID_YAML)

    with pytest.raises(yaml.YAMLError):
        loader.load()


def test_load_raises_on_schema_validation_failure() -> None:
    """YAML that violates the pydantic schema must raise ValidationError."""
    import pydantic

    loader, _, _ = make_loader(escalation=DUPLICATE_ID_ESCALATION_YAML)

    with pytest.raises(pydantic.ValidationError, match="Duplicate policy_id"):
        loader.load()


# ---------------------------------------------------------------------------
# Tests — GitLab URL construction
# ---------------------------------------------------------------------------


def test_gitlab_url_contains_correct_components() -> None:
    """The constructed URL must include project, filename, and branch."""
    fetched_urls: list[str] = []

    def _recording_fetcher(url: str, token: str) -> str:
        fetched_urls.append(url)
        if "escalation.yaml" in url:
            return ESCALATION_YAML
        if "routing.yaml" in url:
            return ROUTING_YAML
        if any(f in url for f in ("environments.yaml", "hierarchy.yaml", "catalog.yaml")):
            raise RuntimeError(f"HTTP 404 fetching '{url}': Not Found")
        raise RuntimeError(f"Unexpected URL: {url!r}")

    loader = GitLabConfigLoader(
        gitlab_project_id="mygroup/myrepo",
        config_branch="release",
        gitlab_base_url="https://gitlab.example.com",
        secretsmanager_client=FakeSecretsManagerClient(),
        file_fetcher=_recording_fetcher,
    )
    loader.load()

    assert len(fetched_urls) == 5  # 2 required + 3 optional files attempted
    for url in fetched_urls[:2]:
        assert "gitlab.example.com" in url
        # Project path must be URL-encoded
        assert "mygroup%2Fmyrepo" in url
        # Branch must appear as a query param
        assert "ref=release" in url


def test_self_hosted_gitlab_base_url() -> None:
    """A custom base URL must be honoured with no trailing-slash duplication."""
    fetched_urls: list[str] = []

    def _recording_fetcher(url: str, token: str) -> str:
        fetched_urls.append(url)
        if "escalation.yaml" in url:
            return ESCALATION_YAML
        if "routing.yaml" in url:
            return ROUTING_YAML
        if any(f in url for f in ("environments.yaml", "hierarchy.yaml", "catalog.yaml")):
            raise RuntimeError(f"HTTP 404 fetching '{url}': Not Found")
        raise RuntimeError(f"Unexpected URL: {url!r}")

    loader = GitLabConfigLoader(
        gitlab_project_id="42",
        gitlab_base_url="https://git.internal.corp/",  # trailing slash should be stripped
        secretsmanager_client=FakeSecretsManagerClient(),
        file_fetcher=_recording_fetcher,
    )
    loader.load()

    for url in fetched_urls:
        assert url.startswith("https://git.internal.corp/api/v4/")
        assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# Tests — convenience accessors
# ---------------------------------------------------------------------------


def test_get_escalation_policy_returns_correct_policy() -> None:
    """get_escalation_policy() should return the matching EscalationPolicy by ID."""
    loader, _, _ = make_loader()
    policy = loader.get_escalation_policy("pol-p1")
    assert policy is not None
    assert policy.name == "p1-critical"


def test_get_escalation_policy_returns_none_for_unknown_id() -> None:
    """get_escalation_policy() should return None for an unknown policy_id."""
    loader, _, _ = make_loader()
    assert loader.get_escalation_policy("pol-nonexistent") is None
