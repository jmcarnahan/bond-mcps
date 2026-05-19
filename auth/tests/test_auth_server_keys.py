"""Tests for the Authorization Server signing key + JWKS document."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.auth_server.keys import (
    ASKeyError,
    build_jwks_document,
    load_previous_signing_key,
    load_signing_key,
)


@pytest.fixture
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def clean_env():
    keys = (
        "BOND_MCPS_AS_PRIVATE_KEY_PEM",
        "BOND_MCPS_AS_PRIVATE_KEY_FILE",
        "BOND_MCPS_AS_PREVIOUS_KEY_PEM",
        "BOND_MCPS_DB_URL",
    )
    with patch.dict(os.environ, {k: "" for k in keys}, clear=False):
        yield


class TestLoadSigningKey:
    def test_pem_env_wins(self, rsa_pem):
        with patch.dict(os.environ, {"BOND_MCPS_AS_PRIVATE_KEY_PEM": rsa_pem}):
            sk = load_signing_key()
            assert isinstance(sk.private_key, rsa.RSAPrivateKey)
            assert len(sk.kid) == 8

    def test_file_env(self, rsa_pem, tmp_path: Path):
        f = tmp_path / "key.pem"
        f.write_text(rsa_pem)
        with patch.dict(os.environ, {"BOND_MCPS_AS_PRIVATE_KEY_FILE": str(f)}):
            sk = load_signing_key()
            assert len(sk.kid) == 8

    def test_invalid_pem_raises(self):
        with patch.dict(os.environ, {"BOND_MCPS_AS_PRIVATE_KEY_PEM": "not-a-pem"}):
            with pytest.raises(ASKeyError):
                load_signing_key()

    def test_postgres_without_explicit_key_raises(self, tmp_path):
        # No PEM env, no file env, and Postgres → must refuse autogen.
        with patch.dict(
            os.environ,
            {"BOND_MCPS_DB_URL": "postgresql://x/y?sslmode=require"},
        ):
            # Also redirect default key path so we don't pick up a stray file.
            with patch(
                "auth.auth_server.keys._DEFAULT_KEY_PATH",
                tmp_path / "nope.pem",
            ):
                with pytest.raises(ASKeyError, match="Postgres"):
                    load_signing_key()

    def test_kid_stable_for_same_key(self, rsa_pem):
        with patch.dict(os.environ, {"BOND_MCPS_AS_PRIVATE_KEY_PEM": rsa_pem}):
            assert load_signing_key().kid == load_signing_key().kid


class TestJWKSDocument:
    def test_one_key_when_no_previous(self, rsa_pem):
        with patch.dict(os.environ, {"BOND_MCPS_AS_PRIVATE_KEY_PEM": rsa_pem}):
            doc = build_jwks_document()
        assert len(doc["keys"]) == 1
        jwk = doc["keys"][0]
        assert jwk["kty"] == "RSA"
        assert jwk["alg"] == "RS256"
        assert jwk["use"] == "sig"
        assert len(jwk["kid"]) == 8
        # n and e are valid base64url big-endian unsigned ints
        _b64u_to_int(jwk["n"])
        assert _b64u_to_int(jwk["e"]) == 65537

    def test_two_keys_during_rotation(self, rsa_pem):
        previous_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        previous_pem = previous_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_AS_PRIVATE_KEY_PEM": rsa_pem,
                "BOND_MCPS_AS_PREVIOUS_KEY_PEM": previous_pem,
            },
        ):
            doc = build_jwks_document()
        kids = {k["kid"] for k in doc["keys"]}
        assert len(kids) == 2

    def test_no_previous_when_env_unset(self):
        assert load_previous_signing_key() is None


def _b64u_to_int(value: str) -> int:
    padding = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + padding)
    return int.from_bytes(raw, "big")
