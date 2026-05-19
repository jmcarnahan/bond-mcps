"""Tests for resolve_user_key_for_request() — the per-request resolver.

Behaviour branches:
  1. JWT disabled                              → current_user_key()
  2. JWT enabled, fastmcp not importable       → current_user_key()
  3. JWT enabled, get_access_token() raises    → current_user_key() (no HTTP ctx)
  4. JWT enabled, get_access_token() is None   → current_user_key() (no HTTP ctx)
  5. JWT enabled, validated AccessToken sub    → claims[<sub_claim>]
  6. JWT enabled, AccessToken missing sub      → RuntimeError (defensive)

The FastMCP middleware enforces signature/audience/issuer at the HTTP layer
before any tool runs, so by the time resolve_user_key_for_request() is called
the access token has already been validated. These tests stub
fastmcp.server.dependencies.get_access_token to model that contract.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# FastMCP stub
# ---------------------------------------------------------------------------


def _install_fastmcp_stub(get_access_token):
    """Insert a stub fastmcp.server.dependencies module whose get_access_token
    callable matches the caller's expectation (returns an AccessToken-like
    object, returns None, or raises)."""
    fastmcp = ModuleType("fastmcp")
    server = ModuleType("fastmcp.server")
    dependencies = ModuleType("fastmcp.server.dependencies")
    dependencies.get_access_token = get_access_token
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
    _remove_fastmcp_stub()
    yield
    _remove_fastmcp_stub()


def _access_token(claims: dict):
    """Minimal AccessToken-shaped object for the resolver to read."""
    return SimpleNamespace(claims=claims)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolverJWTDisabled:
    """JWT mode off (no JWKS URI, no public key) → env-based identity."""

    def test_no_fastmcp_module(self):
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "env-user",
                "BOND_MCPS_JWT_JWKS_URI": "",
                "BOND_MCPS_JWT_PUBLIC_KEY": "",
            },
        ):
            assert resolve_user_key_for_request() == "env-user"

    def test_fastmcp_present_jwt_off(self):
        _install_fastmcp_stub(lambda: _access_token({"sub": "should-not-be-used"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "env-user",
                "BOND_MCPS_JWT_JWKS_URI": "",
                "BOND_MCPS_JWT_PUBLIC_KEY": "",
            },
        ):
            assert resolve_user_key_for_request() == "env-user"


class TestResolverJWTEnabledFallbacks:
    """JWT mode on but no live HTTP context — fall through to env."""

    def test_fastmcp_not_importable(self):
        from auth.token_store import resolve_user_key_for_request

        # No stub installed — `from fastmcp.server.dependencies import
        # get_access_token` raises ImportError.
        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "cli-user",
                "BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder",
            },
        ):
            assert resolve_user_key_for_request() == "cli-user"

    def test_get_access_token_raises(self):
        def _raises():
            raise RuntimeError("no active request context")

        _install_fastmcp_stub(_raises)
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "cli-user",
                "BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder",
            },
        ):
            assert resolve_user_key_for_request() == "cli-user"

    def test_get_access_token_returns_none(self):
        _install_fastmcp_stub(lambda: None)
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "cli-user",
                "BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder",
            },
        ):
            assert resolve_user_key_for_request() == "cli-user"


class TestResolverJWTValidatedToken:
    """JWT mode on, middleware-validated AccessToken present."""

    def test_returns_sub_claim(self):
        _install_fastmcp_stub(lambda: _access_token({"sub": "alice"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_USER_ID": "fallback-should-not-be-used",
                "BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder",
            },
        ):
            assert resolve_user_key_for_request() == "alice"

    def test_returns_custom_sub_claim(self):
        _install_fastmcp_stub(lambda: _access_token({"user_id": "u-42"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {
                "BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder",
                "BOND_MCPS_JWT_SUB_CLAIM": "user_id",
            },
        ):
            assert resolve_user_key_for_request() == "u-42"

    def test_missing_sub_claim_raises(self):
        _install_fastmcp_stub(lambda: _access_token({"email": "nobody@example.com"}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {"BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder"},
        ):
            with pytest.raises(RuntimeError, match="sub"):
                resolve_user_key_for_request()

    def test_empty_sub_claim_raises(self):
        _install_fastmcp_stub(lambda: _access_token({"sub": "   "}))
        from auth.token_store import resolve_user_key_for_request

        with patch.dict(
            os.environ,
            {"BOND_MCPS_JWT_PUBLIC_KEY": "pem-placeholder"},
        ):
            with pytest.raises(RuntimeError):
                resolve_user_key_for_request()
