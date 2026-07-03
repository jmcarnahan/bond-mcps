"""RFC 8693 token-exchange grant for the bond-mcps Authorization Server.

bond-ai issues an HS256 "bond JWT" (``iss=bond-ai``, ``aud`` contains
``mcp-server``, ``sub=email``, signed with a secret shared with this AS). This
grant exchanges that JWT for an AS-issued RS256 access token, so MCP pods keep
a single verifier (the AS JWKS) regardless of whether the caller is Claude Code
(interactive ``authorization_code`` flow) or bond-ai (this delegated path).

The grant is **inert unless** ``BOND_MCPS_AS_BOND_JWT_SECRET`` is set: the
dispatcher returns ``unsupported_grant_type`` and the metadata endpoint does
not advertise it. This keeps the interactive-only deployments unchanged.
"""

from __future__ import annotations

import logging
import os

import jwt
from starlette.responses import JSONResponse, Response

from auth.auth_server import cognito_lookup

logger = logging.getLogger(__name__)

GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SUBJECT_TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
_ISSUED_TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

ENV_BOND_JWT_SECRET = "BOND_MCPS_AS_BOND_JWT_SECRET"
ENV_BOND_JWT_AUDIENCE = "BOND_MCPS_AS_BOND_JWT_AUDIENCE"
ENV_BOND_JWT_ISSUER = "BOND_MCPS_AS_BOND_JWT_ISSUER"
ENV_EXCHANGE_CLIENT_ID = "BOND_MCPS_AS_EXCHANGE_CLIENT_ID"
ENV_EXCHANGE_TOKEN_TTL = "BOND_MCPS_AS_EXCHANGE_TOKEN_TTL_SECONDS"

_DEFAULT_AUDIENCE = "mcp-server"
_DEFAULT_ISSUER = "bond-ai"
_DEFAULT_CLIENT_ID = "bond-ai"
_DEFAULT_TTL_SECONDS = 300
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3600

# PyJWT leeway (seconds) on exp/iat to tolerate small clock skew between
# bond-ai and this AS.
_LEEWAY_SECONDS = 30


def is_enabled() -> bool:
    """The grant is active only when the shared secret is configured."""
    return bool(os.environ.get(ENV_BOND_JWT_SECRET, "").strip())


def handle_token_exchange(form) -> Response:
    """Handle a ``grant_type=urn:...:token-exchange`` /oauth/token POST."""
    secret = os.environ.get(ENV_BOND_JWT_SECRET, "").strip()
    if not secret:
        return _oauth_error(
            "unsupported_grant_type",
            f"Unsupported grant_type={GRANT_TOKEN_EXCHANGE!r}.",
        )

    subject_token = form.get("subject_token")
    if not subject_token:
        return _oauth_error("invalid_request", "subject_token is required.")

    subject_token_type = form.get("subject_token_type")
    if subject_token_type and subject_token_type != _SUBJECT_TOKEN_TYPE_JWT:
        return _oauth_error(
            "invalid_request",
            f"subject_token_type must be {_SUBJECT_TOKEN_TYPE_JWT!r}.",
        )

    resource = form.get("resource")
    if not resource:
        return _oauth_error("invalid_target", "resource is required and identifies the target MCP.")

    client_id = form.get("client_id")
    expected_client_id = os.environ.get(ENV_EXCHANGE_CLIENT_ID, "").strip() or _DEFAULT_CLIENT_ID
    if not client_id or client_id != expected_client_id:
        return _oauth_error(
            "invalid_client", "client_id is required and must be the exchange client."
        )

    audience = os.environ.get(ENV_BOND_JWT_AUDIENCE, "").strip() or _DEFAULT_AUDIENCE
    issuer = os.environ.get(ENV_BOND_JWT_ISSUER, "").strip() or _DEFAULT_ISSUER
    try:
        claims = jwt.decode(
            subject_token,
            secret,
            algorithms=["HS256"],
            audience=audience,
            issuer=issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("token-exchange subject_token rejected: %s", exc)
        return _oauth_error("invalid_grant", "subject_token is invalid or expired.")

    email = str(claims["sub"]).strip().lower()
    if not email:
        return _oauth_error("invalid_grant", "subject_token has an empty subject.")

    cognito_sub = cognito_lookup.resolve_cognito_sub(email)
    if not cognito_sub:
        logger.warning("token-exchange could not resolve Cognito sub for %s", email)
        return _oauth_error("invalid_grant", "Subject could not be resolved to a known user.")

    # Deferred to avoid a circular import (endpoints imports this module for
    # the /oauth/token dispatcher).
    from auth.auth_server.endpoints import _sign_access_token

    ttl = _exchange_ttl_seconds()
    access_token = _sign_access_token(
        user_key=cognito_sub,
        email=email,
        client_id=expected_client_id,
        resource=resource,
        scope=None,
        ttl_seconds=ttl,
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "issued_token_type": _ISSUED_TOKEN_TYPE_ACCESS,
            "token_type": "Bearer",
            "expires_in": ttl,
        }
    )


def _exchange_ttl_seconds() -> int:
    raw = (os.environ.get(ENV_EXCHANGE_TOKEN_TTL) or "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %ss",
            ENV_EXCHANGE_TOKEN_TTL,
            raw,
            _DEFAULT_TTL_SECONDS,
        )
        return _DEFAULT_TTL_SECONDS
    return max(_MIN_TTL_SECONDS, min(value, _MAX_TTL_SECONDS))


def _oauth_error(error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=400)
