"""JWT-based caller identity for multi-tenant deployments.

When enabled (one of ``BOND_MCPS_JWT_JWKS_URI`` or ``BOND_MCPS_JWT_PUBLIC_KEY``
is set), each MCP becomes an OAuth 2.1 Resource Server. Incoming requests must
carry ``Authorization: Bearer <jwt>``; the bond-mcps Authorization Server (see
``auth.auth_server``) issues those JWTs after upstream Cognito/Okta sign-in.

This module exposes two builders:

* :func:`build_verifier` -- a configured FastMCP ``JWTVerifier`` that handles
  JWKS fetching, signature validation, audience/issuer checks. Used both as a
  standalone verifier (e.g. ``/connect`` ticket auth) and as the inner token
  verifier for :func:`build_remote_auth_provider`.

* :func:`build_remote_auth_provider` -- a FastMCP ``RemoteAuthProvider`` that
  wraps the verifier and adds RFC 9728 discovery (``/.well-known/oauth-
  protected-resource``) and ``WWW-Authenticate`` 401 responses. Pass it to
  ``FastMCP(auth=...)`` in each MCP's entry file.

When neither env var is set the deployment is treated as single-tenant and
:func:`current_user_key` (env-based) is the source of identity for everyone.
The builders return ``None`` in that case so the MCP starts without an auth
middleware.

``fastmcp`` is imported lazily so non-MCP consumers of this package (the
proxy server, alembic migrations, the CLI) don't pay the import cost.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

logger = logging.getLogger(__name__)

# Verifier config
ENV_JWKS_URI = "BOND_MCPS_JWT_JWKS_URI"
ENV_PUBLIC_KEY = "BOND_MCPS_JWT_PUBLIC_KEY"
ENV_ISSUER = "BOND_MCPS_JWT_ISSUER"
ENV_AUDIENCE = "BOND_MCPS_JWT_AUDIENCE"
ENV_ALGORITHM = "BOND_MCPS_JWT_ALGORITHM"
ENV_SUB_CLAIM = "BOND_MCPS_JWT_SUB_CLAIM"

# RemoteAuthProvider config
ENV_AS_BASE_URL = "BOND_MCPS_AS_BASE_URL"
ENV_RS_PUBLIC_URL = "BOND_MCPS_PUBLIC_URL"

DEFAULT_ALGORITHM = "RS256"
DEFAULT_SUB_CLAIM = "sub"


class JWTConfigError(RuntimeError):
    """JWT verification env config is incomplete or malformed."""


def is_jwt_verification_enabled() -> bool:
    """True iff a JWT verification source is configured.

    Either ``BOND_MCPS_JWT_JWKS_URI`` (preferred, for RS256 + key rotation)
    or ``BOND_MCPS_JWT_PUBLIC_KEY`` (PEM, or HS256 shared secret for tests).
    """
    return bool(
        os.environ.get(ENV_JWKS_URI, "").strip() or os.environ.get(ENV_PUBLIC_KEY, "").strip()
    )


def get_sub_claim() -> str:
    """The JWT claim used as the per-request user_key (default ``sub``)."""
    return os.environ.get(ENV_SUB_CLAIM, "").strip() or DEFAULT_SUB_CLAIM


def _parse_csv(env_value: str) -> list[str] | str | None:
    """Audience/issuer support comma-separated lists; bare values stay bare."""
    if not env_value:
        return None
    parts = [p.strip() for p in env_value.split(",") if p.strip()]
    if not parts:
        return None
    return parts if len(parts) > 1 else parts[0]


def _resolve_audience() -> list[str] | str | None:
    """Pick the audience list the verifier should accept.

    Background: FastMCP's ``RemoteAuthProvider`` advertises the protected-
    resource URI as ``<BOND_MCPS_PUBLIC_URL>/mcp`` (RFC 9728). Per RFC 8707,
    Claude Code passes that URI as ``resource`` on ``/oauth/authorize``, and
    the bond-mcps AS sets it as the JWT's ``aud`` claim. The verifier here
    MUST accept that URI, or every Claude Code request will 401.

    Resolution:
      * If ``BOND_MCPS_JWT_AUDIENCE`` is set, honour it (CSV or scalar).
      * Always also accept ``<BOND_MCPS_PUBLIC_URL>/mcp`` when the public
        URL is known — that's the canonical aud value Claude Code will
        send. We merge rather than replace so operator-set friendly names
        keep working alongside.
    """
    explicit = _parse_csv(os.environ.get(ENV_AUDIENCE, "").strip())
    pub = os.environ.get(ENV_RS_PUBLIC_URL, "").strip().rstrip("/")
    canonical = f"{pub}/mcp" if pub else None

    if explicit is None:
        return canonical  # may be None — that's fine, audience check then skipped

    values: list[str] = [explicit] if isinstance(explicit, str) else list(explicit)
    if canonical and canonical not in values:
        values.append(canonical)
    return values if len(values) > 1 else values[0]


def build_verifier() -> JWTVerifier:
    """Construct a FastMCP ``JWTVerifier`` from env config.

    Mutually exclusive sources: JWKS URI (preferred) or static public key /
    HS256 shared secret. Issuer and audience are validated when configured.

    Raises:
        JWTConfigError: neither source is set, or both are set, or the
            algorithm doesn't match the source shape.
    """
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    jwks_uri = os.environ.get(ENV_JWKS_URI, "").strip()
    public_key = os.environ.get(ENV_PUBLIC_KEY, "").strip()

    if jwks_uri and public_key:
        raise JWTConfigError(f"Only one of {ENV_JWKS_URI} or {ENV_PUBLIC_KEY} may be set.")
    if not jwks_uri and not public_key:
        raise JWTConfigError(
            f"Either {ENV_JWKS_URI} or {ENV_PUBLIC_KEY} must be set " "to enable JWT verification."
        )

    algorithm = os.environ.get(ENV_ALGORITHM, "").strip() or DEFAULT_ALGORITHM
    issuer = _parse_csv(os.environ.get(ENV_ISSUER, "").strip())
    audience = _resolve_audience()

    kwargs: dict = {
        "algorithm": algorithm,
        "issuer": issuer,
        "audience": audience,
    }
    if jwks_uri:
        kwargs["jwks_uri"] = jwks_uri
    else:
        kwargs["public_key"] = public_key

    return JWTVerifier(**kwargs)


def build_remote_auth_provider(resource_name: str) -> RemoteAuthProvider | None:
    """Construct a ``RemoteAuthProvider`` for use in ``FastMCP(auth=...)``.

    Returns ``None`` when JWT verification is disabled — callers should pass
    ``None`` straight through to FastMCP, which leaves the server in the
    laptop / single-tenant mode (no middleware, no discovery endpoint).

    Required env when enabled:

    * ``BOND_MCPS_AS_BASE_URL`` -- public URL of the bond-mcps Authorization
      Server. Surfaced in protected-resource-metadata so MCP clients can
      discover where to authenticate.
    * ``BOND_MCPS_PUBLIC_URL`` -- public URL of *this* MCP. Used as the
      resource server URL in the metadata document.
    """
    if not is_jwt_verification_enabled():
        return None

    from fastmcp.server.auth import RemoteAuthProvider
    from pydantic import AnyHttpUrl

    as_base_url = os.environ.get(ENV_AS_BASE_URL, "").strip()
    if not as_base_url:
        raise JWTConfigError(f"{ENV_AS_BASE_URL} must be set when JWT verification is enabled.")
    rs_public_url = os.environ.get(ENV_RS_PUBLIC_URL, "").strip()
    if not rs_public_url:
        raise JWTConfigError(f"{ENV_RS_PUBLIC_URL} must be set when JWT verification is enabled.")

    return RemoteAuthProvider(
        token_verifier=build_verifier(),
        authorization_servers=[AnyHttpUrl(as_base_url)],
        base_url=rs_public_url,
        resource_name=resource_name,
    )
