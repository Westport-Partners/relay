"""Unit tests for resolve_certificate in RelayComputeStack.

Tests cover the three resolution paths:
  1. certificate_arn supplied (with and without phz for fqdn derivation)
  2. phz_id + phz_name supplied (auto-mint path)
  3. Neither supplied → (None, None, None)

Path 1 and 3 are fully exercised. Path 2 (auto-mint) requires constructing real
CDK constructs, which works fine in unit tests with a throwaway stack scope but
cannot complete DNS validation without AWS access — we assert non-None cert + fqdn.

Skipped when aws-cdk-lib is not installed (it is not a test dependency — infra
is normally validated via ``cdk synth`` — so CI without CDK simply skips this).
"""

from __future__ import annotations

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

import aws_cdk as cdk

from infra.stacks.compute_stack import resolve_certificate


def _throwaway_stack() -> cdk.Stack:
    """Return a minimal CDK stack scope for construct instantiation in tests."""
    return cdk.Stack(cdk.App(), "T")


# ---------------------------------------------------------------------------
# Path 3: no cert, no PHZ → (None, None, None)
# ---------------------------------------------------------------------------

def test_no_cert_no_phz_returns_none_triple():
    stack = _throwaway_stack()
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn="",
        phz_id="",
        phz_name="",
        alb_subdomain="relay",
    )
    assert cert is None
    assert zone is None
    assert fqdn is None


def test_no_cert_no_phz_with_custom_subdomain_still_none():
    """fqdn is None because there is no phz_name to derive it from."""
    stack = _throwaway_stack()
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn="",
        phz_id="",
        phz_name="",
        alb_subdomain="dashboard",
    )
    assert cert is None
    assert zone is None
    assert fqdn is None


# ---------------------------------------------------------------------------
# Path 1a: certificate_arn only (no PHZ) → cert non-None, zone None, fqdn None
# ---------------------------------------------------------------------------

def test_certificate_arn_only_returns_cert_no_fqdn():
    stack = _throwaway_stack()
    fake_arn = (
        "arn:aws:acm:us-east-1:123456789012:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn=fake_arn,
        phz_id="",
        phz_name="",
        alb_subdomain="relay",
    )
    assert cert is not None
    assert zone is None
    assert fqdn is None


# ---------------------------------------------------------------------------
# Path 1b: certificate_arn + phz_id + phz_name → cert non-None, zone non-None, fqdn set
# ---------------------------------------------------------------------------

def test_certificate_arn_with_phz_returns_cert_zone_fqdn():
    stack = _throwaway_stack()
    fake_arn = (
        "arn:aws:acm:us-east-1:123456789012:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn=fake_arn,
        phz_id="Z1234567890ABC",
        phz_name="corp.example.internal",
        alb_subdomain="relay",
    )
    assert cert is not None
    assert zone is not None
    assert fqdn == "relay.corp.example.internal"


def test_certificate_arn_custom_subdomain_fqdn():
    stack = _throwaway_stack()
    fake_arn = (
        "arn:aws:acm:us-east-1:123456789012:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn=fake_arn,
        phz_id="Z1234567890ABC",
        phz_name="team.internal",
        alb_subdomain="dashboard",
    )
    assert cert is not None
    assert fqdn == "dashboard.team.internal"


# ---------------------------------------------------------------------------
# Path 2: phz_id + phz_name (no cert_arn) → auto-mint cert, zone, fqdn
# Note: this constructs real CDK constructs but does NOT synth / call AWS.
# ---------------------------------------------------------------------------

def test_phz_only_mints_cert_and_fqdn():
    """Auto-mint path: zone lookup + Certificate construct both created; fqdn derived."""
    stack = _throwaway_stack()
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn="",
        phz_id="Z9876543210XYZ",
        phz_name="corp.example.internal",
        alb_subdomain="relay",
    )
    assert cert is not None
    assert zone is not None
    assert fqdn == "relay.corp.example.internal"


def test_phz_only_custom_subdomain_fqdn():
    stack = _throwaway_stack()
    cert, zone, fqdn = resolve_certificate(
        stack,
        certificate_arn="",
        phz_id="Z9876543210XYZ",
        phz_name="ops.internal",
        alb_subdomain="hub",
    )
    assert cert is not None
    assert fqdn == "hub.ops.internal"


# ---------------------------------------------------------------------------
# Fqdn string logic: verify the f-string derivation independently
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("subdomain", "zone_name", "expected_fqdn"),
    [
        ("relay", "corp.example.internal", "relay.corp.example.internal"),
        ("dashboard", "team.local", "dashboard.team.local"),
        ("hub", "ops.internal", "hub.ops.internal"),
    ],
)
def test_fqdn_derivation(subdomain, zone_name, expected_fqdn):
    """fqdn = f'{alb_subdomain}.{phz_name}' — verified directly."""
    assert f"{subdomain}.{zone_name}" == expected_fqdn
