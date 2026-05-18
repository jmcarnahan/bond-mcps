"""Tests for auth.startup.verify_runtime_config — the fail-fast boot check.

The MCP lifespan calls this at startup so a container with a misconfigured
DB URL or missing encryption key crashes immediately instead of accepting
requests and silently failing every one.
"""

import os

import pytest

from auth.db import reset_for_tests
from auth.db.session import DeploymentConfigError
from auth.encryption import TokenEncryptionError


def test_verify_passes_on_clean_setup(tmp_path, monkeypatch):
    """With a freshly-migrated DB and env-var key, the check should pass."""
    from auth.alembic_config import upgrade_head
    from auth.encryption import generate_key
    from auth.startup import verify_runtime_config

    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("BOND_MCPS_USER_ID", "alice")
    reset_for_tests()
    try:
        upgrade_head()
        verify_runtime_config()  # should not raise
    finally:
        reset_for_tests()


def test_verify_raises_for_postgres_without_user_id(monkeypatch):
    """F1 path: Postgres URL + no BOND_MCPS_USER_ID → DeploymentConfigError."""
    from auth.startup import verify_runtime_config

    monkeypatch.delenv("BOND_MCPS_USER_ID", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv("BOND_MCPS_DB_URL", "postgresql://x:y@h:5432/d?sslmode=require")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", "dummy")  # never reached

    with pytest.raises(DeploymentConfigError, match="BOND_MCPS_USER_ID"):
        verify_runtime_config()


def test_verify_raises_when_db_url_unset_and_not_a_checkout(monkeypatch):
    import auth.db.session as session_mod
    from auth.startup import verify_runtime_config

    monkeypatch.delenv("BOND_MCPS_DB_URL", raising=False)
    monkeypatch.setattr(
        session_mod, "_repo_root",
        lambda: __import__("pathlib").Path("/var/empty"),
    )
    reset_for_tests()
    try:
        with pytest.raises(DeploymentConfigError, match="BOND_MCPS_DB_URL"):
            verify_runtime_config()
    finally:
        reset_for_tests()


def test_verify_raises_when_schema_missing(tmp_path, monkeypatch):
    """A pointed-at-empty-DB scenario surfaces SchemaOutOfDateError."""
    from auth.db.session import SchemaOutOfDateError
    from auth.encryption import generate_key
    from auth.startup import verify_runtime_config

    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", generate_key())
    reset_for_tests()
    try:
        with pytest.raises(SchemaOutOfDateError, match="migrate-db"):
            verify_runtime_config()
    finally:
        reset_for_tests()


def test_verify_raises_when_encryption_key_missing(tmp_path, monkeypatch):
    """Schema fine, key missing → TokenEncryptionError."""
    from auth.alembic_config import upgrade_head
    from auth.startup import verify_runtime_config

    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("BOND_MCPS_USER_ID", "alice")
    reset_for_tests()
    try:
        upgrade_head()
        # SQLite + file-fallback disabled (file doesn't exist) → raises
        # Wait — actually file-fallback for SQLite is allowed (file just doesn't exist).
        # The resolver should raise "No encryption key configured".
        with pytest.raises(TokenEncryptionError, match="No encryption key"):
            verify_runtime_config()
    finally:
        reset_for_tests()


def test_skip_env_var_bypasses_check(monkeypatch, caplog):
    """BOND_MCPS_SKIP_STARTUP_VERIFY=1 short-circuits with a warning."""
    import logging
    from auth.startup import SKIP_ENV_VAR, verify_runtime_config

    monkeypatch.setenv(SKIP_ENV_VAR, "1")

    with caplog.at_level(logging.WARNING, logger="auth.startup"):
        verify_runtime_config()

    assert any(
        SKIP_ENV_VAR in rec.message and "Skipping" in rec.message
        for rec in caplog.records
    ), "skip should log a clear warning"
