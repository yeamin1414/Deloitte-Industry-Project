output "app_url" {
  description = "Public URL of the KapConnect console."
  value       = "https://${azurerm_container_app.app.ingress[0].fqdn}"
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "ledger_blob_endpoint" {
  value = azurerm_storage_account.ledger.primary_blob_endpoint
}

output "servicebus_namespace" {
  value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
}

output "keyvault_key_id" {
  value = azurerm_key_vault_key.data.id
}

output "app_identity_client_id" {
  value = azurerm_user_assigned_identity.app.client_id
}
