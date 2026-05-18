"""
Local OAuth 2.0 authentication for standalone GitHub use (Claude Code, CLI).

Uses the shared OAuth callback proxy for browser-based authorization code flow.
GitHub tokens are long-lived (no refresh token, no expiry), so token caching
is simple — just store the access_token until the user revokes it.

Requires the shared auth proxy to be running:
  cd auth && poetry run python -m auth
"""

import base64
import hashlib
import logging
import os
import secrets
import webbrowser
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # nosec B105
DEVICE_CODE_URL = "https://github.com/login/device/code"

SCOPES = "repo user read:org"


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    challenge_b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    return verifier, challenge_b64


def get_local_token() -> str:
    """
    Acquire a GitHub access token using local OAuth.

    Resolution order:
    1. Cached token (via TokenStore) — verified against GitHub API
    2. Browser-based authorization code flow (via shared proxy)
    3. Device code flow fallback

    Raises:
        PermissionError: If auth fails or GITHUB_CLIENT_ID is not set.
    """
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    if not client_id:
        raise PermissionError(
            "GITHUB_CLIENT_ID environment variable is required for local authentication."
        )
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")

    from auth import TokenStore, resolve_user_key_for_request
    store = TokenStore("github", user_key=resolve_user_key_for_request())

    # 1. Try cached token
    cached = store.get_token()
    if cached and cached.get("access_token"):
        token = cached["access_token"]
        if _verify_token(token):
            return token
        logger.info("Cached GitHub token is invalid, re-authenticating")

    # 2. Try browser auth via shared proxy
    token = _do_browser_auth(client_id, client_secret)
    if token:
        store.save_token({"access_token": token})
        return token

    # 3. Fallback to device code flow
    token = _do_device_code_auth(client_id)
    if token:
        store.save_token({"access_token": token})
        return token

    raise PermissionError(
        "GitHub authentication failed. Could not acquire a token via "
        "browser or device code flow."
    )


def _verify_token(token: str) -> bool:
    """Check if a GitHub token is still valid by hitting /user."""
    try:
        with httpx.Client(timeout=10.0) as http:
            resp = http.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _do_browser_auth(client_id: str, client_secret: str) -> str | None:
    """Run OAuth2 auth code + PKCE flow via shared proxy."""
    try:
        from auth import AuthStateExpiredError, OAuthProxyClient
        proxy = OAuthProxyClient()
        proxy.check_proxy()
    except (RuntimeError, ImportError) as e:
        logger.warning("Auth proxy not available: %s", e)
        print(
            "\nAuth proxy is not running. Start it with:\n"
            "  cd auth && poetry run python -m auth\n"
            "Falling back to device code flow...\n",
            flush=True,
        )
        return None

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _generate_pkce()
    redirect_uri = proxy.get_redirect_uri("github")

    auth_params = {
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    try:
        proxy.register_auth(state, "github")
    except Exception:
        logger.exception("Failed to register auth with proxy")
        return None

    logger.info("Opening browser for GitHub login...")
    print(
        f"\nOpening browser for GitHub login...\n"
        f"If the browser doesn't open, visit:\n{auth_url}\n",
        flush=True,
    )
    webbrowser.open(auth_url)

    try:
        callback_result = proxy.wait_for_callback(state, timeout=120)
    except AuthStateExpiredError:
        # Subclass of TimeoutError — must come first
        logger.warning("GitHub browser auth state expired or already consumed")
        print(
            "Browser login session expired or was already used. "
            "Trying device code...",
            flush=True,
        )
        return None
    except (TimeoutError, RuntimeError) as e:
        logger.warning("GitHub browser auth failed: %s", e)
        print("Browser login failed. Trying device code...", flush=True)
        return None

    if "code" not in callback_result:
        error = callback_result.get("error", "unknown")
        logger.warning("GitHub browser auth failed: %s", error)
        return None

    if callback_result.get("state") != state:
        logger.warning("State mismatch in GitHub callback")
        return None

    # Exchange code for token
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
) -> str | None:
    """Exchange authorization code for an access token."""
    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
            if not resp.is_success:
                logger.error("GitHub token exchange failed: HTTP %d", resp.status_code)
                return None
            data = resp.json()
            if "error" in data:
                logger.error("GitHub token exchange error: %s", data.get("error", "unknown"))
                return None
            return data.get("access_token")
    except Exception:
        logger.exception("GitHub token exchange request failed")
        return None


def _do_device_code_auth(client_id: str) -> str | None:
    """Device code flow fallback — prints a code for the user to enter."""
    import time

    try:
        with httpx.Client(timeout=30.0) as http:
            resp = http.post(
                DEVICE_CODE_URL,
                data={"client_id": client_id, "scope": SCOPES},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            device_data = resp.json()
    except Exception:
        logger.exception("Device code initiation failed")
        return None

    user_code = device_data.get("user_code")
    verification_uri = device_data.get("verification_uri")
    device_code = device_data.get("device_code")
    interval = device_data.get("interval", 5)
    expires_in = device_data.get("expires_in", 900)

    print(
        f"\nTo sign in, visit: {verification_uri}\n"
        f"Enter code: {user_code}\n"
        f"Waiting for authorization (expires in {expires_in}s)...",
        flush=True,
    )

    deadline = time.time() + expires_in
    try:
        with httpx.Client(timeout=30.0) as http:
            while time.time() < deadline:
                time.sleep(interval)
                resp = http.post(
                    TOKEN_URL,
                    data={
                        "client_id": client_id,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                token_data = resp.json()

                error = token_data.get("error")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    interval = token_data.get("interval", interval + 5)
                    continue
                elif error:
                    logger.error("Device code auth failed: %s", error)
                    return None

                return token_data.get("access_token")
    except Exception:
        logger.exception("Device code polling failed")
        return None

    logger.warning("Device code auth timed out")
    return None
