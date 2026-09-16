"""Provider neutral cloud interfaces.

Every cloud (AWS, Azure, local) implements the same three capabilities so the
platform never depends on a single vendor SDK. This is the seam that makes
failover, dual writes and cost aware routing possible.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot serve a request (outage, throttling, auth)."""


@dataclass
class QueueMessage:
    message_id: str
    body: str
    attempts: int = 1
    attributes: dict[str, str] = field(default_factory=dict)


MessageHandler = Callable[[QueueMessage], None]


class ObjectStore(ABC):
    """Write once read many (WORM) object storage."""

    @abstractmethod
    def put_immutable(self, key: str, data: bytes, retain_until: datetime) -> str:
        """Store an object that cannot be altered or deleted before retain_until.

        Returns a provider specific location string.
        """

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class MessageQueue(ABC):
    """At least once message delivery with dead letter support."""

    @abstractmethod
    def publish(self, topic: str, body: str, message_id: str) -> None: ...

    @abstractmethod
    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        """Receive up to max_messages and run handler on each.

        A message is acknowledged only when the handler returns without raising.
        Failed messages are retried and eventually dead lettered by the provider.
        Returns the number of messages successfully processed.
        """

    @abstractmethod
    def dead_letters(self, topic: str, limit: int = 20) -> list[QueueMessage]: ...


class KeyProvider(ABC):
    """Envelope encryption: a cloud KMS wraps and unwraps data encryption keys."""

    def generate_data_key(self) -> tuple[bytes, bytes]:
        """Return (plaintext_key, wrapped_key)."""
        dek = os.urandom(32)
        return dek, self.wrap(dek)

    @abstractmethod
    def wrap(self, plaintext_key: bytes) -> bytes: ...

    @abstractmethod
    def unwrap(self, wrapped_key: bytes) -> bytes: ...


class CloudProvider(ABC):
    """A bundle of capabilities offered by one cloud."""

    name: str
    kind: str  # aws, azure or local. Drives pricing and display.
    simulated: bool = False
    region: str
    objects: ObjectStore
    queue: MessageQueue
    keys: KeyProvider

    @abstractmethod
    def health_check(self) -> None:
        """Raise ProviderUnavailable when the provider is not usable."""
