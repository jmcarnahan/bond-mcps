"""
Token resolution for the Databricks MCP server.

Path 1: Authorization: Bearer header (Bond AI backend) — always preferred
Path 2: DATABRICKS_CLIENT_ID set → local OAuth U2M via shared proxy
Path 3: DATABRICKS_ACCESS_TOKEN set → return PAT verbatim (dev fallback for
        free / Community Edition workspaces that cannot register OAuth apps)
Path 4: PermissionError
"""

import os
from enum import Enum


class AuthSource(str, Enum):
    """Which path produced the current token — used for friendly error messages."""

    BEARER = "bearer"
    OAUTH = "oauth"
    PAT = "pat"


def get_databricks_token() -> str:
    """
    Resolve a Databricks access token.

    OAuth wins over PAT when both are configured.

    Returns:
        The raw access token string. Could be an OAuth U2M token or a PAT —
        the SQL connector accepts both as Bearer credentials.

    Raises:
        PermissionError: If no valid token source is configured.
    """
    token, _ = _resolve_token_and_source()
    return token


def get_auth_source() -> AuthSource:
    """
    Determine which auth source would be used right now, without minting a
    token. Used by startup logging in databricks_mcp._lifespan and by friendly
    error messages.
    """
    if _has_bearer_header():
        return AuthSource.BEARER
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        return AuthSource.OAUTH
    if os.environ.get("DATABRICKS_ACCESS_TOKEN"):
        return AuthSource.PAT
    raise PermissionError(_no_auth_message())


def _resolve_token_and_source() -> tuple[str, AuthSource]:
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


def _has_bearer_header() -> bool:
    return _read_bearer_header() is not None


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
