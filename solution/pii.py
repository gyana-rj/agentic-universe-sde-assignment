"""PII protection at rest.

Two different needs, two different primitives, both derived from one 32-byte
master key that lives *outside* the database:

* **Matching** events to leads needs a deterministic value: we store
  ``HMAC-SHA256(k_index, canonical_phone)``. A plain SHA-256 would be useless
  protection, since there are only ~4 billion valid Indian mobiles and the whole space
  can be hashed in minutes. A keyed HMAC can't be brute-forced without the key.
* **Recovering** the value (e.g. to actually dial the customer) needs
  reversible encryption: AES-256-GCM with a random nonce per value, so equal
  plaintexts give different ciphertexts.

The key comes from ``AU_PII_KEY`` (base64), or from a key file that is
created on first use. The DB stores a fingerprint of the key so that running
with the wrong key fails loudly instead of silently orphaning every event.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "AU_PII_KEY"


def _derive(master: bytes, purpose: bytes) -> bytes:
    return hmac.new(master, purpose, hashlib.sha256).digest()


class PiiCodec:
    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("PII master key must be 32 bytes")
        self._k_index = _derive(master_key, b"au/phone-index/v1")
        self._aead = AESGCM(_derive(master_key, b"au/pii-encrypt/v1"))
        self.fingerprint = _derive(master_key, b"au/key-fingerprint/v1").hex()[:16]

    def blind_index(self, value: str) -> str:
        return hmac.new(self._k_index, value.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, value.encode(), None)

    def decrypt(self, blob: bytes) -> str:
        return self._aead.decrypt(blob[:12], blob[12:], None).decode()


def load_codec(key_file: Path) -> PiiCodec:
    env = os.environ.get(KEY_ENV)
    if env:
        return PiiCodec(base64.b64decode(env))
    if key_file.exists():
        return PiiCodec(base64.b64decode(key_file.read_text().strip()))
    key = secrets.token_bytes(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(key).decode() + "\n")
    return PiiCodec(key)
