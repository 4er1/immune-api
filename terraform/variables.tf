variable "project_name" {
  description = "Prefix for every resource this stack creates."
  type        = string
  default     = "immune-api"
}

variable "environment" {
  description = "Deployment environment, e.g. dev / staging / prod. Used in resource names and tags."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "lambda_zip_path" {
  description = "Path to the pre-built application zip (code + model.json). Built by scripts/build_lambda.sh."
  type        = string
  default     = "../build/app.zip"
}

variable "lambda_memory_mb" {
  description = "Memory for the API Lambda. Also used by the guard to compute the GB-seconds cost feature."
  type        = number
  default     = 256
}

variable "lambda_timeout_seconds" {
  type    = number
  default = 10
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "throttle_burst_limit" {
  description = "API Gateway account-level burst limit - a hard backstop above the immune guard."
  type        = number
  default     = 200
}

variable "throttle_rate_limit" {
  description = "API Gateway steady-state rate limit (req/s) - a hard backstop above the immune guard."
  type        = number
  default     = 100
}

variable "guard_mode" {
  description = "'enforce' blocks anomalous sources; 'monitor' only logs what it would have blocked."
  type        = string
  default     = "enforce"
  validation {
    condition     = contains(["enforce", "monitor"], var.guard_mode)
    error_message = "guard_mode must be 'enforce' or 'monitor'."
  }
}

variable "guard_min_events" {
  type    = number
  default = 20
}

variable "guard_confirm_seconds" {
  type    = number
  default = 5
}

variable "guard_confirm_evals" {
  type    = number
  default = 5
}

variable "guard_block_base_seconds" {
  type    = number
  default = 60
}

variable "guard_block_max_seconds" {
  type    = number
  default = 3600
}

variable "alert_email" {
  description = "Optional email address subscribed to the block-alert SNS topic. Leave blank to skip the subscription (someone must then confirm it manually, or you wire the topic elsewhere)."
  type        = string
  default     = ""
}

variable "dynamodb_billing_mode" {
  type    = string
  default = "PAY_PER_REQUEST"
}

variable "tags" {
  type    = map(string)
  default = {}
}
