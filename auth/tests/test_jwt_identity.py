"""Tests for auth.jwt_identity — the JWTVerifier/RemoteAuthProvider builders.

We don't re-test JWT cryptography here — FastMCP's JWTVerifier owns that. We
verify the env-var wiring: which keying source wins, how issuer/audience
flow through, the CSV-list fan-out, and the on/off behaviour of
``build_remote_auth_provider`` when JWT mode is disabled.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from auth.jwt_identity import (
    DEFAULT_ALGORITHM,
    DEFAULT_SUB_CLAIM,
    CompositeTokenVerifier,
    JWTConfigError,
    build_remote_auth_provider,
    build_verifier,
    get_sub_claim,
    is_jwt_verification_enabled,
)

JWT_ENV_KEYS = (
    "BOND_MCPS_JWT_JWKS_URI",
    "BOND_MCPS_JWT_PUBLIC_KEY",
    "BOND_MCPS_JWT_ISSUER",
    "BOND_MCPS_JWT_AUDIENCE",
    "BOND_MCPS_JWT_ALGORITHM",
    "BOND_MCPS_JWT_SUB_CLAIM",
    "BOND_MCPS_JWT_SHARED_SECRET",
    "BOND_MCPS_JWT_SHARED_SECRET_ISSUER",
    "BOND_MCPS_AS_BASE_URL",
    "BOND_MCPS_PUBLIC_URL",
)


@pytest.fixture(autouse=True)
def clean_env():
    """Start each test with all JWT env vars cleared."""
    with patch.dict(os.environ, {k: "" for k in JWT_ENV_KEYS}, clear=False):
        yield


# ---------------------------------------------------------------------------
# is_jwt_verification_enabled
# ---------------------------------------------------------------------------


class TestIsJWTVerificationEnabled:
    def test_disabled_when_both_unset(self):
        assert is_jwt_verification_enabled() is False

    def test_enabled_when_jwks_uri_set(self):
        with patch.dict(
            os.environ, {"BOND_MCPS_JWT_JWKS_URI": "https://as.example.com/.well-known/jwks.json"}
        ):
            assert is_jwt_verification_enabled() is True

    def test_enabled_when_public_key_set(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----..."}):
            assert is_jwt_verification_enabled() is True

    def test_disabled_when_whitespace_only(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": "   \n  "}):
            assert is_jwt_verification_enabled() is False


# ---------------------------------------------------------------------------
# get_sub_claim
# ---------------------------------------------------------------------------


class TestGetSubClaim:
    def test_default(self):
        assert get_sub_claim() == DEFAULT_SUB_CLAIM == "sub"

    def test_override(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_SUB_CLAIM": "user_id"}):
            assert get_sub_claim() == "user_id"

    def test_whitespace_override_falls_back_to_default(self):
        with patch.dict(os.environ, {"BOND_MCPS_JWT_SUB_CLAIM": "   "}):
            assert get_sub_claim() == "sub"


# ---------------------------------------------------------------------------
# build_verifier — config validation
# ---------------------------------------------------------------------------


class TestBuildVerifier:
    def test_missing_both_sources_raises(self):
        with pytest.raises(JWTConfigError, match="must be set"):
            build_verifier()

    def test_both_sources_raises(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://as.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----...",
            },
        ):
            with pytest.raises(JWTConfigError, match="Only one of"):
                build_verifier()

    def test_jwks_uri_path(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://as.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_ISSUER": "https://as.example.com",
                "BOND_MCPS_JWT_AUDIENCE": "github-mcp",
            },
        ):
            verifier = build_verifier()
            # FastMCP exposes these as attributes on the verifier.
            assert (
                getattr(verifier, "jwks_uri", None)
                == "https://as.example.com/.well-known/jwks.json"
            )
            assert getattr(verifier, "issuer", None) == "https://as.example.com"
            assert getattr(verifier, "audience", None) == "github-mcp"

    def test_public_key_path_default_algorithm(self):
        pem = "-----BEGIN PUBLIC KEY-----\nstub\n-----END PUBLIC KEY-----"
        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": pem}):
            verifier = build_verifier()
            assert getattr(verifier, "algorithm", None) == DEFAULT_ALGORITHM

    def test_explicit_algorithm(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
            },
        ):
            verifier = build_verifier()
            assert getattr(verifier, "algorithm", None) == "HS256"

    def test_audience_csv_becomes_list(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
                "BOND_MCPS_JWT_AUDIENCE": "github-mcp, atlassian-mcp ,ms-graph-mcp",
            },
        ):
            verifier = build_verifier()
            audience = getattr(verifier, "audience", None)
            assert audience == ["github-mcp", "atlassian-mcp", "ms-graph-mcp"]

    def test_single_audience_stays_scalar(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
                "BOND_MCPS_JWT_AUDIENCE": "github-mcp",
            },
        ):
            verifier = build_verifier()
            assert getattr(verifier, "audience", None) == "github-mcp"


# ---------------------------------------------------------------------------
# build_remote_auth_provider — on/off + required env
# ---------------------------------------------------------------------------


class TestBuildRemoteAuthProvider:
    def test_returns_none_when_disabled(self):
        assert build_remote_auth_provider("github") is None

    def test_requires_as_base_url(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
                "BOND_MCPS_PUBLIC_URL": "https://github-mcp.example.com",
            },
        ):
            with pytest.raises(JWTConfigError, match="BOND_MCPS_AS_BASE_URL"):
                build_remote_auth_provider("github")

    def test_requires_rs_public_url(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
                "BOND_MCPS_AS_BASE_URL": "https://auth.example.com",
            },
        ):
            with pytest.raises(JWTConfigError, match="BOND_MCPS_PUBLIC_URL"):
                build_remote_auth_provider("github")

    def test_returns_remote_auth_provider_when_configured(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "shared-secret",
                "BOND_MCPS_JWT_ALGORITHM": "HS256",
                "BOND_MCPS_AS_BASE_URL": "https://auth.example.com",
                "BOND_MCPS_PUBLIC_URL": "https://github-mcp.example.com",
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com",
                "BOND_MCPS_JWT_AUDIENCE": "github-mcp",
            },
        ):
            from fastmcp.server.auth import RemoteAuthProvider

            provider = build_remote_auth_provider("github")
            assert isinstance(provider, RemoteAuthProvider)


# ---------------------------------------------------------------------------
# Composite JWT verification (JWKS + shared secret)
# ---------------------------------------------------------------------------


class TestCompositeVerifier:
    """Test composite JWT verification for bond-ai integration."""

    def test_composite_mode_enabled_when_both_set(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": "test-shared-secret-key",
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com,bond-ai",
            },
        ):
            verifier = build_verifier()
            assert isinstance(verifier, CompositeTokenVerifier)

    def test_composite_rejects_public_key_conflict(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": "test-secret",
                "BOND_MCPS_JWT_PUBLIC_KEY": "some-key",
            },
        ):
            with pytest.raises(JWTConfigError, match="Cannot set"):
                build_verifier()

    def test_composite_default_secondary_issuer(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": "test-secret",
            },
        ):
            verifier = build_verifier()
            assert isinstance(verifier, CompositeTokenVerifier)
            assert verifier.secondary_issuer == "bond-ai"

    def test_composite_custom_secondary_issuer(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": "test-secret",
                "BOND_MCPS_JWT_SHARED_SECRET_ISSUER": "my-custom-issuer",
            },
        ):
            verifier = build_verifier()
            assert verifier.secondary_issuer == "my-custom-issuer"

    def test_hs256_token_routed_to_secondary(self):
        import asyncio

        import jwt as pyjwt

        secret = "test-shared-secret-32bytes-long!!"
        token = pyjwt.encode(
            {"sub": "user@example.com", "iss": "bond-ai", "aud": "mcp-server", "exp": 9999999999},
            secret,
            algorithm="HS256",
        )

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": secret,
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com,bond-ai",
                "BOND_MCPS_JWT_AUDIENCE": "mcp-server",
            },
        ):
            verifier = build_verifier()
            result = asyncio.run(verifier.verify_token(token))
            assert result is not None
            assert result.claims["sub"] == "user@example.com"
            assert result.claims["iss"] == "bond-ai"

    def test_hs256_token_wrong_issuer_rejected(self):
        import asyncio

        import jwt as pyjwt

        secret = "test-shared-secret-32bytes-long!!"
        token = pyjwt.encode(
            {
                "sub": "attacker@evil.com",
                "iss": "evil-issuer",
                "aud": "mcp-server",
                "exp": 9999999999,
            },
            secret,
            algorithm="HS256",
        )

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": secret,
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com,bond-ai",
                "BOND_MCPS_JWT_AUDIENCE": "mcp-server",
            },
        ):
            verifier = build_verifier()
            result = asyncio.run(verifier.verify_token(token))
            assert result is None

    def test_rs256_token_routed_to_primary(self):
        """RS256 tokens are forwarded to the primary (JWKS) verifier."""
        import asyncio
        from unittest.mock import AsyncMock

        import jwt as pyjwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = pyjwt.encode(
            {
                "sub": "user@example.com",
                "iss": "https://auth.example.com",
                "aud": "mcp-server",
                "exp": 9999999999,
            },
            pem,
            algorithm="RS256",
        )

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_SHARED_SECRET": "test-secret",
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com,bond-ai",
                "BOND_MCPS_JWT_AUDIENCE": "mcp-server",
            },
        ):
            verifier = build_verifier()
            mock_result = AsyncMock()
            mock_result.claims = {"sub": "user@example.com"}
            verifier.primary.verify_token = AsyncMock(return_value=mock_result)

            result = asyncio.run(verifier.verify_token(token))
            verifier.primary.verify_token.assert_called_once_with(token)
            assert result == mock_result

    def test_existing_single_jwks_mode_unchanged(self):
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_JWKS_URI": "https://auth.example.com/.well-known/jwks.json",
                "BOND_MCPS_JWT_ISSUER": "https://auth.example.com",
            },
        ):
            from fastmcp.server.auth.providers.jwt import JWTVerifier

            verifier = build_verifier()
            assert isinstance(verifier, JWTVerifier)
            assert not isinstance(verifier, CompositeTokenVerifier)
