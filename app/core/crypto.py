"""Envelope encryption for patient identifiers.

One data encryption key (DEK) protects patient demographics with AES 256 GCM.
The DEK is wrapped by every configured cloud KMS (AWS KMS and Azure Key Vault),
so patient data stays readable if one provider's key service is unavailable.
Searchable fields use an HMAC blind index so lookups never need plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.cloud.base import CloudProvider, ProviderUnavailable
from app.storage.db import Database, utcnow

log = logging.getLogger(__name__)


class KeyUnavailable(RuntimeError):
    pass


class FieldCipher:
    def __init__(self, db: Database, providers: list[CloudProvider]):
        self.db = db
        self.providers = providers
        self._dek = self._load_or_create()
        self._index_key = hmac.new(self._dek, b"blind-index", hashlib.sha256).digest()

    def _load_or_create(self) -> bytes:
        rows = {r["provider"]: r["wrapped_key"] for r in self.db.all("SELECT * FROM keyring")}
        dek: bytes | None = None
        for provider in self.providers:
            wrapped = rows.get(provider.name)
            if wrapped is None:
                continue
            try:
                dek = provider.keys.unwrap(bytes(wrapped))
                log.info("data key unwrapped with %s", provider.name)
                break
            except (ProviderUnavailable, Exception) as exc:  # noqa: BLE001
                log.warning("could not unwrap data key with %s: %s", provider.name, exc)
        if dek is None and rows:
            raise KeyUnavailable("No configured key service could unwrap the existing data key")

        if dek is None:
            dek, wrapped = self.providers[0].keys.generate_data_key()
            self._store(self.providers[0].name, wrapped)

        # Escrow: make sure every provider holds a wrapped copy.
        for provider in self.providers:
            if provider.name not in rows and not self._has(provider.name):
                try:
                    self._store(provider.name, provider.keys.wrap(dek))
                except ProviderUnavailable as exc:
                    log.warning("key escrow to %s deferred: %s", provider.name, exc)
        return dek

    def _has(self, name: str) -> bool:
        return self.db.one("SELECT 1 FROM keyring WHERE provider = ?", (name,)) is not None

    def _store(self, name: str, wrapped: bytes) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO keyring (provider, wrapped_key, created_at) VALUES (?, ?, ?)",
            (name, wrapped, utcnow()),
        )

    def escrow_status(self) -> dict[str, bool]:
        return {p.name: self._has(p.name) for p in self.providers}

    def encrypt_json(self, payload: dict) -> bytes:
        nonce = os.urandom(12)
        data = json.dumps(payload, sort_keys=True).encode()
        return nonce + AESGCM(self._dek).encrypt(nonce, data, b"patient")

    def decrypt_json(self, blob: bytes) -> dict:
        blob = bytes(blob)
        return json.loads(AESGCM(self._dek).decrypt(blob[:12], blob[12:], b"patient"))

    def blind_index(self, value: str) -> str:
        return hmac.new(self._index_key, value.lower().encode(), hashlib.sha256).hexdigest()[:32]
