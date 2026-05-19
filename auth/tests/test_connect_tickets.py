"""Tests for the connect-ticket store (per-MCP /connect/<provider> bootstrap)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auth.alembic_config import upgrade_head
from auth.connect_tickets import TicketError, consume_ticket, mint_ticket
from auth.db import reset_for_tests


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    upgrade_head()
    yield
    reset_for_tests()


class TestMintAndConsume:
    def test_round_trip(self, fresh_db):
        ticket = mint_ticket(user_key="alice", provider="github")
        consumed = consume_ticket(ticket=ticket, provider="github")
        assert consumed.user_key == "alice"
        assert consumed.provider == "github"

    def test_single_use(self, fresh_db):
        ticket = mint_ticket(user_key="alice", provider="github")
        consume_ticket(ticket=ticket, provider="github")
        with pytest.raises(TicketError, match="already used"):
            consume_ticket(ticket=ticket, provider="github")

    def test_unknown_ticket(self, fresh_db):
        with pytest.raises(TicketError, match="Unknown"):
            consume_ticket(ticket="bogus-value", provider="github")

    def test_provider_mismatch(self, fresh_db):
        ticket = mint_ticket(user_key="alice", provider="github")
        with pytest.raises(TicketError, match="provider mismatch"):
            consume_ticket(ticket=ticket, provider="atlassian")

    def test_tickets_are_unique(self, fresh_db):
        t1 = mint_ticket(user_key="alice", provider="github")
        t2 = mint_ticket(user_key="alice", provider="github")
        assert t1 != t2

    def test_expired_ticket(self, fresh_db):
        ticket = mint_ticket(user_key="alice", provider="github")
        # Force-expire the row.
        from sqlalchemy import update

        from auth.db.models import ConnectTicket
        from auth.db.session import get_session
        from auth.oauth_utils import sha256_b64u

        with get_session() as session:
            session.execute(
                update(ConnectTicket)
                .where(ConnectTicket.ticket_hash == sha256_b64u(ticket))
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        with pytest.raises(TicketError, match="expired|Unknown"):
            consume_ticket(ticket=ticket, provider="github")
