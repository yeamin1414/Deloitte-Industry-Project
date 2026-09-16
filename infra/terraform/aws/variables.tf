variable "project" {
  description = "Project name used in resource names and cost allocation tags."
  type        = string
  default     = "kapconnect"
}

variable "environment" {
  description = "Deployment environment, for example dev, test or prod."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "AWS region. Restricted to Australian regions for health data residency."
  type        = string
  default     = "ap-southeast-2"

  validation {
    condition     = contains(["ap-southeast-2", "ap-southeast-4"], var.region)
    error_message = "Patient data must stay in Australia: use ap-southeast-2 (Sydney) or ap-southeast-4 (Melbourne)."
  }
}

variable "ledger_retention_days" {
  description = "S3 Object Lock COMPLIANCE retention for provenance records."
  type        = number
  default     = 2555

  validation {
    condition     = var.ledger_retention_days >= 1
    error_message = "Retention must be at least one day."
  }
}

variable "image_tag" {
  description = "Container image tag in the ECR repository. Tags are immutable."
  type        = string
  default     = "1.0.0"
}

variable "desired_count" {
  description = "ECS task count. Keep at 1 while the operational store is SQLite; scale out after moving to Aurora PostgreSQL."
  type        = number
  default     = 1
}

variable "cloud_providers" {
  description = "Value for KAP_CLOUD_PROVIDERS inside the container."
  type        = string
  default     = "aws"
}

variable "max_receive_count" {
  description = "Deliveries before SQS moves a message to the dead letter queue."
  type        = number
  default     = 5
}

variable "allowed_ingress_cidrs" {
  description = "CIDR ranges allowed to reach the load balancer. Narrow this to hospital networks."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "Optional ACM certificate ARN. When set, HTTPS is enabled and HTTP redirects to it."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly AWS Budgets limit for resources tagged with this project."
  type        = number
  default     = 350
}

variable "alert_email" {
  description = "Email for budget and operational alarms. Leave empty to skip notifications."
  type        = string
  default     = ""
}
