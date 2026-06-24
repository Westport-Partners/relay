"""Unit tests for RelayComputeStack.resolve_internal_alb.

The ALB defaults to INTERNAL (private subnets) so Relay comes up as an internal
utility for most orgs; only an explicit ``relay:internal_alb=false`` opts into a
public, internet-facing ALB (for accounts with no VPN/peering into the VPC).

Skipped when aws-cdk-lib is not installed (it is not a test dependency — infra
is normally validated via ``cdk synth`` — so CI without CDK simply skips this).
"""

from __future__ import annotations

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

from infra.stacks.compute_stack import resolve_internal_alb


@pytest.mark.parametrize(
    "explicit",
    [
        None,        # unset → internal default
        "",          # empty → internal default
        "  ",        # whitespace → internal default
        "true",      # explicit internal
        "True",      # case-insensitive
        "garbage",   # anything non-"false" stays internal (fail safe)
    ],
)
def test_defaults_to_internal(explicit):
    assert resolve_internal_alb(explicit) is True


@pytest.mark.parametrize(
    "explicit",
    [
        "false",
        "False",
        "FALSE",
        " false ",   # surrounding whitespace ignored
    ],
)
def test_explicit_false_is_public(explicit):
    assert resolve_internal_alb(explicit) is False
