resource "azurerm_monitor_diagnostic_setting" "servicebus" {
  name                       = "diag-servicebus"
  target_resource_id         = azurerm_servicebus_namespace.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category_group = "allLogs"
  }
}

resource "azurerm_monitor_action_group" "ops" {
  count               = var.alert_email == "" ? 0 : 1
  name                = "ag-${local.name}"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "kapops"

  email_receiver {
    name          = "operations"
    email_address = var.alert_email
  }
}

resource "azurerm_monitor_metric_alert" "dead_letters" {
  name                = "alert-${local.name}-dead-letters"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [azurerm_servicebus_namespace.main.id]
  description         = "Result events failed delivery and reached the dead letter queue"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.ServiceBus/namespaces"
    metric_name      = "DeadletteredMessages"
    aggregation      = "Maximum"
    operator         = "GreaterThan"
    threshold        = 0
  }

  dynamic "action" {
    for_each = azurerm_monitor_action_group.ops
    content {
      action_group_id = action.value.id
    }
  }
}

resource "azurerm_consumption_budget_resource_group" "monthly" {
  count             = var.alert_email == "" ? 0 : 1
  name              = "budget-${local.name}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.monthly_budget
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.alert_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}
