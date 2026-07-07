"""Unit tests for RelayComputeStack.resolve_cpu_architecture.

Fargate task defs with no ``runtime_platform`` default to X86_64; an ARM64 image
(built on an aarch64 host) then dies at launch with "exec format error". The
compute stack must honor ``relay:cpu_arch`` so aarch64 build hosts deploy ARM64
tasks. This guards that resolution so an arch regression fails CI, not a live
Fargate task launch.

``resolve_cpu_architecture`` returns a normalized ``"ARM64"``/``"X86_64"``
sentinel (the CDK ``CpuArchitecture`` members are jsii proxies that are neither
identity- nor value-comparable, so the resolver stays a plain string). No
aws-cdk-lib import is needed here.
"""

from __future__ import annotations

import pytest

from infra.stacks.compute_stack import resolve_cpu_architecture


@pytest.mark.parametrize(
    "explicit",
    [
        "ARM64",
        "arm64",
        " ARM64 ",   # whitespace-trimmed
        "Arm64",     # case-insensitive
    ],
)
def test_arm64_selected(explicit):
    assert resolve_cpu_architecture(explicit) == "ARM64"


@pytest.mark.parametrize(
    "explicit",
    [
        None,        # unset → X86_64 (Fargate's own default)
        "",          # empty → X86_64
        "  ",        # whitespace → X86_64
        "X86_64",    # explicit
        "x86_64",    # case-insensitive
        "amd64",     # not the ARM64 sentinel → safe default
        "garbage",   # anything non-ARM64 stays X86_64
    ],
)
def test_defaults_to_x86_64(explicit):
    assert resolve_cpu_architecture(explicit) == "X86_64"
