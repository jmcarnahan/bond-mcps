"""OAuth token cache backed by the encrypted SQLAlchemy database.

Public API is the same as the previous file-based TokenStore so the three
MCP call sites (github, atlassian, atlassian_cli) need no behavioral
changes — only the constructor optionally accepts a ``user_key`` kwarg.

For the read-modify-write refresh path we use a row-level lock so two
processes (e.g., the MCP server and the CLI logged in simultaneously)
don't both call the provider's refresh endpoint with the same refresh
token (which providers like Atlassian and Microsoft invalidate on use).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def current_user_key() -> str:
    """Resolve the user identity for token rows from the process environment.

    BOND_MCPS_USER_ID env var wins. Falls back to getpass.getuser() for
    convenience in local SQLite deployments only.

    For Postgres deployments the env var is required: ``getpass.getuser()``
    inside an ECS/Fargate container typically returns ``"root"``, which
    would silently collide across container tenants and write all users'
    tokens into the same DB rows.

    For per-request multi-tenant resolution, see ``resolve_user_key_for_request()``.
    """
    explicit = os.environ.get("BOND_MCPS_USER_ID")
    if explicit:
        return explicit
    db_url = os.environ.get("BOND_MCPS_DB_URL", "")
    if db_url.startswith("postgres"):
        # Lazy import to avoid pulling auth.db at module-load time
        # (the auth proxy server uses auth.* without the DB stack).
        from auth.db.session import DeploymentConfigError

        raise DeploymentConfigError(
            "BOND_MCPS_USER_ID is required for Postgres deployments. "
            "Falling back to getpass.getuser() in a container would "
            "typically return 'root' and collide across tenants. Set "
            "BOND_MCPS_USER_ID explicitly (e.g., to the tenant identifier "
            "or service account name)."
        )
    return getpass.getuser()


def resolve_user_key_for_request() -> str:
    """Resolve the user_key for the current MCP tool invocation.

    Multi-tenant mode is enabled by setting one of ``BOND_MCPS_JWT_JWKS_URI``
    or ``BOND_MCPS_JWT_PUBLIC_KEY`` and wiring each MCP with
    ``auth=build_remote_auth_provider(...)`` (see ``auth.jwt_identity``).

    In that mode, FastMCP's middleware has already validated the
    ``Authorization: Bearer <jwt>`` header before any tool runs and made the
    decoded claims available via ``get_access_token()``. We just read the
    configured sub claim and return it.

    Outside an HTTP context (CLI invocations, startup, tests), or when JWT
    mode is disabled, fall back to the env-based :func:`current_user_key`.
    """
    if not _jwt_mode_enabled():
        return current_user_key()

    # Lazy import: fastmcp is only present in MCP runtimes, not in the bare
    # auth CLI / proxy server.
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:
        return current_user_key()

    try:
        access = get_access_token()
    except Exception:
        # No active FastMCP request context (CLI, startup, tests).
        return current_user_key()

    if access is None:
        # Middleware would normally reject unauthenticated requests with 401,
        # so this branch is reachable only outside a request context.
        return current_user_key()

    from auth.jwt_identity import get_sub_claim

    sub_claim = get_sub_claim()
    user_key = access.claims.get(sub_claim)
    if not isinstance(user_key, str) or not user_key.strip():
        # Defensive: middleware required the claim, so this should be unreachable.
        raise RuntimeError(f"Validated JWT is missing a usable {sub_claim!r} claim.")
    return user_key


def _jwt_mode_enabled() -> bool:
    """Cheap inline check; mirrors auth.jwt_identity.is_jwt_verification_enabled.

    Inlined to avoid importing auth.jwt_identity (and through it, fastmcp) in
    the laptop / single-tenant code path.
    """
    return bool(
        os.environ.get("BOND_MCPS_JWT_JWKS_URI", "").strip()
        or os.environ.get("BOND_MCPS_JWT_PUBLIC_KEY", "").strip()
    )


class TokenStore:
    """DB-backed token store for a single (user_key, provider) pair."""

    def __init__(self, provider: str, *, user_key: str | None = None):
        self.provider = provider
        self.user_key = user_key or current_user_key()
        # Lazy repo construction: the DB stack is only touched on first method call.
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            from auth.db.repository import TokenRepository

            self._repo = TokenRepository()
        return self._repo

    def get_token(self) -> dict | None:
        """Return decrypted token dict, or None if missing or expired."""
        data = self.repo.get_token(self.user_key, self.provider)
        if data is None:
            return None
        if self._is_expired(data):
            return None
        return data

    def save_token(self, data: dict) -> None:
        self.repo.save_token(self.user_key, self.provider, data)

    def clear(self) -> None:
        self.repo.clear_token(self.user_key, self.provider)

    def refresh_if_needed(self, client_id: str, client_secret: str, token_url: str) -> str | None:
        """Refresh the access token if expired. Returns new access_token or None.

        Wraps the read → refresh → write in a row-level lock so concurrent
        processes don't burn refresh tokens on duplicate calls.
        """
        with self.repo.locked_token(self.user_key, self.provider) as locked:
            if locked.data is None:
                return None

            if not locked.is_expired():
                return locked.data.get("access_token")

            refresh_token = locked.data.get("refresh_token")
            if not refresh_token:
                return None

            logger.info(
                "Refreshing %s access_token (user_key=%s, token_url=%s)",
                self.provider,
                self.user_key,
                token_url,
            )
            try:
                new_data = _do_refresh(client_id, client_secret, token_url, refresh_token)
            except Exception:
                logger.exception("Token refresh failed for %s", self.provider)
                return None

            if not new_data or "access_token" not in new_data:
                logger.warning("Token refresh for %s returned no access_token", self.provider)
                return None

            if "expires_in" in new_data:
                new_data["expires_at"] = time.time() + new_data["expires_in"]
                new_data.pop("expires_in", None)

            locked.update(new_data)
            logger.info(
                "Refreshed %s access_token (user_key=%s, expires_in=%ss)",
                self.provider,
                self.user_key,
                int(new_data.get("expires_at", 0) - time.time())
                if new_data.get("expires_at")
                else "?",
            )
            return locked.data["access_token"]

    @staticmethod
    def _is_expired(data: dict) -> bool:
        expires_at = data.get("expires_at")
        if expires_at is None:
            return False
        return time.time() >= (expires_at - 60)


# Some provider token endpoints sit behind Cloudflare configurations that
# reject default Python client signatures (Python-urllib / python-httpx get
# HTTP 403 error 1010 "browser_signature_banned" — observed on WorkOS AuthKit
# tenants). A browser-like UA passes; other providers ignore it.
OUTBOUND_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _do_refresh(
    client_id: str, client_secret: str, token_url: str, refresh_token: str
) -> dict | None:
    """POST grant_type=refresh_token per RFC 6749 §4.1.3.

    Form-encoded body (required for GitHub, accepted by Atlassian/Microsoft);
    Accept: application/json. Returns parsed JSON dict, or None on HTTP error.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
    ).encode()

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": OUTBOUND_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("Token refresh HTTP error: %s", e.code)
        return None
