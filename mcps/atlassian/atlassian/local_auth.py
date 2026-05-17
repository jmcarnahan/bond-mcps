"""
Local OAuth 2.0 authentication for standalone Atlassian use (Claude Code, CLI).

Uses the shared OAuth callback proxy for browser-based authorization code + PKCE
flow. Token caching and refresh via auth.TokenStore.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import webbrowser
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"  # nosec B105
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

SCOPES = [
    # Jira — classic scopes (required by Jira REST API v3)
    "read:jira-user",
    "read:jira-work",
    "write:jira-work",
    "manage:jira-project",
    # Confluence — granular scopes (v2 API for pages/spaces; v1 content/search still works)
    "read:content:confluence",
    "read:content-details:confluence",
    "write:content:confluence",
    "read:space:confluence",
    "read:space-details:confluence",
    "read:page:confluence",
    "write:page:confluence",
    "offline_access",
]


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    challenge_b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    return verifier, challenge_b64


def get_local_token_and_cloud_id() -> tuple[str, str]:
    """
    Acquire Atlassian access token and cloud ID using local OAuth.

    Resolution order:
    1. Cached token (via TokenStore) — refresh if expired
    2. Full browser OAuth2 flow via shared proxy

    Returns:
        Tuple of (access_token, cloud_id).

    Raises:
        PermissionError: If auth fails or required env vars missing.
    """
    client_id = os.environ.get("ATLASSIAN_CLIENT_ID")
    client_secret = os.environ.get("ATLASSIAN_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise PermissionError(
            "ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET environment variables "
            "are required for local authentication."
        )

    from auth import TokenStore
    store = TokenStore("atlassian")

    # 1. Try cached token (with refresh)
    token = store.refresh_if_needed(client_id, client_secret, TOKEN_URL)
    if token:
        cached = store.get_token()
        cloud_id = (cached or {}).get("cloud_id") or os.environ.get("ATLASSIAN_CLOUD_ID")
        if cloud_id:
            return token, cloud_id

    # 2. Full browser flow
    token_data = _do_browser_auth(client_id, client_secret)
    if not token_data or "access_token" not in token_data:
        raise PermissionError(
            "Atlassian authentication failed. Could not acquire a token."
        )

    # Discover cloud ID
    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID")
    if not cloud_id:
        cloud_id = _discover_cloud_id(token_data["access_token"])

    # Save with cloud_id for future use
    token_data["cloud_id"] = cloud_id
    if "expires_in" in token_data:
        token_data["expires_at"] = time.time() + token_data["expires_in"]
    store.save_token(token_data)

    return token_data["access_token"], cloud_id


def _do_browser_auth(client_id: str, client_secret: str) -> dict | None:
    """Run OAuth2 auth code + PKCE flow via shared proxy."""
    from auth import OAuthProxyClient

    proxy = OAuthProxyClient()
    try:
        proxy.check_proxy()
    except RuntimeError as e:
        logger.error("Auth proxy not available: %s", e)
        print(str(e), flush=True)
        return None

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce()
    redirect_uri = proxy.get_redirect_uri("atlassian")

    auth_params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    try:
        proxy.register_auth(state, "atlassian")
    except Exception:
        logger.exception("Failed to register auth with proxy")
        return None

    logger.info("Opening browser for Atlassian login...")
    print(
        f"\nOpening browser for Atlassian login...\n"
        f"If the browser doesn't open, visit:\n{auth_url}\n",
        flush=True,
    )
    webbrowser.open(auth_url)

    try:
        callback_result = proxy.wait_for_callback(state, timeout=120)
    except (TimeoutError, RuntimeError) as e:
        logger.warning("Atlassian browser auth failed: %s", e)
        print("Browser login failed.", flush=True)
        return None

    if "code" not in callback_result:
        error = callback_result.get("error", "unknown")
        logger.warning("Atlassian browser auth failed: %s", error)
        return None

    if callback_result.get("state") != state:
        logger.warning("State mismatch in Atlassian callback")
        return None

    # Exchange code for tokens
    return _exchange_code(
        client_id, client_secret,
        callback_result["code"], redirect_uri, code_verifier,
    )


def _exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict | None:
    """Exchange authorization code for tokens."""
    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
            if not resp.is_success:
                logger.error("Token exchange failed: HTTP %d", resp.status_code)
                return None
            return resp.json()
    except Exception:
        logger.exception("Token exchange request failed")
        return None


def _discover_cloud_id(access_token: str) -> str:
    """Fetch accessible resources and return the cloud ID."""
    with httpx.Client(timeout=30.0) as http:
        resp = http.get(
            RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        resources = resp.json()

    if not resources:
        raise PermissionError(
            "No Atlassian cloud sites found. Ensure your account has access "
            "to at least one Atlassian site."
        )

    # Deduplicate by cloud ID — Atlassian returns one entry per product (Jira + Confluence)
    seen = {}
    for r in resources:
        seen[r["id"]] = r
    unique = list(seen.values())

    if len(unique) == 1:
        return unique[0]["id"]

    # Genuinely multiple sites — need ATLASSIAN_CLOUD_ID env var
    site_names = [f"  {r.get('name', '?')} (id: {r['id']})" for r in unique]
    raise PermissionError(
        f"Multiple Atlassian sites found. Set ATLASSIAN_CLOUD_ID to one of:\n"
        + "\n".join(site_names)
    )
