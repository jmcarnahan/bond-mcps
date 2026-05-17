"""
Local MSAL authentication for standalone use (Claude Code, CLI).

Provides browser-based authorization code + PKCE flow with device code fallback.
Shared between the MCP server (when no Bearer header is present) and the CLI.

Browser auth requires the shared OAuth callback proxy (auth package)
to be running. Start it with: cd auth && poetry run python -m auth
"""

import logging
import os
import stat
import webbrowser
from pathlib import Path

import msal

logger = logging.getLogger(__name__)

TOKEN_CACHE_PATH = Path.home() / ".ms_graph_tokens.json"

POWERBI_SCOPES = [
    "https://analysis.windows.net/powerbi/api/.default",
]

MAIL_SCOPES = [
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "MailboxSettings.Read",
    "User.Read",
]

TEAMS_SCOPES = [
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Send",
    "ChannelMessage.Read.All",
    "Chat.ReadWrite",
]

FILES_SCOPES = [
    "Files.ReadWrite.All",
]

SITES_SCOPES = [
    "Sites.ReadWrite.All",
]

CALENDAR_SCOPES = [
    "Calendars.ReadWrite",
]


def _get_scopes() -> list[str]:
    """Return scopes based on whether a tenant ID is set (org vs consumer)."""
    scopes = MAIL_SCOPES + FILES_SCOPES + CALENDAR_SCOPES
    if os.environ.get("MS_TENANT_ID"):
        scopes += SITES_SCOPES + TEAMS_SCOPES
    return scopes


def _load_token_cache() -> msal.SerializableTokenCache:
    """Load the MSAL token cache from disk."""
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def _save_token_cache(cache: msal.SerializableTokenCache) -> None:
    """Save cache to disk with 0600 permissions."""
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())
        TOKEN_CACHE_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _create_msal_app(
    client_id: str, cache: msal.SerializableTokenCache
) -> msal.ClientApplication:
    """Create an MSAL app — Confidential if MS_CLIENT_SECRET is set, else Public."""
    authority = (
        f"https://login.microsoftonline.com/"
        f"{os.environ.get('MS_TENANT_ID', 'consumers')}"
    )
    client_secret = os.environ.get("MS_CLIENT_SECRET")
    if client_secret:
        return msal.ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=authority,
            token_cache=cache,
        )
    return msal.PublicClientApplication(
        client_id, authority=authority, token_cache=cache,
    )


def _acquire_token_browser(
    app: msal.ClientApplication, scopes: list[str]
) -> dict | None:
    """
    Authorization code flow with PKCE using the shared OAuth callback proxy.

    Requires the proxy to be running. Returns None if the proxy is unavailable
    (falls through to device code in get_local_token).
    """
    try:
        from auth import OAuthProxyClient
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

    try:
        return _acquire_token_via_proxy(app, scopes, proxy)
    except Exception:
        logger.exception("Proxy auth flow failed")
        return None


def _acquire_token_via_proxy(
    app: msal.ClientApplication,
    scopes: list[str],
    proxy: "OAuthProxyClient",
) -> dict | None:
    """Browser auth using the shared OAuth callback proxy."""
    from auth import AuthStateExpiredError

    redirect_uri = proxy.get_redirect_uri("microsoft")

    flow = app.initiate_auth_code_flow(
        scopes=scopes,
        redirect_uri=redirect_uri,
    )

    if "auth_uri" not in flow:
        logger.warning("Failed to initiate auth code flow: %s", flow.get("error", "unknown"))
        return None

    state = flow.get("state", "")
    proxy.register_auth(state, "microsoft")

    auth_url = flow["auth_uri"]
    logger.info("Opening browser for Microsoft login...")
    print(
        f"\nOpening browser for Microsoft login...\n"
        f"If the browser doesn't open, visit:\n{auth_url}\n",
        flush=True,
    )
    webbrowser.open(auth_url)

    try:
        callback_result = proxy.wait_for_callback(state, timeout=120)
    except AuthStateExpiredError:
        # Subclass of TimeoutError — must come first
        logger.warning("Browser auth state expired or already consumed")
        print(
            "Browser login session expired or was already used. "
            "Trying device code...",
            flush=True,
        )
        return None
    except TimeoutError:
        logger.warning("Browser auth timed out")
        print("Browser login timed out. Trying device code...", flush=True)
        return None

    if "code" not in callback_result:
        error = callback_result.get("error", "unknown")
        logger.warning("Browser auth failed: %s", error)
        print(f"Browser login not completed ({error}). Trying device code...",
              flush=True)
        return None

    result = app.acquire_token_by_auth_code_flow(flow, callback_result)
    if "access_token" not in result:
        logger.warning(
            "MSAL token exchange failed: %s",
            result.get("error", "unknown"),
        )
    return result if "access_token" in result else None


def _acquire_token_device_code(
    app: msal.ClientApplication, scopes: list[str]
) -> dict | None:
    """Device code flow fallback -- prints a code for the user to enter."""
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        logger.error("Device flow initiation failed: %s", flow.get("error", "unknown"))
        return None

    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    return result if "access_token" in result else None


def get_local_token() -> str:
    """
    Acquire a Microsoft Graph access token using local MSAL auth.

    Resolution order:
    1. Cached token (acquire_token_silent)
    2. Browser-based authorization code + PKCE flow (via shared proxy)
    3. Device code flow fallback

    Raises:
        PermissionError: If all auth methods fail or MS_CLIENT_ID is not set.
    """
    client_id = os.environ.get("MS_CLIENT_ID")
    if not client_id:
        raise PermissionError(
            "MS_CLIENT_ID environment variable is required for local authentication."
        )

    cache = _load_token_cache()
    app = _create_msal_app(client_id, cache)
    scopes = _get_scopes()

    # 1. Try silent (cached)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if result and "access_token" in result:
            _save_token_cache(cache)
            return result["access_token"]

    # 2. Try browser auth code + PKCE (requires shared proxy)
    result = _acquire_token_browser(app, scopes)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    # 3. Fallback to device code
    result = _acquire_token_device_code(app, scopes)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    raise PermissionError(
        "Microsoft authentication failed. Could not acquire a token via "
        "browser or device code flow."
    )


def get_local_powerbi_token() -> str:
    """
    Acquire a Power BI access token using local MSAL auth.

    Uses the same app registration and token cache as get_local_token() but
    requests the Power BI resource scope instead of Graph scopes.

    Resolution order:
    1. Cached token (acquire_token_silent)
    2. Browser-based authorization code + PKCE flow (via shared proxy)
    3. Device code flow fallback
    """
    client_id = os.environ.get("MS_CLIENT_ID")
    if not client_id:
        raise PermissionError(
            "MS_CLIENT_ID environment variable is required for local authentication."
        )

    cache = _load_token_cache()
    app = _create_msal_app(client_id, cache)

    # 1. Try silent (cached)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(POWERBI_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_token_cache(cache)
            return result["access_token"]

    # 2. Try browser auth code + PKCE (requires shared proxy)
    result = _acquire_token_browser(app, POWERBI_SCOPES)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    # 3. Fallback to device code
    result = _acquire_token_device_code(app, POWERBI_SCOPES)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    raise PermissionError(
        "Power BI authentication failed. Could not acquire a token via "
        "browser or device code flow."
    )
