"""DB URL validator tests — no DB required, pure parser."""

import pytest

from auth.db.session import validate_db_url


def test_sqlite_passes():
    validate_db_url("sqlite:///./tokens.db")
    validate_db_url("sqlite:////absolute/path/tokens.db")


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_postgres_with_strict_sslmode_passes(sslmode):
    validate_db_url(f"postgresql://user:pwd@host:5432/db?sslmode={sslmode}")
    validate_db_url(f"postgresql+psycopg://user:pwd@host:5432/db?sslmode={sslmode}")


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer", ""])
def test_postgres_without_strict_sslmode_raises(sslmode):
    url = (
        f"postgresql://user:pwd@host:5432/db?sslmode={sslmode}"
        if sslmode
        else "postgresql://user:pwd@host:5432/db"
    )
    with pytest.raises(ValueError, match="sslmode"):
        validate_db_url(url)


def test_postgres_no_sslmode_raises():
    with pytest.raises(ValueError, match="sslmode"):
        validate_db_url("postgresql://user:pwd@host:5432/db")


def test_postgres_alias_scheme():
    """Some libraries use 'postgres' instead of 'postgresql' — should validate identically."""
    with pytest.raises(ValueError, match="sslmode"):
        validate_db_url("postgres://user:pwd@host:5432/db")
    validate_db_url("postgres://user:pwd@host:5432/db?sslmode=require")
