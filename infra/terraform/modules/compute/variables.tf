# Compute module inputs. Unlike the CDK stack (which creates a VPC and IAM roles
# by default), this module ALWAYS imports them — the target government/enterprise
# accounts forbid iam:CreateRole and ec2:CreateVpc. So vpc_id, subnets, and both
# ECS role ARNs are REQUIRED, not optional BYOV/BYOR opt-ins.

# ---- Topology / identity ----------------------------------------------------

variable "role" {
  description = "Relay topology role: \"team\" (default) or \"federated-hub\"."
  type        = string
  default     = "team"
  validation {
    condition     = contains(["team", "federated-hub"], var.role)
    error_message = "role must be \"team\" or \"federated-hub\"."
  }
}

variable "team_name" {
  description = "Team identifier (RELAY_TEAM_NAME)."
  type        = string
  default     = "unnamed-team"
}

variable "hub_scope" {
  description = "RELAY_HUB_SCOPE: \"local\" (team default) | \"local-federated\" (also forward SEV1/2 up) | \"central\" (federated-hub default)."
  type        = string
  default     = ""
}

variable "hub_image_uri" {
  description = "REQUIRED. Real ECR image URI for the Relay container. Never a placeholder."
  type        = string
  validation {
    condition     = length(var.hub_image_uri) > 0 && !can(regex("amazonlinux|PLACEHOLDER", var.hub_image_uri))
    error_message = "hub_image_uri must be a real ECR image URI (build + push first); placeholders are rejected."
  }
}

# ---- Imported data plane (outputs of the data-plane module) -----------------

variable "table_name" {
  description = "Data-plane DynamoDB table name."
  type        = string
}

variable "table_arn" {
  description = "Data-plane DynamoDB table ARN."
  type        = string
}

variable "ingest_queue_url" {
  description = "Data-plane SQS ingest queue URL."
  type        = string
}

variable "ingest_queue_arn" {
  description = "Data-plane SQS ingest queue ARN."
  type        = string
}

variable "ingest_dlq_arn" {
  description = "Data-plane SQS ingest DLQ ARN (the DLQ-depth alarm watches this)."
  type        = string
}

variable "paging_topic_arn" {
  description = "Team paging SNS topic ARN."
  type        = string
}

variable "central_paging_topic_arn" {
  description = "Central team paging SNS topic ARN."
  type        = string
}

# ---- BYOV (required — VPC is never created) ---------------------------------

variable "vpc_id" {
  description = "REQUIRED. Existing VPC to deploy into (these accounts ship a pre-provisioned VPC; we never create one)."
  type        = string
}

variable "private_subnet_ids" {
  description = "REQUIRED. Private subnet IDs for the ECS tasks (and the ALB when internal)."
  type        = list(string)
  validation {
    condition     = length(var.private_subnet_ids) > 0
    error_message = "private_subnet_ids must contain at least one subnet."
  }
}

variable "public_subnet_ids" {
  description = "Public subnet IDs — required only when internal_alb = false (internet-facing ALB)."
  type        = list(string)
  default     = []
}

# ---- BYOR (required — IAM roles are never created) --------------------------

variable "ecs_task_role_arn" {
  description = "REQUIRED. Existing ECS task role ARN (these accounts forbid iam:CreateRole). Attach Relay's inline policy to it — see the byor_* outputs."
  type        = string
}

variable "ecs_execution_role_arn" {
  description = "REQUIRED. Existing ECS execution role ARN. Attach the ECR/logs inline policy — see the byor_* outputs."
  type        = string
}

# ---- ALB / networking -------------------------------------------------------

variable "internal_alb" {
  description = "true (default): internal ALB in private subnets (corporate-network/VPN reachable). false: internet-facing ALB in public subnets. Tasks stay private either way."
  type        = bool
  default     = true
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the ALB. When set the listener is HTTPS:443 with an HTTP→HTTPS redirect; when empty the ALB serves HTTP:80."
  type        = string
  default     = ""
}

# ---- Container behaviour ----------------------------------------------------

variable "environment" {
  description = "Node environment (RELAY_NODE_ENVIRONMENT): prod | dev | test | unrouted. Drives the auth-mode default."
  type        = string
  default     = "unrouted"
}

variable "auth_mode" {
  description = "Explicit UI auth mode (none|alb|dev). Empty → environment-aware default (prod → none, non-prod → dev)."
  type        = string
  default     = ""
}

variable "access_control" {
  description = "Enable per-user access control (RELAY_AUTH_ACCESS_CONTROL)."
  type        = bool
  default     = false
}

variable "auth_allowed_users" {
  description = "Comma-separated usernames allowed when access control is on (RELAY_AUTH_ALLOWED_USERS)."
  type        = string
  default     = ""
}

variable "dev_user" {
  description = "RELAY_DEV_USER when auth_mode resolves to \"dev\"."
  type        = string
  default     = "operator"
}

variable "config_source" {
  description = "RELAY_CONFIG_SOURCE — set to \"local\" to load bundled /app/config."
  type        = string
  default     = ""
}

variable "tz" {
  description = "On-call timezone (RELAY_TZ)."
  type        = string
  default     = "UTC"
}

variable "log_level" {
  description = "Container log level (LOG_LEVEL)."
  type        = string
  default     = "INFO"
}

variable "resolve_alarm_tags" {
  description = "In-account alarm/resource tag resolution (RELAY_RESOLVE_ALARM_TAGS). Gates the tag-read inline-policy hint and the env var."
  type        = bool
  default     = true
}

variable "servicenow_instance" {
  description = "ServiceNow instance hostname (RELAY_SERVICENOW_INSTANCE). Empty disables the env var."
  type        = string
  default     = ""
}

variable "central_hub_bus_arn" {
  description = "Upstream federated-hub bus ARN. Required when hub_scope = local-federated (RELAY_CENTRAL_HUB_BUS_ARN)."
  type        = string
  default     = ""
}

# ---- Node self-identity (tile key) -----------------------------------------

variable "app_name" {
  description = "RELAY_NODE_APP_NAME (defaults to team_name when empty)."
  type        = string
  default     = ""
}

variable "deployment_id" {
  description = "RELAY_NODE_DEPLOYMENT_ID (defaults to team_name when empty)."
  type        = string
  default     = ""
}

variable "service_path" {
  description = "RELAY_NODE_SERVICE_PATH."
  type        = string
  default     = ""
}

variable "org_path" {
  description = "RELAY_NODE_ORG_PATH."
  type        = string
  default     = ""
}

# ---- AI / SMS ---------------------------------------------------------------

variable "ai_enabled" {
  description = "Enable AI augmentation (RELAY_AI_ENABLED)."
  type        = bool
  default     = false
}

variable "ai_provider" {
  description = "AI provider (RELAY_AI_PROVIDER). Empty/bedrock/bedrock-converse use Bedrock."
  type        = string
  default     = ""
}

variable "ai_model_id" {
  description = "RELAY_AI_MODEL_ID."
  type        = string
  default     = ""
}

variable "ai_base_url" {
  description = "RELAY_AI_BASE_URL (OpenAI-compatible endpoints)."
  type        = string
  default     = ""
}

variable "ai_api_key_secret" {
  description = "Secrets Manager secret name holding the AI API key (RELAY_AI_API_KEY_SECRET)."
  type        = string
  default     = ""
}

variable "enable_direct_sms" {
  description = "Grant sns:Publish for direct-to-phone SMS (broad; opt-in)."
  type        = bool
  default     = false
}

# ---- Sizing -----------------------------------------------------------------

variable "min_capacity" {
  description = "Service capacity floor / desired count."
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Auto-scaling ceiling."
  type        = number
  default     = 8
}

variable "cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 1024
}

variable "memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 2048
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
