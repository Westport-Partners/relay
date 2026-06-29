# Root Terragrunt config — shared remote state, provider, and inputs.
#
# Environment is the namespace ABOVE org (Relay's hard env-isolation model): one
# Relay deployment per environment/isolation-zone. Each env leaf (prod/dev/test)
# sets its own account-specific VPC, subnet, and role ARNs; everything common
# lives here.
#
# Fill in the placeholders below for your account (or override per-env). Teams
# point this at their OWN state bucket + lock table — Relay does not provision
# remote-state infrastructure.

locals {
  # Common inputs shared by every environment. Override per-env in env.hcl.
  common = {
    aws_region = "us-east-1"
    team_name  = "unnamed-team" # the relay-<team> resource-name prefix
  }
}

# ---------------------------------------------------------------------------
# Remote state — S3 bucket + DynamoDB lock table. Both must already exist
# (or set `disable_init = true` and create them out-of-band). Keyed by the
# relative module path so data-plane and compute get independent state files.
# ---------------------------------------------------------------------------
remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket         = "CHANGE-ME-relay-tf-state" # your state bucket
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = local.common.aws_region
    encrypt        = true
    dynamodb_table = "CHANGE-ME-relay-tf-locks" # your lock table (PK: LockID)
  }
}

# ---------------------------------------------------------------------------
# Provider — generated so every module gets a consistent region + default tags.
# ---------------------------------------------------------------------------
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    provider "aws" {
      region = "${local.common.aws_region}"
      default_tags {
        tags = {
          Application = "relay"
          ManagedBy   = "terraform"
        }
      }
    }
  EOF
}

inputs = {
  team_name = local.common.team_name
}
