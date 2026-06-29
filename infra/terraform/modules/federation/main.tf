# Relay federated-hub bus — Terraform parity with infra/stacks/federation_stack.py.
#
# Synthesized only for the federated aggregator (role = federated-hub). Owns the
# relay-hub EventBridge bus that team containers forward selected SEV1/2 incidents
# up to, plus the optional rule that routes those events into the aggregator's
# ingest queue. A team deploy does NOT need this module.

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_event_bus" "hub" {
  name = "relay-hub"
  tags = var.tags
}

# Resource policy: org-wide PutEvents when an org id is configured (covers all
# current + future org accounts), else same-account-only ingress.
resource "aws_cloudwatch_event_bus_policy" "hub" {
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  policy = var.org_id != "" ? jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowOrgWidePutEvents"
      Effect    = "Allow"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.hub.arn
      Condition = { StringEquals = { "aws:PrincipalOrgID" = var.org_id } }
    }]
    }) : jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSameAccountPutEvents"
      Effect    = "Allow"
      Principal = "*"
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.hub.arn
      Condition = { StringEquals = { "aws:PrincipalAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

# Route all Relay events on the bus (source prefix "relay.") to the aggregator's
# ingest queue, if one was supplied.
resource "aws_cloudwatch_event_rule" "ingest" {
  count          = var.ingest_queue_arn != "" ? 1 : 0
  name           = "relay-hub-ingest"
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  description    = "Route all Relay events on the hub bus to the ingest SQS queue."
  event_pattern = jsonencode({
    source = [{ prefix = "relay." }]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "ingest" {
  count          = var.ingest_queue_arn != "" ? 1 : 0
  rule           = aws_cloudwatch_event_rule.ingest[0].name
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  target_id      = "relay-hub-ingest"
  arn            = var.ingest_queue_arn
}
