"""GitOps-based configuration loader for the Relay incident management system.

Architecture — Git vs. DynamoDB split
--------------------------------------
Relay separates *configuration* from *live state* into two distinct stores:

* **GitLab (this module)** — holds everything that changes via pull request
  and code review: escalation policies and routing rules.  These are
  human-readable YAML files versioned in Git so that every change has a
  diff, an author, and a review trail.

* **DynamoDB** — holds everything that changes at runtime without a PR:
  live incident records, acknowledgement state, snooze timers, and PII
  contact details (phone numbers, pager tokens).  DynamoDB gives the
  sub-second write latency and point-in-time recovery that incident
  handling demands.

No-git-commit-on-hot-path rule
-------------------------------
Relay must **never** write back to the Git repository during incident
handling.  Git commits are expensive (network round-trip, lock contention,
CI pipeline triggers) and introduce unavoidable latency on the critical
path.  All mutable runtime state goes to DynamoDB; GitLab is read-only from
the perspective of the running service.

Config refresh
--------------
Config is loaded once at startup and can be refreshed without a restart by
calling :meth:`GitLabConfigLoader.refresh`.  In production this is wired to
a GitLab push webhook so that a merged PR automatically propagates within
seconds.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import boto3

from relay.config.schema import RelayConfig

if TYPE_CHECKING:
    from relay.core.model import EscalationPolicy

logger = logging.getLogger(__name__)

# GitLab raw-file API template.
# {file_path} must already be URL-encoded by the caller.
_GITLAB_RAW_URL = (
    "{base_url}/api/v4/projects/{project_id}"
    "/repository/files/{file_path}/raw?ref={branch}"
)

# Default GitLab SaaS base URL.
_DEFAULT_GITLAB_BASE_URL = "https://gitlab.com"

# Config file names expected inside the GitLab repository.
_ESCALATION_FILE = "escalation.yaml"
_ROUTING_FILE = "routing.yaml"
_ENVIRONMENTS_FILE = "environments.yaml"
_HIERARCHY_FILE = "hierarchy.yaml"
_CATALOG_FILE = "catalog.yaml"


class _SecretsManagerClient(Protocol):
    """Structural type for the boto3 secretsmanager client interface used here."""

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
        ...


class GitLabConfigLoader:
    """Loads and caches Relay configuration from a GitLab repository.

    The loader follows a read-through cache pattern:

    * On first access (:meth:`get`) the three YAML files are fetched from
      GitLab, parsed, validated, and stored in ``_config``.
    * Subsequent :meth:`get` calls return the cached object without hitting
      the network.
    * :meth:`refresh` (called by the push-webhook handler) invalidates the
      cache and triggers a fresh fetch.

    All cache mutations are protected by ``_config_lock`` so the loader is
    safe to use from multiple threads (e.g. a gunicorn worker pool).

    Dependency injection
    --------------------
    Two optional constructor parameters enable unit-testing without real AWS
    or GitLab endpoints:

    * ``secretsmanager_client`` — a pre-built boto3-compatible client (or a
      test double) so tests don't need AWS credentials.
    * ``file_fetcher`` — a callable with signature
      ``(url: str, token: str) -> str`` that replaces the default
      ``urllib.request`` HTTP call.  Inject a fake that returns fixture YAML
      content to avoid network calls in tests.

    Args:
        gitlab_project_id:
            GitLab numeric project ID, or URL-encoded namespace/path
            (e.g. ``"mygroup%2Fmyrepo"``).  Slashes must be percent-encoded;
            pass the raw path and the constructor will encode it for you.
        config_branch:
            Branch that holds the config YAML files (default: ``"main"``).
        secrets_manager_secret_name:
            Name or ARN of the AWS Secrets Manager secret that stores the
            GitLab personal-access token.
        gitlab_base_url:
            Base URL of the GitLab instance (default: ``"https://gitlab.com"``).
            Override for self-hosted GitLab installations.
        secretsmanager_client:
            Optional injected boto3 secretsmanager client.  If ``None``, a
            real client is created lazily on first use.
        file_fetcher:
            Optional callable ``(url: str, token: str) -> str`` used to fetch
            a raw file from GitLab.  If ``None``, the default urllib-based
            implementation is used.
    """

    def __init__(
        self,
        gitlab_project_id: str,
        config_branch: str = "main",
        secrets_manager_secret_name: str = "relay/gitlab-token",
        gitlab_base_url: str = _DEFAULT_GITLAB_BASE_URL,
        *,
        secretsmanager_client: _SecretsManagerClient | None = None,
        file_fetcher: Callable[[str, str], str] | None = None,
    ) -> None:
        # URL-encode the project ID so namespace/path forms work correctly.
        self._gitlab_project_id = urllib.parse.quote(
            gitlab_project_id, safe=""
        )
        self._config_branch = config_branch
        self._secrets_manager_secret_name = secrets_manager_secret_name
        self._gitlab_base_url = gitlab_base_url.rstrip("/")

        self._config: RelayConfig | None = None
        self._config_lock = threading.Lock()

        # Injected (or lazily created) AWS client.
        self._secretsmanager_client: _SecretsManagerClient | None = (
            secretsmanager_client
        )
        # Injected (or default urllib) file fetcher.
        self._file_fetcher: Callable[[str, str], str] = (
            file_fetcher if file_fetcher is not None else self._urllib_fetch
        )

        # Cached token — fetched once from Secrets Manager and reused until
        # :meth:`_fetch_gitlab_token` is called with ``force_refresh=True``.
        self._cached_token: str | None = None
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_secretsmanager_client(self) -> _SecretsManagerClient:
        """Return the Secrets Manager client, creating it lazily if needed."""
        if self._secretsmanager_client is None:
            self._secretsmanager_client = boto3.client("secretsmanager")
        return self._secretsmanager_client

    def _fetch_gitlab_token(self, *, force_refresh: bool = False) -> str:
        """Retrieve the GitLab personal-access token from AWS Secrets Manager.

        The token is cached in memory after the first successful call so that
        subsequent config refreshes do not incur an extra Secrets Manager
        round-trip.  Pass ``force_refresh=True`` to bypass the cache and
        re-fetch (e.g. after a token rotation).

        The token must have at least ``read_repository`` scope on the target
        project.

        Args:
            force_refresh: If ``True``, ignore any cached token and fetch a
                fresh value from Secrets Manager.

        Returns:
            The plaintext token string.

        Raises:
            RuntimeError: If the secret cannot be retrieved for any reason
                (missing secret, IAM permission denied, network error, etc.).
        """
        with self._token_lock:
            if self._cached_token is not None and not force_refresh:
                return self._cached_token

            logger.debug(
                "Fetching GitLab token from Secrets Manager secret '%s'",
                self._secrets_manager_secret_name,
            )
            try:
                client = self._get_secretsmanager_client()
                response = client.get_secret_value(
                    SecretId=self._secrets_manager_secret_name
                )
                # get_secret_value returns either SecretString (plaintext) or
                # SecretBinary (base64-encoded).  GitLab tokens are always strings.
                secret: str = response["SecretString"]
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to fetch GitLab token from Secrets Manager secret "
                    f"'{self._secrets_manager_secret_name}': {exc}"
                ) from exc

            self._cached_token = secret
            return secret

    @staticmethod
    def _urllib_fetch(url: str, token: str) -> str:
        """Default file fetcher using stdlib ``urllib.request``.

        Args:
            url:   Fully-formed URL to fetch.
            token: GitLab personal-access token for the ``PRIVATE-TOKEN`` header.

        Returns:
            Decoded UTF-8 response body.

        Raises:
            RuntimeError: On any HTTP or network error.
        """
        req = urllib.request.Request(
            url,
            headers={"PRIVATE-TOKEN": token},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                content: str = response.read().decode("utf-8")
                return content
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GitLab returned HTTP {exc.code} fetching '{url}': {exc.reason}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch '{url}' from GitLab: {exc}"
            ) from exc

    def _build_url(self, file_name: str) -> str:
        """Build the GitLab repository-files raw API URL for *file_name*.

        Args:
            file_name: Bare filename within the repository root
                       (e.g. ``"escalation.yaml"``).

        Returns:
            Fully-formed URL string ready for the file fetcher.
        """
        encoded_path = urllib.parse.quote(file_name, safe="")
        return _GITLAB_RAW_URL.format(
            base_url=self._gitlab_base_url,
            project_id=self._gitlab_project_id,
            file_path=encoded_path,
            branch=urllib.parse.quote(self._config_branch, safe=""),
        )

    def _fetch_file(self, token: str, file_name: str) -> str:
        """Fetch the raw content of a single config file from GitLab.

        Delegates to :attr:`_file_fetcher` (either the default urllib
        implementation or an injected test double).

        Args:
            token:     GitLab personal-access token with ``read_repository`` scope.
            file_name: Filename within the repository root
                       (e.g. ``"escalation.yaml"``).

        Returns:
            The decoded UTF-8 text content of the file.

        Raises:
            RuntimeError: If the fetch fails (non-2xx status, network error,
                or any exception from the injected fetcher).
        """
        url = self._build_url(file_name)
        logger.debug(
            "Fetching config file '%s' from project '%s' (branch '%s')",
            file_name,
            self._gitlab_project_id,
            self._config_branch,
        )
        try:
            return self._file_fetcher(url, token)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Unexpected error fetching '{file_name}' from GitLab "
                f"project '{self._gitlab_project_id}' "
                f"(branch '{self._config_branch}'): {exc}"
            ) from exc

    def _try_fetch_file(self, token: str, file_name: str) -> str | None:
        """Try to fetch a file; return None if it returns 404/Not Found."""
        try:
            return self._fetch_file(token, file_name)
        except RuntimeError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                logger.debug("Optional config file '%s' not found; skipping", file_name)
                return None
            raise

    def _load_fresh(self) -> RelayConfig:
        """Perform the full fetch-parse-validate cycle and return a RelayConfig.

        This is the internal implementation shared by :meth:`load` and
        :meth:`refresh`.  It always hits the network.

        Returns:
            A freshly validated :class:`~relay.config.schema.RelayConfig`.

        Raises:
            RuntimeError: If the GitLab token fetch or any file download fails.
            yaml.YAMLError: If any file is not valid YAML.
            pydantic.ValidationError: If the parsed data fails schema validation.
        """
        token = self._fetch_gitlab_token()

        escalation_yaml = self._fetch_file(token, _ESCALATION_FILE)
        routing_yaml = self._fetch_file(token, _ROUTING_FILE)

        environments_yaml = self._try_fetch_file(token, _ENVIRONMENTS_FILE)
        hierarchy_yaml = self._try_fetch_file(token, _HIERARCHY_FILE)
        catalog_yaml = self._try_fetch_file(token, _CATALOG_FILE)

        logger.info(
            "Parsing and validating config from GitLab project '%s' (branch '%s')",
            self._gitlab_project_id,
            self._config_branch,
        )
        new_config = RelayConfig.from_yaml_files_extended(
            escalation_yaml=escalation_yaml,
            routing_yaml=routing_yaml,
            environments_yaml=environments_yaml,
            hierarchy_yaml=hierarchy_yaml,
            catalog_yaml=catalog_yaml,
        )
        logger.info("Config loaded successfully at %s", new_config.loaded_at)
        return new_config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> RelayConfig:
        """Fetch the three config YAML files from GitLab and cache the result.

        If a valid config is already cached this method still performs a fresh
        network fetch and replaces the cache.  Use :meth:`get` if you want the
        cheap cached path.

        The operation is atomic with respect to the in-memory cache: the lock
        is held while the cache is updated so readers never see a partially
        refreshed state.

        Returns:
            A freshly validated :class:`~relay.config.schema.RelayConfig`.

        Raises:
            RuntimeError: If the GitLab token cannot be fetched or any file
                download fails.
            yaml.YAMLError: If any YAML file is malformed.
            pydantic.ValidationError: If the YAML content fails schema
                validation.
        """
        new_config = self._load_fresh()

        with self._config_lock:
            self._config = new_config

        return new_config

    def refresh(self) -> RelayConfig:
        """Invalidate the in-memory config cache and reload from GitLab.

        This is the entry point for the GitLab push-webhook handler.  Calling
        it ensures that the service picks up any changes merged to
        ``config_branch`` without requiring a process restart.

        Returns:
            The newly loaded and validated :class:`~relay.config.schema.RelayConfig`.

        Raises:
            RuntimeError: If the GitLab token cannot be fetched or any file
                download fails.
            yaml.YAMLError: If any YAML file is malformed.
            pydantic.ValidationError: If the YAML content fails schema
                validation.
        """
        with self._config_lock:
            self._config = None
        return self.load()

    def get(self) -> RelayConfig:
        """Return the cached config, loading it from GitLab on first call.

        This is the primary read path for application code.  It is cheap after
        the first call (no network I/O) and thread-safe.

        Returns:
            The cached (or freshly loaded) :class:`~relay.config.schema.RelayConfig`.

        Raises:
            RuntimeError: If the first load fails (GitLab unreachable, bad token,
                etc.).
        """
        with self._config_lock:
            if self._config is not None:
                return self._config

        # Load outside the lock so we don't hold it during the network round-
        # trip.  A second thread may race and also call load(); the lock inside
        # load() ensures the final write to _config is still atomic.
        return self.load()

    def get_escalation_policy(self, policy_id: str) -> EscalationPolicy | None:
        """Look up a single escalation policy by its ID.

        Args:
            policy_id: The unique policy identifier to look up.

        Returns:
            The matching :class:`~relay.core.model.EscalationPolicy`, or
            ``None`` if no policy with that ID exists in the current config.
        """
        config = self.get()
        for policy in config.escalation.policies:
            if policy.policy_id == policy_id:
                return policy
        return None
