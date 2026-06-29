# Outputs mirror the env vars relay-provision-cli.sh prints — the compute module
# consumes these, and they double as the export list for a local-on-EC2 run.

output "table_name" {
  description = "Relay data-plane DynamoDB table name (RELAY_TABLE_NAME / RELAY_FLEET_TABLE_NAME)."
  value       = aws_dynamodb_table.relay.name
}

output "table_arn" {
  description = "Relay data-plane DynamoDB table ARN."
  value       = aws_dynamodb_table.relay.arn
}

output "table_stream_arn" {
  description = "DynamoDB stream ARN (NEW_AND_OLD_IMAGES) feeding the live dashboard push."
  value       = aws_dynamodb_table.relay.stream_arn
}

output "paging_topic_arn" {
  description = "Team paging SNS topic ARN (RELAY_PAGING_TOPIC_ARN / RELAY_SNS_TOPIC_ARN)."
  value       = aws_sns_topic.paging.arn
}

output "central_paging_topic_arn" {
  description = "Central team paging SNS topic ARN (RELAY_CENTRAL_PAGING_TOPIC_ARN)."
  value       = aws_sns_topic.central_paging.arn
}

output "ingest_queue_url" {
  description = "Relay ingest SQS queue URL (RELAY_SQS_QUEUE_URL)."
  value       = aws_sqs_queue.ingest.id
}

output "ingest_queue_arn" {
  description = "Relay ingest SQS queue ARN."
  value       = aws_sqs_queue.ingest.arn
}

output "ingest_dlq_arn" {
  description = "Relay ingest DLQ ARN (the compute DLQ-depth alarm watches this)."
  value       = aws_sqs_queue.ingest_dlq.arn
}
