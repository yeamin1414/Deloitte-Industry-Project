"""Local provider: runs the full platform on a laptop with no cloud account.

It mirrors cloud semantics closely enough for realistic demos:
objects are write once, queues are at least once with dead lettering,
and keys are wrapped with a locally derived master key.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.cloud.base import (
    CloudProvider,
    KeyProvider,
    MessageHandler,
    MessageQueue,
    ObjectStore,
    QueueMessage,
)


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("..", "_")
        return self.root / safe

    def put_immutable(self, key: str, data: bytes, retain_until: datetime) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() == data:
                return f"file://{path}"  # idempotent retry of the same write
            raise PermissionError(f"Object {key} is immutable and already exists")
        path.write_bytes(data)
        meta = {"retain_until": retain_until.isoformat()}
        path.with_suffix(path.suffix + ".lock.json").write_text(json.dumps(meta))
        os.chmod(path, 0o444)
        return f"file://{path}"

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class LocalQueue(MessageQueue):
    def __init__(self, max_attempts: int = 5):
        self.max_attempts = max_attempts
        self._queues: dict[str, deque[QueueMessage]] = defaultdict(deque)
        self._dlq: dict[str, list[QueueMessage]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, topic: str, body: str, message_id: str) -> None:
        with self._lock:
            self._queues[topic].append(QueueMessage(message_id=message_id, body=body, attempts=0))

    def consume(self, topic: str, handler: MessageHandler, max_messages: int = 10) -> int:
        processed = 0
        for _ in range(max_messages):
            with self._lock:
                if not self._queues[topic]:
                    break
                msg = self._queues[topic].popleft()
            msg.attempts += 1
            try:
                handler(msg)
                processed += 1
            except Exception:  # noqa: BLE001 retry semantics mirror SQS and Service Bus
                with self._lock:
                    if msg.attempts >= self.max_attempts:
                        self._dlq[topic].append(msg)
                    else:
                        self._queues[topic].append(msg)
        return processed

    def dead_letters(self, topic: str, limit: int = 20) -> list[QueueMessage]:
        with self._lock:
            return list(self._dlq[topic][:limit])

    def depth(self, topic: str) -> int:
        with self._lock:
            return len(self._queues[topic])


class LocalKeyProvider(KeyProvider):
    def __init__(self, master_secret: str):
        self._kek = hashlib.sha256(master_secret.encode()).digest()

    def wrap(self, plaintext_key: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._kek).encrypt(nonce, plaintext_key, b"kapconnect-dek")

    def unwrap(self, wrapped_key: bytes) -> bytes:
        nonce, ciphertext = wrapped_key[:12], wrapped_key[12:]
        return AESGCM(self._kek).decrypt(nonce, ciphertext, b"kapconnect-dek")


class LocalProvider(CloudProvider):
    def __init__(
        self,
        root: Path,
        master_secret: str,
        max_attempts: int = 5,
        name: str = "local",
        kind: str = "local",
        region: str = "hospital edge",
    ):
        """A local provider. With kind aws or azure it simulates that cloud for offline demos."""
        self.name = name
        self.kind = kind
        self.simulated = kind != "local"
        self.region = region
        self.objects = LocalObjectStore(root / name / "objects")
        self.queue = LocalQueue(max_attempts=max_attempts)
        self.keys = LocalKeyProvider(master_secret)

    def health_check(self) -> None:
        if not self.objects.root.exists():
            self.objects.root.mkdir(parents=True, exist_ok=True)
