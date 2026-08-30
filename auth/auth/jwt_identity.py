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
ENV_SHARED_SECRET = "BOND_MCPS_JWT_SHARED_SECRET"
ENV_SHARED_SECRET_ISSUER = "BOND_MCPS_JWT_SHARED_SECRET_ISSUER"

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


class CompositeTokenVerifier:
    """Verifies JWTs from multiple issuers with different algorithms.

    Primary: RS256 via JWKS (bond-mcps AS, Claude Code users)
    Secondary: HS256 via shared secret (bond-ai backend)

    Routing: decode JWT header (unverified) to read ``alg``. HS256 tokens
    are verified via the shared secret; all others go to the JWKS verifier.
    HS256 tokens must also have the expected secondary issuer — this prevents
    algorithm confusion attacks.
    """

    def __init__(self, primary_verifier, secondary_verifier, secondary_issuer: str):
        self.primary = primary_verifier
        self.secondary = secondary_verifier
        self.secondary_issuer = secondary_issuer
        self.required_scopes = getattr(primary_verifier, "required_scopes", None) or []
        self.scopes_supported = getattr(primary_verifier, "scopes_supported", None) or []

    async def verify_token(self, token: str):
        """Route verification based on JWT algorithm header."""
        import jwt as pyjwt

        try:
            header = pyjwt.get_unverified_header(token)
        except Exception:
            return await self.primary.verify_token(token)

        alg = header.get("alg", "")

        if alg == "HS256":
            try:
                unverified = pyjwt.decode(token, options={"verify_signature": False})
            except pyjwt.DecodeError:
                logger.debug("HS256 token rejected: malformed JWT payload")
                return None
            if unverified.get("iss") != self.secondary_issuer:
                logger.warning(
                    "HS256 token rejected: issuer %r does not match expected %r",
                    unverified.get("iss"),
                    self.secondary_issuer,
                )
                return None
            return await self.secondary.verify_token(token)

        return await self.primary.verify_token(token)


def build_verifier() -> "JWTVerifier | CompositeTokenVerifier":
    """Construct a JWT verifier from env config.

    Supports three modes:
    1. JWKS only (standard deployed with AS)
    2. Static key only (test/dev)
    3. Composite: JWKS + shared secret (deployed with bond-ai integration)

    Raises:
        JWTConfigError: config is incomplete, conflicting, or malformed.
    """
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    jwks_uri = os.environ.get(ENV_JWKS_URI, "").strip()
    public_key = os.environ.get(ENV_PUBLIC_KEY, "").strip()
    shared_secret = os.environ.get(ENV_SHARED_SECRET, "").strip()

    # --- Composite mode: JWKS + shared secret ---
    if jwks_uri and shared_secret:
        if public_key:
            raise JWTConfigError(
                f"Cannot set {ENV_PUBLIC_KEY} when using composite mode "
                f"({ENV_JWKS_URI} + {ENV_SHARED_SECRET})."
            )

        issuer = _parse_csv(os.environ.get(ENV_ISSUER, "").strip())
        audience = _resolve_audience()
        secondary_issuer = os.environ.get(ENV_SHARED_SECRET_ISSUER, "").strip() or "bond-ai"

        primary = JWTVerifier(
            jwks_uri=jwks_uri,
            algorithm="RS256",
            issuer=issuer,
            audience=audience,
        )
        secondary = JWTVerifier(
            public_key=shared_secret,
            algorithm="HS256",
            issuer=secondary_issuer,
            audience=audience,
        )

        logger.info(
            "Composite JWT verification: JWKS (RS256) + shared secret (HS256, issuer=%s)",
            secondary_issuer,
        )
        return CompositeTokenVerifier(
            primary_verifier=primary,
            secondary_verifier=secondary,
            secondary_issuer=secondary_issuer,
        )

    # --- Single-source modes (existing behavior) ---
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

    ``resource_name`` is only the human-readable label in that metadata
    document -- it does NOT affect token validation. Accepted audiences come
    from ``BOND_MCPS_JWT_AUDIENCE`` plus ``<BOND_MCPS_PUBLIC_URL>/mcp`` (see
    ``build_verifier``), so this may differ from the MCP's path prefix without
    breaking auth. By convention callers pass their provider key.
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


def register_noauth_wellknown(mcp) -> None:
    """Register a JSON-returning /.well-known handler when JWT mode is off.

    MCP SDK clients probe /.well-known/oauth-protected-resource on connect.
    When auth is disabled (local mode), FastMCP doesn't mount this route and
    the default Starlette 404 returns HTML "Not Found" — which the SDK fails
    to parse as JSON, producing a noisy "SDK auth failed" warning.

    This registers a catch-all that returns a proper JSON 404 so the SDK
    handles it gracefully. Only mounted when JWT mode is off (when it's on,
    FastMCP's RemoteAuthProvider provides the real route).
    """
    if is_jwt_verification_enabled():
        return

    from starlette.responses import JSONResponse

    async def _wellknown_noauth(request):
        return JSONResponse(
            {
                "error": "not_found",
                "error_description": "This server does not require transport authentication.",
            },
            status_code=404,
        )

    # The MCP SDK probes multiple discovery/registration paths on connect:
    #   /.well-known/oauth-protected-resource[/mcp]  (RFC 9728)
    #   /.well-known/oauth-authorization-server      (RFC 8414)
    #   /.well-known/openid-configuration            (OIDC)
    #   /register                                    (dynamic client registration)
    # In local mode these don't exist; the default Starlette 404 returns HTML
    # which the SDK can't parse as JSON. Return proper JSON so it fails cleanly.
    mcp.custom_route("/.well-known/{path:path}", methods=["GET"])(_wellknown_noauth)
    mcp.custom_route("/register", methods=["POST"])(_wellknown_noauth)
