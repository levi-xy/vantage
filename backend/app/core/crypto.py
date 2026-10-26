"""Encryption of stored secrets (instance and channel credentials) with the master key.

The master key is 32 random bytes, base64-encoded (``openssl rand -base64 32``),
read from ``VANTAGE_MASTER_KEY_FILE`` (preferred) or ``VANTAGE_MASTER_KEY``.
It is never used directly: each purpose gets its own subkey derived with HKDF,
so later features (e.g. session signing) can reuse the master key safely.

Blob format (stored in the ``secrets`` columns)::

    version (1 byte, 0x01) | nonce (12 bytes) | AES-256-GCM ciphertext + tag (16 bytes)

Every blob is bound to a *context* string naming where it is stored, for example
``row_context("instances", instance_id)``. The context is authenticated, so a
blob copied into another row or table fails to decrypt.

Error messages never contain key material, plaintext or ciphertext.
"""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_FILE_ENV = "VANTAGE_MASTER_KEY_FILE"
KEY_ENV = "VANTAGE_MASTER_KEY"
KEY_BYTES = 32

_VERSION_1 = b"\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_SECRETS_PURPOSE = b"vantage/secrets/v1"
_KEY_FORMAT_HINT = "32 random bytes, base64-encoded (generate with: openssl rand -base64 32)"


class MasterKeyError(RuntimeError):
    """The master key is missing or malformed. The message never contains the key."""


class DecryptionError(Exception):
    """A secrets blob could not be decrypted: wrong key, wrong context or tampered data."""

    def __init__(self) -> None:
        super().__init__("stored secrets could not be decrypted")


def load_master_key(env: Mapping[str, str] | None = None) -> bytes:
    """Read and validate the master key. Raises ``MasterKeyError`` if it is unusable."""
    env = os.environ if env is None else env
    key_file = env.get(KEY_FILE_ENV, "").strip()
    if key_file:
        try:
            raw = Path(key_file).read_bytes().decode("ascii")
        except (OSError, UnicodeDecodeError):
            raise MasterKeyError(f"{KEY_FILE_ENV} is set but the file can't be read") from None
        return _decode_key(raw, KEY_FILE_ENV)

    raw = env.get(KEY_ENV, "")
    if not raw.strip():
        raise MasterKeyError(
            f"No master key. Set {KEY_FILE_ENV} (preferred) or {KEY_ENV} to {_KEY_FORMAT_HINT}."
        )
    return _decode_key(raw, KEY_ENV)


def _decode_key(raw: str, source: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != KEY_BYTES:
        raise MasterKeyError(f"{source} must be {_KEY_FORMAT_HINT}")
    return key


def derive_subkey(master_key: bytes, purpose: bytes) -> bytes:
    """Derive an independent 32-byte key for one purpose from the master key."""
    if len(master_key) != KEY_BYTES:
        raise ValueError(f"master key must be {KEY_BYTES} bytes")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=purpose)
    return hkdf.derive(master_key)


def row_context(table: str, row_id: object) -> str:
    """The context string for the ``secrets`` column of one database row."""
    return f"{table}:{row_id}"


class SecretBox:
    """Encrypts and decrypts secrets blobs. Holds a derived key; never prints it."""

    def __init__(self, master_key: bytes) -> None:
        self._aead = AESGCM(derive_subkey(master_key, _SECRETS_PURPOSE))

    def __repr__(self) -> str:
        return "SecretBox(<redacted>)"

    def encrypt(self, plaintext: bytes, *, context: str) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext, _aad(context))
        return _VERSION_1 + nonce + sealed

    def decrypt(self, blob: bytes, *, context: str) -> bytes:
        header_len = len(_VERSION_1) + _NONCE_BYTES
        if len(blob) < header_len + _TAG_BYTES or blob[:1] != _VERSION_1:
            raise DecryptionError()
        nonce = blob[1:header_len]
        try:
            return self._aead.decrypt(nonce, blob[header_len:], _aad(context))
        except InvalidTag:
            raise DecryptionError() from None

    def encrypt_secrets(self, values: Mapping[str, str], *, context: str) -> bytes:
        """Encrypt a mapping of secret field name to value."""
        payload = json.dumps(dict(values), sort_keys=True, separators=(",", ":"))
        return self.encrypt(payload.encode("utf-8"), context=context)

    def decrypt_secrets(self, blob: bytes, *, context: str) -> dict[str, str]:
        plaintext = self.decrypt(blob, context=context)
        try:
            values = json.loads(plaintext)
        except ValueError:
            raise DecryptionError() from None
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise DecryptionError()
        return values


def _aad(context: str) -> bytes:
    if not context:
        raise ValueError("context is required")
    return _SECRETS_PURPOSE + b"\x00" + context.encode("utf-8")
