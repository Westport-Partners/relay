# Relay compute plane — Terraform parity with infra/stacks/compute_stack.py.
#
# The always-on Fargate container + ALB, the DLQ-depth alarm, and CPU
# autoscaling. Imports the data plane (table/queue/topics) by name + ARN, and —
# unlike the CDK stack — ALWAYS imports the VPC, subnets, and both ECS roles
# (BYOV + BYOR are required, never created), because the target accounts forbid
# ec2:CreateVpc and iam:CreateRole. The ingest queue + CloudWatch-alarm rule live
# in the data-plane module (the "deploy data once" split); this module only
# consumes them.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

data "aws_vpc" "this" {
  id = var.vpc_id
}

locals {
  resolved_scope = var.hub_scope != "" ? var.hub_scope : (var.role == "federated-hub" ? "central" : "local")

  # resolve_auth_mode: explicit wins; else prod → none, non-prod → dev.
  auth_mode = (
    trimspace(var.auth_mode) != "" ? trimspace(var.auth_mode) :
    (lower(trimspace(var.environment)) == "prod" ? "none" : "dev")
  )

  ai_uses_bedrock = var.ai_enabled && contains(["", "bedrock", "bedrock-converse"], lower(var.ai_provider))

  app_name      = var.app_name != "" ? var.app_name : var.team_name
  deployment_id = var.deployment_id != "" ? var.deployment_id : var.team_name

  # Read-only tag-lookup actions the in-process detection pipeline uses to
  # resolve an alarm's + monitored resource's tags. None support resource-level
  # scoping, hence "*". Mirrors _ALARM_TAG_ACTIONS in compute_stack.py.
  alarm_tag_actions = [
    "cloudwatch:ListTagsForResource",
    "lambda:ListTags",
    "sqs:ListQueueTags",
    "ecs:ListTagsForResource",
    "ec2:DescribeTags",
  ]

  dashboard_scheme = var.certificate_arn != "" ? "https" : "http"
  dashboard_host   = aws_lb.relay.dns_name
  dashboard_url    = "${local.dashboard_scheme}://${local.dashboard_host}/"

  alb_subnet_ids = var.internal_alb ? var.private_subnet_ids : var.public_subnet_ids
  alb_port       = var.certificate_arn != "" ? 443 : 80

  container_env = merge(
    {
      RELAY_ROLE                     = "hub"
      RELAY_RUNTIME                  = "fargate"
      RELAY_HUB_SCOPE                = local.resolved_scope
      RELAY_FLEET_TABLE_NAME         = var.table_name
      RELAY_TABLE_NAME               = var.table_name
      RELAY_SQS_QUEUE_URL            = var.ingest_queue_url
      AWS_REGION                     = data.aws_region.current.name
      AWS_DEFAULT_REGION             = data.aws_region.current.name
      RELAY_CENTRAL_PAGING_TOPIC_ARN = var.central_paging_topic_arn
      RELAY_SNS_TOPIC_ARN            = var.paging_topic_arn
      RELAY_PAGING_TOPIC_ARN         = var.paging_topic_arn
      RELAY_SERVICENOW_INSTANCE      = var.servicenow_instance
      RELAY_AUTH_MODE                = local.auth_mode
      RELAY_AUTH_ACCESS_CONTROL      = var.access_control ? "true" : "false"
      RELAY_TZ                       = var.tz
      LOG_LEVEL                      = var.log_level
      RELAY_RESOLVE_ALARM_TAGS       = var.resolve_alarm_tags ? "true" : "false"
      RELAY_TEAM_NAME                = var.team_name
      RELAY_NODE_APP_NAME            = local.app_name
      RELAY_NODE_DEPLOYMENT_ID       = local.deployment_id
      RELAY_NODE_ENVIRONMENT         = var.environment
      RELAY_NODE_SERVICE_PATH        = var.service_path
      RELAY_NODE_ORG_PATH            = var.org_path
      RELAY_DASHBOARD_URL            = local.dashboard_url
    },
    var.auth_allowed_users != "" ? { RELAY_AUTH_ALLOWED_USERS = var.auth_allowed_users } : {},
    local.auth_mode == "dev" ? { RELAY_DEV_USER = var.dev_user } : {},
    var.config_source == "local" ? { RELAY_CONFIG_SOURCE = "local", RELAY_CONFIG_DIR = "/app/config" } : {},
    var.ai_enabled ? merge(
      { RELAY_AI_ENABLED = "true" },
      var.ai_model_id != "" ? { RELAY_AI_MODEL_ID = var.ai_model_id } : {},
      var.ai_provider != "" ? { RELAY_AI_PROVIDER = var.ai_provider } : {},
      var.ai_base_url != "" ? { RELAY_AI_BASE_URL = var.ai_base_url } : {},
      var.ai_api_key_secret != "" ? { RELAY_AI_API_KEY_SECRET = var.ai_api_key_secret } : {},
    ) : {},
    local.resolved_scope == "local-federated" && var.central_hub_bus_arn != "" ? { RELAY_CENTRAL_HUB_BUS_ARN = var.central_hub_bus_arn } : {},
  )
}

# ---------------------------------------------------------------------------
# Security groups. The ALB accepts the listener port from anywhere routable to
# it; the service only accepts the container port from the ALB. Tasks stay
# private (no public IP) regardless of ALB scheme.
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "relay-hub-alb"
  description = "Relay Hub ALB — ingress on the listener port."
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "relay-hub-alb" })

  ingress {
    description = "Dashboard listener"
    from_port   = local.alb_port
    to_port     = local.alb_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  # HTTP→HTTPS redirect listener when HTTPS is enabled.
  dynamic "ingress" {
    for_each = var.certificate_arn != "" ? [80] : []
    content {
      description = "HTTP redirect to HTTPS"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "service" {
  name        = "relay-hub-service"
  description = "Relay Hub Fargate service — accepts the container port from the ALB only."
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "relay-hub-service" })

  ingress {
    description     = "Container port from ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------------
# ECS cluster + log group + task definition.
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "relay" {
  name = "relay-hub"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = var.tags
}

resource "aws_cloudwatch_log_group" "relay" {
  name              = "/relay/hub"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_ecs_task_definition" "relay" {
  family                   = "relay-hub"
  cpu                      = var.cpu
  memory                   = var.memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn
  tags                     = var.tags

  container_definitions = jsonencode([
    {
      name      = "relay-hub"
      image     = var.hub_image_uri
      essential = true
      portMappings = [
        { containerPort = 8080, protocol = "tcp" }
      ]
      environment = [for k, v in local.container_env : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.relay.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "relay-hub"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])
}

# ---------------------------------------------------------------------------
# ALB + target group + listener(s). Internal by default; HTTPS when a cert is
# supplied (with an HTTP→HTTPS redirect), else HTTP:80.
# ---------------------------------------------------------------------------
resource "aws_lb" "relay" {
  name               = "relay-hub"
  internal           = var.internal_alb
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = local.alb_subnet_ids
  tags               = var.tags
}

resource "aws_lb_target_group" "relay" {
  name        = "relay-hub"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  tags        = var.tags

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# HTTPS listener (default when a cert is supplied) + HTTP→HTTPS redirect.
resource "aws_lb_listener" "https" {
  count             = var.certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.relay.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.relay.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  count             = var.certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.relay.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# Plain HTTP listener when no cert is supplied.
resource "aws_lb_listener" "http" {
  count             = var.certificate_arn != "" ? 0 : 1
  load_balancer_arn = aws_lb.relay.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.relay.arn
  }
}

# ---------------------------------------------------------------------------
# Fargate service. Always-on (floor of min_capacity). Zero-downtime deploys:
# minimum_healthy_percent=100 keeps the current task serving until the
# replacement is healthy; deployment circuit breaker rolls back a bad image.
# ---------------------------------------------------------------------------
resource "aws_ecs_service" "relay" {
  name            = "relay-hub"
  cluster         = aws_ecs_cluster.relay.id
  task_definition = aws_ecs_task_definition.relay.arn
  desired_count   = var.min_capacity
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 120

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.relay.arn
    container_name   = "relay-hub"
    container_port   = 8080
  }

  depends_on = [
    aws_lb_listener.https,
    aws_lb_listener.http,
    aws_lb_listener.http_redirect,
  ]

  tags = var.tags

  lifecycle {
    ignore_changes = [desired_count] # autoscaling owns the running count
  }
}

# ---------------------------------------------------------------------------
# CPU auto-scaling — floor of min_capacity, no scale-to-zero. Target 70% CPU.
# ---------------------------------------------------------------------------
resource "aws_appautoscaling_target" "relay" {
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "service/${aws_ecs_cluster.relay.name}/${aws_ecs_service.relay.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "relay-hub-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.relay.resource_id
  scalable_dimension = aws_appautoscaling_target.relay.scalable_dimension
  service_namespace  = aws_appautoscaling_target.relay.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70
    scale_in_cooldown  = 60
    scale_out_cooldown = 30
  }
}

# ---------------------------------------------------------------------------
# Poison-message visibility (issue #21): alarm on any message landing in the
# ingest DLQ and notify out-of-band via the team paging topic (NOT back through
# the ingest pipeline). Mirrors RelayHubIngestDLQDepthAlarm.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name = "relay-hub-ingest-dlq-not-empty"
  alarm_description = (
    "Relay ingest DLQ has poison messages — events failed to process 5x and were redriven. Investigate the un-parseable message(s)."
  )
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = element(split(":", var.ingest_dlq_arn), length(split(":", var.ingest_dlq_arn)) - 1) }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.paging_topic_arn]
  tags                = var.tags
}
