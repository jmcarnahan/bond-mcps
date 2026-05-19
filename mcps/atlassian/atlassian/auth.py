"""
Token and cloud-ID resolution for the Atlassian MCP server.

Two distinct flows:

* **JWT mode (multi-tenant deployment).** ``BOND_MCPS_JWT_JWKS_URI`` or
  ``BOND_MCPS_JWT_PUBLIC_KEY`` is set, FastMCP's RemoteAuthProvider validates
  the incoming Authorization JWT, and the user's stored Atlassian tokens
  come from the encrypted ``tokens.db`` row keyed by the JWT's ``sub``.
  Atlassian saves both the access token and the discovered cloud_id in that
  row, so a single lookup serves both ``get_atlassian_token`` and
  ``get_cloud_id``. A missing row raises ``MissingProviderConnection`` which
  surfaces a ``/connect/atlassian`` URL to the agent.

* **Single-tenant fallback (laptop).** JWT mode is disabled. The historical
  resolution applies: Bearer + X-Atlassian-Cloud-Id headers (legacy Bond AI
  backend path), then local OAuth via ``atlassian.local_auth`` when
  ``ATLASSIAN_CLIENT_ID`` is set.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def get_atlassian_token() -> str:
    if _jwt_mode_enabled():
        return _get_jwt_mode_data()[0]
    return _resolve_legacy_token()


def get_cloud_id() -> str:
    if _jwt_mode_enabled():
        return _get_jwt_mode_data()[1]
    return _resolve_legacy_cloud_id()


# ---------------------------------------------------------------------------
# JWT mode (multi-tenant)
# ---------------------------------------------------------------------------


def _get_jwt_mode_data() -> tuple[str, str]:
    """Return (access_token, cloud_id) for the JWT-identified user."""
    from auth import MissingProviderConnection, TokenStore, resolve_user_key_for_request

    user_key = resolve_user_key_for_request()
    store = TokenStore("atlassian", user_key=user_key)

    # Auto-refresh expired tokens using the stored refresh_token, so the
    # user doesn't have to re-do /connect/atlassian every hour.
    client_id = (os.environ.get("ATLASSIAN_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("ATLASSIAN_CLIENT_SECRET") or "").strip()
    if client_id and client_secret:
        store.refresh_if_needed(
            client_id,
            client_secret,
            "https://auth.atlassian.com/oauth/token",
        )

    cached = store.get_token()
    token = cached.get("access_token") if cached else None
    cloud_id = cached.get("cloud_id") if cached else None
    if token and cloud_id:
        return token, cloud_id

    raise MissingProviderConnection(
        provider="atlassian",
        user_key=user_key,
        connect_url=_build_connect_url(user_key=user_key),
    )


def _build_connect_url(*, user_key: str) -> str | None:
    public_url = (os.environ.get("BOND_MCPS_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return None
    try:
        from auth.connect_tickets import mint_ticket

        ticket = mint_ticket(user_key=user_key, provider="atlassian")
    except Exception:  # nosec B110
        return f"{public_url}/connect/atlassian"
    return f"{public_url}/connect/atlassian?ticket={ticket}"


# ---------------------------------------------------------------------------
# Single-tenant fallback (laptop / CLI)
# ---------------------------------------------------------------------------


def _resolve_legacy_token() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization"})
        auth = headers.get("authorization")
        if auth and auth.startswith("Bearer "):
            return auth[7:]
    except Exception:  # nosec B110
        pass

    if os.environ.get("ATLASSIAN_CLIENT_ID"):
        from atlassian.local_auth import get_local_token_and_cloud_id

        token, _ = get_local_token_and_cloud_id()
        return token

    raise PermissionError(
        "Atlassian authorization required. For standalone use, set "
        "ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET and run "
        "`make login-atlassian`. For backend mode (e.g. Bond AI), ensure the "
        "backend forwards Authorization: Bearer and X-Atlassian-Cloud-Id headers."
    )


def _resolve_legacy_cloud_id() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"x-atlassian-cloud-id"})
        cloud_id = headers.get("x-atlassian-cloud-id")
        if cloud_id:
            return cloud_id
    except Exception:  # nosec B110
        pass

    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID")
    if cloud_id:
        return cloud_id

    if os.environ.get("ATLASSIAN_CLIENT_ID"):
        from atlassian.local_auth import get_local_token_and_cloud_id

        _, cloud_id = get_local_token_and_cloud_id()
        return cloud_id

    raise PermissionError(
        "Atlassian Cloud ID required. For standalone use, run `make login-atlassian` "
        "(the OAuth flow auto-discovers cloud_id) or pin one via the "
        "ATLASSIAN_CLOUD_ID environment variable. For backend mode, ensure the "
        "backend sends the X-Atlassian-Cloud-Id header."
    )


def _jwt_mode_enabled() -> bool:
    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
