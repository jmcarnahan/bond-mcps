"""
Token resolution for the Databricks MCP server.

Path 1: Authorization: Bearer header (Bond AI backend) — always preferred
Path 2: DATABRICKS_CLIENT_ID set → local OAuth U2M via shared proxy
Path 3: DATABRICKS_ACCESS_TOKEN set → return PAT verbatim (dev fallback for
        free / Community Edition workspaces that cannot register OAuth apps)
Path 4: PermissionError

Three entry points share the same precedence chain (implemented once in
`resolve_token_now`):

  * `resolve_token_now()` — used by the MCP server at tool entry. Returns
    BOTH the token AND the source in a single traversal, so the source
    reported in error messages and the token sent to the warehouse can't
    disagree.
  * `get_databricks_token()` — convenience for callers that only want the
    token (used by `_connect(None)` in the CLI/standalone path).
  * `get_auth_source()` — FAST-PATH: returns the source WITHOUT minting an
    OAuth token. Used by startup logging and by the CLI to label the mode
    before running a command. Reading env vars is cheap; minting a U2M token
    can open a browser, which we don't want for logging.
"""

import os
from enum import Enum


class AuthSource(str, Enum):
    """Which path produced the current token — used for friendly error messages."""

    BEARER = "bearer"
    OAUTH = "oauth"
    PAT = "pat"


def resolve_token_now() -> tuple[str, AuthSource]:
    """Resolve a Databricks token AND its source in a single traversal.

    Call this from the async tool body BEFORE handing off to `asyncio.to_thread`
    so the FastMCP request's Bearer header (if any) is captured.
    """
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


def get_databricks_token() -> str:
    """Resolve a Databricks access token. OAuth wins over PAT."""
    token, _ = resolve_token_now()
    return token


def get_auth_source() -> AuthSource:
    """Determine the auth source WITHOUT minting an OAuth token (so this
    can't trigger a browser flow). Reads env vars + the FastMCP request
    header only."""
    if _read_bearer_header() is not None:
        return AuthSource.BEARER
    if os.environ.get("DATABRICKS_CLIENT_ID"):
        return AuthSource.OAUTH
    if os.environ.get("DATABRICKS_ACCESS_TOKEN"):
        return AuthSource.PAT
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
