"""Hub authentication — identity extraction + write authorization.

Relay's dashboard is read-mostly: fleet status and incident views are open
(typically behind an internal ALB), but **write actions** (acknowledge, contact
CRUD, test page, override) require an authenticated identity.

Auth modes (env ``RELAY_AUTH_MODE``):
- ``alb``  (default in production): trust AWS ALB OIDC headers. When an ALB is
  configured with an OIDC/Cognito authenticate action, it injects
  ``x-amzn-oidc-identity`` (the OIDC sub) and ``x-amzn-oidc-data`` (a signed JWT
  with full userinfo claims). We extract the best available username from the JWT
  payload in priority order: ``preferred_username``, ``email``, ``username``,
  ``login`` (GitHub IdP), ``sub``. If the JWT is absent or malformed we fall back
  to the identity header. (The ALB has already authenticated the user; these
  headers are only present on requests it forwarded, and the ALB strips
  client-supplied copies.)
- ``dev``  : a fixed developer identity (``RELAY_DEV_USER``, default "dev") so
  the full UI is usable locally without an IdP. NEVER use in production.
- ``none`` : no identity; the UI is strictly read-only (all writes 403). Safe
  default posture when no auth is wired yet.

The default is ``none`` so a freshly-deployed Hub is never accidentally
write-open; an operator opts into ``alb`` (with an OIDC listener) or ``dev``.

Fine-grained write allowlist (optional):
  Set ``RELAY_AUTH_ACCESS_CONTROL=true`` to enable the allowlist.
  Set ``RELAY_AUTH_ALLOWED_USERS`` to a comma-separated list of usernames
  (matched case-insensitively).  If access control is enabled but the list is
  empty, all writes are denied with a clear misconfiguration message.  If access
  control is disabled, any authenticated identity may write (original behaviour).

GitHub IdP note: GitHub-as-OIDC-provider populates ``login`` (the GitHub
username) and ``email`` in the JWT userinfo; ``preferred_username`` is absent.
The claim priority list above handles this transparently.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """An authenticated principal. ``None`` identity means unauthenticated."""

    subject: str          # stable principal id (OIDC sub / email / dev user)
    email: str | None = None
    source: str = "unknown"   # alb | dev


# Header names ALB sets when an OIDC authenticate action is configured.
_ALB_IDENTITY_HEADER = "x-amzn-oidc-identity"
_ALB_DATA_HEADER = "x-amzn-oidc-data"

# Claim priority order for username extraction from the OIDC JWT payload.
# GitHub-as-IdP yields "login"/"email"; standard OIDC yields
# "preferred_username"/"sub".
_USERNAME_CLAIMS = ("preferred_username", "email", "username", "login", "sub")


def auth_mode() -> str:
    """Return the configured auth mode: alb | dev | none (default none)."""
    return os.environ.get("RELAY_AUTH_MODE", "none").strip().lower()


def _access_control_enabled() -> bool:
    """Return True when RELAY_AUTH_ACCESS_CONTROL is set to 'true'."""
    return os.environ.get("RELAY_AUTH_ACCESS_CONTROL", "false").strip().lower() == "true"


def _allowed_users() -> set[str]:
    """Parse RELAY_AUTH_ALLOWED_USERS into a lowercased set of usernames."""
    raw = os.environ.get("RELAY_AUTH_ALLOWED_USERS", "")
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def _username_from_alb(headers: dict[str, str]) -> tuple[str | None, str | None]:
    """Extract (username, email) from ALB OIDC headers.

    Tries to decode the JWT payload from ``x-amzn-oidc-data`` first, then
    falls back to ``x-amzn-oidc-identity``.

    JWT signature verification is intentionally skipped — the ALB has already
    authenticated the user and strips any client-supplied copies of these
    headers before forwarding.  Signature verification could be added as a
    future hardening step using the ALB's public key endpoint.

    Returns:
        (username, email) tuple; either may be None if not found.
    """
    # Attempt to extract claims from the JWT payload segment.
    jwt_data = headers.get(_ALB_DATA_HEADER)
    if jwt_data:
        try:
            parts = jwt_data.split(".")
            if len(parts) == 3:
                payload_b64 = parts[1]
                # Pad to a multiple of 4 for standard base64 decoding.
                padding = 4 - (len(payload_b64) % 4)
                if padding != 4:
                    payload_b64 += "=" * padding
                payload_bytes = base64.urlsafe_b64decode(payload_b64)
                claims = json.loads(payload_bytes)

                # Extract best username using priority claim list.
                username: str | None = None
                for claim in _USERNAME_CLAIMS:
                    val = claims.get(claim)
                    if val and str(val).strip():
                        username = str(val).strip()
                        break

                # Extract email separately (may already be the username).
                email_val = claims.get("email")
                email = str(email_val).strip() if email_val and str(email_val).strip() else None

                if username:
                    return username, email
        except Exception:  # noqa: BLE001 — be defensive; fall through on any error
            pass

    # Fall back to the identity header (OIDC sub or email, depending on IdP).
    ident = headers.get(_ALB_IDENTITY_HEADER)
    if ident and ident.strip():
        return ident.strip(), None

    return None, None


def identfrom_headers(headers: dict[str, str]) -> Identity | None:
    """Resolve the caller identity from request headers + the configured mode.

    Args:
        headers: case-insensitive-ish mapping of request headers (lowercased keys
                 recommended; we normalize).

    Returns:
        An Identity if the caller is authenticated, else None.
    """
    mode = auth_mode()
    # Normalize header keys to lowercase.
    h = {k.lower(): v for k, v in headers.items()}

    if mode == "dev":
        user = os.environ.get("RELAY_DEV_USER", "dev")
        return Identity(subject=user, email=f"{user}@local", source="dev")

    if mode == "alb":
        username, email = _username_from_alb(h)
        if username:
            return Identity(subject=username, email=email or username, source="alb")
        return None

    # mode == "none" (or unknown): never authenticated → read-only.
    return None


def can_write(headers: dict[str, str]) -> bool:
    """Return True only if the caller has an identity AND may write.

    This is the 'who + may they write' check for informational endpoints (e.g.
    the ``/auth`` status endpoint).  The allowlist gate mirrors ``require_writer``
    but returns a bool instead of raising.
    """
    ident = identfrom_headers(headers)
    if ident is None:
        return False
    if not _access_control_enabled():
        return True
    allowed = _allowed_users()
    return bool(allowed) and ident.subject.strip().lower() in allowed


def require_writer(headers: dict[str, str]) -> Identity:
    """Return the writer Identity or raise 403 if the caller can't write.

    Raises:
        fastapi.HTTPException(403) when no authenticated identity is present,
        or when access control is enabled and the identity is not on the
        write allowlist.
    """
    from fastapi import HTTPException

    ident = identfrom_headers(headers)
    if ident is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Write actions require authentication. This Hub's RELAY_AUTH_MODE "
                f"is '{auth_mode()}'. Configure an ALB OIDC listener "
                f"(RELAY_AUTH_MODE=alb) or, for local use, RELAY_AUTH_MODE=dev."
            ),
        )

    if _access_control_enabled():
        allowed = _allowed_users()
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Write access is denied: RELAY_AUTH_ACCESS_CONTROL is enabled "
                    "but RELAY_AUTH_ALLOWED_USERS is empty or not set. "
                    "Add at least one username to the allowlist or disable access control."
                ),
            )
        if ident.subject.strip().lower() not in allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"User '{ident.subject}' is authenticated but is not on the "
                    f"write allowlist. Contact your Hub administrator to be added "
                    f"to RELAY_AUTH_ALLOWED_USERS."
                ),
            )

    return ident
