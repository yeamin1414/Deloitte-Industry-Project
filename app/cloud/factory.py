"""Builds cloud providers from configuration."""

from __future__ import annotations

import logging

from app.cloud.base import CloudProvider
from app.cloud.local import LocalKeyProvider, LocalProvider
from app.config import Settings

log = logging.getLogger(__name__)


def _aws(settings: Settings) -> CloudProvider:
    from app.cloud.aws import AWSProvider, _client

    kms_key_id = settings.aws_kms_key_id
    if settings.cloud_bootstrap and not kms_key_id:
        # Emulators only: create a customer managed key so the full KMS path is exercised.
        kms = _client("kms", settings.aws_region, settings.aws_endpoint_url)
        kms_key_id = kms.create_key(Description="KapConnect local data key wrapper")["KeyMetadata"]["KeyId"]
        log.info("bootstrapped KMS key %s", kms_key_id)
    return AWSProvider(
        region=settings.aws_region,
        bucket=settings.aws_ledger_bucket,
        queue_prefix=settings.aws_queue_prefix,
        kms_key_id=kms_key_id,
        endpoint_url=settings.aws_endpoint_url,
        object_lock=settings.aws_object_lock,
        max_attempts=settings.max_delivery_attempts,
        bootstrap=settings.cloud_bootstrap,
    )


def _azure(settings: Settings) -> CloudProvider:
    from app.cloud.azure import AzureProvider

    fallback = None
    if not settings.azure_keyvault_key_id:
        # Key Vault has no local emulator. Fall back to a local key wrapper for emulated runs.
        log.warning("KAP_AZURE_KEYVAULT_KEY_ID not set, using local key wrapping for azure")
        fallback = LocalKeyProvider(settings.local_master_key + ":azure")
    return AzureProvider(
        region=settings.azure_region,
        container=settings.azure_ledger_container,
        storage_connection_string=settings.azure_storage_connection_string,
        storage_account_url=settings.azure_storage_account_url,
        messaging=settings.azure_messaging,
        servicebus_connection_string=settings.azure_servicebus_connection_string,
        servicebus_namespace=settings.azure_servicebus_namespace,
        keyvault_key_id=settings.azure_keyvault_key_id,
        max_attempts=settings.max_delivery_attempts,
        fallback_keys=fallback,
        bootstrap=settings.cloud_bootstrap,
    )


def build_providers(settings: Settings) -> list[CloudProvider]:
    providers: list[CloudProvider] = []
    root = settings.data_dir / "providers"
    for token in settings.provider_list:
        if token == "aws":
            providers.append(_aws(settings))
        elif token == "azure":
            providers.append(_azure(settings))
        elif token == "aws_sim":
            providers.append(
                LocalProvider(
                    root,
                    settings.local_master_key + ":aws",
                    settings.max_delivery_attempts,
                    name="aws",
                    kind="aws",
                    region=settings.aws_region,
                )
            )
        elif token == "azure_sim":
            providers.append(
                LocalProvider(
                    root,
                    settings.local_master_key + ":azure",
                    settings.max_delivery_attempts,
                    name="azure",
                    kind="azure",
                    region=settings.azure_region,
                )
            )
        elif token == "local":
            providers.append(LocalProvider(root, settings.local_master_key, settings.max_delivery_attempts))
        else:
            raise ValueError(f"Unknown cloud provider '{token}'")
    names = [p.name for p in providers]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate provider names in KAP_CLOUD_PROVIDERS: {names}")
    return providers
