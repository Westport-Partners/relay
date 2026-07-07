"""Synth-based BYOR/BYOV gate tests for RelayComputeStack.

Locked-down accounts (government/enterprise) forbid ``iam:CreateRole`` and
``ec2:CreateVpc``.  The BYOR/BYOV path lets operators pass pre-provisioned ARNs
(``relay:ecs_task_role_arn``, ``relay:ecs_execution_role_arn``, ``relay:vpc_id``)
so the stack *imports* those resources instead of creating them.  When that path
fires, the stack emits three inline-policy/trust outputs that the operator pastes
onto their pre-provisioned roles.

These tests synth the compute stack under BYOR/BYOV context and assert:

1. **BYOR outputs emitted and well-formed** — ``ByorTaskRoleInlinePolicy``,
   ``ByorExecutionRoleInlinePolicy``, ``ByorEcsRoleTrust`` are present and carry
   the expected structure / required Sids.
2. **No IAM roles or VPC created** — ``AWS::IAM::Role`` and ``AWS::EC2::VPC``
   must not appear in the BYOR+BYOV synthesised template (the account cannot
   create them).
3. **``enable_direct_sms`` grant is correctly scoped** — the SNS Publish grant
   for direct-to-phone SMS uses ``aws:RequestedRegion`` (not the inapplicable
   ``sns:Protocol`` condition key which is absent from a Publish request context).
   This holds on BOTH the BYOR inline-policy path and the standard non-BYOR IAM
   policy path.
4. **RuntimePlatform is always emitted** — both ARM64 and X86_64 task defs carry
   a ``RuntimePlatform`` property, guarding the "exec format error" class of
   failures that occur when the pushed image arch does not match the Fargate task
   definition.

``resolve_cpu_architecture`` unit tests (``test_compute_cpu_arch.py``) already
cover the resolver logic; these tests cover the end-to-end synth output.

Skipped when aws-cdk-lib is not installed (it is an optional ``[infra]`` dep —
the dev venv has it, but a minimal runtime install does not).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed")

import aws_cdk as cdk
from aws_cdk import assertions

# infra/ must be on sys.path for the stacks/ imports below.  The existing infra
# tests rely on the same convention (pytest conftest or PYTHONPATH set by the
# test runner). We add it here defensively so the file is self-contained.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.compute_stack import RelayComputeStack  # noqa: E402
from stacks.data_stack import RelayDataStack  # noqa: E402

# ---------------------------------------------------------------------------
# Shared constants + helpers
# ---------------------------------------------------------------------------

_FAKE_EXEC_ARN = "arn:aws:iam::123456789012:role/relay-ecs-execution"
_FAKE_TASK_ARN = "arn:aws:iam::123456789012:role/relay-ecs-task"
_FAKE_VPC_ID = "vpc-0123456789abcdef0"
_ACCOUNT = "123456789012"
_REGION = "us-east-1"


def _synth_compute(extra_context: dict[str, str] | None = None) -> assertions.Template:
    """Return a CDK ``Template`` for the compute stack under BYOR+BYOV context.

    ``relay:image_check=false`` is set automatically — these tests do not need a
    real ECR image to synth.
    """
    ctx: dict[str, str] = {
        "relay:ecs_execution_role_arn": _FAKE_EXEC_ARN,
        "relay:ecs_task_role_arn": _FAKE_TASK_ARN,
        "relay:vpc_id": _FAKE_VPC_ID,
        "relay:image_check": "false",
    }
    if extra_context:
        ctx.update(extra_context)

    app = cdk.App(context=ctx)
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    data = RelayDataStack(app, "TestData", role="team", env=env)
    compute = RelayComputeStack(
        app,
        "TestCompute",
        role="team",
        data_table_name=data.table_name,
        data_table_arn=data.table_arn,
        paging_topic_arn=data.paging_topic.topic_arn,
        central_paging_topic_arn=data.central_paging_topic.topic_arn,
        env=env,
    )
    return assertions.Template.from_stack(compute)


def _synth_compute_permissive(extra_context: dict[str, str] | None = None) -> assertions.Template:
    """Return a CDK ``Template`` for the compute stack WITHOUT BYOR context."""
    ctx: dict[str, str] = {"relay:image_check": "false"}
    if extra_context:
        ctx.update(extra_context)

    app = cdk.App(context=ctx)
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    data = RelayDataStack(app, "TestData", role="team", env=env)
    compute = RelayComputeStack(
        app,
        "TestCompute",
        role="team",
        data_table_name=data.table_name,
        data_table_arn=data.table_arn,
        paging_topic_arn=data.paging_topic.topic_arn,
        central_paging_topic_arn=data.central_paging_topic.topic_arn,
        env=env,
    )
    return assertions.Template.from_stack(compute)


def _flatten_fn_join(value: Any) -> str:
    """Flatten a CloudFormation ``Fn::Join`` value to a string.

    Cross-stack token fragments (dicts) are skipped; only literal string
    fragments contribute to the result.  This is sufficient for asserting
    that well-known Sid strings are present in an inline-policy output.
    """
    parts: list[str] = []
    for fragment in value.get("Fn::Join", ["", []])[1]:
        if isinstance(fragment, str):
            parts.append(fragment)
    return "".join(parts)


# ---------------------------------------------------------------------------
# 1. BYOR outputs — emitted and well-formed
# ---------------------------------------------------------------------------


def test_byor_task_role_inline_policy_output_emitted() -> None:
    """ByorTaskRoleInlinePolicy output is present in BYOR synth."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    assert "ByorTaskRoleInlinePolicy" in outputs, (
        "ByorTaskRoleInlinePolicy output missing from BYOR synth — "
        "operators cannot paste task role permissions."
    )


def test_byor_execution_role_inline_policy_output_emitted() -> None:
    """ByorExecutionRoleInlinePolicy output is present in BYOR synth."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    assert "ByorExecutionRoleInlinePolicy" in outputs, (
        "ByorExecutionRoleInlinePolicy output missing from BYOR synth — "
        "operators cannot paste execution role permissions."
    )


def test_byor_ecs_role_trust_output_emitted() -> None:
    """ByorEcsRoleTrust output is present in BYOR synth."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    assert "ByorEcsRoleTrust" in outputs, (
        "ByorEcsRoleTrust output missing from BYOR synth — "
        "operators cannot configure the trust relationship."
    )


def test_byor_outputs_absent_in_permissive_mode() -> None:
    """BYOR outputs must NOT be emitted when no pre-provisioned roles are set.

    Emitting them in permissive mode would be confusing (they would contain
    unresolved Tokens rather than usable JSON).
    """
    template = _synth_compute_permissive()
    outputs = template.find_outputs("*")
    for key in ("ByorTaskRoleInlinePolicy", "ByorExecutionRoleInlinePolicy", "ByorEcsRoleTrust"):
        assert key not in outputs, (
            f"{key} should not appear in a non-BYOR synth — "
            "it is only emitted when pre-provisioned role ARNs are supplied."
        )


def test_byor_task_policy_contains_required_sids() -> None:
    """Task role inline policy contains the core required Sids."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    text = _flatten_fn_join(outputs["ByorTaskRoleInlinePolicy"]["Value"])
    for sid in (
        "RelayHubFleetTable",
        "RelayHubPaging",
        "RelayHubIngestConsume",
    ):
        assert sid in text, f"Required Sid '{sid}' missing from ByorTaskRoleInlinePolicy"


def test_byor_execution_policy_contains_ecr_and_logs() -> None:
    """Execution role inline policy covers ECR pull and CloudWatch log writes."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    text = _flatten_fn_join(outputs["ByorExecutionRoleInlinePolicy"]["Value"])
    for sid in ("RelayHubEcr", "RelayHubLogs"):
        assert sid in text, f"Required Sid '{sid}' missing from ByorExecutionRoleInlinePolicy"


def test_byor_trust_contains_ecs_principal_and_source_account() -> None:
    """Trust policy grants ecs-tasks.amazonaws.com with aws:SourceAccount condition."""
    template = _synth_compute()
    outputs = template.find_outputs("*")
    # ByorEcsRoleTrust is a plain JSON string (no cross-stack references).
    trust_json: str = outputs["ByorEcsRoleTrust"]["Value"]
    trust = json.loads(trust_json)
    stmts = trust["Statement"]
    assert len(stmts) == 1
    stmt = stmts[0]
    assert stmt["Principal"]["Service"] == "ecs-tasks.amazonaws.com"
    assert stmt["Action"] == "sts:AssumeRole"
    # Require a source-account condition to prevent the confused-deputy attack.
    cond = stmt.get("Condition", {}).get("StringEquals", {})
    assert "aws:SourceAccount" in cond, (
        "ByorEcsRoleTrust trust policy must restrict sts:AssumeRole to the "
        "deploying account (aws:SourceAccount condition missing)."
    )
    assert cond["aws:SourceAccount"] == _ACCOUNT


# ---------------------------------------------------------------------------
# 2. No IAM roles or VPC created in BYOR+BYOV mode
# ---------------------------------------------------------------------------


def test_byor_mode_creates_no_iam_roles() -> None:
    """No AWS::IAM::Role resource in a BYOR+BYOV synth.

    Accounts that forbid iam:CreateRole must not get a template that tries to
    create roles — the operator supplies pre-provisioned ARNs instead.
    """
    template = _synth_compute()
    roles = template.find_resources("AWS::IAM::Role")
    assert len(roles) == 0, (
        f"BYOR synth must not create IAM roles, but found: {list(roles.keys())}. "
        "Check that relay:ecs_task_role_arn + relay:ecs_execution_role_arn "
        "trigger the import-instead-of-create path."
    )


def test_byor_mode_creates_no_vpc() -> None:
    """No AWS::EC2::VPC resource in a BYOR+BYOV synth.

    Accounts that forbid ec2:CreateVpc supply a vpc_id; the stack must import,
    not create.
    """
    template = _synth_compute()
    vpcs = template.find_resources("AWS::EC2::VPC")
    assert len(vpcs) == 0, (
        f"BYOR/BYOV synth must not create a VPC, but found: {list(vpcs.keys())}. "
        "Check that relay:vpc_id triggers the Vpc.from_lookup path."
    )


# ---------------------------------------------------------------------------
# 3. enable_direct_sms grant scoped by aws:RequestedRegion (not sns:Protocol)
# ---------------------------------------------------------------------------
#
# sns:Protocol is a Subscribe-only condition key; it is absent from a Publish
# request context.  Using it on a Publish grant would fail *closed*, silently
# breaking direct paging.  The correct key is aws:RequestedRegion.


def test_byor_direct_sms_uses_requested_region_not_protocol() -> None:
    """BYOR task-role inline policy scopes direct-SMS by region, not sns:Protocol."""
    template = _synth_compute({"relay:enable_direct_sms": "true"})
    outputs = template.find_outputs("*")
    text = _flatten_fn_join(outputs["ByorTaskRoleInlinePolicy"]["Value"])

    assert "RelayHubDirectSms" in text, (
        "DirectSms Sid missing from ByorTaskRoleInlinePolicy when "
        "relay:enable_direct_sms=true."
    )
    assert "aws:RequestedRegion" in text, (
        "Direct-SMS grant must scope by aws:RequestedRegion (absent: would grant "
        "sns:Publish to ALL regions)."
    )
    assert "sns:Protocol" not in text, (
        "sns:Protocol is a Subscribe-only condition key and must NOT appear on a "
        "Publish grant — it is absent from a Publish request context and would "
        "silently break direct paging."
    )


def test_permissive_direct_sms_uses_requested_region_not_protocol() -> None:
    """Standard (non-BYOR) task-role IAM policy also scopes direct-SMS by region.

    This covers the non-BYOR path so a regression on either branch is caught.
    """
    template = _synth_compute_permissive({"relay:enable_direct_sms": "true"})
    policies = template.find_resources("AWS::IAM::Policy")

    found_sms_stmt: dict[str, Any] | None = None
    for _key, pol_val in policies.items():
        for stmt in pol_val.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
            if stmt.get("Sid") == "RelayHubDirectSms":
                found_sms_stmt = stmt
                break
        if found_sms_stmt is not None:
            break

    assert found_sms_stmt is not None, (
        "RelayHubDirectSms statement missing from standard IAM policy when "
        "relay:enable_direct_sms=true."
    )
    cond = found_sms_stmt.get("Condition", {})
    assert "StringEquals" in cond and "aws:RequestedRegion" in cond["StringEquals"], (
        "Direct-SMS grant must have aws:RequestedRegion condition."
    )
    assert "sns:Protocol" not in str(cond), (
        "sns:Protocol must not appear on a Publish grant."
    )


# ---------------------------------------------------------------------------
# 4. RuntimePlatform always present (guards the exec-format-error class)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpu_arch", "expected"),
    [
        ("X86_64", "X86_64"),
        ("ARM64", "ARM64"),
    ],
)
def test_task_def_carries_runtime_platform(cpu_arch: str, expected: str) -> None:
    """Fargate task def has a RuntimePlatform for both X86_64 and ARM64 in BYOR mode.

    A task def with no RuntimePlatform defaults to X86_64 at runtime, so an
    ARM64 image (built on an aarch64 host) would crash with "exec format error".
    The BYOR synth must always emit RuntimePlatform regardless of architecture.
    """
    template = _synth_compute({"relay:cpu_arch": cpu_arch})
    task_defs = template.find_resources("AWS::ECS::TaskDefinition")
    assert len(task_defs) > 0, "No ECS TaskDefinition found in BYOR synth."
    for _key, td_val in task_defs.items():
        props = td_val.get("Properties", {})
        runtime_platform = props.get("RuntimePlatform")
        assert runtime_platform is not None, (
            f"ECS TaskDefinition {_key!r} has no RuntimePlatform — an ARM64 image "
            "pushed to this task def will crash with exec format error at launch."
        )
        assert runtime_platform.get("CpuArchitecture") == expected, (
            f"Expected CpuArchitecture={expected!r}, got "
            f"{runtime_platform.get('CpuArchitecture')!r}."
        )
        assert runtime_platform.get("OperatingSystemFamily") == "LINUX"
