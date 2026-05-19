"""
Token resolution for the GitHub MCP server.

Two distinct flows:

* **JWT mode (multi-tenant deployment).** ``BOND_MCPS_JWT_JWKS_URI`` or
  ``BOND_MCPS_JWT_PUBLIC_KEY`` is set, FastMCP's RemoteAuthProvider validates
  the incoming Authorization JWT before any tool runs, and the resolved user
  identity comes from the JWT's ``sub`` claim. Provider tokens are looked up
  from the encrypted ``tokens.db`` keyed by that user. Missing rows raise
  ``MissingProviderConnection`` which is surfaced to the agent as a structured
  tool error pointing at the per-MCP ``/connect/github`` flow.

* **Single-tenant fallback (laptop).** JWT mode is disabled. Resolution
  matches the historical behaviour: Bearer header takes precedence (legacy
  Bond AI backend path), then local OAuth via ``github.local_auth`` when
  ``GITHUB_CLIENT_ID`` is set.
"""

from __future__ import annotations

import os


def get_github_token() -> str:
    """Resolve a GitHub OAuth access token.

    Raises:
        MissingProviderConnection: JWT mode is on but the user has no stored
            GitHub token.
        PermissionError: laptop mode and no auth source is configured.
    """
    if _jwt_mode_enabled():
        return _resolve_jwt_mode_token()
    return _resolve_legacy_token()


# ---------------------------------------------------------------------------
# JWT mode (multi-tenant)
# ---------------------------------------------------------------------------


def _resolve_jwt_mode_token() -> str:
    from auth import MissingProviderConnection, TokenStore, resolve_user_key_for_request

    user_key = resolve_user_key_for_request()
    cached = TokenStore("github", user_key=user_key).get_token()
    if cached and cached.get("access_token"):
        return cached["access_token"]

    raise MissingProviderConnection(
        provider="github",
        user_key=user_key,
        connect_url=_build_connect_url(user_key=user_key),
    )


def _build_connect_url(*, user_key: str) -> str | None:
    """Mint a one-shot ticket bound to the JWT-identified user and embed it
    in the MCP's public /connect/github URL. The agent surfaces this URL
    so the user can complete provider OAuth in a browser."""
    public_url = (os.environ.get("BOND_MCPS_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return None
    try:
        from auth.connect_tickets import mint_ticket

        ticket = mint_ticket(user_key=user_key, provider="github")
    except Exception:  # nosec B110 — best-effort; fall back to a path-only URL
        return f"{public_url}/connect/github"
    return f"{public_url}/connect/github?ticket={ticket}"


# ---------------------------------------------------------------------------
# Single-tenant fallback (laptop / CLI)
# ---------------------------------------------------------------------------


def _resolve_legacy_token() -> str:
    # Path 1: Bearer header (legacy Bond AI backend path)
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization"})
        auth = headers.get("authorization")
        if auth and auth.startswith("Bearer "):
            return auth[7:]
    except Exception:  # nosec B110
        pass  # outside HTTP request context (stdio transport, CLI)

    # Path 2: Local OAuth
    if os.environ.get("GITHUB_CLIENT_ID"):
        from github.local_auth import get_local_token

        return get_local_token()

    # Path 3: nothing left
    raise PermissionError(
        "GitHub authorization required. For standalone use, set GITHUB_CLIENT_ID "
        "(and GITHUB_CLIENT_SECRET) and run `make login-github`. For backend mode "
        "(e.g. Bond AI), ensure the backend forwards an Authorization: Bearer header."
    )


def _jwt_mode_enabled() -> bool:
    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
