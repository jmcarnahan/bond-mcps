"""Opt-in Postgres integration tests.

These run only if BOND_MCPS_TEST_POSTGRES_URL is set. Locally you can
spin up Postgres in Docker and point the env var at it:

    docker run -d --name pg-test -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16
    export BOND_MCPS_TEST_POSTGRES_URL='postgresql+psycopg://postgres:test@localhost:5432/postgres?sslmode=disable'
    poetry run pytest tests/test_postgres_integration.py

We deliberately allow sslmode=disable for local Docker; the production
validate_db_url check still refuses these URLs at engine creation. The
test bypasses validation by calling create_engine directly via session
module internals.
"""

import os
import threading
import time

import pytest

from auth.alembic_config import upgrade_head
from auth.db import reset_for_tests
from auth.db.models import Base
from auth.db.repository import TokenRepository
from auth.db.session import get_engine

POSTGRES_URL = os.environ.get("BOND_MCPS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="BOND_MCPS_TEST_POSTGRES_URL not set (set to a Postgres URL to run)",
)


@pytest.fixture
def pg_engine(monkeypatch, encryption_key):
    """Postgres engine with the schema dropped + recreated per test."""
    # Allow the test URL through validate_db_url even if sslmode=disable.
    # Production deployments still get the strict check at engine creation.
    monkeypatch.setattr("auth.db.session.validate_db_url", lambda _url: None)
    monkeypatch.setenv("BOND_MCPS_DB_URL", POSTGRES_URL)
    reset_for_tests()
    engine = get_engine()
    # Drop any leftovers, then create fresh from ORM metadata for speed.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    reset_for_tests()


@pytest.fixture
def pg_repo(pg_engine):
    return TokenRepository()


def test_postgres_alembic_upgrade_head(monkeypatch):
    """Alembic upgrade runs cleanly against a fresh Postgres DB."""
    monkeypatch.setattr("auth.db.session.validate_db_url", lambda _url: None)
    monkeypatch.setenv("BOND_MCPS_DB_URL", POSTGRES_URL)
    reset_for_tests()
    engine = get_engine()
    Base.metadata.drop_all(engine)
    try:
        from sqlalchemy import inspect
        from sqlalchemy import text as _text

        with engine.begin() as conn:
            conn.execute(_text("DROP TABLE IF EXISTS alembic_version"))
        upgrade_head()
        tables = set(inspect(engine).get_table_names())
        assert {"provider_tokens", "msal_token_caches", "alembic_version"} <= tables
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            from sqlalchemy import text as _text

            conn.execute(_text("DROP TABLE IF EXISTS alembic_version"))
        reset_for_tests()


def test_postgres_save_get_round_trip(pg_repo):
    pg_repo.save_token(
        "alice",
        "github",
        {
            "access_token": "tok",
            "refresh_token": "rtk",
            "cloud_id": "cid",
        },
    )
    got = pg_repo.get_token("alice", "github")
    assert got["access_token"] == "tok"
    assert got["refresh_token"] == "rtk"
    assert got["cloud_id"] == "cid"


def test_postgres_concurrent_save_no_integrity_error(pg_repo):
    """The race that would fail on Postgres without ON CONFLICT DO UPDATE."""
    barrier = threading.Barrier(4)
    errors = []

    def worker(seq):
        barrier.wait()
        try:
            pg_repo.save_token("alice", "github", {"access_token": f"tok-{seq}"})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"upsert race produced errors: {errors}"
    got = pg_repo.get_token("alice", "github")
    assert got["access_token"].startswith("tok-")


def test_postgres_locked_token_serializes_writers(pg_repo):
    """SELECT FOR UPDATE genuinely locks the row on Postgres."""
    pg_repo.save_token(
        "alice",
        "github",
        {
            "access_token": "old",
            "refresh_token": "rtk",
            "expires_at": time.time() - 10,
        },
    )

    enter_times = []
    exit_times = []
    barrier = threading.Barrier(2)

    def worker(seq):
        barrier.wait()
        with pg_repo.locked_token("alice", "github") as locked:
            enter_times.append(time.monotonic())
            time.sleep(0.05)
            locked.update(
                {
                    "access_token": f"new-{seq}",
                    "refresh_token": "rtk",
                    "expires_at": time.time() + 3600,
                }
            )
            exit_times.append(time.monotonic())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(enter_times) == 2 and len(exit_times) == 2
    first_exit = min(exit_times)
    later_enter = max(enter_times)
    assert later_enter >= first_exit, "locked_token did not serialize on Postgres"


def test_postgres_msal_cache_round_trip(pg_repo):
    blob = '{"AccessToken": {"acct": {"credential_type": "AccessToken"}}}'
    pg_repo.save_msal_cache("alice", blob)
    assert pg_repo.get_msal_cache("alice") == blob


def test_postgres_timezone_aware_round_trip(pg_repo):
    """expires_at survives a Postgres TIMESTAMP WITH TIME ZONE round-trip."""
    in_epoch = time.time() + 3600
    pg_repo.save_token(
        "alice",
        "github",
        {
            "access_token": "x",
            "expires_at": in_epoch,
        },
    )
    got = pg_repo.get_token("alice", "github")
    assert abs(got["expires_at"] - in_epoch) < 1.0
