include "root" {
  path = find_in_parent_folders()
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl")).locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules/data-plane"
}

inputs = {
  role = "team"
  # team_name comes from the root inputs.
}
