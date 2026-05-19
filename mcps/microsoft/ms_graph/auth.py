"""
Token resolution for the Microsoft Graph MCP server.

Two distinct flows:

* **JWT mode (multi-tenant deployment).** ``BOND_MCPS_JWT_JWKS_URI`` or
  ``BOND_MCPS_JWT_PUBLIC_KEY`` is set, FastMCP's RemoteAuthProvider validates
  the incoming Authorization JWT, and per-user provider tokens are looked up
  from the encrypted ``tokens.db`` keyed by the JWT's ``sub`` claim. Missing
  rows raise ``MissingProviderConnection`` which the agent surfaces as a
  structured error pointing at ``/connect/microsoft``.

* **Single-tenant fallback (laptop).** JWT mode is disabled. The historical
  resolution applies: Bearer header (legacy Bond AI backend path), then
  local MSAL auth when ``MS_CLIENT_ID`` is set.

Power BI uses a separate resource (analysis.windows.net) and therefore a
distinct token. Both follow the same mode split.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def get_graph_token() -> str:
    if _jwt_mode_enabled():
        return _resolve_jwt_mode_token(provider="microsoft")
    return _resolve_legacy_graph_token()


def get_powerbi_token() -> str:
    if _jwt_mode_enabled():
        # Power BI tokens are stored under their own provider key so they
        # don't collide with Graph tokens for the same user.
        return _resolve_jwt_mode_token(provider="microsoft_powerbi")
    return _resolve_legacy_powerbi_token()


# ---------------------------------------------------------------------------
# JWT mode (multi-tenant)
# ---------------------------------------------------------------------------


def _resolve_jwt_mode_token(*, provider: str) -> str:
    from auth import MissingProviderConnection, TokenStore, resolve_user_key_for_request

    user_key = resolve_user_key_for_request()
    store = TokenStore(provider, user_key=user_key)

    # Try auto-refresh first. refresh_if_needed() returns the current
    # access_token if not expired, exchanges the refresh_token if expired,
    # or returns None if there is no usable cached token at all (no row,
    # no refresh_token, or upstream refresh failed). Saves the user from
    # re-doing the OAuth flow every hour.
    client_id = (os.environ.get("MS_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("MS_CLIENT_SECRET") or "").strip()
    if client_id:
        tenant = (os.environ.get("MS_TENANT_ID") or "").strip() or "consumers"
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        token = store.refresh_if_needed(client_id, client_secret, token_url)
        if token:
            return token

    raise MissingProviderConnection(
        provider=provider,
        user_key=user_key,
        connect_url=_build_connect_url(provider=provider, user_key=user_key),
    )


def _build_connect_url(*, provider: str, user_key: str) -> str | None:
    """Mint a ticket bound to the JWT-identified user and embed it in the
    MCP's public /connect/microsoft URL. The agent surfaces this URL so the
    user can complete Microsoft OAuth in a browser.

    Power BI shares the same /connect/microsoft endpoint — both store under
    different provider keys but bootstrap via the same OAuth app + flow.
    The ticket binds to the requesting JWT's sub, so cross-provider reuse
    is gated by user identity, not by which tool surfaced the error.
    """
    public_url = (os.environ.get("BOND_MCPS_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return None
    try:
        from auth.connect_tickets import mint_ticket

        # Use the same ticket-provider name as the /connect route so it
        # matches the registration in ms_graph_mcp.py.
        ticket_provider = "microsoft" if provider == "microsoft_powerbi" else provider
        ticket = mint_ticket(user_key=user_key, provider=ticket_provider)
    except Exception:  # nosec B110 — best-effort; fall back to a path-only URL
        return f"{public_url}/connect/{provider}"
    ticket_provider = "microsoft" if provider == "microsoft_powerbi" else provider
    return f"{public_url}/connect/{ticket_provider}?ticket={ticket}"


# ---------------------------------------------------------------------------
# Single-tenant fallback (laptop / CLI)
# ---------------------------------------------------------------------------


def _resolve_legacy_graph_token() -> str:
    bearer = _read_bearer_header()
    if bearer is not None:
        return bearer

    if os.environ.get("MS_CLIENT_ID"):
        from ms_graph.local_auth import get_local_token

        return get_local_token()

    raise PermissionError(
        "Microsoft authorization required. For standalone use, set MS_CLIENT_ID "
        "(plus MS_CLIENT_SECRET if confidential) and run `make login-microsoft`. "
        "For backend mode (e.g. Bond AI), ensure the backend forwards an "
        "Authorization: Bearer header."
    )


def _resolve_legacy_powerbi_token() -> str:
    bearer = _read_bearer_header()
    if bearer is not None:
        return bearer

    if os.environ.get("MS_CLIENT_ID"):
        from ms_graph.local_auth import get_local_powerbi_token

        return get_local_powerbi_token()

    raise PermissionError(
        "Power BI authorization required. For standalone use, set MS_CLIENT_ID "
        "(plus MS_CLIENT_SECRET if confidential) and run `make login-microsoft` — "
        "Power BI uses a separate PBI-scoped token from Graph. For backend mode, "
        "ensure the backend forwards an Authorization: Bearer header with a PBI token."
    )


def _read_bearer_header() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include={"authorization"})
    except Exception:  # nosec B110
        return None
    auth = headers.get("authorization") if headers else None
    if auth and auth.startswith("Bearer "):
        return auth[7:]
    return None


def _jwt_mode_enabled() -> bool:
    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
