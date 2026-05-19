"""Short-lived tickets that gate the per-MCP ``/connect/<provider>`` flow.

When a tool call in JWT mode raises ``MissingProviderConnection``, the MCP
mints a ticket bound to the authenticated user and embeds it in the
``connect_url`` surfaced to the agent. The user opens the URL, the MCP's
``GET /connect/<provider>?ticket=...`` route consumes the ticket once,
and the resulting upstream provider OAuth callback writes a row into
``tokens.db`` keyed by the ticket's user_key.

The ticket itself is high-entropy random and is stored hashed (SHA-256
base64url) so a DB snapshot can't be replayed. Tickets are single-use and
TTL-bounded (5 minutes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from auth.db.models import ConnectTicket
from auth.db.session import get_session
from auth.oauth_utils import generate_opaque_secret, sha256_b64u

TICKET_TTL_SECONDS = 300


class TicketError(RuntimeError):
    """Ticket missing, expired, already used, or scoped to the wrong provider."""


@dataclass(frozen=True)
class ConsumedTicket:
    user_key: str
    provider: str


def mint_ticket(*, user_key: str, provider: str) -> str:
    """Persist a new ticket and return the opaque value to embed in URLs."""
    ticket = generate_opaque_secret(32)
    ticket_hash = sha256_b64u(ticket)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        _sweep(session, now)
        session.add(
            ConnectTicket(
                ticket_hash=ticket_hash,
                user_key=user_key,
                provider=provider,
                expires_at=now + timedelta(seconds=TICKET_TTL_SECONDS),
            )
        )
    return ticket


def consume_ticket(*, ticket: str, provider: str) -> ConsumedTicket:
    """Atomically mark a ticket used. Raises ``TicketError`` on failure."""
    ticket_hash = sha256_b64u(ticket)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        updated = session.execute(
            update(ConnectTicket)
            .where(
                ConnectTicket.ticket_hash == ticket_hash,
                ConnectTicket.used_at.is_(None),
            )
            .values(used_at=now)
        ).rowcount
        if not updated:
            existing = session.get(ConnectTicket, ticket_hash)
            if existing is None:
                raise TicketError("Unknown or expired connect ticket.")
            raise TicketError("Connect ticket already used.")

        row = session.get(ConnectTicket, ticket_hash)
        if _aware(row.expires_at) < now:
            raise TicketError("Connect ticket expired.")
        if row.provider != provider:
            raise TicketError(
                f"Ticket provider mismatch: bound to {row.provider!r}, "
                f"used with {provider!r}."
            )
        return ConsumedTicket(user_key=row.user_key, provider=row.provider)


def _aware(value: datetime) -> datetime:
    """Coerce SQLite's TZ-naive datetimes back to UTC-aware (Postgres preserves)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _sweep(session, now: datetime) -> None:
    cutoff = now - timedelta(minutes=10)
    session.query(ConnectTicket).filter(ConnectTicket.expires_at < cutoff).delete(
        synchronize_session=False
    )
