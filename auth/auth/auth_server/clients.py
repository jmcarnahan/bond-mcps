"""OAuth client registry — DCR + static fallback.

The bond-mcps AS supports two ways to register OAuth clients:

* **Dynamic Client Registration** (RFC 7591) via ``POST /oauth/register``.
  Public clients only — every accepted registration sets
  ``token_endpoint_auth_method=none`` and grant types are forced to
  ``[authorization_code, refresh_token]``. Returns a generated client_id.

* **Static configuration** via ``BOND_MCPS_STATIC_CLIENTS`` (JSON env var).
  Useful when deploying behind a CDN that strips DCR, or for pre-seeding a
  shared Claude Code client_id. Static clients are seeded into the DB on
  first call and are flagged ``is_static=True``.

Either way, redirect URIs must match the deployment's allowlist (loopback
addresses are always permitted for public clients).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from sqlalchemy import select

from auth.db.models import OAuthClient
from auth.db.session import get_session
from auth.oauth_utils import generate_opaque_secret

logger = logging.getLogger(__name__)

ENV_STATIC_CLIENTS = "BOND_MCPS_STATIC_CLIENTS"
ENV_ALLOWED_REDIRECT_HOSTS = "BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS"

DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
DEFAULT_RESPONSE_TYPES = ["code"]


class ClientRegistrationError(ValueError):
    """The client metadata is missing or violates policy."""


@dataclass(frozen=True)
class ClientRecord:
    client_id: str
    client_name: str | None
    redirect_uris: list[str]
    token_endpoint_auth_method: str
    grant_types: list[str]
    response_types: list[str]
    scope: str | None
    is_static: bool


# ---------------------------------------------------------------------------
# Registration (DCR)
# ---------------------------------------------------------------------------


def register_client(metadata: dict) -> ClientRecord:
    """Persist a new DCR-registered client.

    Forces public-client semantics: ``token_endpoint_auth_method=none``
    and the default grant/response type set. Redirect URIs are validated
    against the deployment's allowlist (plus loopback).
    """
    redirect_uris = metadata.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        raise ClientRegistrationError("redirect_uris must be a non-empty list.")
    for uri in redirect_uris:
        _ensure_allowed_redirect(uri)

    client_id = "bm-" + generate_opaque_secret(16)
    record = ClientRecord(
        client_id=client_id,
        client_name=metadata.get("client_name"),
        redirect_uris=list(redirect_uris),
        token_endpoint_auth_method="none",
        grant_types=DEFAULT_GRANT_TYPES,
        response_types=DEFAULT_RESPONSE_TYPES,
        scope=metadata.get("scope"),
        is_static=False,
    )
    _persist(record)
    return record


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_client(client_id: str) -> ClientRecord | None:
    """Return a client record, seeding statics on demand."""
    _ensure_statics_seeded()
    with get_session() as session:
        row = session.get(OAuthClient, client_id)
        if row is None:
            return None
        return _row_to_record(row)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_redirect_uri_allowed(client: ClientRecord, redirect_uri: str) -> bool:
    """Exact-match check, OAuth 2.1 §1.5: registered URIs only.

    One carve-out, RFC 8252 §7.3: native apps bind their callback listener to
    whatever loopback port is free at auth time, so for a loopback redirect the
    port MUST be allowed to differ from the registered one. Scheme, host, and
    path still have to match a registered loopback URI exactly.
    """
    presented = urlparse(redirect_uri)
    # Rejected before the exact-match check, so a row registered before _ensure_allowed_redirect
    # validated these (or seeded via BOND_MCPS_STATIC_CLIENTS) still cannot be used: the AS
    # appends its own ?code=..., and a pre-existing query would fold `code` into it.
    if presented.query or presented.fragment or presented.username or presented.password:
        return False
    if redirect_uri in client.redirect_uris:
        return True
    if presented.scheme != "http" or presented.hostname not in _LOOPBACK_HOSTS:
        return False
    try:
        presented.port  # raises ValueError on a non-numeric port
    except ValueError:
        return False
    return any(
        reg.scheme == "http"
        and reg.hostname == presented.hostname
        and reg.path == presented.path
        and not (reg.query or reg.fragment)
        for reg in map(urlparse, client.redirect_uris)
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_STATICS_SEEDED = False


def _ensure_statics_seeded() -> None:
    global _STATICS_SEEDED
    if _STATICS_SEEDED:
        return
    raw = os.environ.get(ENV_STATIC_CLIENTS, "").strip()
    if not raw:
        _STATICS_SEEDED = True
        return
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid %s JSON: %s", ENV_STATIC_CLIENTS, exc)
        _STATICS_SEEDED = True
        return
    if not isinstance(entries, list):
        logger.error("%s must be a JSON array of client objects.", ENV_STATIC_CLIENTS)
        _STATICS_SEEDED = True
        return
    for entry in entries:
        try:
            record = _record_from_static(entry)
            _persist(record, replace=True)
        except Exception:
            logger.exception("Failed to seed static client: %s", entry)
    _STATICS_SEEDED = True


def _record_from_static(entry: dict) -> ClientRecord:
    client_id = entry.get("client_id")
    if not client_id:
        raise ClientRegistrationError("Static client missing client_id.")
    redirect_uris = entry.get("redirect_uris") or []
    if not redirect_uris:
        raise ClientRegistrationError(f"Static client {client_id} has no redirect_uris.")
    for uri in redirect_uris:
        _ensure_allowed_redirect(uri)
    return ClientRecord(
        client_id=client_id,
        client_name=entry.get("client_name"),
        redirect_uris=list(redirect_uris),
        token_endpoint_auth_method=entry.get("token_endpoint_auth_method", "none"),
        grant_types=entry.get("grant_types", DEFAULT_GRANT_TYPES),
        response_types=entry.get("response_types", DEFAULT_RESPONSE_TYPES),
        scope=entry.get("scope"),
        is_static=True,
    )


def _persist(record: ClientRecord, *, replace: bool = False) -> None:
    with get_session() as session:
        existing = session.get(OAuthClient, record.client_id)
        if existing is not None and not replace:
            raise ClientRegistrationError(f"client_id collision: {record.client_id}")
        if existing is not None:
            existing.client_name = record.client_name
            existing.redirect_uris = record.redirect_uris
            existing.token_endpoint_auth_method = record.token_endpoint_auth_method
            existing.grant_types = record.grant_types
            existing.response_types = record.response_types
            existing.scope = record.scope
            existing.is_static = record.is_static
        else:
            session.add(
                OAuthClient(
                    client_id=record.client_id,
                    client_name=record.client_name,
                    redirect_uris=record.redirect_uris,
                    token_endpoint_auth_method=record.token_endpoint_auth_method,
                    grant_types=record.grant_types,
                    response_types=record.response_types,
                    scope=record.scope,
                    is_static=record.is_static,
                )
            )
        session.commit()


def _row_to_record(row: OAuthClient) -> ClientRecord:
    return ClientRecord(
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=list(row.redirect_uris or []),
        token_endpoint_auth_method=row.token_endpoint_auth_method,
        grant_types=list(row.grant_types or []),
        response_types=list(row.response_types or []),
        scope=row.scope,
        is_static=bool(row.is_static),
    )


def _ensure_allowed_redirect(uri: str) -> None:
    """Validate a redirect_uri against OAuth 2.1 §1.5 + our allowlist.

    Policy:
      * Loopback HTTP (RFC 8252 §7.3) is always allowed: ``http://localhost``,
        ``http://127.0.0.1``, ``http://[::1]`` — any port.
      * HTTPS to a host in ``BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS`` (CSV) is
        allowed.
      * Anything else is **rejected**, including non-loopback HTTP and
        HTTPS to hosts not in the allowlist. When the env is unset, ONLY
        loopback is permitted — fail-closed posture.
      * A query or fragment is **rejected**: the AS appends its own
        ``?code=...`` to this URI, so a pre-existing query would corrupt the
        response (``?evil=1?code=...`` parses ``code`` into ``evil``), and
        RFC 6749 §3.1.2 forbids a fragment outright.
    """
    parsed = urlparse(uri)
    if not parsed.scheme or not parsed.netloc:
        raise ClientRegistrationError(f"Malformed redirect_uri: {uri}")
    if parsed.query or parsed.fragment:
        raise ClientRegistrationError(f"redirect_uri must have no query or fragment: {uri}")
    if parsed.username or parsed.password:
        raise ClientRegistrationError(f"redirect_uri must not contain userinfo: {uri}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http":
        if host in {"localhost", "127.0.0.1", "::1"}:
            return
        raise ClientRegistrationError(f"Non-HTTPS redirect_uri only permitted on loopback: {uri}")
    if parsed.scheme != "https":
        raise ClientRegistrationError(f"Unsupported redirect scheme: {parsed.scheme}")
    allowed = {
        h.strip().lower()
        for h in os.environ.get(ENV_ALLOWED_REDIRECT_HOSTS, "").split(",")
        if h.strip()
    }
    if not allowed:
        # Fail-closed: an unconfigured deployment must NOT accept arbitrary
        # HTTPS callbacks. Operator must explicitly opt in via
        # BOND_MCPS_AS_ALLOWED_REDIRECT_HOSTS. Loopback (above) is the only
        # path that works without configuration.
        raise ClientRegistrationError(
            f"HTTPS redirect_uri {uri!r} rejected: "
            f"{ENV_ALLOWED_REDIRECT_HOSTS} is not configured. "
            "Set it to a CSV of trusted hosts (e.g. workstation hostnames "
            "for org-internal Claude Code installs)."
        )
    if host not in allowed:
        raise ClientRegistrationError(
            f"redirect_uri host {host!r} not in {ENV_ALLOWED_REDIRECT_HOSTS} allowlist."
        )


def all_clients() -> Iterable[ClientRecord]:
    """Debug helper: list every registered client."""
    _ensure_statics_seeded()
    with get_session() as session:
        rows = session.execute(select(OAuthClient)).scalars().all()
        return [_row_to_record(r) for r in rows]
