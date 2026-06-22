"""Per-MCP ``/connect/<provider>`` Starlette routes (JWT-mode only).

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

* ``GET /connect/<name>/status`` -- returns ``{connected, valid, scopes}`` for
  the authenticated user. ``connected`` = a token row exists; ``valid`` = the
  access token isn't expired OR a refresh token is present.

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

    The ``authorize_url`` and ``token_url`` fields accept either a literal
    string OR a zero-arg callable returning a string. The callable form is
    necessary for providers whose URLs include an env-derived value (e.g.
    Microsoft's tenant ID) that may change between import time and request
    time — capturing such env values at import time produces stale URLs.

    ``post_exchange`` is an optional hook that receives the raw token JSON
    returned by the provider's token endpoint and produces the dict to
    persist via ``TokenStore.save_token``. Useful when a provider returns
    extra fields (e.g. Atlassian's cloud_id discovery step).
    """

    name: str
    authorize_url: str | Callable[[], str]
    token_url: str | Callable[[], str]
    scopes: str
    client_id_env: str
    client_secret_env: str
    public_url_env: str = "BOND_MCPS_PUBLIC_URL"
    post_exchange: Callable[[dict], dict] | None = None

    def resolved_authorize_url(self) -> str:
        return self.authorize_url() if callable(self.authorize_url) else self.authorize_url

    def resolved_token_url(self) -> str:
        return self.token_url() if callable(self.token_url) else self.token_url


# ---------------------------------------------------------------------------
# Registration entry point
# ---------------------------------------------------------------------------


def register_connect_routes(mcp, config: ProviderConnectConfig) -> None:
    """Register the connect routes on a FastMCP instance.

    Skips registration when JWT mode is off — the connect routes are only
    meaningful in multi-tenant / delegation deployments. The decorator pattern
    matches what each MCP already does for /healthz.
    """
    jwt_enabled = bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
    if not jwt_enabled:
        logger.debug("JWT mode off; skipping /connect/%s routes.", config.name)
        return

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

    @mcp.custom_route(f"{base}/status", methods=["GET"])
    async def _status(request: Request) -> Response:  # pragma: no cover (integration)
        return await _connect_status(request, config)

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


# ---------------------------------------------------------------------------
# POST /connect/<name>/ticket
# ---------------------------------------------------------------------------


async def _mint_ticket(request: Request, config: ProviderConnectConfig) -> Response:
    """Mint a short-lived ticket; optionally bind a ``return_url``.

    The user_key comes from the validated JWT (FastMCP middleware has already
    enforced it). The optional ``return_url`` (JSON body) is validated against
    the allowlist here and carried in the connect_url.
    """
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
    ticket = request.query_params.get("ticket")
    if not ticket:
        return _html(400, f"Missing ticket. Reopen the link printed by the {config.name} tool.")
    try:
        consumed = consume_ticket(ticket=ticket, provider=config.name)
    except TicketError as exc:
        return _html(400, str(exc))

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
    _stash_pkce(
        state=state,
        user_key=consumed.user_key,
        code_verifier=code_verifier,
        provider=config.name,
        return_url=return_url,
    )

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
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
    """Report the caller's connection status for this provider."""
    import time

    user_key, err = _authenticated_user_key()
    if err is not None:
        return err

    data = TokenRepository().get_token(user_key, config.name)
    if data is None:
        return JSONResponse({"connected": False, "valid": True, "scopes": None})

    expires_at = data.get("expires_at")
    has_refresh = bool(data.get("refresh_token"))
    expired = expires_at is not None and time.time() >= float(expires_at)
    valid = (not expired) or has_refresh
    return JSONResponse({"connected": True, "valid": valid, "scopes": data.get("scopes")})


async def _disconnect(request: Request, config: ProviderConnectConfig) -> Response:
    """Delete the caller's stored token for this provider."""
    user_key, err = _authenticated_user_key()
    if err is not None:
        return err

    existed = TokenRepository().get_token(user_key, config.name) is not None
    TokenStore(config.name, user_key=user_key).clear()
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
                resource=None,
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
    base = _public_base(config)
    if not base:
        raise RuntimeError(
            f"{ENV_CONNECT_PUBLIC_URL} or {config.public_url_env} must be set for /connect."
        )
    return f"{base}/connections/{config.name}/callback"


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
            config.resolved_token_url(), data=body, headers={"Accept": "application/json"}
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
