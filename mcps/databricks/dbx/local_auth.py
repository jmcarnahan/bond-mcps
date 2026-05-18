"""
Local OAuth 2.0 (U2M, authorization code + PKCE) for standalone Databricks use.

Uses the shared OAuth callback proxy for browser-based auth. Token caching
and refresh via auth.TokenStore. Databricks OAuth endpoints are workspace-
specific, so DATABRICKS_HOST must be set before auth begins.

Confidential and public OAuth apps are both supported — DATABRICKS_CLIENT_SECRET
is included in the token exchange only when set.
"""

import base64
import hashlib
import logging
import os
import secrets
import time
import webbrowser
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

SCOPES = "sql offline_access"


def _normalize_host(host: str) -> str:
    """Return host with scheme, no trailing slash. Adds https:// if missing."""
    host = host.strip().rstrip("/")
    if not host:
        return ""
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


def host_without_scheme(host: str) -> str:
    """Return bare hostname (no scheme, no path) — what the SQL connector wants."""
    normalized = _normalize_host(host)
    parsed = urlparse(normalized)
    return parsed.netloc or normalized


def _auth_endpoints(host: str) -> tuple[str, str]:
    """Return (auth_url, token_url) for the workspace OAuth endpoints."""
    base = _normalize_host(host)
    return f"{base}/oidc/v1/authorize", f"{base}/oidc/v1/token"


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    challenge_b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    return verifier, challenge_b64


def get_local_token() -> str:
    """
    Acquire a Databricks OAuth U2M access token.

    Resolution order:
    1. Cached token (via TokenStore) — refresh if expired and a refresh_token
       is available
    2. Browser PKCE flow via shared proxy

    Returns:
        Access token string.

    Raises:
        PermissionError: If required env vars are missing or auth fails.
    """
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    if not client_id:
        raise PermissionError(
            "DATABRICKS_CLIENT_ID environment variable is required for local "
            "OAuth authentication."
        )
    host = os.environ.get("DATABRICKS_HOST")
    if not host:
        raise PermissionError(
            "DATABRICKS_HOST environment variable is required (workspace URL, "
            "e.g. https://dbc-12345-abcd.cloud.databricks.com)."
        )
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET") or None
    _, token_url = _auth_endpoints(host)

    from auth import TokenStore, resolve_user_key_for_request
    store = TokenStore("databricks", user_key=resolve_user_key_for_request())

    # 1. Cached token (with refresh)
    # TokenStore.refresh_if_needed requires a non-None client_secret arg; pass
    # empty string for public apps — Databricks token endpoint ignores it when
    # the app is registered as public.
    token = store.refresh_if_needed(client_id, client_secret or "", token_url)
    if token:
        return token

    # 2. Full browser flow
    token_data = _do_browser_auth(client_id, client_secret, host)
    if not token_data or "access_token" not in token_data:
        raise PermissionError(
            "Databricks authentication failed. Could not acquire a token."
        )

    if "expires_in" in token_data:
        token_data["expires_at"] = time.time() + token_data["expires_in"]
    store.save_token(token_data)

    return token_data["access_token"]


def _do_browser_auth(
    client_id: str, client_secret: str | None, host: str
) -> dict | None:
    """Run OAuth2 authorization code + PKCE flow via the shared proxy."""
    from auth import AuthStateExpiredError, OAuthProxyClient

    proxy = OAuthProxyClient()
    try:
        proxy.check_proxy()
    except RuntimeError as e:
        logger.error("Auth proxy not available: %s", e)
        print(str(e), flush=True)
        return None

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce()
    redirect_uri = proxy.get_redirect_uri("databricks")
    auth_url, _ = _auth_endpoints(host)

    auth_params = {
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    full_auth_url = f"{auth_url}?{urlencode(auth_params)}"

    try:
        proxy.register_auth(state, "databricks")
    except Exception:
        logger.exception("Failed to register auth with proxy")
        return None

    logger.info("Opening browser for Databricks login...")
    print(
        f"\nOpening browser for Databricks login...\n"
        f"If the browser doesn't open, visit:\n{full_auth_url}\n",
        flush=True,
    )
    webbrowser.open(full_auth_url)

    try:
        callback_result = proxy.wait_for_callback(state, timeout=120)
    except AuthStateExpiredError:
        logger.warning("Databricks browser auth state expired or already consumed")
        print(
            "Browser login session expired or was already used. Please retry.",
            flush=True,
        )
        return None
    except (TimeoutError, RuntimeError) as e:
        logger.warning("Databricks browser auth failed: %s", e)
        print("Browser login failed.", flush=True)
        return None

    if "code" not in callback_result:
        error = callback_result.get("error", "unknown")
        logger.warning("Databricks browser auth failed: %s", error)
        return None

    if callback_result.get("state") != state:
        logger.warning("State mismatch in Databricks callback")
        return None

    return _exchange_code(
        client_id, client_secret,
        callback_result["code"], redirect_uri, code_verifier, host,
    )


def _exchange_code(
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    host: str,
) -> dict | None:
    """Exchange the authorization code for an access (+ refresh) token."""
    _, token_url = _auth_endpoints(host)
    body = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    # Confidential apps include the secret; public apps must omit it entirely
    # rather than send an empty string (some IdPs reject the latter).
    if client_secret:
        body["client_secret"] = client_secret

    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                token_url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            if not resp.is_success:
                logger.error(
                    "Databricks token exchange failed: HTTP %d %s",
                    resp.status_code, resp.text[:200],
                )
                return None
            return resp.json()
    except Exception:
        logger.exception("Databricks token exchange request failed")
        return None
