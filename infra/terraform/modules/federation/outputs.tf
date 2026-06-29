output "event_bus_arn" {
  description = "Relay federated-hub EventBridge bus ARN — hand to team deploys as central_hub_bus_arn."
  value       = aws_cloudwatch_event_bus.hub.arn
}

output "event_bus_name" {
  description = "Relay federated-hub EventBridge bus name."
  value       = aws_cloudwatch_event_bus.hub.name
}
