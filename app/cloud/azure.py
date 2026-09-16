"""Azure provider.

Services used
* Azure Blob Storage with a container level time based retention (WORM) policy for the provenance ledger
* Azure Service Bus queues (production) or Azure Storage Queues (Azurite emulation) for result events
* Azure Key Vault keys for envelope encryption, authenticated with Managed Identity
"""

from __future__ import annotations

from datetime import datetime

from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueServiceClient

from app.cloud.base import (
    CloudProvider,
    KeyProvider,
    MessageHandler,
    MessageQueue,
    ObjectStore,
    ProviderUnavailable,
    QueueMessage,
)

FAST_FAIL = {"retry_total": 1, "connection_timeout": 2, "read_timeout": 5}


def _credential():
    from azure.identity import DefaultAzureCredential

    # Managed Identity in Azure Container Apps, developer login locally. No secrets in code.
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class BlobWormStore(ObjectStore):
    def __init__(self, container: str, connection_string: str | None, account_url: str | None):
        if connection_string:
            self.service = BlobServiceClient.from_connection_string(connection_string, **FAST_FAIL)
        elif account_url:
            self.service = BlobServiceClient(account_url, credential=_credential(), **FAST_FAIL)
        else:
            raise ValueError("Azure blob storage needs a connection string or account url")
        self.container = self.service.get_container_client(container)

    def ensure_container(self) -> None:
        try:
            self.container.create_container()
        except ResourceExistsError:
            pass

    def put_immutable(self, key: str, data: bytes, retain_until: datetime) -> str:
        blob = self.container.get_blob_client(key)
        try:
            blob.upload_blob(
                data,
                overwrite=False,  # never replace an existing ledger entry
                content_settings=ContentSettings(content_type="application/json"),
                metadata={"retain_until": retain_until.isoformat()},
            )
        except ResourceExistsError as exc:
            if blob.download_blob().readall() == data:
                return blob.url
            raise PermissionError(f"Object {key} is immutable and already exists") from exc
        except AzureError as exc:
            raise ProviderUnavailable(f"azure blob put failed: {exc}") from exc
        return blob.url

    def get(self, key: str) -> bytes:
        try:
            return self.container.get_blob_client(key).download_blob().readall()
        except AzureError as exc:
            raise ProviderUnavailable(f"azure blob get failed: {exc}") from exc

    def exists(self, key: str) -> bool:
        try:
            return self.container.get_blob_client(key).exists()
        except AzureError as exc:
            raise ProviderUnavailable(f"azure blob exists failed: {exc}") from exc


class StorageQueue(MessageQueue):
    """Azure Storage Queues with a poison queue, the same pattern Azure Functions uses."""

    def __init__(self, connection_string: str | None, account_url: str | None, prefix: str, max_attempts: int = 5):
        if connection_string:
            self.service = QueueServiceClient.from_connection_string(connection_string, **FAST_FAIL)
        elif account_url:
            self.service = QueueServiceClient(account_url, credential=_credential(), **FAST_FAIL)
        else:
            raise ValueError("Azure storage queue needs a connection string or account url")
        self.prefix = prefix
        self.max_attempts = max_attempts
        self._ready: set[str] = set()

    def _client(self, name: str):
        client = self.service.get_queue_client(name)
        if name not in self._ready:
            try:
                client.create_queue()
            except ResourceExistsError:
                pass
            self._ready.add(name)
        return client

    def publish(self, topic: str, body: str, message_id: str) -> None:
        try:
            self._client(f"{self.prefix}-{topic}").send_message(body)
        except AzureError as exc:
            raise ProviderUnavailable(f"azure queue publish failed: {exc}") from exc

    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        try:
            client = self._client(f"{self.prefix}-{topic}")
            messages = list(client.receive_messages(max_messages=max_messages, visibility_timeout=30))
        except AzureError as exc:
            raise ProviderUnavailable(f"azure queue receive failed: {exc}") from exc

        processed = 0
        for raw in messages:
            msg = QueueMessage(message_id=raw.id, body=raw.content, attempts=raw.dequeue_count or 1)
            try:
                handler(msg)
                client.delete_message(raw)
                processed += 1
            except Exception:  # noqa: BLE001
                if msg.attempts >= self.max_attempts:
                    self._client(f"{self.prefix}-{topic}-poison").send_message(raw.content)
                    client.delete_message(raw)
                else:
                    client.update_message(raw, visibility_timeout=min(2 ** (msg.attempts - 1), 60))
        return processed

    def dead_letters(self, topic: str, limit: int = 20) -> list[QueueMessage]:
        try:
            client = self._client(f"{self.prefix}-{topic}-poison")
            peeked = client.peek_messages(max_messages=min(limit, 32))
            return [QueueMessage(message_id=m.id, body=m.content) for m in peeked]
        except AzureError:
            return []


class ServiceBusQueue(MessageQueue):
    """Azure Service Bus queues. Dead lettering is native after MaxDeliveryCount."""

    def __init__(self, connection_string: str | None, namespace: str | None, prefix: str):
        from azure.servicebus import ServiceBusClient

        if connection_string:
            self.client = ServiceBusClient.from_connection_string(connection_string)
        elif namespace:
            self.client = ServiceBusClient(namespace, credential=_credential())
        else:
            raise ValueError("Service Bus needs a connection string or namespace")
        self.prefix = prefix

    def publish(self, topic: str, body: str, message_id: str) -> None:
        from azure.servicebus import ServiceBusMessage

        try:
            with self.client.get_queue_sender(f"{self.prefix}-{topic}") as sender:
                sender.send_messages(ServiceBusMessage(body, message_id=message_id))
        except AzureError as exc:
            raise ProviderUnavailable(f"azure service bus publish failed: {exc}") from exc

    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        processed = 0
        try:
            with self.client.get_queue_receiver(f"{self.prefix}-{topic}", max_wait_time=1) as receiver:
                for raw in receiver.receive_messages(max_message_count=max_messages, max_wait_time=1):
                    msg = QueueMessage(message_id=raw.message_id or "", body=str(raw), attempts=raw.delivery_count or 1)
                    try:
                        handler(msg)
                        receiver.complete_message(raw)
                        processed += 1
                    except Exception:  # noqa: BLE001
                        receiver.abandon_message(raw)
        except AzureError as exc:
            raise ProviderUnavailable(f"azure service bus receive failed: {exc}") from exc
        return processed

    def dead_letters(self, topic: str, limit: int = 20) -> list[QueueMessage]:
        from azure.servicebus import ServiceBusSubQueue

        try:
            with self.client.get_queue_receiver(
                f"{self.prefix}-{topic}", sub_queue=ServiceBusSubQueue.DEAD_LETTER
            ) as receiver:
                return [
                    QueueMessage(message_id=m.message_id or "", body=str(m))
                    for m in receiver.peek_messages(max_message_count=limit)
                ]
        except AzureError:
            return []


class KeyVaultKeyProvider(KeyProvider):
    def __init__(self, key_id: str):
        from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm

        self._alg = KeyWrapAlgorithm.rsa_oaep_256
        self.client = CryptographyClient(key_id, credential=_credential())

    def wrap(self, plaintext_key: bytes) -> bytes:
        try:
            return self.client.wrap_key(self._alg, plaintext_key).encrypted_key
        except AzureError as exc:
            raise ProviderUnavailable(f"azure key vault wrap failed: {exc}") from exc

    def unwrap(self, wrapped_key: bytes) -> bytes:
        try:
            return self.client.unwrap_key(self._alg, wrapped_key).key
        except AzureError as exc:
            raise ProviderUnavailable(f"azure key vault unwrap failed: {exc}") from exc


class AzureProvider(CloudProvider):
    def __init__(
        self,
        *,
        region: str,
        container: str,
        storage_connection_string: str | None,
        storage_account_url: str | None,
        messaging: str,
        servicebus_connection_string: str | None,
        servicebus_namespace: str | None,
        keyvault_key_id: str | None,
        queue_prefix: str = "kapconnect",
        max_attempts: int = 5,
        fallback_keys: KeyProvider | None = None,
        bootstrap: bool = False,
    ):
        self.name = "azure"
        self.kind = "azure"
        self.region = region
        self.objects = BlobWormStore(container, storage_connection_string, storage_account_url)
        if messaging == "servicebus":
            self.queue = ServiceBusQueue(servicebus_connection_string, servicebus_namespace, queue_prefix)
        else:
            queue_url = storage_account_url.replace(".blob.", ".queue.") if storage_account_url else None
            self.queue = StorageQueue(storage_connection_string, queue_url, queue_prefix, max_attempts)
        if keyvault_key_id:
            self.keys = KeyVaultKeyProvider(keyvault_key_id)
        elif fallback_keys is not None:
            self.keys = fallback_keys
        else:
            raise ValueError("Azure provider needs KAP_AZURE_KEYVAULT_KEY_ID or a fallback key provider")
        if bootstrap:
            self.objects.ensure_container()

    def health_check(self) -> None:
        try:
            self.objects.container.get_container_properties()
        except (AzureError, ResourceNotFoundError) as exc:
            raise ProviderUnavailable(f"azure unhealthy: {exc}") from exc
