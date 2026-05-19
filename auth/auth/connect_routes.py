"""Per-MCP ``/connect/<provider>`` Starlette routes (JWT-mode only).

Builds three routes from a ``ProviderConnectConfig``:

* ``POST /connect/<name>/ticket`` -- mints a short-lived ticket for the
  authenticated user (read via FastMCP's ``get_access_token``) which is
  embedded in the connect URL the MCP returns to the agent.

* ``GET /connect/<name>?ticket=...`` -- validates the ticket, generates an
  OAuth PKCE pair, redirects the user's browser to the upstream provider's
  authorize URL with state-bound to (ticket_user_key, code_verifier).

* ``GET /connect/<name>/callback`` -- exchanges the provider's code for an
  access token, persists it into ``tokens.db`` keyed by the ticket's
  user_key, and shows a "you can close this tab" page.

This is the generic OAuth-2.0 authorization-code-with-PKCE flow. Providers
that use a different idiom (Microsoft via MSAL, Atlassian with its cloud_id
discovery step, Databricks U2M) wire their own bespoke flow on top of the
same ticket store. This module is the reference implementation; ``github``
uses it as-is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from auth import TokenStore, encryption
from auth.connect_tickets import TicketError, consume_ticket, mint_ticket
from auth.db.models import OAuthPendingAuth
from auth.db.repository import build_default_resolver
from auth.db.session import get_session
from auth.oauth_utils import (
    generate_pkce_pair,
    generate_state,
)

logger = logging.getLogger(__name__)


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
    """Register the three routes on a FastMCP instance.

    Skips registration when JWT mode is off — the connect routes are only
    meaningful in multi-tenant deployments. The decorator pattern matches
    what each MCP already does for /healthz.
    """
    import os

    jwt_enabled = bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )
    if not jwt_enabled:
        logger.debug("JWT mode off; skipping /connect/%s routes.", config.name)
        return

    base = f"/connect/{config.name}"

    @mcp.custom_route(f"{base}/ticket", methods=["POST"])
    async def _mint(request: Request) -> Response:  # pragma: no cover (covered by integration)
        return await _mint_ticket(request, config)

    @mcp.custom_route(base, methods=["GET"])
    async def _start(request: Request) -> Response:
        return await _start_connect(request, config)

    @mcp.custom_route(f"{base}/callback", methods=["GET"])
    async def _callback(request: Request) -> Response:
        return await _finish_connect(request, config)


# ---------------------------------------------------------------------------
# POST /connect/<name>/ticket
# ---------------------------------------------------------------------------


async def _mint_ticket(request: Request, config: ProviderConnectConfig) -> Response:
    """Internal endpoint: tool error path calls this with the user's JWT.

    The user_key is taken from the validated JWT's ``sub`` claim — FastMCP's
    middleware has already enforced that on this request.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:
        return JSONResponse(
            {"error": "server_error", "error_description": "fastmcp not installed."},
            status_code=500,
        )

    access = get_access_token()
    if access is None:
        return JSONResponse(
            {"error": "unauthorized", "error_description": "Missing access token."},
            status_code=401,
        )
    user_key = access.claims.get("sub")
    if not isinstance(user_key, str) or not user_key.strip():
        return JSONResponse(
            {"error": "unauthorized", "error_description": "Token missing sub claim."},
            status_code=401,
        )

    ticket = mint_ticket(user_key=user_key, provider=config.name)
    public_base = _public_base(config)
    connect_url = f"{public_base}/connect/{config.name}?ticket={ticket}"
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
        state=state, user_key=consumed.user_key, code_verifier=code_verifier, provider=config.name
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

    client_id, client_secret = _provider_secrets(config)
    if not client_id or not client_secret:
        return _html(
            500,
            f"Server misconfiguration: {config.client_id_env} and "
            f"{config.client_secret_env} must be set on the MCP.",
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
        return _html(502, f"Provider token exchange failed: {exc}")

    save_data = (
        config.post_exchange(token_response)
        if config.post_exchange is not None
        else _default_token_shape(token_response)
    )
    if not save_data.get("access_token"):
        return _html(502, "Provider did not return an access_token.")

    TokenStore(config.name, user_key=stash["user_key"]).save_token(save_data)
    logger.info(
        "Stored %s token for user_key=%s via /connect flow.",
        config.name,
        stash["user_key"],
    )
    return _html(
        200,
        f"{config.name.title()} is now connected. You can close this tab.",
    )


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
#   code_challenge -> "" (unused; PKCE verifier is in upstream_code_verifier)
#
# If this table ever grows another consumer, split out a dedicated
# ``connect_pending_auth`` table. The two helpers below are the only places
# that touch the repurposed columns.
# ---------------------------------------------------------------------------

_CONNECT_CLIENT_PREFIX = "connect:"


def _stash_pkce(*, state: str, user_key: str, code_verifier: str, provider: str) -> None:
    """Persist a connect-flow PKCE verifier + user_key keyed by ``state``."""
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
                client_state=None,
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
        verifier = encryption.decrypt(
            row.upstream_code_verifier_encrypted,
            user_key=user_key,
            provider="connect_routes",
            field="code_verifier",
            key_version=row.key_version,
            resolver=build_default_resolver(),
        ).decode("utf-8")
        session.delete(row)
        return {"user_key": user_key, "code_verifier": verifier}


# ---------------------------------------------------------------------------
# Provider OAuth helpers
# ---------------------------------------------------------------------------


def _provider_secrets(config: ProviderConnectConfig) -> tuple[str | None, str | None]:
    import os

    return (
        (os.environ.get(config.client_id_env) or "").strip() or None,
        (os.environ.get(config.client_secret_env) or "").strip() or None,
    )


def _public_base(config: ProviderConnectConfig) -> str:
    import os

    return (os.environ.get(config.public_url_env) or "").strip().rstrip("/")


def _redirect_uri(config: ProviderConnectConfig) -> str:
    base = _public_base(config)
    if not base:
        raise RuntimeError(f"{config.public_url_env} must be set for /connect.")
    return f"{base}/connect/{config.name}/callback"


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
    if scope := token_response.get("scope"):
        out["scope"] = scope
    if tt := token_response.get("token_type"):
        out["token_type"] = tt
    return out


def _html(status: int, message: str) -> Response:
    body = (
        f"<html><body><h2>{('OK' if status == 200 else 'Authentication')}</h2>"
        f"<p>{message}</p></body></html>"
    ).encode()
    return Response(content=body, media_type="text/html", status_code=status)
