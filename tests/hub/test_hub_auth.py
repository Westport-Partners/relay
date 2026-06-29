"""Tests for src/relay/hub/auth.py — identity extraction + write-allowlist."""
from __future__ import annotations

import base64
import json

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(claims: dict[str, str]) -> str:
    """Build a minimal (unsigned) ALB-style JWT with the given payload claims."""
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _alb_headers(*, identity: str | None = None, jwt: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if identity:
        h["x-amzn-oidc-identity"] = identity
    if jwt:
        h["x-amzn-oidc-data"] = jwt
    return h


# ---------------------------------------------------------------------------
# Identity extraction — alb mode
# ---------------------------------------------------------------------------

class TestIdentFromHeaders:
    def test_alb_identity_header_only(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        ident = auth.identfrom_headers(_alb_headers(identity="alice@example.com"))
        assert ident is not None
        assert ident.subject == "alice@example.com"
        assert ident.source == "alb"

    def test_alb_jwt_preferred_username(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        jwt = _make_jwt({"preferred_username": "alice", "email": "alice@example.com", "sub": "sub-123"})
        ident = auth.identfrom_headers(_alb_headers(identity="sub-123", jwt=jwt))
        assert ident is not None
        assert ident.subject == "alice"
        assert ident.email == "alice@example.com"

    def test_alb_jwt_email_fallback(self, monkeypatch):
        """preferred_username absent → fall back to email."""
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        jwt = _make_jwt({"email": "bob@example.com", "sub": "sub-456"})
        ident = auth.identfrom_headers(_alb_headers(identity="sub-456", jwt=jwt))
        assert ident is not None
        assert ident.subject == "bob@example.com"

    def test_alb_jwt_login_github(self, monkeypatch):
        """GitHub IdP: 'login' claim is the GitHub username."""
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        jwt = _make_jwt({"login": "gh-user", "sub": "12345"})
        ident = auth.identfrom_headers(_alb_headers(identity="12345", jwt=jwt))
        assert ident is not None
        assert ident.subject == "gh-user"

    def test_alb_jwt_sub_last_resort(self, monkeypatch):
        """Only 'sub' present — should use it."""
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        jwt = _make_jwt({"sub": "only-sub"})
        ident = auth.identfrom_headers(_alb_headers(identity="only-sub", jwt=jwt))
        assert ident is not None
        assert ident.subject == "only-sub"

    def test_alb_malformed_jwt_falls_back_to_identity(self, monkeypatch):
        """Malformed JWT must not raise; falls back to identity header."""
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        ident = auth.identfrom_headers({
            "x-amzn-oidc-identity": "fallback-user",
            "x-amzn-oidc-data": "not.a.valid!!!jwt",
        })
        assert ident is not None
        assert ident.subject == "fallback-user"

    def test_alb_no_headers_returns_none(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        from relay.hub import auth
        assert auth.identfrom_headers({}) is None

    def test_none_mode_always_none(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "none")
        from relay.hub import auth
        assert auth.identfrom_headers(_alb_headers(identity="whoever")) is None

    def test_dev_mode_returns_dev_user(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_DEV_USER", "tester")
        from relay.hub import auth
        ident = auth.identfrom_headers({})
        assert ident is not None
        assert ident.subject == "tester"
        assert ident.source == "dev"


# ---------------------------------------------------------------------------
# require_writer — no access control
# ---------------------------------------------------------------------------

class TestRequireWriterNoAccessControl:
    def test_none_mode_raises_403(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "none")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "false")
        from fastapi import HTTPException

        from relay.hub import auth
        with pytest.raises(HTTPException) as exc_info:
            auth.require_writer({})
        assert exc_info.value.status_code == 403

    def test_alb_authenticated_no_ac_returns_identity(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "false")
        from relay.hub import auth
        ident = auth.require_writer(_alb_headers(identity="alice"))
        assert ident.subject == "alice"

    def test_dev_no_ac_returns_identity(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "false")
        monkeypatch.setenv("RELAY_DEV_USER", "devuser")
        from relay.hub import auth
        ident = auth.require_writer({})
        assert ident.subject == "devuser"


# ---------------------------------------------------------------------------
# require_writer — with access control enabled
# ---------------------------------------------------------------------------

class TestRequireWriterWithAccessControl:
    def test_user_in_allowlist_returns_identity(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "alice,bob")
        from relay.hub import auth
        ident = auth.require_writer(_alb_headers(identity="alice"))
        assert ident.subject == "alice"

    def test_user_not_in_allowlist_raises_403(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "alice,bob")
        from fastapi import HTTPException

        from relay.hub import auth
        with pytest.raises(HTTPException) as exc_info:
            auth.require_writer(_alb_headers(identity="charlie"))
        assert exc_info.value.status_code == 403
        assert "write allowlist" in exc_info.value.detail

    def test_empty_allowlist_raises_403_misconfigured(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "")
        from fastapi import HTTPException

        from relay.hub import auth
        with pytest.raises(HTTPException) as exc_info:
            auth.require_writer(_alb_headers(identity="alice"))
        assert exc_info.value.status_code == 403
        # Message should indicate misconfiguration / empty allowlist
        assert "empty" in exc_info.value.detail.lower() or "allowlist" in exc_info.value.detail.lower()

    def test_case_insensitive_match(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "Alice,Bob")
        from relay.hub import auth
        # lowercase identity should match uppercase allowlist entry
        ident = auth.require_writer(_alb_headers(identity="alice"))
        assert ident.subject == "alice"
        # uppercase identity should also match
        ident2 = auth.require_writer(_alb_headers(identity="BOB"))
        assert ident2.subject == "BOB"

    def test_dev_user_in_allowlist_passes(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_DEV_USER", "devuser")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "devuser")
        from relay.hub import auth
        ident = auth.require_writer({})
        assert ident.subject == "devuser"

    def test_dev_user_not_in_allowlist_raises_403(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_DEV_USER", "devuser")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "someone-else")
        from fastapi import HTTPException

        from relay.hub import auth
        with pytest.raises(HTTPException) as exc_info:
            auth.require_writer({})
        assert exc_info.value.status_code == 403

    def test_whitespace_in_allowlist_stripped(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "  alice , bob  ")
        from relay.hub import auth
        ident = auth.require_writer(_alb_headers(identity="alice"))
        assert ident.subject == "alice"


# ---------------------------------------------------------------------------
# can_write helper
# ---------------------------------------------------------------------------

class TestCanWrite:
    def test_no_identity_returns_false(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "none")
        from relay.hub import auth
        assert auth.can_write({}) is False

    def test_authenticated_no_ac_returns_true(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "false")
        from relay.hub import auth
        assert auth.can_write(_alb_headers(identity="alice")) is True

    def test_ac_enabled_user_in_list_returns_true(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "alice")
        from relay.hub import auth
        assert auth.can_write(_alb_headers(identity="alice")) is True

    def test_ac_enabled_user_not_in_list_returns_false(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "alice")
        from relay.hub import auth
        assert auth.can_write(_alb_headers(identity="bob")) is False

    def test_ac_enabled_empty_list_returns_false(self, monkeypatch):
        monkeypatch.setenv("RELAY_AUTH_MODE", "alb")
        monkeypatch.setenv("RELAY_AUTH_ACCESS_CONTROL", "true")
        monkeypatch.setenv("RELAY_AUTH_ALLOWED_USERS", "")
        from relay.hub import auth
        assert auth.can_write(_alb_headers(identity="alice")) is False
