"""
Local MSAL authentication for standalone use (Claude Code, CLI).

Provides browser-based authorization code + PKCE flow with device code fallback.
Shared between the MCP server (when no Bearer header is present) and the CLI.

Browser auth requires the shared OAuth callback proxy (auth package)
to be running. Start it with: cd auth && poetry run python -m auth
"""

import logging
import os
import webbrowser

import msal

logger = logging.getLogger(__name__)

POWERBI_SCOPES = [
    "https://analysis.windows.net/powerbi/api/.default",
]

MAIL_SCOPES = [
    "Mail.Read",
    "Mail.Read.Shared",
    "Mail.ReadWrite",
    "Mail.ReadWrite.Shared",
    "Mail.Send",
    "Mail.Send.Shared",
    "MailboxSettings.ReadWrite",
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

# What the org tenant's admin has actually consented for this registration.
#
# Entra evaluates a consent request as ONE bundle: a single admin-gated scope
# in it (Chat.ReadWrite, ChannelMessage.Read.All, Sites.ReadWrite.All, ...)
# walls the ENTIRE sign-in behind "Approval required" — including the mail
# scopes the user could have granted alone. So the default org request must be
# exactly the consented set, not the wish-list. The scope groups above stay as
# the documented menu; widening is a config change (MS_SCOPES), not a code
# change, the day the admin approves more.
CONSENTED_ORG_SCOPES = [
    "Mail.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "MailboxSettings.Read",
    "User.Read",
    "Files.Read.All",
]


def login_scopes() -> list[str]:
    """The scopes a sign-in requests.

    ``MS_SCOPES`` (space-separated) wins outright when set — that is the
    escape hatch for a tenant whose admin has approved more, and for tests.
    Otherwise an org tenant gets the consented set above, and a consumer
    account — where admin consent does not exist and nothing can wall the
    request — keeps the full mail/files/calendar feature set.

    A tool whose scope is not in the request simply gets a Graph 403 when
    called; requesting less never breaks the server, while requesting too
    much can make sign-in impossible.
    """
    env = (os.environ.get("MS_SCOPES") or "").split()
    if env:
        return env
    if os.environ.get("MS_TENANT_ID"):
        return list(CONSENTED_ORG_SCOPES)
    return MAIL_SCOPES + FILES_SCOPES + CALENDAR_SCOPES


def _get_repo():
    """Lazy-import to avoid pulling SQLAlchemy into auth.proxy_server."""
    from auth.db.repository import TokenRepository

    return TokenRepository()


def _user_key() -> str:
    """Resolve the user_key for the current MSAL operation.

    Inside an HTTP request, this honours the per-request identity JWT when
    BOND_MCPS_JWT_PUBLIC_KEY is set (multi-tenant mode). Outside a request
    context (CLIs, tests) it falls back to the env-based user_key — the
    resolver handles both transparently.
    """
    from auth.token_store import resolve_user_key_for_request

    return resolve_user_key_for_request()


def _load_token_cache() -> msal.SerializableTokenCache:
    """Load the MSAL token cache from the encrypted DB.

    Used for the interactive auth paths (browser, device code) which mint
    fresh access + refresh tokens and are not race-sensitive. The silent
    acquisition path uses ``_try_silent_under_lock`` instead, which spans
    the read-mutate-write under a single row lock to prevent two processes
    from invalidating each other's refresh tokens.
    """
    cache = msal.SerializableTokenCache()
    blob = _get_repo().get_msal_cache(_user_key())
    if blob:
        cache.deserialize(blob)
    return cache


def _save_token_cache(cache: msal.SerializableTokenCache) -> None:
    """Persist the MSAL cache to the encrypted DB if it has changed.

    ``TokenRepository.save_msal_cache`` itself acquires a row-level write
    lock, but this is only safe for paths that don't depend on a prior
    READ of the same row (i.e., interactive flows). For the silent path,
    use ``_try_silent_under_lock`` instead.
    """
    if cache.has_state_changed:
        _get_repo().save_msal_cache(_user_key(), cache.serialize())


def _try_silent_under_lock(client_id: str, scopes: list[str]) -> str | None:
    """Attempt MSAL silent acquisition while holding the MSAL cache row lock.

    Returns the access_token on success, or None to signal the caller should
    fall through to interactive (browser / device code) flows.

    The lock spans:
      1. Read the encrypted cache blob.
      2. MSAL acquire_token_silent (may rotate refresh_token in-memory).
      3. Write the updated blob back.

    Without this lock, two processes can both read the same blob, both call
    Microsoft with the same refresh_token, and the second call fails because
    the first invalidated the refresh_token on use. The lock serializes the
    silent path so the second caller sees the rotated token.
    """
    repo = _get_repo()
    user_key = _user_key()
    with repo.locked_msal_cache(user_key) as handle:
        cache = msal.SerializableTokenCache()
        if handle.blob:
            cache.deserialize(handle.blob)
        app = _create_msal_app(client_id, cache)
        # EVERY cached account gets a try, not just the first. A long-lived
        # cache accumulates accounts (a consumer MSA from an old sign-in
        # beside the org account), and MSAL orders them arbitrarily. Silent
        # acquisition against the wrong-realm account answers None — and a
        # None here sends the caller to a fresh browser round, whose save
        # ADDS tokens but never reorders the accounts. Pinning to
        # accounts[0] therefore turned one stale account into an interactive
        # sign-in on every single call, forever.
        token: str | None = None
        for account in app.get_accounts():
            result = app.acquire_token_silent(scopes, account=account)
            if result and "access_token" in result:
                token = result["access_token"]
                break
        if cache.has_state_changed:
            handle.set_blob(cache.serialize())
        return token


def _create_msal_app(client_id: str, cache: msal.SerializableTokenCache) -> msal.ClientApplication:
    """Create an MSAL app — Confidential if MS_CLIENT_SECRET is set, else Public."""
    authority = (
        f"https://login.microsoftonline.com/" f"{os.environ.get('MS_TENANT_ID', 'consumers')}"
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
        client_id,
        authority=authority,
        token_cache=cache,
    )


def _acquire_token_browser(app: msal.ClientApplication, scopes: list[str]) -> dict | None:
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
            "Browser login session expired or was already used. " "Trying device code...",
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
        print(f"Browser login not completed ({error}). Trying device code...", flush=True)
        return None

    result = app.acquire_token_by_auth_code_flow(flow, callback_result)
    if "access_token" not in result:
        logger.warning(
            "MSAL token exchange failed: %s",
            result.get("error", "unknown"),
        )
    return result if "access_token" in result else None


def _acquire_token_device_code(app: msal.ClientApplication, scopes: list[str]) -> dict | None:
    """Device code flow fallback -- prints a code for the user to enter."""
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        logger.error("Device flow initiation failed: %s", flow.get("error", "unknown"))
        return None

    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    return result if "access_token" in result else None


def _acquire_token_interactive(client_id: str, scopes: list[str]) -> str | None:
    """Interactive fallback: browser PKCE then device code.

    Done OUTSIDE the MSAL cache lock. These flows mint fresh access +
    refresh tokens (they don't consume an existing refresh_token), so two
    processes racing here is not a correctness problem — each obtains its
    own tokens and the last writer wins on save.
    """
    cache = _load_token_cache()
    app = _create_msal_app(client_id, cache)

    result = _acquire_token_browser(app, scopes)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    result = _acquire_token_device_code(app, scopes)
    if result and "access_token" in result:
        _save_token_cache(cache)
        return result["access_token"]

    return None


def get_local_token() -> str:
    """
    Acquire a Microsoft Graph access token using local MSAL auth.

    Resolution order:
    1. Cached token under MSAL cache row lock (acquire_token_silent)
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

    scopes = login_scopes()

    token = _try_silent_under_lock(client_id, scopes)
    if token is not None:
        return token

    token = _acquire_token_interactive(client_id, scopes)
    if token is not None:
        return token

    raise PermissionError(
        "Microsoft authentication failed. Could not acquire a token via "
        "browser or device code flow."
    )


def get_local_powerbi_token() -> str:
    """
    Acquire a Power BI access token using local MSAL auth.

    Uses the same app registration and token cache as get_local_token() but
    requests the Power BI resource scope instead of Graph scopes.
    """
    client_id = os.environ.get("MS_CLIENT_ID")
    if not client_id:
        raise PermissionError(
            "MS_CLIENT_ID environment variable is required for local authentication."
        )

    token = _try_silent_under_lock(client_id, POWERBI_SCOPES)
    if token is not None:
        return token

    token = _acquire_token_interactive(client_id, POWERBI_SCOPES)
    if token is not None:
        return token

    raise PermissionError(
        "Power BI authentication failed. Could not acquire a token via "
        "browser or device code flow."
    )
