"""IncidentSink implementation that creates GitLab issues for incident tracking and links runbooks. Used by the Relay Hub."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from relay.core.model import Incident

logger = logging.getLogger(__name__)

# GitLab project access levels (numeric, as the API reports them). Creating and
# closing incident-type issues needs Reporter (20) or above — a Guest can only
# comment. See https://docs.gitlab.com/ee/api/members.html#roles.
_REPORTER_ACCESS_LEVEL = 20
_ACCESS_LEVEL_NAMES = {
    0: "none",
    5: "minimal",
    10: "guest",
    20: "reporter",
    30: "developer",
    40: "maintainer",
    50: "owner",
}


def _max_project_access_level(project_body: dict[str, Any] | None) -> int:
    """Return the effective access level the token has on a project.

    A GitLab ``GET /projects/:id`` response carries a ``permissions`` object with
    ``project_access`` (direct membership) and ``group_access`` (inherited from a
    parent group); either may be ``null``. The effective level is the higher of
    the two. Returns ``0`` when neither is present (no access).
    """
    perms = (project_body or {}).get("permissions") or {}
    levels: list[int] = []
    for key in ("project_access", "group_access"):
        node = perms.get(key)
        if isinstance(node, dict) and node.get("access_level") is not None:
            try:
                levels.append(int(node["access_level"]))
            except (TypeError, ValueError):
                continue
    return max(levels) if levels else 0


@dataclass
class GitLabConfig:
    """Configuration for the GitLab API connection.

    A single token authenticates against one GitLab instance; the *project* is
    resolved per incident (from ``incident.external_tickets["gitlab_project"]``)
    so one Hub can file issues into whichever project owns the failing
    deployment. ``project_id`` is only a fallback used when an incident carries
    no resolved project.

    Attributes:
        token:              GitLab Personal/Project/Group Access Token. Must
                            carry the ``api`` scope to create + close
                            incident-type issues.
        project_id:         Optional fallback project (numeric ID or URL-encoded
                            path) used only when an incident has no resolved
                            ``external_tickets["gitlab_project"]``.
        base_url:           GitLab instance base URL.
                            Defaults to ``"https://gitlab.com"``.
        label:              Issue label applied to all auto-created incidents.
                            Defaults to ``"incident"``.
        environment_tier_map: Maps a Relay ``incident.environment`` to a GitLab
                            environment tier (e.g. ``{"prod": "production"}``).
                            Used to attach an ``environment::<tier>`` scoped
                            label so the issue feeds GitLab DORA per-tier.
        runbook_project_id: Optional project ID for runbook MRs.  If set,
                            created issues will link to the runbook project.
    """

    token: str
    project_id: str | None = None
    base_url: str = "https://gitlab.com"
    label: str = "incident"
    environment_tier_map: dict[str, str] = field(default_factory=dict)
    runbook_project_id: str | None = None


def _parse_env_tier_map(env_val: str | None) -> dict[str, str]:
    """Parse a RELAY_GITLAB_ENV_TIER_MAP string into {relay_env: gitlab_tier}.

    Format: comma-separated ``relay_env:gitlab_tier`` pairs, e.g.
    ``"prod:production,staging:staging,test:testing"``. Maps a Relay
    ``incident.environment`` to a GitLab environment tier so the created
    incident-type issue can be tagged per tier for DORA. Empty/unset → {}.
    Malformed pairs are logged and skipped. Lives in the adapter because the
    tier concept is GitLab-/DORA-specific knowledge.
    """
    raw = (env_val or "").strip()
    if not raw:
        return {}
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            logger.warning("Ignoring malformed RELAY_GITLAB_ENV_TIER_MAP pair %r", pair)
            continue
        env_name, _, tier = pair.partition(":")
        env_name, tier = env_name.strip(), tier.strip()
        if env_name and tier:
            mapping[env_name] = tier
    return mapping


def _urllib_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[int, dict[str, Any]]:
    """Execute an HTTP request using stdlib urllib.

    Args:
        method:  HTTP verb (``"GET"``, ``"POST"``, ``"PUT"``).
        url:     Full request URL.
        headers: Request headers dict.
        body:    Optional request body bytes.

    Returns:
        Tuple of ``(status_code, response_dict)``.  On ``HTTPError`` returns
        ``(exc.code, {})`` rather than raising.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            status: int = resp.status
            body_dict: dict[str, Any] = json.loads(resp.read().decode())
            return status, body_dict
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class GitLabSink:
    """IncidentSink that creates GitLab issues for incident tracking.

    Used by the Relay Hub to produce a paper trail and trigger runbook MRs.

    Implements the IncidentSink protocol from relay.adapters.base.
    """

    def __init__(
        self,
        config: GitLabConfig,
        http_fn: Any | None = None,
        token_provider: Any | None = None,
    ) -> None:
        """Initialise the sink.

        Args:
            config:  GitLab connection and project configuration.
            http_fn: Optional injectable HTTP callable for testing.  Must
                     match the signature
                     ``(method, url, headers, body) -> (status, dict)``.
                     Defaults to ``_urllib_request``.
            token_provider: Optional zero-arg callable returning a token string
                     (or None). When it returns a non-empty value it OVERRIDES
                     ``config.token`` — this is how a UI-set token (settings
                     store) takes precedence over the Secrets Manager fallback,
                     resolved live on each request so a newly-saved token applies
                     without a restart.
        """
        self._config = config
        self._http_fn = http_fn or _urllib_request
        self._token_provider = token_provider

    @classmethod
    def from_env(
        cls,
        token_provider: Any | None = None,
        secret_fetcher: Any | None = None,
        http_fn: Any | None = None,
    ) -> GitLabSink | None:
        """Build a GitLabSink from environment, or None if not configured.

        Reads ``RELAY_GITLAB_PROJECT_ID`` (optional fallback project),
        ``RELAY_GITLAB_TOKEN_SECRET`` (Secrets Manager secret name),
        ``RELAY_GITLAB_BASE_URL``, and ``RELAY_GITLAB_ENV_TIER_MAP``. The adapter
        owns its env-var names, the env→tier-map parsing, and the token
        precedence rule: a ``token_provider`` (UI settings-store token) overrides
        the Secrets Manager fallback, resolved live per request.

        Enabled when EITHER a Secrets Manager token OR a token_provider value is
        present; returns None otherwise so the Hub treats GitLab as disabled.
        ``secret_fetcher(name) -> str`` is injected so this module needs no AWS
        dependency.
        """
        import os

        project_id = os.environ.get("RELAY_GITLAB_PROJECT_ID", "").strip()
        secret_name = os.environ.get("RELAY_GITLAB_TOKEN_SECRET", "").strip()
        base_url = (
            os.environ.get("RELAY_GITLAB_BASE_URL", "https://gitlab.com").strip()
            or "https://gitlab.com"
        )
        env_tier_map = _parse_env_tier_map(os.environ.get("RELAY_GITLAB_ENV_TIER_MAP"))

        token = ""
        if secret_name and secret_fetcher is not None:
            try:
                token = secret_fetcher(secret_name) or ""
            except Exception:
                logger.warning("GitLab token secret fetch failed; using provider only")
                token = ""

        provider_has_token = False
        if token_provider is not None:
            try:
                provider_has_token = bool(token_provider())
            except Exception:
                provider_has_token = False

        if not token and not provider_has_token:
            return None

        return cls(
            GitLabConfig(
                token=token,
                project_id=project_id or None,
                base_url=base_url,
                environment_tier_map=env_tier_map,
            ),
            http_fn=http_fn,
            token_provider=token_provider,
        )

    def _token(self) -> str:
        """Return the active token: provider override, else config token."""
        if self._token_provider is not None:
            try:
                override = self._token_provider()
            except Exception:
                override = None
            if override:
                return str(override)
        return self._config.token

    @staticmethod
    def test_token(
        token: str,
        base_url: str = "https://gitlab.com",
        http_fn: Any | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Validate a token end-to-end: authentication, ``api`` scope, project access.

        Authenticating (``GET /user`` succeeds) is necessary but *not* sufficient
        — a read-only token authenticates yet cannot file incidents. This runs
        three escalating checks so a token that will fail at incident time fails
        the test instead:

        1. **Auth** — ``GET /api/v4/user`` proves the token is valid and names
           the account it belongs to.
        2. **``api`` scope** — ``GET /api/v4/personal_access_tokens/self`` reports
           the token's scopes; creating/closing issues needs the ``api`` scope
           (read-only ``read_api``/``read_repository`` tokens are rejected). When
           GitLab doesn't expose this endpoint (older self-managed, or a
           group/CI token), the check is skipped rather than failed.
        3. **Project access** — when ``project`` is given,
           ``GET /api/v4/projects/:id`` reports the effective access level; the
           token needs Reporter (20)+ to create incident-type issues. A token
           that can't even see the project, or only has Guest, is rejected.

        Keeps all GitLab API knowledge (URL shapes, PRIVATE-TOKEN header, access
        levels) inside the adapter.

        Args:
            token:    The token to validate.
            base_url: GitLab instance base URL.
            http_fn:  Optional injectable HTTP callable (testing).
            project:  Optional numeric ID or ``group/project`` path to verify
                      write access against. Omit to check auth + scope only.

        Returns ``{"ok", "username", "scopes", "project", "access_level",
        "error"}``. Never raises — network/HTTP failures come back as
        ``ok=False`` with ``error``.
        """
        http = http_fn or _urllib_request
        root = base_url.rstrip("/")
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        result: dict[str, Any] = {
            "ok": False,
            "username": "",
            "scopes": [],
            "project": project,
            "access_level": None,
            "error": None,
        }

        # 1. Authentication.
        try:
            status, body = http("GET", f"{root}/api/v4/user", headers, None)
        except Exception as exc:
            result["error"] = str(exc)
            return result
        if not (200 <= status < 300):
            result["error"] = f"authentication failed: HTTP {status}"
            return result
        result["username"] = (body or {}).get("username", "")

        # 2. Token scope. The self-introspection endpoint isn't always available
        #    (older self-managed GitLab; group/CI-job tokens) — a non-2xx there
        #    means "can't tell", so we skip rather than reject.
        try:
            s_status, s_body = http(
                "GET", f"{root}/api/v4/personal_access_tokens/self", headers, None
            )
        except Exception:
            s_status, s_body = 0, {}
        if 200 <= s_status < 300 and isinstance(s_body, dict):
            scopes = s_body.get("scopes") or []
            result["scopes"] = scopes
            if scopes and "api" not in scopes:
                result["error"] = (
                    "token lacks the 'api' scope (has "
                    f"{', '.join(scopes)}) — read-only tokens cannot file incidents"
                )
                return result

        # 3. Project access (only when a project is supplied).
        if project:
            encoded = urllib.parse.quote(str(project), safe="")
            try:
                p_status, p_body = http(
                    "GET", f"{root}/api/v4/projects/{encoded}", headers, None
                )
            except Exception as exc:
                result["error"] = f"project check failed: {exc}"
                return result
            if not (200 <= p_status < 300):
                result["error"] = (
                    f"cannot access project '{project}': HTTP {p_status} "
                    "(token has no membership, or the path is wrong)"
                )
                return result
            level = _max_project_access_level(p_body)
            result["access_level"] = level
            if level < _REPORTER_ACCESS_LEVEL:
                name = _ACCESS_LEVEL_NAMES.get(level, str(level))
                result["error"] = (
                    f"insufficient access to '{project}': have {name}, "
                    "need reporter or higher to create incident issues"
                )
                return result

        result["ok"] = True
        return result

    def _resolve_project(self, incident: Incident) -> str | None:
        """Resolve the GitLab project for an incident, URL-encoded for the API.

        Prefers the project resolved onto the incident
        (``incident.external_tickets["gitlab_project"]``, set from the
        catalog/org tree), falling back to the config's default project. Returns
        None when neither is available — the caller skips.

        GitLab accepts a numeric ID or a URL-encoded ``group/project`` path in
        the ``:id`` path segment, so a catalog path like ``identity/auth-api``
        works directly once percent-encoded.
        """
        project = incident.get_ticket("gitlab_project") or self._config.project_id
        if not project:
            return None
        return urllib.parse.quote(str(project), safe="")

    def _labels(self, incident: Incident) -> list[str]:
        """Build the issue label set: base label, severity, and env tier (DORA)."""
        labels = [self._config.label, str(incident.severity).lower()]
        tier = self._config.environment_tier_map.get(incident.environment)
        if tier:
            # Scoped label so GitLab DORA / boards can slice incidents per tier.
            labels.append(f"environment::{tier}")
        return labels

    def create_incident(self, incident: Incident) -> str:
        """Create a GitLab incident-type issue for the incident.

        Creates the issue as ``issue_type=incident`` so it feeds GitLab's DORA
        metrics (time-to-restore, change-failure-rate). The target project is
        resolved per incident (see :meth:`_resolve_project`).

        Returns the issue IID (project-scoped integer ID) as a string, or
        ``""`` on failure / when no project resolves.

        Args:
            incident: The relay Incident to record as a GitLab issue.

        Returns:
            The GitLab issue ``iid`` (project-scoped) as a string, or ``""``
            if the request fails or returns a non-2xx status.
        """
        project = self._resolve_project(incident)
        if project is None:
            logger.info(
                "GitLabSink.create_incident: no project resolved for %s — skipping",
                incident.correlation_id,
            )
            return ""

        payload = {
            "title": f"[{incident.severity}] {incident.alarm_name} — {incident.app_name}",
            "description": _build_issue_description(incident),
            "labels": ",".join(self._labels(incident)),
            # DORA: incident-type issues are what GitLab's incident metrics read.
            "issue_type": "incident",
        }

        url = (
            f"{self._config.base_url}"
            f"/api/v4/projects/{project}/issues"
        )
        headers = {
            "PRIVATE-TOKEN": self._token(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode()

        try:
            status, response = self._http_fn("POST", url, headers, body)
        except Exception:
            logger.warning(
                "GitLabSink.create_incident failed for %s",
                incident.correlation_id,
                exc_info=True,
            )
            return ""

        if not (200 <= status < 300):
            logger.warning(
                "GitLabSink.create_incident received HTTP %s for %s",
                status,
                incident.correlation_id,
            )
            return ""

        try:
            iid = response["iid"]
            return str(iid)
        except (KeyError, TypeError):
            logger.warning(
                "GitLabSink.create_incident: unexpected response shape for %s: %r",
                incident.correlation_id,
                response,
            )
            return ""

    def update_incident(self, external_id: str, incident: Incident) -> None:
        """Update an existing GitLab issue — title, description, labels.

        Args:
            external_id: The GitLab issue IID (project-scoped) as a string.
            incident:    The current incident state.
        """
        project = self._resolve_project(incident)
        if project is None:
            return
        put_payload = {
            "title": f"[{incident.severity}] {incident.alarm_name} — {incident.app_name}",
            "description": _build_issue_description(incident),
            "labels": ",".join(self._labels(incident)),
        }

        url = (
            f"{self._config.base_url}"
            f"/api/v4/projects/{project}/issues/{external_id}"
        )
        headers = {
            "PRIVATE-TOKEN": self._token(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(put_payload).encode()

        try:
            status, _ = self._http_fn("PUT", url, headers, body)
        except Exception:
            logger.warning(
                "GitLabSink.update_incident failed for issue iid %s",
                external_id,
                exc_info=True,
            )
            return

        if not (200 <= status < 300):
            logger.warning(
                "GitLabSink.update_incident received HTTP %s for issue iid %s",
                status,
                external_id,
            )

    def close_incident(self, external_id: str, incident: Incident) -> None:
        """Close the GitLab issue for a resolved incident.

        Sends a PUT to set ``state_event=close`` then a POST to add a closing
        comment with the incident resolution summary.

        Args:
            external_id: The GitLab issue IID (project-scoped) as a string.
            incident:    The resolved incident (used for close comment).
        """
        project = self._resolve_project(incident)
        if project is None:
            return
        close_payload = {"state_event": "close"}

        url = (
            f"{self._config.base_url}"
            f"/api/v4/projects/{project}/issues/{external_id}"
        )
        headers = {
            "PRIVATE-TOKEN": self._token(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(close_payload).encode()

        try:
            status, _ = self._http_fn("PUT", url, headers, body)
            if not (200 <= status < 300):
                logger.warning(
                    "GitLabSink.close_incident PUT received HTTP %s for issue iid %s",
                    status,
                    external_id,
                )
        except Exception:
            logger.warning(
                "GitLabSink.close_incident PUT failed for issue iid %s",
                external_id,
                exc_info=True,
            )

        # Add a closing comment regardless of close state success.
        note_payload = {
            "body": f"Relay: incident resolved — {incident.correlation_id}",
        }
        note_url = (
            f"{self._config.base_url}"
            f"/api/v4/projects/{project}/issues/{external_id}/notes"
        )
        note_body = json.dumps(note_payload).encode()

        try:
            note_status, _ = self._http_fn("POST", note_url, headers, note_body)
            if not (200 <= note_status < 300):
                logger.warning(
                    "GitLabSink.close_incident note POST received HTTP %s for issue iid %s",
                    note_status,
                    external_id,
                )
        except Exception:
            logger.warning(
                "GitLabSink.close_incident note POST failed for issue iid %s",
                external_id,
                exc_info=True,
            )

    def _make_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build and execute an authenticated request to the GitLab API.

        Args:
            method:  HTTP method (``"GET"``, ``"POST"``, ``"PUT"``).
            path:    URL path relative to base_url
                     (e.g. ``"/api/v4/projects/42/issues"``).
            payload: Optional dict to serialize as JSON body.

        Returns:
            Parsed JSON response body as a dict.

        Notes:
            TODO: add timeout and retry with back-off.
        """
        url = f"{self._config.base_url}{path}"
        data: bytes | None = None
        headers: dict[str, str] = {
            "PRIVATE-TOKEN": self._token(),
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            result: dict[str, Any] = json.loads(resp.read().decode())
            return result


def _build_issue_description(incident: Incident) -> str:
    """Build a markdown issue description with incident metadata.

    Args:
        incident: The incident to describe.

    Returns:
        A multi-line markdown string suitable for a GitLab issue body.
    """
    return (
        "## Incident Details\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Correlation ID | `{incident.correlation_id}` |\n"
        f"| Severity | **{incident.severity}** |\n"
        f"| App | {incident.app_name} |\n"
        f"| Environment | {incident.environment} |\n"
        f"| Account | {incident.account_id} |\n"
        f"| Alarm | `{incident.alarm_name}` |\n"
        f"| Created | {incident.created_at.isoformat()} |\n"
        f"| State | {incident.state} |\n\n"
        "_Auto-created by Relay Hub._"
    )
