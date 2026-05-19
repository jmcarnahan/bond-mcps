"""
Token resolution for the Databricks MCP server.

Two distinct flows:

* **JWT mode (multi-tenant deployment).** ``BOND_MCPS_JWT_JWKS_URI`` or
  ``BOND_MCPS_JWT_PUBLIC_KEY`` is set. The Authorization header is the
  user's bond-mcps JWT (consumed by FastMCP's RemoteAuthProvider before
  tools run), so per-user Databricks tokens come from ``tokens.db`` keyed
  by the JWT's ``sub``. The deployment-wide ``DATABRICKS_ACCESS_TOKEN``
  PAT is still honored as a last-resort fallback for clusters that
  pre-provision a shared service-account token. Missing user tokens raise
  ``MissingProviderConnection`` which surfaces a ``/connect/databricks``
  URL to the agent.

* **Single-tenant fallback (laptop).** JWT mode is disabled. The historical
  resolution applies: Authorization Bearer header (legacy Bond AI backend),
  then local OAuth U2M when ``DATABRICKS_CLIENT_ID`` is set, then PAT.

Three entry points share the same precedence chain through
``resolve_token_now``:

  * ``resolve_token_now()`` — used by the MCP server at tool entry. Returns
    BOTH the token AND the source in a single traversal, so the source
    reported in error messages and the token sent to the warehouse can't
    disagree.
  * ``get_databricks_token()`` — convenience for callers that only want
    the token (used by ``_connect(None)`` in the CLI/standalone path).
  * ``get_auth_source()`` — FAST-PATH: returns the source WITHOUT minting
    an OAuth token. Used by startup logging and by the CLI to label the
    mode before running a command. Reading env vars is cheap; minting a
    U2M token can open a browser, which we don't want for logging.
"""

from __future__ import annotations

import os
from enum import Enum


class AuthSource(str, Enum):
    """Which path produced the current token — used for friendly error messages."""

    BEARER = "bearer"
    OAUTH = "oauth"
    PAT = "pat"


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def resolve_token_now() -> tuple[str, AuthSource]:
    """Resolve a Databricks token AND its source in a single traversal."""
    if _jwt_mode_enabled():
        return _resolve_jwt_mode_token()
    return _resolve_legacy_token()


def get_databricks_token() -> str:
    """Resolve a Databricks access token. OAuth wins over PAT."""
    token, _ = resolve_token_now()
    return token


def get_auth_source() -> AuthSource:
    """Determine the auth source WITHOUT minting an OAuth token.

    In JWT mode the calling agent is authenticated via a bond-mcps JWT and
    the upstream token comes from ``tokens.db`` (treated as the OAUTH source)
    or, as a fallback, from ``DATABRICKS_ACCESS_TOKEN`` (PAT). In single-
    tenant mode the historical four-path chain still drives the answer.
    """
    if _jwt_mode_enabled():
        # We don't peek at tokens.db here to keep this cheap, but the
        # per-user OAuth token in the DB is treated as the canonical path
        # for reporting purposes.
        if os.environ.get("DATABRICKS_ACCESS_TOKEN"):
            return AuthSource.PAT
        return AuthSource.OAUTH

    if _read_bearer_header() is not None:
        return AuthSource.BEARER
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        return AuthSource.OAUTH
    if os.environ.get("DATABRICKS_ACCESS_TOKEN"):
        return AuthSource.PAT
    raise PermissionError(_no_auth_message())


# ---------------------------------------------------------------------------
# JWT mode (multi-tenant)
# ---------------------------------------------------------------------------


def _resolve_jwt_mode_token() -> tuple[str, AuthSource]:
    from auth import MissingProviderConnection, TokenStore, resolve_user_key_for_request

    user_key = resolve_user_key_for_request()
    cached = TokenStore("databricks", user_key=user_key).get_token()
    if cached and cached.get("access_token"):
        return cached["access_token"], AuthSource.OAUTH

    # PAT is a cluster-wide fallback (single token shared by everyone). It's
    # intentionally last so a per-user OAuth token always wins when present.
    pat = os.environ.get("DATABRICKS_ACCESS_TOKEN")
    if pat:
        return pat, AuthSource.PAT

    raise MissingProviderConnection(provider="databricks", user_key=user_key)


# ---------------------------------------------------------------------------
# Single-tenant fallback (laptop / CLI)
# ---------------------------------------------------------------------------


def _resolve_legacy_token() -> tuple[str, AuthSource]:
    bearer = _read_bearer_header()
    if bearer:
        return bearer, AuthSource.BEARER

    if os.environ.get("DATABRICKS_CLIENT_ID"):
        from dbx.local_auth import get_local_token

        return get_local_token(), AuthSource.OAUTH

    pat = os.environ.get("DATABRICKS_ACCESS_TOKEN")
    if pat:
        return pat, AuthSource.PAT

    raise PermissionError(_no_auth_message())


def _read_bearer_header() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization"})
    except Exception:  # nosec B110 — outside HTTP request context (stdio, CLI)
        return None
    auth = headers.get("authorization") if headers else None
    if auth and auth.startswith("Bearer "):
        return auth[7:]
    return None


def _no_auth_message() -> str:
    return (
        "Databricks authorization required. Configure one of:\n"
        "  - OAuth (recommended): set DATABRICKS_CLIENT_ID (and optionally "
        "DATABRICKS_CLIENT_SECRET), then run `make login-databricks`.\n"
        "  - PAT (dev fallback): set DATABRICKS_ACCESS_TOKEN for free-tier "
        "workspaces that cannot register OAuth apps.\n"
        "  - Backend mode (e.g. Bond AI): ensure the backend forwards an "
        "Authorization: Bearer header."
    )


def _jwt_mode_enabled() -> bool:
    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
