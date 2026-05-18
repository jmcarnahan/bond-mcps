"""Tests for auth.jwt_identity — JWT verification + user_key resolution."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.jwt_identity import (
    DEFAULT_ALGORITHM,
    IdentityVerificationError,
    JWTConfigError,
    is_jwt_verification_enabled,
    verify_identity_token,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """Generate a fresh RSA-2048 keypair as PEM strings."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture
def signing_setup(rsa_keypair):
    """Provide signing helpers + clean env for each test."""
    private_pem, public_pem = rsa_keypair

    def sign(payload: dict, *, algorithm: str = DEFAULT_ALGORITHM) -> str:
        return jwt.encode(payload, private_pem, algorithm=algorithm)

    # Each test starts with a clean BOND_MCPS_JWT_* env. patch.dict in tests
    # will set what they need.
    with patch.dict(
        os.environ,
        {
            k: ""
            for k in (
                "BOND_MCPS_JWT_PUBLIC_KEY",
                "BOND_MCPS_JWT_ISSUER",
                "BOND_MCPS_JWT_AUDIENCE",
                "BOND_MCPS_JWT_ALGORITHM",
                "BOND_MCPS_JWT_SUB_CLAIM",
            )
        },
        clear=False,
    ):
        yield sign, public_pem


# ---------------------------------------------------------------------------
# is_jwt_verification_enabled
# ---------------------------------------------------------------------------


class TestIsJWTVerificationEnabled:
    def test_disabled_when_unset(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": ""}):
            assert is_jwt_verification_enabled() is False

    def test_enabled_when_set(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----..."}):
            assert is_jwt_verification_enabled() is True

    def test_disabled_when_whitespace_only(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": "   \n  "}):
            assert is_jwt_verification_enabled() is False


# ---------------------------------------------------------------------------
# verify_identity_token — happy path
# ---------------------------------------------------------------------------


class TestVerifyHappyPath:
    def test_minimal_valid_token(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "user-42", "exp": time.time() + 60})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            assert verify_identity_token(token) == "user-42"

    def test_issuer_and_audience_checked(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({
            "sub": "user-42",
            "iss": "bond-ai",
            "aud": "bond-mcps",
            "exp": time.time() + 60,
        })
        with patch.dict(os.environ, {
            "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            "BOND_MCPS_JWT_ISSUER": "bond-ai",
            "BOND_MCPS_JWT_AUDIENCE": "bond-mcps",
        }):
            assert verify_identity_token(token) == "user-42"

    def test_custom_sub_claim(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"user_id": "u-9", "exp": time.time() + 60})
        with patch.dict(os.environ, {
            "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            "BOND_MCPS_JWT_SUB_CLAIM": "user_id",
        }):
            assert verify_identity_token(token) == "u-9"


# ---------------------------------------------------------------------------
# verify_identity_token — failure modes
# ---------------------------------------------------------------------------


class TestVerifyFailures:
    def test_unconfigured_raises_config_error(self, signing_setup):
        sign, _ = signing_setup
        token = sign({"sub": "u", "exp": time.time() + 60})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": ""}):
            with pytest.raises(JWTConfigError):
                verify_identity_token(token)

    def test_invalid_signature(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "u", "exp": time.time() + 60})
        # Tamper with the signature segment
        head, payload, _sig = token.split(".")
        tampered = f"{head}.{payload}.AAAA"
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(tampered)

    def test_expired_token(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "u", "exp": time.time() - 5})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_missing_exp_claim_rejected(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "u"})  # no exp
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_wrong_issuer(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "u", "iss": "attacker", "exp": time.time() + 60})
        with patch.dict(os.environ, {
            "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            "BOND_MCPS_JWT_ISSUER": "bond-ai",
        }):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_wrong_audience(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "u", "aud": "other", "exp": time.time() + 60})
        with patch.dict(os.environ, {
            "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            "BOND_MCPS_JWT_AUDIENCE": "bond-mcps",
        }):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_missing_sub_claim(self, signing_setup):
        sign, public_pem = signing_setup
        # Token has exp but no sub claim. PyJWT's options.require catches this.
        token = sign({"foo": "bar", "exp": time.time() + 60})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_empty_sub_claim_rejected(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": "   ", "exp": time.time() + 60})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_non_string_sub_claim_rejected(self, signing_setup):
        sign, public_pem = signing_setup
        token = sign({"sub": 12345, "exp": time.time() + 60})
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)

    def test_wrong_algorithm_rejected(self, signing_setup):
        sign, public_pem = signing_setup
        # Token signed with RS256 but config expects HS256 — won't match.
        token = sign({"sub": "u", "exp": time.time() + 60})
        with patch.dict(os.environ, {
            "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            "BOND_MCPS_JWT_ALGORITHM": "HS256",
        }):
            with pytest.raises(IdentityVerificationError):
                verify_identity_token(token)
