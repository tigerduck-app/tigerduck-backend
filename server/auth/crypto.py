"""AES-256-GCM envelope encryption for external account credentials.

The DB stores ciphertext only (`external_account_credentials`); the AES
keys live in `settings.credential_keys` (env var / KMS-injected). Multiple
keys are kept so rotation works without a stop-the-world re-encrypt: new
writes use `active_key_id`, old rows decrypt under whatever `key_id` they
were written with.

The AAD binds a blob to its owning row (`build_credential_aad`), so a
ciphertext copied onto another account row fails authentication.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_KEY_BYTES = 32

ENCRYPTION_ALGORITHM = "AES-256-GCM"


class CredentialCipherError(Exception):
    """Raised on any encryption/decryption failure (bad key, tampering)."""


@dataclass(frozen=True)
class EncryptedBlob:
    key_id: str
    nonce: bytes
    ciphertext: bytes  # GCM tag appended by AESGCM
    aad: str


def build_credential_aad(external_account_id: int, provider: str) -> str:
    return f"external_account:{external_account_id}:provider:{provider}"


class CredentialCipher:
    def __init__(self, keys: dict[str, str], active_key_id: str) -> None:
        self._keys: dict[str, bytes] = {}
        for key_id, encoded in keys.items():
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise CredentialCipherError(
                    f"credential key {key_id!r} is not valid base64"
                ) from exc
            if len(raw) != _KEY_BYTES:
                raise CredentialCipherError(
                    f"credential key {key_id!r} must be {_KEY_BYTES} bytes, "
                    f"got {len(raw)}"
                )
            self._keys[key_id] = raw
        if active_key_id not in self._keys:
            raise CredentialCipherError(
                f"active key id {active_key_id!r} not present in credential keys"
            )
        self._active_key_id = active_key_id

    @classmethod
    def from_settings(cls, settings) -> "CredentialCipher":
        return cls(
            keys=settings.credential_keys,
            active_key_id=settings.credential_active_key_id,
        )

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def encrypt(self, payload: dict, aad: str) -> EncryptedBlob:
        nonce = os.urandom(_NONCE_BYTES)
        data = json.dumps(payload, separators=(",", ":")).encode()
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce, data, aad.encode()
        )
        return EncryptedBlob(
            key_id=self._active_key_id, nonce=nonce, ciphertext=ciphertext, aad=aad
        )

    def decrypt(self, blob: EncryptedBlob) -> dict:
        key = self._keys.get(blob.key_id)
        if key is None:
            raise CredentialCipherError(
                f"no credential key for key id {blob.key_id!r}"
            )
        try:
            data = AESGCM(key).decrypt(blob.nonce, blob.ciphertext, blob.aad.encode())
        except InvalidTag as exc:
            raise CredentialCipherError(
                "credential blob failed authentication (tampered or wrong AAD)"
            ) from exc
        return json.loads(data)
