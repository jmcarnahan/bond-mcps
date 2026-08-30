"""Per-MCP ``/connect/<provider>`` Starlette routes.

Builds the connect surface from a ``ProviderConnectConfig``:

* ``POST /connect/<name>/ticket`` -- mints a short-lived ticket for the
  authenticated user (read via FastMCP's ``get_access_token``). Accepts an
  optional ``return_url`` (JSON body) that is carried through to the callback
  so the user is sent back to the calling app (e.g. bond-ai's Connect screen).

* ``GET /connect/<name>?ticket=...[&return_url=...]`` -- validates the ticket,
  generates an OAuth PKCE pair, redirects the browser to the upstream
  provider's authorize URL with state bound to (user_key, code_verifier,
  return_url).

* ``GET /connections/<name>/callback`` -- the provider OAuth redirect target.
  Exchanges the code for a token, persists it into ``tokens.db`` keyed by the
  user_key, then 302s back to ``return_url`` with ``?connection_success=<name>``
  (or ``?connection_error=<name>&error=...``). With no ``return_url`` (legacy
  CLI flow) it renders a terminal "you can close this tab" page. Note this is
  ``/connections/`` (plural) — the canonical, already-registered redirect path
  shared with the CLI flow — so delegating needs NO new provider registration.

* ``GET /connect/<name>/status`` -- returns ``{connected, valid, scopes,
  expires_at, has_refresh_token}`` for the authenticated user. ``connected`` =
  a token row exists; ``valid`` = the access token isn't expired OR a refresh
  token is present; ``expires_at`` = epoch seconds (null when the provider
  didn't report an expiry).

* ``DELETE /connect/<name>`` -- deletes the user's stored token; returns
  ``{disconnected: <whether a row existed>}``.

This is the generic OAuth-2.0 authorization-code-with-PKCE flow. Providers
that use a different idiom (Microsoft via MSAL, Atlassian with its cloud_id
discovery step, Databricks U2M) wire their own bespoke flow on top of the
same ticket store. ``github`` uses it as-is.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlencode, urlparse

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from auth import TokenStore, encryption
from auth.connect_tickets import TicketError, consume_ticket, mint_ticket
from auth.db.models import OAuthPendingAuth
from auth.db.repository import TokenRepository, build_default_resolver
from auth.db.session import get_session
from auth.oauth_utils import (
    generate_pkce_pair,
    generate_state,
)
from auth.token_store import (
    OUTBOUND_USER_AGENT,
    current_user_key,
    resolve_user_key_for_request,
)

logger = logging.getLogger(__name__)

# CSV of hostnames a connect callback may redirect back to (open-redirect
# guard). Empty/unset → no host is allowed → callbacks fall back to the
# terminal HTML page. In combined local dev set this to ``localhost``.
ENV_ALLOWED_RETURN_HOSTS = "BOND_MCPS_ALLOWED_RETURN_HOSTS"

# Public origin the browser-facing connect_url and the provider redirect_uri
# are built from. In combined/delegation mode this is the front door
# (e.g. http://localhost:8000), which is distinct from each MCP's own
# BOND_MCPS_PUBLIC_URL (used for JWT resource metadata). Falls back to the
# config's public_url_env when unset.
ENV_CONNECT_PUBLIC_URL = "BOND_MCPS_CONNECT_PUBLIC_URL"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConnectConfig:
    """Per-provider OAuth code-grant parameters.

    The ``authorize_url``, ``token_url``, and ``scopes`` fields accept either
    a literal string OR a zero-arg callable returning a string. The callable
    form is necessary when the value depends on env (e.g. Microsoft's tenant
    ID, or a scope policy branching on MS_TENANT_ID/MS_SCOPES) that may change
    between import time and request time — capturing such env values at import
    time produces stale values.

    ``post_exchange`` is an optional hook that receives the raw token JSON
    returned by the provider's token endpoint and produces the dict to
    persist via ``TokenStore.save_token``. Useful when a provider returns
    extra fields (e.g. Atlassian's cloud_id discovery step).
    """

    name: str
    authorize_url: str | Callable[[], str]
    token_url: str | Callable[[], str]
    scopes: str | Callable[[], str]
    client_id_env: str
    client_secret_env: str
    public_url_env: str = "BOND_MCPS_PUBLIC_URL"
    post_exchange: Callable[[dict], dict] | None = None
    extra_authorize_params: dict[str, str] | None = None

    def resolved_authorize_url(self) -> str:
        return self.authorize_url() if callable(self.authorize_url) else self.authorize_url

    def resolved_token_url(self) -> str:
        return self.token_url() if callable(self.token_url) else self.token_url

    def resolved_scopes(self) -> str:
        return self.scopes() if callable(self.scopes) else self.scopes


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------


def register_connect_routes(mcp, config: ProviderConnectConfig) -> None:
    """Register the connect routes on a FastMCP instance.

    Routes are registered in both JWT mode (multi-tenant deployment) and
    proxy/local mode. In local mode the ticket endpoint returns 404 (tickets
    are unnecessary), the start endpoint uses the local user identity
    directly, and the provider callback is additionally served from
    ``/connect/<name>/callback`` for the auth-proxy relay.
    """
    # Status + token-management routes are shared with register_status_routes
    # (callers may also invoke it directly); registering here first means
    # existing register_connect_routes call sites need no edits, and the
    # guard inside register_status_routes makes a second explicit call a no-op.
    register_status_routes(mcp, config)

    # Loud misconfiguration guard (JWT mode only): without the front-door
    # origin, _redirect_uri falls back to this MCP's own BOND_MCPS_PUBLIC_URL,
    # producing a per-MCP redirect_uri that no provider console has registered.
    # Nothing fails server-side — the provider rejects the redirect at
    # authorize time and the user sees an opaque provider error. Warn at
    # startup, where operators look.
    if _is_jwt_mode() and not os.environ.get(ENV_CONNECT_PUBLIC_URL, "").strip():
        logger.warning(
            "%s is not set; /connect/%s will build provider redirect_uris from "
            "%s (this MCP's own origin), which is almost certainly NOT a "
            "registered OAuth callback. Set %s to the front-door origin.",
            ENV_CONNECT_PUBLIC_URL,
            config.name,
            config.public_url_env,
            ENV_CONNECT_PUBLIC_URL,
        )

    base = f"/connect/{config.name}"

    @mcp.custom_route(f"{base}/ticket", methods=["POST"])
    async def _mint(request: Request) -> Response:  # pragma: no cover (integration)
        return await _mint_ticket(request, config)

    @mcp.custom_route(base, methods=["GET"])
    async def _start(request: Request) -> Response:
        return await _start_connect(request, config)

    # The provider OAuth callback lives at /connections/<name>/callback — the
    # canonical, already-registered redirect path (the CLI flow + bond-ai use
    # it too). Keeping it here means NO new provider-console registration when
    # delegating. The other routes (ticket/start/status/delete) stay under
    # /connect/<name> since they're server-to-server or internal and never
    # registered with providers.
    @mcp.custom_route(f"/connections/{config.name}/callback", methods=["GET"])
    async def _callback(request: Request) -> Response:
        return await _finish_connect(request, config)

    if not _is_jwt_mode():
        # Local mode only: the auth proxy relays the provider callback to
        # /connect/<name>/callback (see _register_with_proxy). JWT mode
        # deliberately does NOT serve this path — the canonical
        # /connections/<name>/callback above is the only registered redirect.
        @mcp.custom_route(f"{base}/callback", methods=["GET"])
        async def _local_callback(request: Request) -> Response:
            return await _finish_connect(request, config)

    @mcp.custom_route(base, methods=["DELETE"])
    async def _delete(request: Request) -> Response:  # pragma: no cover (integration)
        return await _disconnect(request, config)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _authenticated_user_key() -> tuple[str | None, Response | None]:
    """Resolve the caller's user_key from the validated JWT.

    Returns ``(user_key, None)`` on success or ``(None, error_response)``.
    The claim name honours ``BOND_MCPS_JWT_SUB_CLAIM`` (default ``sub``) so
    connect-time and tool-call-time key on the same value.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:
        return None, JSONResponse(
            {"error": "server_error", "error_description": "fastmcp not installed."},
            status_code=500,
        )

    access = get_access_token()
    if access is None:
        return None, JSONResponse(
            {"error": "unauthorized", "error_description": "Missing access token."},
            status_code=401,
        )

    from auth.jwt_identity import get_sub_claim

    user_key = access.claims.get(get_sub_claim())
    if not isinstance(user_key, str) or not user_key.strip():
        return None, JSONResponse(
            {"error": "unauthorized", "error_description": "Token missing sub claim."},
            status_code=401,
        )
    return user_key.strip(), None


def register_status_routes(mcp, config: ProviderConnectConfig) -> None:
    """Register status and token management routes (always active, both modes).

    Idempotent per (mcp, provider): register_connect_routes calls this first,
    and callers that also invoke it explicitly get a no-op the second time.
    The guard is an attribute on the mcp object itself — NOT keyed by id(mcp),
    which CPython recycles across short-lived objects.
    """
    registered = getattr(mcp, "_bond_status_routes_registered", None)
    if registered is None:
        registered = set()
        mcp._bond_status_routes_registered = registered
    if config.name in registered:
        return
    registered.add(config.name)

    base = f"/connect/{config.name}"

    @mcp.custom_route(f"{base}/status", methods=["GET"])
    async def _status(request: Request) -> Response:
        return await _connect_status(request, config)

    @mcp.custom_route(f"{base}/token", methods=["DELETE"])
    async def _delete_token(request: Request) -> Response:
        return await _clear_token(request, config)


async def _clear_token(request: Request, config: ProviderConnectConfig) -> Response:
    user_key, err = _request_user_key()
    if err is not None:
        return err

    store = TokenStore(config.name, user_key=user_key)
    store.clear()
    _clear_msal_cache(config, user_key)

    logger.info("Cleared %s token for user_key=%s via DELETE.", config.name, user_key)
    return JSONResponse({"cleared": True, "provider": config.name})


def _clear_msal_cache(config: ProviderConnectConfig, user_key: str) -> None:
    """Drop the user's MSAL token cache for MSAL-managed providers.

    Without this, a disconnect leaves the msal_token_caches row alive and the
    provider silently reconnects itself on the next tool call.
    """
    if config.name not in _MSAL_PROVIDERS:
        return
    try:
        from auth.db.repository import TokenRepository

        TokenRepository().clear_msal_cache(user_key)
    except Exception:
        logger.debug("MSAL cache clear failed for user_key=%s", user_key, exc_info=True)


def _resolve_or_401() -> str | Response:
    """Resolve user identity, returning a 401 Response on auth failure.

    Re-raises DeploymentConfigError (server misconfiguration) rather than
    masking it as a client auth error.
    """
    from auth.db.session import DeploymentConfigError

    try:
        return resolve_user_key_for_request()
    except DeploymentConfigError:
        raise
    except RuntimeError:
        return JSONResponse(
            {"error": "unauthorized", "error_description": "Valid authentication required."},
            status_code=401,
        )


def _request_user_key() -> tuple[str | None, Response | None]:
    """Resolve the caller's identity per deployment mode.

    JWT mode: the validated JWT's sub claim (middleware has enforced it) —
    never the local-identity fallback, which in a deployment would answer for
    whatever BOND_MCPS_USER_ID names. Local mode: the proxy/local resolution.
    """
    if _is_jwt_mode():
        return _authenticated_user_key()
    resolved = _resolve_or_401()
    if isinstance(resolved, Response):
        return None, resolved
    return resolved, None


# Providers whose tokens are managed via MSAL (stored in msal_token_caches
# rather than provider_tokens).
_MSAL_PROVIDERS = frozenset({"microsoft", "microsoft_powerbi"})


def _has_msal_connection(user_key: str) -> bool:
    """Check whether the MSAL cache has usable tokens for the given user.

    Non-destructive: decrypts and inspects the cache JSON but never calls
    Microsoft or attempts token acquisition.
    """
    import json

    from auth.db.repository import TokenRepository

    try:
        repo = TokenRepository()
        cache_json = repo.get_msal_cache(user_key)
    except Exception:
        logger.debug("MSAL cache lookup failed for user_key=%s", user_key, exc_info=True)
        return False

    if not cache_json:
        return False

    try:
        cache_data = json.loads(cache_json)
    except (json.JSONDecodeError, TypeError):
        return False

    refresh_tokens = cache_data.get("RefreshToken")
    if refresh_tokens and isinstance(refresh_tokens, dict) and len(refresh_tokens) > 0:
        return True

    access_tokens = cache_data.get("AccessToken")
    if access_tokens and isinstance(access_tokens, dict):
        import time

        now = time.time()
        for _key, token_entry in access_tokens.items():
            expires_on = token_entry.get("expires_on")
            if expires_on:
                try:
                    if float(expires_on) > now:
                        return True
                except (ValueError, TypeError):
                    pass
    return False


def _is_jwt_mode() -> bool:
    """Return True when JWT verification is enabled (multi-tenant deployment)."""
    import os

    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )


# ---------------------------------------------------------------------------
# POST /connect/<name>/ticket
# ---------------------------------------------------------------------------


async def _mint_ticket(request: Request, config: ProviderConnectConfig) -> Response:
    """Mint a short-lived ticket; optionally bind a ``return_url``.

    The user_key comes from the validated JWT (FastMCP middleware has already
    enforced it). The optional ``return_url`` (JSON body) is validated against
    the allowlist here and carried in the connect_url.
    Only available in JWT mode; in local/proxy mode tickets are unnecessary.
    """
    if not _is_jwt_mode():
        return JSONResponse(
            {"error": "not_found", "error_description": "Tickets are not used in local mode."},
            status_code=404,
        )

    user_key, err = _authenticated_user_key()
    if err is not None:
        return err

    return_url = None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty/non-JSON body is fine; return_url stays None
        body = None
    if isinstance(body, dict):
        return_url = body.get("return_url")
    if return_url is not None and not _validate_return_url(return_url):
        return JSONResponse(
            {"error": "invalid_return_url", "error_description": "return_url host is not allowed."},
            status_code=400,
        )

    ticket = mint_ticket(user_key=user_key, provider=config.name)
    connect_url = f"{_public_base(config)}/connect/{config.name}?ticket={ticket}"
    if return_url:
        connect_url += f"&return_url={quote(return_url, safe='')}"
    return JSONResponse({"ticket": ticket, "connect_url": connect_url})


# ---------------------------------------------------------------------------
# GET /connect/<name>?ticket=...
# ---------------------------------------------------------------------------


async def _start_connect(request: Request, config: ProviderConnectConfig) -> Response:
    if _is_jwt_mode():
        ticket = request.query_params.get("ticket")
        if not ticket:
            return _html(400, f"Missing ticket. Reopen the link printed by the {config.name} tool.")
        try:
            consumed = consume_ticket(ticket=ticket, provider=config.name)
        except TicketError as exc:
            return _html(400, str(exc))
        user_key = consumed.user_key
    else:
        user_key = current_user_key()

    return_url = request.query_params.get("return_url")
    if return_url is not None and not _validate_return_url(return_url):
        return _html(400, "Invalid return_url.")

    client_id, client_secret = _provider_secrets(config)
    if not client_id or not client_secret:
        return _html(
            500,
            f"Server misconfiguration: {config.client_id_env} and "
            f"{config.client_secret_env} must be set on the MCP.",
        )

    code_verifier, code_challenge = generate_pkce_pair()
    state = generate_state()
    redirect_uri = _redirect_uri(config)

    if not _is_jwt_mode():
        _register_with_proxy(state=state, provider=config.name, config=config)

    _stash_pkce(
        state=state,
        user_key=user_key,
        code_verifier=code_verifier,
        provider=config.name,
        return_url=return_url,
    )

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": config.resolved_scopes(),
        "state": state,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if config.extra_authorize_params:
        params.update(config.extra_authorize_params)
    return RedirectResponse(
        f"{config.resolved_authorize_url()}?{urlencode(params)}", status_code=302
    )


# ---------------------------------------------------------------------------
# GET /connect/<name>/callback
# ---------------------------------------------------------------------------


async def _finish_connect(request: Request, config: ProviderConnectConfig) -> Response:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return _html(400, "Missing code or state from provider.")

    try:
        stash = _consume_pkce(state=state, provider=config.name)
    except TicketError as exc:
        return _html(400, str(exc))

    return_url = stash.get("return_url")

    client_id, client_secret = _provider_secrets(config)
    if not client_id or not client_secret:
        return _connect_result(
            return_url,
            config.name,
            ok=False,
            error="server_misconfig",
            html_status=500,
            html_msg=(
                f"Server misconfiguration: {config.client_id_env} and "
                f"{config.client_secret_env} must be set on the MCP."
            ),
        )

    try:
        token_response = _exchange_code(
            config=config,
            code=code,
            code_verifier=stash["code_verifier"],
            redirect_uri=_redirect_uri(config),
            client_id=client_id,
            client_secret=client_secret,
        )
    except RuntimeError as exc:
        return _connect_result(
            return_url,
            config.name,
            ok=False,
            error="token_exchange_failed",
            html_status=502,
            html_msg=f"Provider token exchange failed: {exc}",
        )

    save_data = (
        config.post_exchange(token_response)
        if config.post_exchange is not None
        else _default_token_shape(token_response)
    )
    if not save_data.get("access_token"):
        return _connect_result(
            return_url,
            config.name,
            ok=False,
            error="no_access_token",
            html_status=502,
            html_msg="Provider did not return an access_token.",
        )

    TokenStore(config.name, user_key=stash["user_key"]).save_token(save_data)
    logger.info(
        "Stored %s token for user_key=%s via /connect flow.",
        config.name,
        stash["user_key"],
    )
    return _connect_result(
        return_url,
        config.name,
        ok=True,
        html_msg=f"{config.name.title()} is now connected. You can close this tab.",
    )


def _connect_result(
    return_url: str | None,
    name: str,
    *,
    ok: bool,
    error: str | None = None,
    html_status: int = 200,
    html_msg: str = "",
) -> Response:
    """302 back to ``return_url`` when present+allowed; else terminal HTML.

    Re-validates ``return_url`` against the allowlist at redirect time so a
    tampered stash can only ever send the user to an allowlisted host.
    """
    if return_url and _validate_return_url(return_url):
        sep = "&" if "?" in return_url else "?"
        if ok:
            target = f"{return_url}{sep}connection_success={quote(name, safe='')}"
        else:
            target = (
                f"{return_url}{sep}connection_error={quote(name, safe='')}"
                f"&error={quote(error or 'unknown', safe='')}"
            )
        return RedirectResponse(target, status_code=302)
    return _html(200 if ok else html_status, html_msg)


# ---------------------------------------------------------------------------
# GET /connect/<name>/status   and   DELETE /connect/<name>
# ---------------------------------------------------------------------------


async def _connect_status(request: Request, config: ProviderConnectConfig) -> Response:
    """Report the caller's connection status for this provider.

    Superset of both lineages' shapes so every consumer keeps working:
    ``connected``/``valid``/``scopes``/``expires_at``/``has_refresh_token``
    (bond-ai's contract: connected = a token row exists; valid = not expired
    OR refreshable) plus ``provider``/``token``/``reason`` (the fork's
    diagnostics: token = valid|refreshed|msal, reason = why not usable).

    Self-heals: an expired-but-refreshable token is refreshed in a worker
    thread (warms the token for scheduled jobs); MSAL-managed providers fall
    back to the msal_token_caches row, which provider_tokens never sees.
    """
    import time

    user_key, err = _request_user_key()
    if err is not None:
        return err

    def _payload(
        *,
        connected: bool,
        valid: bool,
        scopes=None,
        expires_at=None,
        has_refresh: bool = False,
        token: str | None = None,
        reason: str | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "connected": connected,
                "valid": valid,
                "scopes": scopes,
                # Epoch seconds; consumers (bond-ai) render it for the user.
                "expires_at": float(expires_at) if expires_at is not None else None,
                "has_refresh_token": has_refresh,
                "provider": config.name,
                "token": token,
                "reason": reason,
            }
        )

    repo = TokenRepository()
    data = repo.get_token(user_key, config.name)
    if data is None:
        # No provider_tokens row. MSAL-managed providers (Microsoft) keep
        # their tokens in msal_token_caches instead — treat a usable cache
        # as connected, else main's historical "no row" shape.
        if config.name in _MSAL_PROVIDERS and _has_msal_connection(user_key):
            return _payload(connected=True, valid=True, token="msal")
        return _payload(connected=False, valid=True, reason="not_connected")

    expires_at = data.get("expires_at")
    has_refresh = bool(data.get("refresh_token"))
    expired = expires_at is not None and time.time() >= float(expires_at)
    if not expired:
        return _payload(
            connected=True,
            valid=True,
            scopes=data.get("scopes"),
            expires_at=expires_at,
            has_refresh=has_refresh,
            token="valid",
        )

    # Access token expired. Before reporting a stale expiry, attempt a real
    # refresh: refreshing here also warms the token so the next tool call
    # (e.g. a scheduled job) doesn't have to. Never let a network error 500
    # the status route. refresh_if_needed does synchronous, blocking network
    # I/O (urllib) while holding a row lock, so run it in a worker thread —
    # a slow upstream IdP must not stall other requests on this pod.
    store = TokenStore(config.name, user_key=user_key)
    client_id, client_secret = _provider_secrets(config)
    if has_refresh and client_id:
        try:
            token_url = config.resolved_token_url()
            refreshed = await anyio.to_thread.run_sync(
                store.refresh_if_needed, client_id, client_secret or "", token_url
            )
        except Exception:
            logger.warning(
                "Status refresh attempt failed for %s (user_key=%s)",
                config.name,
                user_key,
                exc_info=True,
            )
            refreshed = None
        if refreshed:
            fresh = repo.get_token(user_key, config.name) or {}
            return _payload(
                connected=True,
                valid=True,
                scopes=fresh.get("scopes", data.get("scopes")),
                expires_at=fresh.get("expires_at"),
                has_refresh=bool(fresh.get("refresh_token")) or has_refresh,
                token="refreshed",
            )

    if config.name in _MSAL_PROVIDERS and _has_msal_connection(user_key):
        return _payload(connected=True, valid=True, scopes=data.get("scopes"), token="msal")

    # Row exists but the token isn't usable. connected stays True (bond-ai's
    # contract: a row exists); valid keeps main's formula (refreshable counts
    # as valid, even when this refresh attempt failed — the next may succeed).
    # reason distinguishes "refresh failed; reconnect likely needed" from
    # "expired with nothing to refresh", so scheduled-job failures are
    # diagnosable.
    return _payload(
        connected=True,
        valid=has_refresh,
        scopes=data.get("scopes"),
        expires_at=expires_at,
        has_refresh=has_refresh,
        reason="refresh_failed" if has_refresh else "not_connected",
    )


# Fork lineage name for the status handler; kept so existing imports and
# patch sites (tests, fork call sites) resolve unchanged.
_check_status = _connect_status


async def _disconnect(request: Request, config: ProviderConnectConfig) -> Response:
    """Delete the caller's stored token for this provider."""
    user_key, err = _request_user_key()
    if err is not None:
        return err

    existed = TokenRepository().get_token(user_key, config.name) is not None
    TokenStore(config.name, user_key=user_key).clear()
    _clear_msal_cache(config, user_key)
    return JSONResponse({"disconnected": existed})


# ---------------------------------------------------------------------------
# State stash for the connect flow
#
# We piggy-back on the AS's ``oauth_pending_auth`` table because its columns
# are a superset of what /connect/<provider> needs. To keep that piggy-back
# self-documenting we store a sentinel in ``client_id`` (``connect:<name>``)
# so consumers can tell connect rows apart from real AS pending-auth rows
# at a glance.
#
# Column repurposing:
#   client_id      -> "connect:<provider>" sentinel
#   redirect_uri   -> user_key             (the only place we have to put it;
#                                            the column has no other use here)
#   client_state   -> return_url           (where to 302 the user afterwards)
#   code_challenge -> "" (unused; PKCE verifier is in upstream_code_verifier)
#
# If this table ever grows another consumer, split out a dedicated
# ``connect_pending_auth`` table. The two helpers below are the only places
# that touch the repurposed columns.
# ---------------------------------------------------------------------------

_CONNECT_CLIENT_PREFIX = "connect:"


def _register_with_proxy(*, state: str, provider: str, config: ProviderConnectConfig) -> None:
    """Register OAuth state with the auth proxy for browser callback relay."""
    import json
    import os
    import urllib.request

    proxy_port = os.environ.get("BOND_AUTH_PROXY_PORT", "8000")
    public_url = _public_base(config)
    redirect_target = f"{public_url}/connect/{provider}/callback"

    body = json.dumps(
        {
            "state": state,
            "provider": provider,
            "redirect_target": redirect_target,
        }
    ).encode()

    req = urllib.request.Request(
        f"http://localhost:{proxy_port}/auth/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3)  # nosec B310
    except Exception as e:
        logger.warning("Failed to register with auth proxy: %s", e)


def _stash_pkce(
    *,
    state: str,
    user_key: str,
    code_verifier: str,
    provider: str,
    return_url: str | None = None,
) -> None:
    """Persist a connect-flow PKCE verifier + user_key + return_url by ``state``."""
    blob, key_version = encryption.encrypt(
        code_verifier.encode("utf-8"),
        user_key=user_key,
        provider="connect_routes",
        field="code_verifier",
        resolver=build_default_resolver(),
    )
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(
            OAuthPendingAuth(
                bond_state=state,
                client_id=f"{_CONNECT_CLIENT_PREFIX}{provider}",
                redirect_uri=user_key,  # repurposed slot — see header comment
                client_state=return_url,  # repurposed slot — see header comment
                code_challenge="",
                code_challenge_method="S256",
                resource=return_url,
                scope=None,
                upstream_code_verifier_encrypted=blob,
                key_version=key_version,
                expires_at=now + timedelta(seconds=600),
            )
        )


def _consume_pkce(*, state: str, provider: str) -> dict:
    now = datetime.now(timezone.utc)
    with get_session() as session:
        row = session.get(OAuthPendingAuth, state)
        if row is None:
            raise TicketError("Unknown or already-consumed connect state.")
        if _aware(row.expires_at) < now:
            session.delete(row)
            raise TicketError("Connect state expired; restart the flow.")
        if row.client_id != f"{_CONNECT_CLIENT_PREFIX}{provider}":
            raise TicketError("Connect state was issued for a different provider.")
        user_key = row.redirect_uri  # repurposed slot — see header comment
        return_url = row.client_state  # repurposed slot — see header comment
        verifier = encryption.decrypt(
            row.upstream_code_verifier_encrypted,
            user_key=user_key,
            provider="connect_routes",
            field="code_verifier",
            key_version=row.key_version,
            resolver=build_default_resolver(),
        ).decode("utf-8")
        session.delete(row)
        return {"user_key": user_key, "code_verifier": verifier, "return_url": return_url}


# ---------------------------------------------------------------------------
# Provider OAuth helpers
# ---------------------------------------------------------------------------


def _validate_return_url(url: str | None) -> bool:
    """True iff ``url`` is http(s) and its host is in the allowlist.

    Allowlist = ``BOND_MCPS_ALLOWED_RETURN_HOSTS`` (CSV of hostnames). Empty/
    unset → nothing is allowed (safe default; open-redirect guard).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    allowed = {
        h.strip().lower()
        for h in os.environ.get(ENV_ALLOWED_RETURN_HOSTS, "").split(",")
        if h.strip()
    }
    return parsed.hostname.lower() in allowed


def _provider_secrets(config: ProviderConnectConfig) -> tuple[str | None, str | None]:
    return (
        (os.environ.get(config.client_id_env) or "").strip() or None,
        (os.environ.get(config.client_secret_env) or "").strip() or None,
    )


def _public_base(config: ProviderConnectConfig) -> str:
    """Browser-facing origin for connect_url + redirect_uri.

    Prefers ``BOND_MCPS_CONNECT_PUBLIC_URL`` (the front door in combined/
    delegation mode); falls back to the config's ``public_url_env``.
    """
    connect_url = (os.environ.get(ENV_CONNECT_PUBLIC_URL) or "").strip().rstrip("/")
    if connect_url:
        return connect_url
    return (os.environ.get(config.public_url_env) or "").strip().rstrip("/")


def _redirect_uri(config: ProviderConnectConfig) -> str:
    if _is_jwt_mode():
        base = _public_base(config)
        if not base:
            raise RuntimeError(
                f"{ENV_CONNECT_PUBLIC_URL} or {config.public_url_env} must be set for /connect."
            )
        return f"{base}/connections/{config.name}/callback"
    proxy_port = os.environ.get("BOND_AUTH_PROXY_PORT", "8000")
    return f"http://localhost:{proxy_port}/connections/{config.name}/callback"


def _aware(value: datetime) -> datetime:
    """Coerce SQLite TZ-naive datetimes back to UTC-aware."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _exchange_code(
    *,
    config: ProviderConnectConfig,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict:
    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=30.0) as http:
        resp = http.post(
            config.resolved_token_url(),
            data=body,
            headers={"Accept": "application/json", "User-Agent": OUTBOUND_USER_AGENT},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response: {exc}") from exc


def _default_token_shape(token_response: dict) -> dict:
    """Shape a generic OAuth token response for ``TokenStore.save_token``."""
    out = {"access_token": token_response.get("access_token")}
    if rt := token_response.get("refresh_token"):
        out["refresh_token"] = rt
    if exp := token_response.get("expires_in"):
        try:
            import time

            out["expires_at"] = time.time() + int(exp)
        except (TypeError, ValueError):
            pass
    # Providers return the granted scopes as "scope"; persist under the
    # storage key "scopes" (what TokenRepository.save_token reads).
    if scope := token_response.get("scope"):
        out["scopes"] = scope
    if tt := token_response.get("token_type"):
        out["token_type"] = tt
    return out


def _html(status: int, message: str) -> Response:
    body = (
        f"<html><body><h2>{('OK' if status == 200 else 'Authentication')}</h2>"
        f"<p>{message}</p></body></html>"
    ).encode()
    return Response(content=body, media_type="text/html", status_code=status)
