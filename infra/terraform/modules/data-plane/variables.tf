variable "role" {
  description = "Relay topology role: \"team\" (default) or \"federated-hub\". Selects the table name (relay-<team> vs relay-hub-fleet) and the central paging topic name."
  type        = string
  default     = "team"
  validation {
    condition     = contains(["team", "federated-hub"], var.role)
    error_message = "role must be \"team\" or \"federated-hub\"."
  }
}

variable "team_name" {
  description = "Team identifier used in resource names (relay-<team>). Required for the team role."
  type        = string
  default     = "unnamed-team"
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
