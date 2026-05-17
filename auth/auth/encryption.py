"""AES-256-GCM encryption for OAuth tokens.

Design notes:
- 32-byte key, 12-byte nonce from os.urandom, built-in 16-byte auth tag.
- AAD binds each ciphertext to (user_key, provider, field, key_version) so a
  DBA who swaps ciphertexts between rows or columns gets InvalidTag on decrypt.
- Storage layout: raw bytes nonce || ciphertext (in LargeBinary columns).
- EncryptionKeyResolver indirection makes future key rotation a small change.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

NONCE_BYTES = 12
KEY_BYTES = 32
DEFAULT_KEY_FILE = Path.home() / ".bond_mcps" / "encryption_key"
SENTINEL_PLAINTEXT = b"bond-mcps-encryption-sentinel"


class TokenEncryptionError(Exception):
    """Raised for any encryption/decryption failure or misconfiguration."""


class KeyResolver(Protocol):
    def get_key(self, version: int) -> bytes: ...
    @property
    def write_version(self) -> int: ...


class EncryptionKeyResolver:
    """Resolves an AES-256 key by version.

    v1 is the only key today, sourced from BOND_MCPS_ENCRYPTION_KEY env var.
    For SQLite-only deployments, falls back to a 0600 file at
    ~/.bond_mcps/encryption_key. For Postgres URLs the file fallback is
    refused — callers should pass allow_file_fallback=False.
    """

    WRITE_VERSION = 1

    def __init__(
        self,
        *,
        allow_file_fallback: bool,
        env_var: str = "BOND_MCPS_ENCRYPTION_KEY",
        key_file: Path | None = None,
    ):
        self._env_var = env_var
        self._key_file = key_file or _resolve_key_file_path()
        self._allow_file_fallback = allow_file_fallback
        self._cache: dict[int, bytes] = {}

    @property
    def write_version(self) -> int:
        return self.WRITE_VERSION

    def get_key(self, version: int) -> bytes:
        if version in self._cache:
            return self._cache[version]
        if version != self.WRITE_VERSION:
            raise TokenEncryptionError(
                f"Unknown encryption key version: {version}. "
                "If you've rotated keys, add a resolver mapping for older versions."
            )
        key = self._load_v1_key()
        self._cache[version] = key
        return key

    def _load_v1_key(self) -> bytes:
        env_value = os.environ.get(self._env_var)
        if env_value:
            return _decode_key(env_value, source=f"${self._env_var}")

        if not self._allow_file_fallback:
            raise TokenEncryptionError(
                f"{self._env_var} is required. Set it in the environment "
                "(generate one with `bond-mcps generate-key`)."
            )

        if self._key_file.exists():
            logger.warning(
                "Using encryption key from file %s. For stronger posture, "
                "set %s in the environment and remove the file.",
                self._key_file,
                self._env_var,
            )
            return _decode_key(
                self._key_file.read_text().strip(), source=str(self._key_file)
            )

        raise TokenEncryptionError(
            f"No encryption key configured. Set {self._env_var} in the "
            f"environment or write a base64-encoded 32-byte key to "
            f"{self._key_file}. Use `bond-mcps generate-key` to mint one."
        )


def _resolve_key_file_path() -> Path:
    override = os.environ.get("BOND_MCPS_ENCRYPTION_KEY_FILE")
    if override:
        return Path(override).expanduser()
    return DEFAULT_KEY_FILE


def _decode_key(value: str, *, source: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, base64.binascii.Error) as e:
        raise TokenEncryptionError(
            f"Encryption key from {source} is not valid base64: {e}"
        ) from None
    if len(raw) != KEY_BYTES:
        raise TokenEncryptionError(
            f"Encryption key from {source} is {len(raw)} bytes; expected {KEY_BYTES}."
        )
    return raw


def generate_key() -> str:
    """Generate a fresh base64-encoded 32-byte key suitable for the env var."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _aad(user_key: str, provider: str, field: str, key_version: int) -> bytes:
    return f"{user_key}|{provider}|{field}|v{key_version}".encode("utf-8")


def encrypt(
    plaintext: bytes,
    *,
    user_key: str,
    provider: str,
    field: str,
    resolver: KeyResolver,
) -> tuple[bytes, int]:
    """Encrypt plaintext at the resolver's write version.

    Returns (nonce || ciphertext, key_version). AAD pins (user_key, provider,
    field, key_version) so the ciphertext is bound to its row identity.
    """
    if plaintext is None:
        raise TokenEncryptionError("Cannot encrypt None")
    version = resolver.write_version
    key = resolver.get_key(version)
    nonce = os.urandom(NONCE_BYTES)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, _aad(user_key, provider, field, version))
    return nonce + ct, version


def decrypt(
    blob: bytes,
    *,
    user_key: str,
    provider: str,
    field: str,
    key_version: int,
    resolver: KeyResolver,
) -> bytes:
    """Decrypt nonce||ciphertext, verifying AAD binding to the row identity."""
    if blob is None or len(blob) <= NONCE_BYTES:
        raise TokenEncryptionError("Ciphertext is missing or too short")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    key = resolver.get_key(key_version)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct, _aad(user_key, provider, field, key_version))
    except InvalidTag as e:
        raise TokenEncryptionError(
            "Authentication failed: ciphertext, key, or AAD does not match. "
            "Possible causes: wrong key, tampered DB row, mismatched key_version."
        ) from e


def encrypt_optional(
    plaintext: bytes | None, **kwargs
) -> tuple[bytes | None, int | None]:
    """Encrypt if not None; otherwise return (None, None).

    Useful for nullable columns like refresh_token where presence is optional.
    """
    if plaintext is None:
        return None, None
    blob, version = encrypt(plaintext, **kwargs)
    return blob, version


def decrypt_optional(
    blob: bytes | None, key_version: int | None, **kwargs
) -> bytes | None:
    """Decrypt if not None; otherwise return None."""
    if blob is None:
        return None
    if key_version is None:
        raise TokenEncryptionError(
            "Ciphertext present but key_version is None — DB row is corrupt"
        )
    return decrypt(blob, key_version=key_version, **kwargs)


def verify_encryption_setup(resolver: KeyResolver) -> None:
    """Round-trip a sentinel string to verify the encryption setup at startup.

    Raises TokenEncryptionError if the key isn't configured or doesn't work.
    """
    blob, version = encrypt(
        SENTINEL_PLAINTEXT,
        user_key="__sentinel__",
        provider="__sentinel__",
        field="__sentinel__",
        resolver=resolver,
    )
    got = decrypt(
        blob,
        user_key="__sentinel__",
        provider="__sentinel__",
        field="__sentinel__",
        key_version=version,
        resolver=resolver,
    )
    if got != SENTINEL_PLAINTEXT:
        raise TokenEncryptionError(
            "Sentinel round-trip produced unexpected output; encryption is misconfigured"
        )
