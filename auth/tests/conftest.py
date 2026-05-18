"""Shared pytest fixtures for the auth test suite."""

import pytest

from auth import encryption
from auth.db import models as db_models
from auth.db import reset_for_tests
from auth.db.repository import TokenRepository
from auth.db.session import get_engine


@pytest.fixture
def encryption_key(monkeypatch):
    """Set a fresh BOND_MCPS_ENCRYPTION_KEY for the test."""
    key = encryption.generate_key()
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", key)
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY_FILE", raising=False)
    return key


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    """Per-test SQLite DB at tmp_path/tokens.db.

    Also clears any cached engine from previous tests.
    """
    path = tmp_path / "tokens.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("BOND_MCPS_DB_URL", url)
    reset_for_tests()
    yield url
    reset_for_tests()


@pytest.fixture
def engine(db_url):
    """Engine pointing at a fresh DB with schema created from ORM metadata.

    Faster than running Alembic per test; Alembic itself is tested separately.
    """
    eng = get_engine(db_url)
    db_models.Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def repo(engine, encryption_key):
    return TokenRepository()
