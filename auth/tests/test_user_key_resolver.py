"""Tests for resolve_user_key_for_request() — the per-request resolver.

Five behaviour branches:
  1. Outside an HTTP context, JWT disabled  → current_user_key() (env)
  2. Outside an HTTP context, JWT enabled   → current_user_key() (CLI fallback)
  3. Inside an HTTP context, JWT disabled   → current_user_key()
  4. Inside an HTTP context, JWT enabled,
     no X-Bond-Auth header                  → IdentityVerificationError
  5. Inside an HTTP context, JWT enabled,
     valid X-Bond-Auth header               → JWT sub claim
"""

from __future__ import annotations

import os
import sys
import time
from types import ModuleType
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.jwt_identity import IdentityVerificationError


# ---------------------------------------------------------------------------
# Shared keypair + signing helper
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def keypair():
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


def _sign(payload: dict, private_pem: str) -> str:
    return jwt.encode(payload, private_pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# HTTP-context simulation
#
# resolve_user_key_for_request() does a lazy `from fastmcp.server.dependencies
# import get_http_headers`. We swap a stub module into sys.modules so the
# function sees the headers we want (or raises to simulate non-HTTP context).
# ---------------------------------------------------------------------------


class _FakeFastMCPDeps:
    """Backing for the stubbed fastmcp.server.dependencies module."""

    def __init__(self, *, headers: dict | None = None, raise_outside_ctx: bool = False):
        self.headers = headers or {}
        self.raise_outside_ctx = raise_outside_ctx

    def get_http_headers(self, include=None):
        if self.raise_outside_ctx:
            raise RuntimeError("Not inside an HTTP request context")
        if include:
            return {k.lower(): v for k, v in self.headers.items() if k.lower() in {n.lower() for n in include}}
        return dict(self.headers)


def _install_fastmcp_stub(deps: _FakeFastMCPDeps):
    """Insert (or replace) a stub fastmcp.server.dependencies module so the
    lazy import inside resolve_user_key_for_request() picks up our stub."""
    fastmcp = ModuleType("fastmcp")
    server = ModuleType("fastmcp.server")
    dependencies = ModuleType("fastmcp.server.dependencies")
    dependencies.get_http_headers = deps.get_http_headers
    server.dependencies = dependencies
    fastmcp.server = server
    sys.modules["fastmcp"] = fastmcp
    sys.modules["fastmcp.server"] = server
    sys.modules["fastmcp.server.dependencies"] = dependencies


def _remove_fastmcp_stub():
    for k in ("fastmcp.server.dependencies", "fastmcp.server", "fastmcp"):
        sys.modules.pop(k, None)


@pytest.fixture(autouse=True)
def isolate_fastmcp():
    """Each test starts with no fastmcp module visible."""
    _remove_fastmcp_stub()
    yield
    _remove_fastmcp_stub()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolverNoFastMCP:
    """When fastmcp isn't installed (or not on the path), fall back to env."""

    def test_falls_back_to_current_user_key(self):
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {"BOND_MCPS_USER_ID": "env-user", "BOND_MCPS_JWT_PUBLIC_KEY": ""},
        ):
            assert resolve_user_key_for_request() == "env-user"


class TestResolverOutsideHTTPContext:
    """fastmcp is importable but we're not inside a request (CLI / tests)."""

    def test_jwt_disabled_falls_back_to_env(self):
        _install_fastmcp_stub(_FakeFastMCPDeps(raise_outside_ctx=True))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {"BOND_MCPS_USER_ID": "cli-user", "BOND_MCPS_JWT_PUBLIC_KEY": ""},
        ):
            assert resolve_user_key_for_request() == "cli-user"

    def test_jwt_enabled_still_falls_back_to_env(self, keypair):
        """CLI paths don't carry an identity JWT; env is the right fallback
        when JWT is otherwise enabled for HTTP requests."""
        _, public_pem = keypair
        _install_fastmcp_stub(_FakeFastMCPDeps(raise_outside_ctx=True))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "cli-user",
                "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            },
        ):
            assert resolve_user_key_for_request() == "cli-user"


class TestResolverInsideHTTPContext:
    """fastmcp is reachable and a request context is active."""

    def test_jwt_disabled_returns_env_user(self):
        _install_fastmcp_stub(_FakeFastMCPDeps(headers={}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {"BOND_MCPS_USER_ID": "env-user", "BOND_MCPS_JWT_PUBLIC_KEY": ""},
        ):
            assert resolve_user_key_for_request() == "env-user"

    def test_jwt_enabled_missing_header_raises(self, keypair):
        _, public_pem = keypair
        _install_fastmcp_stub(_FakeFastMCPDeps(headers={}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "fallback",
                "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            },
        ):
            with pytest.raises(IdentityVerificationError, match="X-Bond-Auth"):
                resolve_user_key_for_request()

    def test_jwt_enabled_non_bearer_header_raises(self, keypair):
        _, public_pem = keypair
        _install_fastmcp_stub(_FakeFastMCPDeps(headers={"x-bond-auth": "notBearer xxx"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                resolve_user_key_for_request()

    def test_jwt_enabled_valid_token_returns_sub(self, keypair):
        private_pem, public_pem = keypair
        token = _sign({"sub": "alice", "exp": time.time() + 60}, private_pem)
        _install_fastmcp_stub(_FakeFastMCPDeps(headers={"x-bond-auth": f"Bearer {token}"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "fallback-should-not-be-used",
                "BOND_MCPS_JWT_PUBLIC_KEY": public_pem,
            },
        ):
            assert resolve_user_key_for_request() == "alice"

    def test_jwt_enabled_invalid_token_raises(self, keypair):
        _, public_pem = keypair
        _install_fastmcp_stub(_FakeFastMCPDeps(headers={"x-bond-auth": "Bearer not.a.jwt"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(os.environ, {"BOND_MCPS_JWT_PUBLIC_KEY": public_pem}):
            with pytest.raises(IdentityVerificationError):
                resolve_user_key_for_request()
