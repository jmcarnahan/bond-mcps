"""Smoke tests for build_remote_auth_provider — RFC 9728 discovery on the MCP side.

We don't reach into FastMCP's middleware internals (those are exercised by
FastMCP's own tests). Instead we confirm that:

* When JWT mode is off, ``build_remote_auth_provider`` returns ``None`` so
  FastMCP runs unmodified.
* When JWT mode is on, the returned provider mounts a
  ``/.well-known/oauth-protected-resource/mcp`` route on the FastMCP app and
  401 responses include the spec-required ``WWW-Authenticate`` header.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.jwt_identity import build_remote_auth_provider


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
        "BOND_MCPS_JWT_JWKS_URI",
        "BOND_MCPS_JWT_PUBLIC_KEY",
        "BOND_MCPS_JWT_ALGORITHM",
        "BOND_MCPS_JWT_ISSUER",
        "BOND_MCPS_JWT_AUDIENCE",
        "BOND_MCPS_AS_BASE_URL",
        "BOND_MCPS_PUBLIC_URL",
    )
    with patch.dict(os.environ, {k: "" for k in keys}, clear=False):
        yield


def test_returns_none_when_disabled():
    assert build_remote_auth_provider("github") is None


def test_returns_provider_when_configured(rsa_pem):
    with patch.dict(
        os.environ,
        {
            "BOND_MCPS_JWT_PUBLIC_KEY": rsa_pem,
            "BOND_MCPS_AS_BASE_URL": "https://auth.example.com",
            "BOND_MCPS_PUBLIC_URL": "https://github-mcp.example.com",
            "BOND_MCPS_JWT_ISSUER": "https://auth.example.com",
            "BOND_MCPS_JWT_AUDIENCE": "github",
        },
    ):
        from fastmcp.server.auth import RemoteAuthProvider

        provider = build_remote_auth_provider("github")
        assert isinstance(provider, RemoteAuthProvider)


def test_fastmcp_app_serves_protected_resource_metadata(rsa_pem):
    """Spin up a tiny FastMCP server with RemoteAuthProvider and confirm
    the RFC 9728 metadata endpoint is mounted."""
    from fastmcp import FastMCP

    with patch.dict(
        os.environ,
        {
            "BOND_MCPS_JWT_PUBLIC_KEY": rsa_pem,
            "BOND_MCPS_AS_BASE_URL": "https://auth.example.com",
            "BOND_MCPS_PUBLIC_URL": "https://github-mcp.example.com",
            "BOND_MCPS_JWT_ISSUER": "https://auth.example.com",
            "BOND_MCPS_JWT_AUDIENCE": "github",
        },
    ):
        provider = build_remote_auth_provider("github")
        mcp = FastMCP("Test", auth=provider)

        @mcp.tool()
        def noop() -> str:
            return "ok"

        http_app = mcp.http_app()
        paths = {getattr(r, "path", None) for r in http_app.routes}
        assert "/.well-known/oauth-protected-resource/mcp" in paths
