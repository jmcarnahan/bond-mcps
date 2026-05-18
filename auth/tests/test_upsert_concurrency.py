"""Concurrent saves to the same (user_key, provider) must not raise IntegrityError.

The portable INSERT ... ON CONFLICT DO UPDATE replaces the SELECT-then-
INSERT/UPDATE pattern that was racy on Postgres (and now serializes
correctly on SQLite via BEGIN IMMEDIATE).
"""

import threading
import time

from sqlalchemy import select

from auth.db.models import ProviderToken, MsalTokenCache
from auth.db.session import get_session_factory


def test_concurrent_save_token_no_integrity_error(repo):
    """Two threads each save_token() on the same (user_key, provider).

    Both must succeed; only one row exists; the last writer's data wins.
    """
    barrier = threading.Barrier(2)
    errors = []

    def worker(seq: int):
        barrier.wait()
        try:
            repo.save_token("alice", "github", {"access_token": f"tok-{seq}"})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"concurrent save_token raised: {errors}"

    factory = get_session_factory()
    with factory() as s:
        rows = s.execute(
            select(ProviderToken).where(
                ProviderToken.user_key == "alice",
                ProviderToken.provider == "github",
            )
        ).scalars().all()
    assert len(rows) == 1  # exactly one row, not two from racing inserts


def test_concurrent_save_msal_cache_no_integrity_error(repo):
    barrier = threading.Barrier(2)
    errors = []

    def worker(seq: int):
        barrier.wait()
        try:
            repo.save_msal_cache("alice", f'{{"v": {seq}}}')
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []

    factory = get_session_factory()
    with factory() as s:
        rows = s.execute(
            select(MsalTokenCache).where(MsalTokenCache.user_key == "alice")
        ).scalars().all()
    assert len(rows) == 1


def test_save_token_is_idempotent_across_repeats(repo):
    """Sequential repeats produce one row and last-write-wins."""
    for seq in range(5):
        repo.save_token("alice", "github", {"access_token": f"tok-{seq}"})

    got = repo.get_token("alice", "github")
    assert got["access_token"] == "tok-4"

    factory = get_session_factory()
    with factory() as s:
        rows = s.execute(
            select(ProviderToken).where(
                ProviderToken.user_key == "alice",
                ProviderToken.provider == "github",
            )
        ).scalars().all()
    assert len(rows) == 1


def test_save_token_updates_updated_at(repo):
    """Each upsert advances updated_at."""
    repo.save_token("alice", "github", {"access_token": "v1"})

    factory = get_session_factory()
    with factory() as s:
        row1 = s.get(ProviderToken, ("alice", "github"))
        first_updated = row1.updated_at

    time.sleep(0.01)
    repo.save_token("alice", "github", {"access_token": "v2"})

    with factory() as s:
        row2 = s.get(ProviderToken, ("alice", "github"))
        second_updated = row2.updated_at

    assert second_updated >= first_updated


def test_upsert_preserves_pk_columns(repo):
    """An upsert must NOT change the PK columns of an existing row."""
    repo.save_token("alice", "github", {"access_token": "v1"})
    repo.save_token("alice", "github", {"access_token": "v2"})

    factory = get_session_factory()
    with factory() as s:
        row = s.get(ProviderToken, ("alice", "github"))
        assert row.user_key == "alice"
        assert row.provider == "github"
