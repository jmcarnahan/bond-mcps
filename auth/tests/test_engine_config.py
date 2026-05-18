"""Verify SQLite pragmas + Postgres engine kwargs are set correctly.

These are deployment-critical: busy_timeout governs how long an SQLite
operation will wait for a write lock during MSAL refresh; connect_timeout
bounds Aurora Serverless v2 cold-start wait.
"""

from sqlalchemy import text


def test_sqlite_busy_timeout_is_30s(engine):
    """busy_timeout=30000ms — sized for MSAL silent-acquire holding the lock."""
    with engine.connect() as conn:
        value = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert value == 30000


def test_sqlite_journal_mode_is_wal(engine):
    """WAL mode enables concurrent reads while a writer holds the lock."""
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_sqlite_foreign_keys_enabled(engine):
    """FK enforcement is opt-in in SQLite. We want it on."""
    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert fk == 1


def test_db_file_is_0600(db_url, engine):
    """The on-disk SQLite file is chmod'd to 0600 on creation. WAL/SHM
    sidecars inherit perms from the main file."""
    import os
    from pathlib import Path

    # db_url is sqlite:///<tmp_path>/tokens.db; parse out the path.
    assert db_url.startswith("sqlite:///")
    path = Path(db_url[len("sqlite:///"):])
    # Touch the engine to ensure the file exists.
    with engine.connect():
        pass
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_postgres_engine_kwargs_include_connect_timeout(monkeypatch):
    """Without actually connecting, verify the create_engine call for a
    Postgres URL gets a 30s connect_timeout. Aurora Serverless v2 cold
    start can take 10-30s; without this, container health checks loop."""
    from unittest.mock import patch as _patch

    import auth.db.session as session_mod

    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs

        class _Stub:
            dialect = type("D", (), {"name": "postgresql"})()

            def connect(self):
                raise RuntimeError("stub")

            def dispose(self):
                pass

        return _Stub()

    with _patch.object(session_mod, "create_engine", side_effect=fake_create_engine):
        session_mod._make_engine(
            "postgresql://u:p@h:5432/d?sslmode=verify-full"
        )

    assert "connect_args" in captured["kwargs"]
    assert captured["kwargs"]["connect_args"].get("connect_timeout") == 30
    # And pool_pre_ping for Aurora failover handling.
    assert captured["kwargs"].get("pool_pre_ping") is True


def test_postgres_engine_pool_sized_for_aurora_serverless_v2(monkeypatch):
    """Pool sizing is deployment-critical for Aurora Serverless v2.

    Aurora at 1.0 ACU caps connections around 113. We have ~9 worker
    processes (4 MCPs × 2 replicas + auth × 1), so per-process pool must
    stay small enough that 9× sum ≤ 113 with headroom. pool_size=3 +
    max_overflow=2 puts the per-process cap at 5, total peak at 45.
    """
    from unittest.mock import patch as _patch

    import auth.db.session as session_mod

    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["kwargs"] = kwargs

        class _Stub:
            dialect = type("D", (), {"name": "postgresql"})()
            def connect(self): raise RuntimeError("stub")
            def dispose(self): pass

        return _Stub()

    with _patch.object(session_mod, "create_engine", side_effect=fake_create_engine):
        session_mod._make_engine("postgresql://u:p@h:5432/d?sslmode=verify-full")

    assert captured["kwargs"].get("pool_size") == 3
    assert captured["kwargs"].get("max_overflow") == 2
