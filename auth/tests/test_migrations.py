"""Verify Alembic migrations are runnable end-to-end on a fresh SQLite DB."""

from sqlalchemy import inspect

from auth.alembic_config import get_head_revision, upgrade_head
from auth.db import reset_for_tests
from auth.db.session import get_engine


def test_upgrade_head_creates_expected_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    try:
        upgrade_head()

        engine = get_engine()
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert "provider_tokens" in tables
        assert "msal_token_caches" in tables
        assert "alembic_version" in tables

        # Verify PK columns exist
        pt_cols = {c["name"] for c in insp.get_columns("provider_tokens")}
        assert "user_key" in pt_cols and "provider" in pt_cols
        assert "access_token_encrypted" in pt_cols
        assert "refresh_token_encrypted" in pt_cols
        assert "key_version" in pt_cols
        assert "extra_metadata" in pt_cols

        msal_cols = {c["name"] for c in insp.get_columns("msal_token_caches")}
        assert "user_key" in msal_cols
        assert "cache_data_encrypted" in msal_cols
    finally:
        reset_for_tests()


def test_head_revision_is_initial(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    head = get_head_revision()
    assert head == "0001_initial_schema"


def test_ensure_schema_current_passes_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    try:
        upgrade_head()
        from auth.db.session import ensure_schema_current

        ensure_schema_current()  # should not raise
    finally:
        reset_for_tests()


def test_ensure_schema_current_raises_for_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    try:
        from auth.db.session import SchemaOutOfDateError, ensure_schema_current

        # Trigger engine creation (creates the empty file) but no migrations
        get_engine()
        import pytest as _pt
        with _pt.raises(SchemaOutOfDateError, match="migrate-db"):
            ensure_schema_current()
    finally:
        reset_for_tests()
