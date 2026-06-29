# Per-environment input shape. Each env leaf (prod/dev/test) reads this to
# document and default the account-specific values it must supply. These are the
# values that DIFFER per environment/account — the imported VPC, subnets, and
# pre-provisioned ECS role ARNs (BYOV + BYOR are mandatory; nothing is created).
#
# A leaf includes this file and overrides `locals.env` with its real values:
#
#   include "env" { path = "${dirname(find_in_parent_folders())}/_env/env.hcl" }
#
# Then references them via dependency outputs + inputs in the leaf.

locals {
  # Placeholders — a real deploy overrides these in the env leaf or via TF_VAR_*.
  env = {
    environment = "unrouted"

    # BYOV — the pre-provisioned VPC + subnets handed to the team's account.
    vpc_id             = "vpc-CHANGE-ME"
    private_subnet_ids = ["subnet-CHANGE-ME-a", "subnet-CHANGE-ME-b"]
    public_subnet_ids  = [] # only needed when internal_alb = false

    # BYOR — existing ECS roles (teams cannot create roles). Paste Relay's
    # inline policy onto these (see the compute module's byor_* outputs).
    ecs_task_role_arn      = "arn:aws:iam::ACCOUNT:role/CHANGE-ME-relay-task"
    ecs_execution_role_arn = "arn:aws:iam::ACCOUNT:role/CHANGE-ME-relay-exec"

    # Real ECR image URI (built + pushed first).
    hub_image_uri = "ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/relay-hub:CHANGE-ME"

    # ALB exposure — internal by default. Supply certificate_arn for HTTPS.
    internal_alb    = true
    certificate_arn = ""
  }
}
