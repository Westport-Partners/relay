variable "org_id" {
  description = "AWS Organizations id (o-xxxxxxxxxx). When set, the bus accepts PutEvents from any account in the org; when empty, same-account only."
  type        = string
  default     = ""
}

variable "ingest_queue_arn" {
  description = "Optional: route all Relay events on the bus (source prefix \"relay.\") to this SQS queue — the federated hub's data-plane ingest queue."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
