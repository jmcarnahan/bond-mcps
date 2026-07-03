"""Resolve a user's email → Cognito ``sub`` for the token-exchange grant.

bond-ai identifies users by email (its ``bond JWT`` carries ``sub=email``),
but the access tokens this AS mints for the ``authorization_code`` flow carry
``sub=<Cognito sub>`` (the upstream IdP subject). To keep MCP pods verifying a
single subject identifier, the token-exchange path maps the incoming email to
the same Cognito ``sub`` the interactive flow would have produced.

Lookups hit Cognito's ``ListUsers`` with an ``email = "..."`` filter, cached
in-process so a burst of exchanges for one user doesn't fan out to the Cognito
API. When no user pool is configured we fall back to using the email itself as
the subject — but only for SQLite dev; a Postgres-backed deployment refuses the
passthrough (mirrors ``keys.py``'s fail-closed stance on auto-generated keys).
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

ENV_COGNITO_USER_POOL_ID = "BOND_MCPS_AS_COGNITO_USER_POOL_ID"
ENV_COGNITO_REGION = "BOND_MCPS_AS_COGNITO_REGION"
ENV_AWS_REGION = "AWS_REGION"

_POSITIVE_TTL_SECONDS = 15 * 60  # 15 min
_NEGATIVE_TTL_SECONDS = 60  # 60 s

# email -> (sub_or_None, expires_at_monotonic)
_cache: dict[str, tuple[str | None, float]] = {}
_cache_lock = threading.Lock()


def reset_cache_for_tests() -> None:
    """Clear the in-process lookup cache. For test isolation only."""
    with _cache_lock:
        _cache.clear()


def resolve_cognito_sub(email: str) -> str | None:
    """Map ``email`` to a Cognito ``sub``, or ``None`` if not resolvable.

    Results are cached in-process (positive 15 min, negative 60 s). When no
    user pool is configured, the email is returned as-is (dev passthrough) —
    except on a Postgres deployment, where the passthrough is refused.
    """
    email = email.strip().lower()
    if not email:
        return None

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(email)
        if cached is not None and cached[1] > now:
            return cached[0]

    sub = _lookup_uncached(email)

    ttl = _POSITIVE_TTL_SECONDS if sub is not None else _NEGATIVE_TTL_SECONDS
    with _cache_lock:
        _cache[email] = (sub, time.monotonic() + ttl)
    return sub


def _lookup_uncached(email: str) -> str | None:
    pool_id = os.environ.get(ENV_COGNITO_USER_POOL_ID, "").strip()
    if not pool_id:
        # Dev passthrough: identify the user by their email directly. Refused
        # on a Postgres deployment so a misconfigured prod never silently
        # mints tokens with an email subject instead of a real Cognito sub.
        if _is_postgres_deployment():
            logger.error(
                "%s is unset on a Postgres deployment; refusing the email "
                "passthrough for token exchange (would mint sub=email).",
                ENV_COGNITO_USER_POOL_ID,
            )
            return None
        return email

    region = (
        os.environ.get(ENV_COGNITO_REGION, "").strip() or os.environ.get(ENV_AWS_REGION, "").strip()
    )

    # Lazy import so laptop use (no pool id, no boto3 installed) still works.
    import boto3

    client = boto3.client("cognito-idp", region_name=region or None)
    try:
        resp = client.list_users(
            UserPoolId=pool_id,
            Filter=f'email = "{email}"',
            Limit=1,
        )
    except Exception:  # noqa: BLE001 - boto3 raises many client-error types
        logger.exception("Cognito ListUsers failed for %s", email)
        return None

    users = resp.get("Users") or []
    if not users:
        logger.warning("No Cognito user found for email=%s", email)
        return None
    return _extract_sub(users[0])


def _extract_sub(user: dict) -> str | None:
    """Pull the ``sub`` attribute out of a Cognito user record."""
    for attr in user.get("Attributes", []):
        if attr.get("Name") == "sub":
            value = attr.get("Value")
            if value:
                return value
    # Fall back to the immutable Username, which is the sub for pools created
    # with sub as the sign-in identifier.
    return user.get("Username") or None


def _is_postgres_deployment() -> bool:
    return os.environ.get("BOND_MCPS_DB_URL", "").startswith("postgres")
