variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "project" {
  description = "Project name used in resource names and tags."
  type        = string
  default     = "kapconnect"
}

variable "environment" {
  description = "Deployment environment, for example dev, test or prod."
  type        = string
  default     = "dev"
}

variable "location" {
  description = "Azure region. Restricted to Australian regions for health data residency."
  type        = string
  default     = "australiaeast"

  validation {
    condition     = contains(["australiaeast", "australiasoutheast", "australiacentral"], var.location)
    error_message = "Patient data must stay in Australia: use australiaeast, australiasoutheast or australiacentral."
  }
}

variable "allowed_locations" {
  description = "Regions allowed by the Azure Policy assignment on the resource group."
  type        = list(string)
  default     = ["australiaeast", "australiasoutheast"]
}

variable "ledger_retention_days" {
  description = "Time based retention (WORM) on the provenance ledger container."
  type        = number
  default     = 2555
}

variable "lock_immutability_policy" {
  description = "Lock the WORM policy. A locked policy cannot be shortened or removed, so enable only for production."
  type        = bool
  default     = false
}

variable "container_image" {
  description = "Public container image for KapConnect."
  type        = string
  default     = "ghcr.io/yeamin1414/kapconnect:1.0.0"
}

variable "cloud_providers" {
  description = "Value for KAP_CLOUD_PROVIDERS inside the container."
  type        = string
  default     = "azure"
}

variable "monthly_budget" {
  description = "Monthly budget amount in the subscription billing currency."
  type        = number
  default     = 500
}

variable "alert_email" {
  description = "Email for budget and operational alerts. Leave empty to skip notifications."
  type        = string
  default     = ""
}
