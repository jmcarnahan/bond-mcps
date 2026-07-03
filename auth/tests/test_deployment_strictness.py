"""Tests for the strict deployment-mode guards added in the third review.

Covers:
- F1: current_user_key refuses getpass fallback for Postgres.
- F4: validate_db_url rejects unsupported dialects at engine creation.
- F5: ensure_schema_current narrows ProgrammingError to 'table missing'
  rather than swallowing permission errors as "schema empty".
- F6: doctor checks the sslrootcert file exists when sslmode=verify-full.
"""

import pytest

from auth.db.session import (
    DeploymentConfigError,
    _is_missing_table_error,
    validate_db_url,
)

# ---------- F1 ----------


def test_current_user_key_refuses_postgres_without_env_var(monkeypatch):
    """Containerized Postgres deployment without BOND_MCPS_USER_ID must fail."""
    from auth.token_store import current_user_key

    monkeypatch.delenv("BOND_MCPS_USER_ID", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv("BOND_MCPS_DB_URL", "postgresql://x:y@h:5432/d?sslmode=require")

    with pytest.raises(DeploymentConfigError, match="BOND_MCPS_USER_ID"):
        current_user_key()


def test_current_user_key_accepts_explicit_for_postgres(monkeypatch):
    """If BOND_MCPS_USER_ID is set, Postgres deployments work fine."""
    from auth.token_store import current_user_key

    monkeypatch.setenv("BOND_MCPS_DB_URL", "postgresql://x:y@h:5432/d?sslmode=require")
    monkeypatch.setenv("BOND_MCPS_USER_ID", "tenant-alpha")
    assert current_user_key() == "tenant-alpha"


def test_current_user_key_allows_sqlite_fallback(monkeypatch):
    """Local SQLite still gets the getpass.getuser() convenience."""
    from auth.token_store import current_user_key

    monkeypatch.delenv("BOND_MCPS_USER_ID", raising=False)
    monkeypatch.setenv("BOND_MCPS_DB_URL", "sqlite:///./tokens.db")
    # Should not raise; returns *something* (the OS user).
    assert isinstance(current_user_key(), str)


# ---------- F4 ----------


def test_validate_rejects_mysql_at_engine_creation():
    with pytest.raises(ValueError, match="Unsupported DB dialect"):
        validate_db_url("mysql://u:p@h:3306/d")


def test_validate_rejects_oracle_at_engine_creation():
    with pytest.raises(ValueError, match="Unsupported DB dialect"):
        validate_db_url("oracle://u:p@h:1521/d")


def test_validate_accepts_sqlite_and_postgres():
    validate_db_url("sqlite:///./tokens.db")
    validate_db_url("postgresql://u:p@h:5432/d?sslmode=require")
    validate_db_url("postgres://u:p@h:5432/d?sslmode=verify-full")
    validate_db_url("postgresql+psycopg://u:p@h/d?sslmode=require")


# ---------- F5 ----------


def test_is_missing_table_error_recognizes_sqlite_message():
    """SQLite OperationalError contains 'no such table: <name>'."""
    from sqlalchemy.exc import OperationalError

    exc = OperationalError("SELECT 1", {}, Exception("no such table: alembic_version"))
    assert _is_missing_table_error(exc) is True


def test_is_missing_table_error_recognizes_postgres_message():
    """Postgres ProgrammingError contains 'relation ... does not exist'."""
    from sqlalchemy.exc import ProgrammingError

    exc = ProgrammingError(
        "SELECT 1",
        {},
        Exception('relation "alembic_version" does not exist'),
    )
    assert _is_missing_table_error(exc) is True


def test_is_missing_table_error_rejects_permission_denied():
    """A 42501 permission error must NOT be classified as missing table."""
    from sqlalchemy.exc import ProgrammingError

    inner = Exception("permission denied for table alembic_version")
    inner.sqlstate = "42501"  # InsufficientPrivilege
    exc = ProgrammingError("SELECT 1", {}, inner)
    assert _is_missing_table_error(exc) is False


def test_is_missing_table_error_recognizes_pgcode():
    """Even without a recognizable message, pgcode 42P01 (undefined_table) wins."""
    from sqlalchemy.exc import ProgrammingError

    inner = Exception("opaque error message")
    inner.sqlstate = "42P01"
    exc = ProgrammingError("SELECT 1", {}, inner)
    assert _is_missing_table_error(exc) is True


def test_ensure_schema_current_propagates_permission_error(tmp_path, monkeypatch):
    """A permission error from the DB must NOT be masked as 'schema empty'."""
    from unittest.mock import patch

    from sqlalchemy.exc import ProgrammingError

    from auth.alembic_config import upgrade_head
    from auth.db import reset_for_tests
    from auth.db.session import ensure_schema_current

    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    reset_for_tests()
    try:
        upgrade_head()

        inner = Exception("permission denied for table alembic_version")
        inner.sqlstate = "42501"
        fake = ProgrammingError("SELECT 1", {}, inner)

        with patch("sqlalchemy.engine.Connection.execute", side_effect=fake):
            with pytest.raises(ProgrammingError, match="permission denied"):
                ensure_schema_current()
    finally:
        reset_for_tests()


# ---------- F6 ----------


def test_doctor_warns_on_verify_full_without_sslrootcert(monkeypatch, capsys):
    """sslmode=verify-full without sslrootcert → WARN (psycopg uses system trust)."""
    from auth.cli import main
    from auth.db import reset_for_tests
    from auth.encryption import generate_key

    monkeypatch.setenv(
        "BOND_MCPS_DB_URL",
        "postgresql://u:p@h:5432/d?sslmode=verify-full",
    )
    monkeypatch.setenv("BOND_MCPS_USER_ID", "alice")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", generate_key())
    reset_for_tests()
    try:
        rc = main(["doctor"])
        # We expect doctor to fail (can't connect to fake host), but it
        # must emit the sslrootcert WARN before getting that far.
        err = capsys.readouterr().err
        assert "sslrootcert" in err.lower()
        assert "warn" in err.lower() or "sslmode=verify-full" in err.lower()
    finally:
        reset_for_tests()


def test_doctor_fails_when_sslrootcert_file_missing(monkeypatch, capsys, tmp_path):
    """sslmode=verify-full + nonexistent sslrootcert path → FAIL."""
    from auth.cli import main
    from auth.db import reset_for_tests
    from auth.encryption import generate_key

    missing = tmp_path / "nope.pem"
    monkeypatch.setenv(
        "BOND_MCPS_DB_URL",
        f"postgresql://u:p@h:5432/d?sslmode=verify-full&sslrootcert={missing}",
    )
    monkeypatch.setenv("BOND_MCPS_USER_ID", "alice")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", generate_key())
    reset_for_tests()
    try:
        rc = main(["doctor"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "sslrootcert" in err.lower()
        assert "does not exist" in err.lower()
    finally:
        reset_for_tests()


def test_doctor_passes_when_sslrootcert_file_exists(monkeypatch, tmp_path, capsys):
    """File present → doctor moves past the TLS-cert check and tries to connect."""
    from auth.cli import main
    from auth.db import reset_for_tests
    from auth.encryption import generate_key

    cert = tmp_path / "rds-bundle.pem"
    cert.write_text("dummy CA contents")
    monkeypatch.setenv(
        "BOND_MCPS_DB_URL",
        f"postgresql://u:p@h:5432/d?sslmode=verify-full&sslrootcert={cert}",
    )
    monkeypatch.setenv("BOND_MCPS_USER_ID", "alice")
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", generate_key())
    reset_for_tests()
    try:
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert "TLS cert path:" in out and "present" in out
        # Will then fail later trying to actually connect — that's fine.
    finally:
        reset_for_tests()


# ---------- token-exchange Cognito passthrough fail-closed ----------


def test_cognito_passthrough_refused_on_postgres(monkeypatch):
    """No Cognito pool + Postgres DB must NOT fall back to sub=email."""
    from auth.auth_server import cognito_lookup

    cognito_lookup.reset_cache_for_tests()
    monkeypatch.delenv("BOND_MCPS_AS_COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.setenv("BOND_MCPS_DB_URL", "postgresql://u:p@h:5432/d?sslmode=require")
    try:
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") is None
    finally:
        cognito_lookup.reset_cache_for_tests()


def test_cognito_passthrough_allowed_on_sqlite(monkeypatch):
    """No Cognito pool + SQLite dev returns the email as the subject."""
    from auth.auth_server import cognito_lookup

    cognito_lookup.reset_cache_for_tests()
    monkeypatch.delenv("BOND_MCPS_AS_COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.setenv("BOND_MCPS_DB_URL", "sqlite:///./tokens.db")
    try:
        assert cognito_lookup.resolve_cognito_sub("alice@example.com") == "alice@example.com"
    finally:
        cognito_lookup.reset_cache_for_tests()


# ---------- F3 doctor + user_key for Postgres ----------


def test_doctor_fails_when_user_key_unresolvable_on_postgres(monkeypatch, capsys):
    from auth.cli import main
    from auth.db import reset_for_tests

    monkeypatch.delenv("BOND_MCPS_USER_ID", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv(
        "BOND_MCPS_DB_URL",
        "postgresql://u:p@h:5432/d?sslmode=require",
    )
    reset_for_tests()
    try:
        rc = main(["doctor"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "BOND_MCPS_USER_ID" in err
    finally:
        reset_for_tests()
