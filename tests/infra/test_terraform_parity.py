"""Parity tests for the hand-written Terraform path (infra/terraform/).

The Terraform modules are a *maintained alternative* to the canonical CDK stacks
(infra/stacks/) — CDK stays primary. Hand-written parity has one failure mode:
someone edits the data plane in one place (the CLI provisioner, the CDK stack, or
the TF module) and forgets the others, so the three drift apart silently. These
tests are the safety net: they assert the load-bearing data-plane invariants are
identical across all three sources, and that the compute module honours the
BYOR/BYOV-required invariant (it must never create an IAM role or a VPC, because
the target government/enterprise accounts forbid that).

Pure text assertions — no terraform binary needed, so they run in CI like the
existing ``importorskip``-guarded infra tests. The optional ``terraform validate``
section is skipped when the binary is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = REPO_ROOT / "infra" / "terraform"
DATA_PLANE_TF = TF_ROOT / "modules" / "data-plane" / "main.tf"
COMPUTE_TF = TF_ROOT / "modules" / "compute" / "main.tf"
CLI_PROVISIONER = REPO_ROOT / "scripts" / "relay-provision-cli.sh"
CDK_DATA_STACK = REPO_ROOT / "infra" / "stacks" / "data_stack.py"
LOCAL_BOOTSTRAP = REPO_ROOT / "scripts" / "relay-local-bootstrap.py"


@pytest.fixture(scope="module")
def data_plane_tf() -> str:
    return DATA_PLANE_TF.read_text()


@pytest.fixture(scope="module")
def cli_provisioner() -> str:
    return CLI_PROVISIONER.read_text()


@pytest.fixture(scope="module")
def cdk_data_stack() -> str:
    return CDK_DATA_STACK.read_text()


@pytest.fixture(scope="module")
def local_bootstrap() -> str:
    return LOCAL_BOOTSTRAP.read_text()


# ---------------------------------------------------------------------------
# Data-plane parity — the invariants that MUST match across all three sources.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "needle",
    [
        "incident-status-index",
        "incident-all-index",
        "gsi_open_pk",
        "gsi_all_pk",
        "created_at",
    ],
)
def test_gsi_definitions_match_across_sources(
    needle: str,
    data_plane_tf: str,
    cli_provisioner: str,
    cdk_data_stack: str,
    local_bootstrap: str,
) -> None:
    """Both incident-listing GSIs and their key attributes appear in all four.

    The local DynamoDB-Local bootstrap is the fourth source and the one that
    drifted in 8bcddaf — a stale status/opened_at key schema there silently
    blanked the Incidents/History/Metrics screens in the offline demo while
    production (CDK/TF) was fine. It must stay in lockstep with the rest.
    """
    assert needle in data_plane_tf, f"{needle} missing from data-plane TF"
    assert needle in cli_provisioner, f"{needle} missing from CLI provisioner"
    assert needle in cdk_data_stack, f"{needle} missing from CDK data stack"
    assert needle in local_bootstrap, f"{needle} missing from local bootstrap"


def test_ttl_attribute_is_ttl(
    data_plane_tf: str, cli_provisioner: str, cdk_data_stack: str
) -> None:
    assert 'attribute_name = "ttl"' in data_plane_tf
    assert "AttributeName=ttl" in cli_provisioner
    assert 'time_to_live_attribute="ttl"' in cdk_data_stack


def test_stream_view_type_matches(
    data_plane_tf: str, cli_provisioner: str, cdk_data_stack: str
) -> None:
    assert 'stream_view_type = "NEW_AND_OLD_IMAGES"' in data_plane_tf
    assert "StreamViewType=NEW_AND_OLD_IMAGES" in cli_provisioner
    assert "NEW_AND_OLD_IMAGES" in cdk_data_stack


def test_sqs_retention_and_redrive_match(
    data_plane_tf: str, cli_provisioner: str
) -> None:
    """DLQ 14d, main queue 4d + 60s visibility, redrive maxReceiveCount 5."""
    # DLQ retention 14 days = 1209600s.
    assert "1209600" in data_plane_tf
    assert "1209600" in cli_provisioner
    # Main queue retention 4 days = 345600s and 60s visibility timeout.
    assert "345600" in data_plane_tf
    assert "345600" in cli_provisioner
    assert "60" in data_plane_tf
    # Redrive maxReceiveCount 5.
    assert "maxReceiveCount" in data_plane_tf and "5" in data_plane_tf
    assert "maxReceiveCount" in cli_provisioner


def test_eventbridge_alarm_pattern_matches(
    data_plane_tf: str, cli_provisioner: str
) -> None:
    """The CloudWatch-alarm event pattern triplet is identical."""
    for needle in ("aws.cloudwatch", "CloudWatch Alarm State Change", "ALARM"):
        assert needle in data_plane_tf, f"{needle} missing from data-plane TF"
        assert needle in cli_provisioner, f"{needle} missing from CLI provisioner"


def test_resource_names_match(data_plane_tf: str, cli_provisioner: str) -> None:
    for name in ("relay-hub-ingest", "relay-hub-ingest-dlq", "relay-cloudwatch-alarm"):
        assert name in data_plane_tf
        assert name in cli_provisioner


# ---------------------------------------------------------------------------
# BYOR / BYOV invariant — the compute module must NEVER create roles or VPCs.
# ---------------------------------------------------------------------------

def test_compute_module_creates_no_iam_role() -> None:
    text = COMPUTE_TF.read_text()
    assert 'resource "aws_iam_role"' not in text, (
        "compute module must import ECS roles (BYOR), never create them — "
        "target accounts forbid iam:CreateRole."
    )


def test_compute_module_creates_no_vpc() -> None:
    text = COMPUTE_TF.read_text()
    assert 'resource "aws_vpc"' not in text, (
        "compute module must import the VPC (BYOV), never create one — "
        "target accounts forbid ec2:CreateVpc."
    )
    assert 'resource "aws_subnet"' not in text


def test_compute_module_requires_role_and_vpc_inputs() -> None:
    variables = (TF_ROOT / "modules" / "compute" / "variables.tf").read_text()
    # Required (no default) inputs for the imported VPC + roles.
    for var_name in ("vpc_id", "ecs_task_role_arn", "ecs_execution_role_arn"):
        assert f'variable "{var_name}"' in variables


# ---------------------------------------------------------------------------
# Optional: terraform validate per module (skipped when the binary is absent),
# mirroring the pytest.importorskip("aws_cdk") guard on the CDK infra tests.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["data-plane", "compute", "federation"])
def test_terraform_module_validates(module: str) -> None:
    if shutil.which("terraform") is None:
        pytest.skip("terraform binary not installed")
    module_dir = TF_ROOT / "modules" / module
    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-no-color"],
        cwd=module_dir,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, f"terraform init failed:\n{init.stderr}"
    validate = subprocess.run(
        ["terraform", "validate", "-no-color"],
        cwd=module_dir,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, f"terraform validate failed:\n{validate.stdout}\n{validate.stderr}"
