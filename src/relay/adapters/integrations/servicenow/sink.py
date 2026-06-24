"""IncidentSink implementation that creates and updates ServiceNow incident records via the ServiceNow Table API REST endpoint. Used exclusively by the Relay Hub (central role); team nodes never call ServiceNow."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from relay.core.model import Incident, Severity

logger = logging.getLogger(__name__)


@dataclass
class ServiceNowConfig:
    """Configuration for the ServiceNow Table API connection.

    Attributes:
        instance_url:     Full base URL of the ServiceNow instance,
                          e.g. ``"https://yourinstance.service-now.com"``.
        username:         API user account name.
        password:         API user password.
                          TODO: move credential to AWS Secrets Manager;
                          never hardcode in source or config files.
        incident_table:   REST table name for incidents.  Defaults to
                          ``"incident"``.
        assignment_group: ServiceNow assignment group name or sys_id.
                          Leave empty to use the table default.
        category:         Incident category field value.  Defaults to
                          ``"Software"``.
    """

    instance_url: str
    username: str
    password: str  # TODO: move to Secrets Manager; never hardcode
    incident_table: str = "incident"
    assignment_group: str = ""
    category: str = "Software"


def _urllib_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[int, dict]:
    """Execute an HTTP request using stdlib urllib.

    Args:
        method:  HTTP verb (``"GET"``, ``"POST"``, ``"PATCH"``).
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
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class ServiceNowSink:
    """IncidentSink that creates and manages ServiceNow incident records.

    Used exclusively by the Relay Hub; team nodes never call ServiceNow
    directly.

    Implements the IncidentSink protocol from relay.adapters.base.
    """

    def __init__(
        self,
        config: ServiceNowConfig,
        http_fn: Any | None = None,
        instance_url_provider: Any | None = None,
        username_provider: Any | None = None,
        password_provider: Any | None = None,
    ) -> None:
        """Initialise the sink with the given ServiceNow configuration.

        Args:
            config:  Connection and field-mapping configuration. Holds the
                     deploy-time/env fallback credentials.
            http_fn: Optional injectable HTTP callable for testing.  Must
                     match the signature
                     ``(method, url, headers, body) -> (status, dict)``.
                     Defaults to ``_urllib_request``.
            instance_url_provider / username_provider / password_provider:
                     Optional ``() -> str | None`` callables read live per
                     request. A non-empty provider value overrides the
                     corresponding config field (the UI settings-store
                     credential overrides the env/Secrets-Manager fallback),
                     mirroring the GitLab token-provider precedence.
        """
        self._config = config
        self._http_fn = http_fn or _urllib_request
        self._instance_url_provider = instance_url_provider
        self._username_provider = username_provider
        self._password_provider = password_provider

    @staticmethod
    def _resolve(provider: Any | None, fallback: str) -> str:
        """Return the provider's value if it yields a non-empty string, else fallback."""
        if provider is not None:
            try:
                override = provider()
            except Exception:
                override = None
            if override:
                return override
        return fallback

    def _instance_url(self) -> str:
        """Active instance URL: settings-store override, else config fallback."""
        return self._resolve(self._instance_url_provider, self._config.instance_url).rstrip("/")

    def _username(self) -> str:
        """Active username: settings-store override, else config fallback."""
        return self._resolve(self._username_provider, self._config.username)

    def _password(self) -> str:
        """Active password: settings-store override, else config fallback."""
        return self._resolve(self._password_provider, self._config.password)

    def _auth_header(self) -> str:
        """Build the Basic auth header value from the live username/password."""
        credentials = base64.b64encode(
            f"{self._username()}:{self._password()}".encode()
        ).decode()
        return f"Basic {credentials}"

    @classmethod
    def from_env(
        cls,
        secret_fetcher: Any | None = None,
        http_fn: Any | None = None,
        instance_url_provider: Any | None = None,
        username_provider: Any | None = None,
        password_provider: Any | None = None,
    ) -> ServiceNowSink | None:
        """Build a ServiceNowSink from environment + settings providers, or None.

        Credential precedence (resolved live per request via the providers, so a
        token saved on the Settings screen takes effect without a restart):
        the UI settings-store value overrides the env/Secrets-Manager fallback.

        Fallback env vars: ``RELAY_SERVICENOW_INSTANCE_URL`` / ``_USERNAME`` /
        ``_SECRET`` (a Secrets Manager secret *name* resolved via the injected
        ``secret_fetcher(name) -> str`` — so this module needs no AWS dependency).

        Returns None when neither the env fallback nor any settings provider
        yields an instance URL *and* a password, so the Hub treats ServiceNow as
        simply disabled (it never blocks startup).
        """
        import os

        instance_url = os.environ.get("RELAY_SERVICENOW_INSTANCE_URL", "").strip()
        username = os.environ.get("RELAY_SERVICENOW_USERNAME", "").strip()
        secret_name = os.environ.get("RELAY_SERVICENOW_SECRET", "").strip()
        password = ""
        if secret_name and secret_fetcher is not None:
            try:
                password = secret_fetcher(secret_name) or ""
            except Exception:
                logger.warning("ServiceNow secret fetch failed; using providers only")
                password = ""

        # A settings-store value for any field means ServiceNow may be UI-configured
        # even with no env fallback. Enable the sink when the *effective* instance
        # URL and password are both present (env fallback or live provider).
        def _effective(provider: Any | None, fallback: str) -> str:
            return cls._resolve(provider, fallback)

        eff_instance = _effective(instance_url_provider, instance_url)
        eff_password = _effective(password_provider, password)
        if not eff_instance or not eff_password:
            return None
        return cls(
            ServiceNowConfig(
                instance_url=instance_url,
                username=username,
                password=password,
            ),
            http_fn=http_fn,
            instance_url_provider=instance_url_provider,
            username_provider=username_provider,
            password_provider=password_provider,
        )

    @staticmethod
    def test_connection(
        instance_url: str,
        username: str,
        password: str,
        http_fn: Any | None = None,
    ) -> dict[str, Any]:
        """Validate ServiceNow credentials end-to-end against the Table API.

        Performs a single authenticated read against the incident table
        (``GET /api/now/table/incident?sysparm_limit=1``). A 2xx proves the
        instance URL is reachable and the username/password authenticate with at
        least read access to incidents. Keeps all ServiceNow API knowledge inside
        the adapter.

        Returns ``{"ok", "instance_url", "username", "error"}``. Never raises —
        network/HTTP failures come back as ``ok=False`` with ``error``.
        """
        http = http_fn or _urllib_request
        root = (instance_url or "").strip().rstrip("/")
        result: dict[str, Any] = {
            "ok": False,
            "instance_url": root,
            "username": username,
            "error": None,
        }
        if not root or not password:
            result["error"] = "instance URL and password are required"
            return result
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        }
        url = f"{root}/api/now/table/incident?sysparm_limit=1"
        try:
            status, _ = http("GET", url, headers, None)
        except Exception as exc:
            result["error"] = str(exc)
            return result
        if status == 401:
            result["error"] = "authentication failed: HTTP 401 (check username/password)"
            return result
        if not (200 <= status < 300):
            result["error"] = (
                f"ServiceNow check failed: HTTP {status} "
                "(check the instance URL and that the user can read incidents)"
            )
            return result
        result["ok"] = True
        return result

    def create_incident(self, incident: Incident) -> str:
        """POST to ServiceNow Table API to create an incident.

        Returns the sys_id of the created record, or ``""`` on failure.

        Args:
            incident: The relay Incident to record in ServiceNow.

        Returns:
            The ServiceNow ``sys_id`` of the newly created record, or ``""``
            if the request fails or returns a non-2xx status.
        """
        dm = incident.deployment_metadata or {}
        tags = incident.tags or {}
        # Build optional deployment-context appendix (never raises; folded into description).
        description_extra = ""
        try:
            if dm:
                description_extra += _format_context_block("Deployment context:", dm)
            if tags:
                description_extra += _format_context_block("Resource tags:", tags)
        except Exception:
            logger.warning("ServiceNowSink: failed to build context block", exc_info=True)
            description_extra = ""

        payload = {
            "short_description": (
                f"[{incident.severity}] {incident.alarm_name} — {incident.app_name}"
            ),
            "description": (
                f"Relay auto-created incident.\n\n"
                f"Correlation ID : {incident.correlation_id}\n"
                f"Account        : {incident.account_id}\n"
                f"Region         : {getattr(incident, 'region', 'unknown')}\n"
                f"Alarm ARN      : {getattr(incident, 'alarm_arn', 'N/A')}\n"
                f"Triggered at   : {incident.created_at.isoformat()}"
                f"{description_extra}"
            ),
            "urgency": _severity_to_urgency(incident.severity),
            "impact": _severity_to_impact(incident.severity),
            "category": self._config.category,
            "assignment_group": self._config.assignment_group,
            "correlation_id": incident.correlation_id,
        }

        url = (
            f"{self._instance_url()}"
            f"/api/now/table/{self._config.incident_table}"
        )
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode()

        try:
            status, response = self._http_fn("POST", url, headers, body)
        except Exception:
            logger.warning(
                "ServiceNowSink.create_incident failed for %s",
                incident.correlation_id,
                exc_info=True,
            )
            return ""

        if not (200 <= status < 300):
            logger.warning(
                "ServiceNowSink.create_incident received HTTP %s for %s",
                status,
                incident.correlation_id,
            )
            return ""

        try:
            sys_id: str = response["result"]["sys_id"]
            return sys_id
        except (KeyError, TypeError):
            logger.warning(
                "ServiceNowSink.create_incident: unexpected response shape for %s: %r",
                incident.correlation_id,
                response,
            )
            return ""

    def update_incident(self, external_id: str, incident: Incident) -> None:
        """Update an existing ServiceNow incident — severity, state, timeline.

        Args:
            external_id: The ServiceNow ``sys_id`` of the record to update.
            incident:    The current incident state.
        """
        patch_payload = {
            "urgency": _severity_to_urgency(incident.severity),
            "impact": _severity_to_impact(incident.severity),
            "description": (
                f"Relay incident updated.\n\n"
                f"Correlation ID : {incident.correlation_id}\n"
                f"State          : {incident.state}\n"
                f"Updated at     : {incident.updated_at.isoformat()}"
            ),
        }

        url = (
            f"{self._instance_url()}"
            f"/api/now/table/{self._config.incident_table}/{external_id}"
        )
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(patch_payload).encode()

        try:
            status, _ = self._http_fn("PATCH", url, headers, body)
        except Exception:
            logger.warning(
                "ServiceNowSink.update_incident failed for sys_id %s",
                external_id,
                exc_info=True,
            )
            return

        if not (200 <= status < 300):
            logger.warning(
                "ServiceNowSink.update_incident received HTTP %s for sys_id %s",
                status,
                external_id,
            )

    def close_incident(self, external_id: str, incident: Incident) -> None:
        """Mark the ServiceNow incident resolved/closed.

        Args:
            external_id: The ServiceNow ``sys_id`` to close.
            incident:    The resolved incident (used for close notes).
        """
        patch_payload = {
            "state": "6",
            "close_notes": f"Relay incident resolved: {incident.correlation_id}",
        }

        url = (
            f"{self._instance_url()}"
            f"/api/now/table/{self._config.incident_table}/{external_id}"
        )
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps(patch_payload).encode()

        try:
            status, _ = self._http_fn("PATCH", url, headers, body)
        except Exception:
            logger.warning(
                "ServiceNowSink.close_incident failed for sys_id %s",
                external_id,
                exc_info=True,
            )
            return

        if not (200 <= status < 300):
            logger.warning(
                "ServiceNowSink.close_incident received HTTP %s for sys_id %s",
                status,
                external_id,
            )

    def _make_request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        """Build and execute an authenticated HTTP request to the ServiceNow Table API.

        Args:
            method:  HTTP method (``"GET"``, ``"POST"``, ``"PATCH"``).
            path:    URL path relative to instance_url
                     (e.g. ``"/api/now/table/incident"``).
            payload: Optional dict to serialize as JSON body.

        Returns:
            Parsed JSON response body as a dict.

        Notes:
            TODO: add configurable retry with exponential back-off.
            TODO: raise a domain-specific exception type instead of bare
                  urllib.error.HTTPError so callers don't need to import urllib.
        """
        url = f"{self._instance_url()}{path}"

        data: bytes | None = None
        headers: dict[str, str] = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode())


def _format_context_block(header: str, data: dict) -> str:
    """Format a dict as a labelled key:value block for the ServiceNow description.

    Keys are sorted for deterministic output.  Never raises.
    """
    lines = "\n".join(f"  {k}: {v}" for k, v in sorted(data.items()))
    return f"\n\n{header}\n{lines}"


def _severity_to_urgency(severity: Severity) -> str:
    """Map a relay Severity to a ServiceNow urgency value (1=High, 3=Low).

    Args:
        severity: Relay severity enum value.

    Returns:
        String digit ``"1"``, ``"2"``, or ``"3"``.
    """
    mapping: dict[Severity, str] = {
        Severity.SEV1: "1",
        Severity.SEV2: "2",
        Severity.SEV3: "3",
        Severity.SEV4: "3",
    }
    return mapping.get(severity, "3")


def _severity_to_impact(severity: Severity) -> str:
    """Map a relay Severity to a ServiceNow impact value (1=High, 3=Low).

    Args:
        severity: Relay severity enum value.

    Returns:
        String digit ``"1"``, ``"2"``, or ``"3"``.
    """
    mapping: dict[Severity, str] = {
        Severity.SEV1: "1",
        Severity.SEV2: "2",
        Severity.SEV3: "3",
        Severity.SEV4: "3",
    }
    return mapping.get(severity, "3")
