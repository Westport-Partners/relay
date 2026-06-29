include "root" {
  path = find_in_parent_folders()
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules/compute"
}

# Compute imports the data plane — Terragrunt resolves the dependency outputs
# (table/queue/topic ARNs) and orders `apply` data-plane → compute automatically.
dependency "data_plane" {
  config_path = "../data-plane"

  # Lets `terragrunt plan` work before the data plane is applied (mock outputs).
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
  mock_outputs = {
    table_name               = "relay-mock"
    table_arn                = "arn:aws:dynamodb:us-east-1:000000000000:table/relay-mock"
    ingest_queue_url         = "https://sqs.us-east-1.amazonaws.com/000000000000/relay-hub-ingest"
    ingest_queue_arn         = "arn:aws:sqs:us-east-1:000000000000:relay-hub-ingest"
    ingest_dlq_arn           = "arn:aws:sqs:us-east-1:000000000000:relay-hub-ingest-dlq"
    paging_topic_arn         = "arn:aws:sns:us-east-1:000000000000:relay-mock-paging"
    central_paging_topic_arn = "arn:aws:sns:us-east-1:000000000000:relay-mock-central-paging"
  }
}

inputs = {
  role        = "team"
  environment = local.env.environment

  # Imported data plane.
  table_name               = dependency.data_plane.outputs.table_name
  table_arn                = dependency.data_plane.outputs.table_arn
  ingest_queue_url         = dependency.data_plane.outputs.ingest_queue_url
  ingest_queue_arn         = dependency.data_plane.outputs.ingest_queue_arn
  ingest_dlq_arn           = dependency.data_plane.outputs.ingest_dlq_arn
  paging_topic_arn         = dependency.data_plane.outputs.paging_topic_arn
  central_paging_topic_arn = dependency.data_plane.outputs.central_paging_topic_arn

  # BYOV + BYOR (from env.hcl).
  vpc_id                 = local.env.vpc_id
  private_subnet_ids     = local.env.private_subnet_ids
  public_subnet_ids      = local.env.public_subnet_ids
  ecs_task_role_arn      = local.env.ecs_task_role_arn
  ecs_execution_role_arn = local.env.ecs_execution_role_arn

  hub_image_uri   = local.env.hub_image_uri
  internal_alb    = local.env.internal_alb
  certificate_arn = local.env.certificate_arn
}
