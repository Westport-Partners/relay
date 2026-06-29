# test environment — account-specific values. See ../_env/env.hcl for the shape.
# Fill in the CHANGE-ME placeholders with this account's real VPC/subnets/roles.

locals {
  env = {
    environment = "test"

    # BYOV — the pre-provisioned VPC + subnets in the dev account.
    vpc_id             = "vpc-CHANGE-ME"
    private_subnet_ids = ["subnet-CHANGE-ME-a", "subnet-CHANGE-ME-b"]
    public_subnet_ids  = []

    # BYOR — existing ECS roles (paste the compute module's byor_* inline policies).
    ecs_task_role_arn      = "arn:aws:iam::ACCOUNT:role/CHANGE-ME-relay-task"
    ecs_execution_role_arn = "arn:aws:iam::ACCOUNT:role/CHANGE-ME-relay-exec"

    hub_image_uri = "ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/relay-hub:CHANGE-ME"

    internal_alb    = true
    certificate_arn = ""
  }
}
