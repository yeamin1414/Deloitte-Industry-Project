data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  name    = "${var.project}-${var.environment}"
  compact = lower("${var.project}${var.environment}${random_string.suffix.result}")

  tags = {
    project     = var.project
    environment = var.environment
    data_class  = "health"
    cost_centre = "pathology-integration"
    managed_by  = "terraform"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# Policy as code: block resources outside Australia in this resource group.
resource "azurerm_resource_group_policy_assignment" "allowed_locations" {
  name                 = "kapconnect-allowed-locations"
  resource_group_id    = azurerm_resource_group.main.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/e56962a6-4747-49cd-b67b-bf8b01975c4c"
  description          = "Health data residency: Australian regions only"

  parameters = jsonencode({
    listOfAllowedLocations = { value = var.allowed_locations }
  })
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 90
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# Immutable provenance ledger (Blob Storage with WORM) --------------------------

resource "azurerm_storage_account" "ledger" {
  name                              = substr("st${local.compact}", 0, 24)
  resource_group_name               = azurerm_resource_group.main.name
  location                          = azurerm_resource_group.main.location
  account_tier                      = "Standard"
  account_kind                      = "StorageV2"
  account_replication_type          = "ZRS"
  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  allow_nested_items_to_be_public   = false
  shared_access_key_enabled         = false # Entra ID only, no account keys
  infrastructure_encryption_enabled = true

  blob_properties {
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  tags = local.tags
}

resource "azurerm_storage_container" "ledger" {
  name                  = "rpr-ledger"
  storage_account_id    = azurerm_storage_account.ledger.id
  container_access_type = "private"
}

resource "azurerm_storage_container_immutability_policy" "ledger" {
  storage_container_resource_manager_id = azurerm_storage_container.ledger.id
  immutability_period_in_days           = var.ledger_retention_days
  protected_append_writes_all_enabled   = false
  protected_append_writes_enabled       = false
  locked                                = var.lock_immutability_policy
}

# Messaging (Service Bus with native dead lettering) ----------------------------

resource "azurerm_servicebus_namespace" "main" {
  name                = "sb-${local.compact}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Standard"
  local_auth_enabled  = false # Entra ID only, no SAS keys
  minimum_tls_version = "1.2"
  tags                = local.tags
}

resource "azurerm_servicebus_queue" "results" {
  name                                    = "kapconnect-results"
  namespace_id                            = azurerm_servicebus_namespace.main.id
  max_delivery_count                      = 5
  lock_duration                           = "PT30S"
  default_message_ttl                     = "P14D"
  dead_lettering_on_message_expiration    = true
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "PT10M"
}

# Key Vault for envelope encryption --------------------------------------------

resource "azurerm_key_vault" "main" {
  name                       = substr("kv-${local.compact}", 0, 24)
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  enable_rbac_authorization  = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7
  tags                       = local.tags
}

resource "azurerm_role_assignment" "deployer_crypto_officer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_key_vault_key" "data" {
  name         = "kapconnect-data-wrap"
  key_vault_id = azurerm_key_vault.main.id
  key_type     = "RSA"
  key_size     = 3072
  key_opts     = ["wrapKey", "unwrapKey"]

  depends_on = [azurerm_role_assignment.deployer_crypto_officer]
}

# Least privilege data plane roles for the app identity -------------------------

resource "azurerm_role_assignment" "app_blob" {
  scope                = azurerm_storage_account.ledger.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_sb_send" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_sb_receive" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_crypto" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Container Apps --------------------------------------------------------------------

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.name}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.tags
}

resource "azurerm_container_app" "app" {
  name                         = "ca-${local.name}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  secret {
    name  = "jwt-secret"
    value = random_password.jwt.result
  }

  template {
    # One replica while the operational store is SQLite. Move to Azure Database for PostgreSQL to scale out.
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "kapconnect"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "KAP_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "KAP_CLOUD_PROVIDERS"
        value = var.cloud_providers
      }
      env {
        name  = "KAP_AZURE_REGION"
        value = var.location
      }
      env {
        name  = "KAP_AZURE_STORAGE_ACCOUNT_URL"
        value = azurerm_storage_account.ledger.primary_blob_endpoint
      }
      env {
        name  = "KAP_AZURE_LEDGER_CONTAINER"
        value = azurerm_storage_container.ledger.name
      }
      env {
        name  = "KAP_AZURE_MESSAGING"
        value = "servicebus"
      }
      env {
        name  = "KAP_AZURE_SERVICEBUS_NAMESPACE"
        value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
      }
      env {
        name  = "KAP_AZURE_KEYVAULT_KEY_ID"
        value = azurerm_key_vault_key.data.id
      }
      env {
        name  = "KAP_LEDGER_RETENTION_DAYS"
        value = tostring(var.ledger_retention_days)
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name        = "KAP_JWT_SECRET"
        secret_name = "jwt-secret"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }

      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/readyz"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  depends_on = [
    azurerm_role_assignment.app_blob,
    azurerm_role_assignment.app_sb_send,
    azurerm_role_assignment.app_sb_receive,
    azurerm_role_assignment.app_crypto,
  ]
}
