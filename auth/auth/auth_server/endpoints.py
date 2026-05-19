"""OAuth 2.1 Authorization Server endpoints (Starlette ASGI app)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Iterable
from urllib.parse import urlencode

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from auth import log_discipline
from auth.auth_server import clients as client_registry
from auth.auth_server import codes as code_store
from auth.auth_server.keys import build_jwks_document, load_signing_key
from auth.auth_server.upstream import (
    UpstreamAuthError,
    UpstreamConfigError,
    get_upstream_idp,
)
from auth.oauth_utils import generate_pkce_pair, generate_state

logger = logging.getLogger(__name__)

ENV_AS_BASE_URL = "BOND_MCPS_AS_BASE_URL"
ENV_AS_ENABLED = "BOND_MCPS_AS_ENABLED"

_DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 86400  # 24h
_ENV_ACCESS_TOKEN_TTL_SECONDS = "BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS"


def _access_token_ttl_seconds() -> int:
    """24h default — matches bond-ai's ACCESS_TOKEN_EXPIRE_MINUTES=1440.

    One Cognito sign-in covers a working day across all MCP servers;
    per-provider upstream tokens auto-refresh server-side via
    ``TokenStore.refresh_if_needed`` inside each MCP's auth.py, so the
    user only sees Cognito once a day. Operators can override via
    ``BOND_MCPS_AS_ACCESS_TOKEN_TTL_SECONDS`` (integer seconds, 60–2592000).
    """
    raw = (os.environ.get(_ENV_ACCESS_TOKEN_TTL_SECONDS) or "").strip()
    if not raw:
        return _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %ss",
            _ENV_ACCESS_TOKEN_TTL_SECONDS,
            raw,
            _DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
        )
        return _DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    # Sanity clamp: < 60s means Claude Code can't possibly complete a flow
    # before expiry; > 30 days is the refresh_token TTL (and beyond makes
    # the revocation window unmanageable).
    return max(60, min(value, 30 * 24 * 3600))


# Kept as a module-level constant for back-compat with tests; the
# function above is the source of truth at issue time. Initialised once
# on import for the metadata endpoint (`expires_in` advertised value).
ACCESS_TOKEN_TTL_SECONDS = _access_token_ttl_seconds()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_app() -> Starlette:
    """Construct the AS Starlette app. Safe to call once at process start."""
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route(
                "/.well-known/oauth-authorization-server",
                authorization_server_metadata,
                methods=["GET"],
            ),
            Route("/.well-known/jwks.json", jwks, methods=["GET"]),
            Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
            Route(
                "/oauth/upstream/callback",
                oauth_upstream_callback,
                methods=["GET"],
            ),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/oauth/register", oauth_register, methods=["POST"]),
        ]
    )
    return app


def run(host: str = "127.0.0.1", port: int = 8001) -> None:
    """Run the AS via uvicorn. Blocks until interrupted."""
    if not _is_enabled():
        raise RuntimeError(
            f"{ENV_AS_ENABLED} is not set. Refusing to start the Authorization "
            "Server with default config — set the env explicitly."
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log_discipline.apply()
    base_url = os.environ.get(ENV_AS_BASE_URL, "").strip()
    if not base_url:
        raise RuntimeError(f"{ENV_AS_BASE_URL} must be set.")
    logger.info("bond-mcps Authorization Server listening on %s:%d (public %s)", host, port, base_url)
    # Touch the signing key + DB once at boot so misconfig fails fast.
    load_signing_key()
    import uvicorn

    uvicorn.run(build_app(), host=host, port=port)


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def authorization_server_metadata(_: Request) -> JSONResponse:
    base = _base_url()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["openid", "email", "profile"],
        }
    )


async def jwks(_: Request) -> JSONResponse:
    return JSONResponse(build_jwks_document())


# ---------------------------------------------------------------------------
# /oauth/authorize  →  upstream IdP
# ---------------------------------------------------------------------------


async def oauth_authorize(request: Request) -> Response:
    q = request.query_params
    client_id = q.get("client_id")
    redirect_uri = q.get("redirect_uri")
    response_type = q.get("response_type")
    code_challenge = q.get("code_challenge")
    code_challenge_method = q.get("code_challenge_method", "S256")
    state = q.get("state")
    resource = q.get("resource")
    scope = q.get("scope")

    if not client_id or not redirect_uri or not code_challenge:
        return _error_redirect_or_400(
            redirect_uri,
            state,
            "invalid_request",
            "client_id, redirect_uri, and code_challenge are required.",
        )
    if response_type != "code":
        return _error_redirect_or_400(
            redirect_uri, state, "unsupported_response_type", "Only code is supported."
        )
    if code_challenge_method != "S256":
        return _error_redirect_or_400(
            redirect_uri,
            state,
            "invalid_request",
            "code_challenge_method must be S256.",
        )

    client = client_registry.get_client(client_id)
    if client is None:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "Unknown client_id."},
            status_code=400,
        )
    if not client_registry.is_redirect_uri_allowed(client, redirect_uri):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri not registered."},
            status_code=400,
        )

    try:
        upstream = get_upstream_idp()
    except UpstreamConfigError as exc:
        logger.error("Upstream IdP misconfigured: %s", exc)
        return JSONResponse(
            {"error": "server_error", "error_description": "Upstream IdP misconfigured."},
            status_code=500,
        )

    # Mint PKCE for the upstream leg; the client's PKCE is preserved separately.
    upstream_verifier, upstream_challenge = generate_pkce_pair()
    bond_state = code_store.store_pending_auth(
        client_id=client_id,
        redirect_uri=redirect_uri,
        client_state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        resource=resource,
        scope=scope,
        upstream_code_verifier=upstream_verifier,
    )
    upstream_url = upstream.authorize_url(
        state=bond_state,
        code_challenge=upstream_challenge,
        code_challenge_method="S256",
    )
    return RedirectResponse(upstream_url, status_code=302)


# ---------------------------------------------------------------------------
# /oauth/upstream/callback  →  client redirect_uri with auth code
# ---------------------------------------------------------------------------


async def oauth_upstream_callback(request: Request) -> Response:
    q = request.query_params
    bond_state = q.get("state")
    code = q.get("code")
    upstream_error = q.get("error")

    if not bond_state:
        return _html_error(400, "Missing state from upstream IdP.")
    try:
        pending = code_store.consume_pending_auth(bond_state)
    except code_store.AuthCodeError as exc:
        logger.warning("Unknown/expired bond_state in upstream callback: %s", exc)
        return _html_error(400, "Sign-in session expired. Please try again.")

    if upstream_error:
        logger.warning("Upstream returned error=%s for %s", upstream_error, pending.client_id)
        return RedirectResponse(
            _client_redirect_with_error(
                pending.redirect_uri,
                pending.client_state,
                error="access_denied",
                description=q.get("error_description", upstream_error),
            ),
            status_code=302,
        )
    if not code:
        return _html_error(400, "Upstream callback missing code.")

    try:
        upstream = get_upstream_idp()
        user_info = upstream.exchange_code(
            code=code, code_verifier=pending.upstream_code_verifier
        )
    except (UpstreamConfigError, UpstreamAuthError) as exc:
        logger.exception("Upstream code exchange failed")
        return RedirectResponse(
            _client_redirect_with_error(
                pending.redirect_uri,
                pending.client_state,
                error="server_error",
                description=str(exc),
            ),
            status_code=302,
        )

    auth_code = code_store.issue_auth_code(
        client_id=pending.client_id,
        user_key=user_info.sub,
        email=user_info.email,
        code_challenge=pending.code_challenge,
        code_challenge_method=pending.code_challenge_method,
        redirect_uri=pending.redirect_uri,
        resource=pending.resource,
        scope=pending.scope,
    )
    params = {"code": auth_code}
    if pending.client_state:
        params["state"] = pending.client_state
    return RedirectResponse(
        f"{pending.redirect_uri}?{urlencode(params)}", status_code=302
    )


# ---------------------------------------------------------------------------
# /oauth/token
# ---------------------------------------------------------------------------


async def oauth_token(request: Request) -> Response:
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return _handle_code_grant(form)
    if grant_type == "refresh_token":
        return _handle_refresh_grant(form)
    return JSONResponse(
        {"error": "unsupported_grant_type", "error_description": f"Unsupported grant_type={grant_type!r}."},
        status_code=400,
    )


def _handle_code_grant(form) -> Response:
    code = form.get("code")
    client_id = form.get("client_id")
    redirect_uri = form.get("redirect_uri")
    code_verifier = form.get("code_verifier")
    if not all([code, client_id, redirect_uri, code_verifier]):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code, client_id, redirect_uri, code_verifier required."},
            status_code=400,
        )
    try:
        issued = code_store.consume_auth_code(
            code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
    except code_store.AuthCodeError as exc:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": str(exc)},
            status_code=400,
        )

    return _build_token_response(
        user_key=issued.user_key,
        email=issued.email,
        client_id=issued.client_id,
        resource=issued.resource,
        scope=issued.scope,
    )


def _handle_refresh_grant(form) -> Response:
    refresh_token = form.get("refresh_token")
    client_id = form.get("client_id")
    if not all([refresh_token, client_id]):
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "refresh_token and client_id required.",
            },
            status_code=400,
        )
    try:
        issued = code_store.consume_refresh_token(
            refresh_token, client_id=client_id
        )
    except code_store.AuthCodeError as exc:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": str(exc)},
            status_code=400,
        )
    # Email isn't preserved on the refresh-token row (it can drift from the
    # upstream IdP between sessions). The access token re-issued here is
    # bound to the original user_key + audience.
    return _build_token_response(
        user_key=issued.user_key,
        email=None,
        client_id=issued.client_id,
        resource=issued.resource,
        scope=issued.scope,
    )


def _build_token_response(
    *,
    user_key: str,
    email: str | None,
    client_id: str,
    resource: str | None,
    scope: str | None,
) -> Response:
    """Common path for the two grants — sign a JWT + issue a rotating
    refresh token + format the OAuth 2.0 token response."""
    access_token = _sign_access_token(
        user_key=user_key,
        email=email,
        client_id=client_id,
        resource=resource,
        scope=scope,
    )
    refresh_token = code_store.issue_refresh_token(
        client_id=client_id,
        user_key=user_key,
        resource=resource,
        scope=scope,
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": _access_token_ttl_seconds(),
            "refresh_token": refresh_token,
            "scope": scope or "",
        }
    )


# ---------------------------------------------------------------------------
# /oauth/register (DCR)
# ---------------------------------------------------------------------------


async def oauth_register(request: Request) -> Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "Body must be JSON."},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "Body must be a JSON object."},
            status_code=400,
        )
    try:
        record = client_registry.register_client(payload)
    except client_registry.ClientRegistrationError as exc:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": str(exc)},
            status_code=400,
        )
    # RFC 7591 §3.2.1 compliant response. We omit null/empty fields rather
    # than emit `"scope": null` etc., because some OAuth clients (notably
    # strict parsers) reject responses with unexpected null values where
    # the spec says "string".
    body: dict = {
        "client_id": record.client_id,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,  # 0 == never expires (per RFC 7591)
        "redirect_uris": record.redirect_uris,
        "grant_types": record.grant_types,
        "response_types": record.response_types,
        "token_endpoint_auth_method": record.token_endpoint_auth_method,
    }
    if record.client_name:
        body["client_name"] = record.client_name
    if record.scope:
        body["scope"] = record.scope
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# JWT signing
# ---------------------------------------------------------------------------


def _sign_access_token(
    *,
    user_key: str,
    email: str | None,
    client_id: str,
    resource: str | None,
    scope: str | None,
) -> str:
    signing = load_signing_key()
    now = int(time.time())
    payload = {
        "iss": _base_url(),
        "sub": user_key,
        "aud": resource or "bond-mcps",
        "client_id": client_id,
        "iat": now,
        "exp": now + _access_token_ttl_seconds(),
        "jti": str(uuid.uuid4()),
    }
    if email:
        payload["email"] = email
    if scope:
        payload["scope"] = scope
    return jwt.encode(
        payload,
        signing.private_key,
        algorithm="RS256",
        headers={"kid": signing.kid},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    base = os.environ.get(ENV_AS_BASE_URL, "").strip()
    if not base:
        raise RuntimeError(f"{ENV_AS_BASE_URL} must be set.")
    return base.rstrip("/")


def _is_enabled() -> bool:
    return os.environ.get(ENV_AS_ENABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def _error_redirect_or_400(
    redirect_uri: str | None, state: str | None, error: str, description: str
) -> Response:
    """Redirect the user-agent with an OAuth error when we have a valid
    redirect_uri; otherwise return a JSON 400 to whoever called us directly."""
    if redirect_uri:
        return RedirectResponse(
            _client_redirect_with_error(redirect_uri, state, error=error, description=description),
            status_code=302,
        )
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=400
    )


def _client_redirect_with_error(
    redirect_uri: str, state: str | None, *, error: str, description: str
) -> str:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode(params)}"


def _html_error(status: int, message: str) -> Response:
    body = f"<html><body><h2>Authentication failed</h2><p>{message}</p></body></html>".encode()
    return Response(content=body, media_type="text/html", status_code=status)


def all_routes() -> Iterable[Route]:  # pragma: no cover (debug)
    return list(build_app().routes)
