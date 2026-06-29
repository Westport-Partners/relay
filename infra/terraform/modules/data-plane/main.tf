# Relay data plane — Terraform parity with scripts/relay-provision-cli.sh and
# infra/stacks/data_stack.py (+ the ingest half of RelayComputeStack).
#
# Durable, compute-free resources: the single DynamoDB table, the two paging SNS
# topics, the SQS ingest queue + DLQ, and the CloudWatch-alarm EventBridge rule
# that feeds the queue. Deploy this once; the compute module imports it by name
# and ARN. prevent_destroy guards the table (CDK uses RemovalPolicy.RETAIN).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # One table name per topology. Team: relay-<team>. Federated hub: relay-hub-fleet.
  table_name = var.role == "federated-hub" ? "relay-hub-fleet" : "relay-${var.team_name}"

  paging_topic_name = "relay-${var.team_name}-paging"
  central_topic_name = (
    var.role == "federated-hub" ? "relay-hub-central-paging" : "relay-${var.team_name}-central-paging"
  )

  ingest_queue_name = "relay-hub-ingest"
  ingest_dlq_name   = "relay-hub-ingest-dlq"
  alarm_rule_name   = "relay-cloudwatch-alarm"
}

# ---------------------------------------------------------------------------
# DynamoDB single table — contacts/incident/escalation/fleet/schedule state.
# pk/sk, PAY_PER_REQUEST, SSE, PITR, TTL=ttl, stream NEW_AND_OLD_IMAGES, and the
# two single-partition incident-listing GSIs. Mirrors RelayDataStack exactly.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "relay" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi_open_pk"
    type = "S"
  }
  attribute {
    name = "gsi_all_pk"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }

  # Sparse OPEN index — backs the live "Open" board (list_open_incidents).
  global_secondary_index {
    name            = "incident-status-index"
    hash_key        = "gsi_open_pk"
    range_key       = "created_at"
    projection_type = "ALL"
  }
  # Every incident — backs history + metrics (list_incidents).
  global_secondary_index {
    name            = "incident-all-index"
    hash_key        = "gsi_all_pk"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  tags = merge(var.tags, { Name = local.table_name })

  # CDK RemovalPolicy.RETAIN — never destroy the data plane on a stack teardown.
  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# SNS paging topics. The container publishes after resolving on-call.
# Per-contact subscriptions are managed at runtime by Relay (not declared here).
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "paging" {
  name         = local.paging_topic_name
  display_name = "Relay on-call paging — ${var.team_name}"
  tags         = var.tags
}

resource "aws_sns_topic" "central_paging" {
  name         = local.central_topic_name
  display_name = "Relay Hub — central team paging"
  tags         = var.tags
}

# ---------------------------------------------------------------------------
# SQS ingest DLQ + queue with redrive (matches the ingest half of the CLI
# provisioner / RelayComputeStack). The compute container consumes this queue.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "ingest_dlq" {
  name                      = local.ingest_dlq_name
  message_retention_seconds = 1209600 # 14 days
  tags                      = var.tags
}

resource "aws_sqs_queue" "ingest" {
  name                       = local.ingest_queue_name
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4 days
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn
    maxReceiveCount     = 5
  })
  tags = var.tags
}

# ---------------------------------------------------------------------------
# EventBridge rule: CloudWatch ALARM state change → the ingest queue. The
# zero-config CloudWatch seam — every alarm transition to ALARM is delivered.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "alarm" {
  name        = local.alarm_rule_name
  description = "Route CloudWatch alarm state changes to the Relay ingest queue."
  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail = {
      state = {
        value = ["ALARM"]
      }
    }
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "alarm_to_queue" {
  rule      = aws_cloudwatch_event_rule.alarm.name
  target_id = "relay-ingest"
  arn       = aws_sqs_queue.ingest.arn
}

# Allow EventBridge to deliver to the queue, scoped to this rule.
data "aws_iam_policy_document" "ingest_queue_policy" {
  statement {
    sid       = "AllowEventBridgeToRelayIngest"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.alarm.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy    = data.aws_iam_policy_document.ingest_queue_policy.json
}
