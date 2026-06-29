"""Unit tests for RelayComputeStack.resolve_auth_mode.

The non-prod team board should come up write-capable (``dev``) out of the box,
while a prod board stays locked read-only (``none``) unless an operator opts in.
An explicit ``relay:auth_mode`` always wins over the environment-derived default.

Skipped when aws-cdk-lib is not installed (it is not a test dependency — infra
is normally validated via ``cdk synth`` — so CI without CDK simply skips this).
"""

from __future__ import annotations

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

from infra.stacks.compute_stack import resolve_auth_mode


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("prod", "none"),       # prod stays locked read-only
        ("dev", "dev"),         # non-prod is write-capable
        ("test", "dev"),
        ("unrouted", "dev"),    # the default-when-unset env is treated as non-prod
        (None, "dev"),
        ("PROD", "none"),       # case-insensitive
        (" prod ", "none"),     # surrounding whitespace ignored
    ],
)
def test_default_auth_mode_is_environment_aware(environment, expected):
    assert resolve_auth_mode(None, environment) == expected
    # An empty/whitespace explicit value is treated as unset.
    assert resolve_auth_mode("  ", environment) == expected


@pytest.mark.parametrize(
    ("explicit", "environment"),
    [
        ("alb", "dev"),      # explicit alb on a non-prod board
        ("none", "dev"),     # deliberately lock a non-prod board
        ("dev", "prod"),     # deliberately open a prod board
    ],
)
def test_explicit_auth_mode_always_wins(explicit, environment):
    assert resolve_auth_mode(explicit, environment) == explicit
