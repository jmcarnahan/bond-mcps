"""Tests for the AES-256-GCM token encryption module."""

import base64
import os
from pathlib import Path

import pytest

from auth.encryption import (
    EncryptionKeyResolver,
    KEY_BYTES,
    TokenEncryptionError,
    decrypt,
    decrypt_optional,
    encrypt,
    encrypt_optional,
    generate_key,
    verify_encryption_setup,
)


@pytest.fixture
def env_key(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", key)
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY_FILE", raising=False)
    return key


@pytest.fixture
def resolver(env_key):
    return EncryptionKeyResolver(allow_file_fallback=False)


def test_round_trip(resolver):
    plaintext = b"hello world"
    blob, version = encrypt(
        plaintext, user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    assert version == 1
    assert blob != plaintext
    assert len(blob) > len(plaintext)  # nonce + tag overhead

    got = decrypt(
        blob, user_key="u1", provider="github", field="access_token",
        key_version=version, resolver=resolver,
    )
    assert got == plaintext


def test_nonce_is_unique_per_encryption(resolver):
    blob1, _ = encrypt(
        b"same", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    blob2, _ = encrypt(
        b"same", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    assert blob1 != blob2  # different nonces


def test_wrong_user_key_fails_decrypt(resolver):
    blob, version = encrypt(
        b"secret", user_key="alice", provider="github", field="access_token",
        resolver=resolver,
    )
    with pytest.raises(TokenEncryptionError, match="Authentication failed"):
        decrypt(
            blob, user_key="mallory", provider="github", field="access_token",
            key_version=version, resolver=resolver,
        )


def test_wrong_provider_fails_decrypt(resolver):
    blob, version = encrypt(
        b"secret", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    with pytest.raises(TokenEncryptionError, match="Authentication failed"):
        decrypt(
            blob, user_key="u1", provider="atlassian", field="access_token",
            key_version=version, resolver=resolver,
        )


def test_wrong_field_fails_decrypt(resolver):
    blob, version = encrypt(
        b"secret", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    with pytest.raises(TokenEncryptionError, match="Authentication failed"):
        decrypt(
            blob, user_key="u1", provider="github", field="refresh_token",
            key_version=version, resolver=resolver,
        )


def test_wrong_key_version_fails_decrypt(resolver):
    blob, _ = encrypt(
        b"secret", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    # version 2 isn't registered with this resolver
    with pytest.raises(TokenEncryptionError, match="Unknown encryption key version"):
        decrypt(
            blob, user_key="u1", provider="github", field="access_token",
            key_version=2, resolver=resolver,
        )


def test_truncated_ciphertext_fails(resolver):
    blob, version = encrypt(
        b"secret", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    with pytest.raises(TokenEncryptionError):
        decrypt(
            blob[:10], user_key="u1", provider="github", field="access_token",
            key_version=version, resolver=resolver,
        )


def test_tampered_ciphertext_fails(resolver):
    blob, version = encrypt(
        b"secret", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01  # flip a bit in the auth tag
    with pytest.raises(TokenEncryptionError, match="Authentication failed"):
        decrypt(
            bytes(tampered), user_key="u1", provider="github", field="access_token",
            key_version=version, resolver=resolver,
        )


def test_missing_env_var_strict_mode(monkeypatch):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY_FILE", raising=False)
    resolver = EncryptionKeyResolver(allow_file_fallback=False)
    with pytest.raises(TokenEncryptionError, match="required"):
        resolver.get_key(1)


def test_missing_env_var_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    key_file = tmp_path / "encryption_key"
    key_file.write_text(generate_key())
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY_FILE", str(key_file))

    resolver = EncryptionKeyResolver(allow_file_fallback=True)
    key = resolver.get_key(1)
    assert len(key) == KEY_BYTES


def test_file_fallback_disabled_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    key_file = tmp_path / "encryption_key"
    key_file.write_text(generate_key())
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY_FILE", str(key_file))

    resolver = EncryptionKeyResolver(allow_file_fallback=False)
    with pytest.raises(TokenEncryptionError, match="required"):
        resolver.get_key(1)


def test_file_fallback_logs_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    key_file = tmp_path / "encryption_key"
    key_file.write_text(generate_key())
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY_FILE", str(key_file))

    resolver = EncryptionKeyResolver(allow_file_fallback=True)
    with caplog.at_level("WARNING", logger="auth.encryption"):
        resolver.get_key(1)
    assert any("encryption key from file" in r.message.lower() for r in caplog.records)


def test_invalid_base64_key_in_env(monkeypatch):
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", "not-valid-base64!!!@@@")
    resolver = EncryptionKeyResolver(allow_file_fallback=False)
    with pytest.raises(TokenEncryptionError, match="not valid base64|bytes; expected"):
        resolver.get_key(1)


def test_wrong_length_key_in_env(monkeypatch):
    short = base64.urlsafe_b64encode(b"too short").decode("ascii")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", short)
    resolver = EncryptionKeyResolver(allow_file_fallback=False)
    with pytest.raises(TokenEncryptionError, match="bytes; expected"):
        resolver.get_key(1)


def test_key_caching(resolver):
    k1 = resolver.get_key(1)
    k2 = resolver.get_key(1)
    assert k1 is k2  # cached, not re-decoded


def test_encrypt_none_raises(resolver):
    with pytest.raises(TokenEncryptionError, match="None"):
        encrypt(
            None, user_key="u1", provider="github", field="access_token",
            resolver=resolver,
        )


def test_encrypt_optional_returns_none_for_none(resolver):
    blob, version = encrypt_optional(
        None, user_key="u1", provider="github", field="refresh_token",
        resolver=resolver,
    )
    assert blob is None
    assert version is None


def test_encrypt_optional_round_trip(resolver):
    blob, version = encrypt_optional(
        b"secret", user_key="u1", provider="github", field="refresh_token",
        resolver=resolver,
    )
    assert blob is not None and version == 1
    got = decrypt_optional(
        blob, version, user_key="u1", provider="github", field="refresh_token",
        resolver=resolver,
    )
    assert got == b"secret"


def test_decrypt_optional_none(resolver):
    assert decrypt_optional(
        None, None, user_key="u1", provider="github", field="refresh_token",
        resolver=resolver,
    ) is None


def test_decrypt_optional_blob_without_version_is_corrupt(resolver):
    blob, _ = encrypt(
        b"x", user_key="u1", provider="github", field="access_token",
        resolver=resolver,
    )
    with pytest.raises(TokenEncryptionError, match="corrupt"):
        decrypt_optional(
            blob, None, user_key="u1", provider="github", field="access_token",
            resolver=resolver,
        )


def test_verify_encryption_setup_happy_path(resolver):
    verify_encryption_setup(resolver)  # should not raise


def test_verify_encryption_setup_no_key_raises(monkeypatch):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY_FILE", raising=False)
    resolver = EncryptionKeyResolver(allow_file_fallback=False)
    with pytest.raises(TokenEncryptionError):
        verify_encryption_setup(resolver)


def test_generate_key_produces_decodable_32_bytes():
    k = generate_key()
    raw = base64.urlsafe_b64decode(k.encode("ascii"))
    assert len(raw) == KEY_BYTES


def test_two_users_cannot_decrypt_each_others_tokens(resolver):
    """Sanity: AAD binding is enforced even within same provider/field."""
    blob_a, version = encrypt(
        b"alice-secret", user_key="alice", provider="github", field="access_token",
        resolver=resolver,
    )
    # Bob can't decrypt Alice's row even though he has the key
    with pytest.raises(TokenEncryptionError):
        decrypt(
            blob_a, user_key="bob", provider="github", field="access_token",
            key_version=version, resolver=resolver,
        )
