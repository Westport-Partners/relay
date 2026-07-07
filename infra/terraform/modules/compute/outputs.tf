# Compute outputs. Because the ECS roles are imported (BYOR is mandatory here),
# the module cannot attach policies itself — it emits the inline-policy + trust
# JSON for the team to paste onto their two pre-provisioned roles, mirroring
# RelayComputeStack._emit_byor_outputs.

locals {
  _account   = data.aws_caller_identity.current.account_id
  _region    = data.aws_region.current.name
  _partition = data.aws_partition.current.partition

  _log_group_arn = "arn:${local._partition}:logs:${local._region}:${local._account}:log-group:/relay/hub:*"

  _task_statements = concat(
    [
      {
        Sid    = "RelayHubFleetTable"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:Scan",
          "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem",
          # DescribeTable backs the /health/ready dynamodb probe.
          "dynamodb:DescribeTable",
        ]
        Resource = [var.table_arn, "${var.table_arn}/index/*"]
      },
      {
        Sid    = "RelayHubPaging"
        Effect = "Allow"
        # GetTopicAttributes backs the /health/ready sns_paging_topic probe.
        Action   = ["sns:Publish", "sns:GetTopicAttributes"]
        Resource = [var.central_paging_topic_arn, var.paging_topic_arn]
      },
      {
        Sid    = "RelayHubIngestConsume"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage",
          "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
        ]
        Resource = var.ingest_queue_arn
      },
    ],
    var.resolve_alarm_tags ? [{
      Sid      = "RelayAlarmTagResolution"
      Effect   = "Allow"
      Action   = local.alarm_tag_actions
      Resource = "*"
    }] : [],
    local.resolved_scope == "local-federated" && var.central_hub_bus_arn != "" ? [{
      Sid      = "RelayHubForwardEvents"
      Effect   = "Allow"
      Action   = ["events:PutEvents"]
      Resource = var.central_hub_bus_arn
    }] : [],
    var.enable_direct_sms ? [{
      Sid    = "RelayHubDirectSms"
      Effect = "Allow"
      # CheckIfPhoneNumberIsOptedOut backs the /health/ready sns_direct_sms probe.
      Action = ["sns:Publish", "sns:CheckIfPhoneNumberIsOptedOut"]
      # Scope by region, not sns:Protocol: Protocol is a Subscribe-only condition
      # key, absent from a Publish request context, so gating on it fails closed
      # and silently breaks direct paging (mirrors compute_stack.py).
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = local._region } }
    }] : [],
    local.ai_uses_bedrock ? [{
      Sid    = "RelayHubBedrockInvoke"
      Effect = "Allow"
      Action = ["bedrock:InvokeModel", "bedrock:Converse"]
      Resource = [
        "arn:aws:bedrock:*:${local._account}:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/*",
      ]
    }] : [],
    var.ai_api_key_secret != "" ? [{
      Sid      = "RelayHubAIKeySecret"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:${local._partition}:secretsmanager:${local._region}:${local._account}:secret:${var.ai_api_key_secret}*"
    }] : [],
  )

  _exec_statements = [
    {
      Sid    = "RelayHubEcr"
      Effect = "Allow"
      Action = [
        "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
      ]
      Resource = "*"
    },
    {
      Sid      = "RelayHubLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = local._log_group_arn
    },
  ]
}

output "dashboard_url" {
  description = "Relay dashboard URL."
  value       = local.dashboard_url
}

output "alb_dns_name" {
  description = "ALB DNS name."
  value       = aws_lb.relay.dns_name
}

output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.relay.name
}

output "service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.relay.name
}

output "byor_task_role_inline_policy" {
  description = "BYOR: paste this inline policy onto your ECS TASK role (ecs_task_role_arn)."
  value       = jsonencode({ Version = "2012-10-17", Statement = local._task_statements })
}

output "byor_execution_role_inline_policy" {
  description = "BYOR: paste this inline policy onto your ECS EXECUTION role (ecs_execution_role_arn)."
  value       = jsonencode({ Version = "2012-10-17", Statement = local._exec_statements })
}

output "byor_ecs_role_trust" {
  description = "BYOR: trust relationship both ECS roles must have."
  value = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local._account } }
    }]
  })
}
