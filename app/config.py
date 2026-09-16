"""Typed application settings loaded from environment variables or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KAP_", extra="ignore")

    # General
    app_name: str = "KapConnect"
    environment: str = "local"
    data_dir: Path = ROOT_DIR / "data"
    database_file: str = "kapconnect.db"

    # Cloud routing
    # Comma separated provider list in priority order.
    # aws and azure use the real SDKs (real cloud, LocalStack or Azurite).
    # aws_sim and azure_sim run offline with the same semantics for zero setup demos.
    # local is a single on premises provider.
    cloud_providers: str = "aws_sim,azure_sim"
    cloud_bootstrap: bool = False  # create buckets, queues and keys on start (emulators only)
    routing_strategy: str = Field(default="priority", pattern="^(priority|cost)$")
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: int = 20
    ledger_retention_days: int = 2555  # roughly seven years

    # AWS
    aws_region: str = "ap-southeast-2"
    aws_endpoint_url: str | None = None  # set to LocalStack URL for local emulation
    aws_ledger_bucket: str = "kapconnect-rpr-ledger"
    aws_queue_prefix: str = "kapconnect"
    aws_kms_key_id: str | None = None
    aws_object_lock: bool = True

    # Azure
    azure_region: str = "australiaeast"
    azure_storage_connection_string: str | None = None
    azure_storage_account_url: str | None = None  # used with managed identity
    azure_ledger_container: str = "rpr-ledger"
    azure_messaging: str = Field(default="storagequeue", pattern="^(storagequeue|servicebus)$")
    azure_servicebus_connection_string: str | None = None
    azure_servicebus_namespace: str | None = None  # fqdn, used with managed identity
    azure_keyvault_key_id: str | None = None

    # Security
    jwt_secret: str = "change-me-in-production-please-32-bytes-min"
    jwt_ttl_minutes: int = 120
    local_master_key: str = "local-development-master-key-change-me"

    # Clinical behaviour
    escalation_seconds: int = 900  # fifteen minutes
    consumer_poll_seconds: float = 1.0
    max_delivery_attempts: int = 5
    enable_background_workers: bool = True

    # FinOps
    pricing_file: Path = ROOT_DIR / "config" / "pricing.json"
    monthly_budget_aud: float = 500.0
    projected_monthly_tests: int = 1_500_000  # planned network volume used for cost projection

    @property
    def provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.cloud_providers.split(",") if p.strip()]

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_file


@lru_cache
def get_settings() -> Settings:
    return Settings()
