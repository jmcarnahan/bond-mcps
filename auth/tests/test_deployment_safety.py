"""Tests for the deployment-safety guards in auth/auth/db/session.py.

These pin the behavior that distinguishes a local dev checkout (default
SQLite URL is fine) from a container/installed deployment (must set
BOND_MCPS_DB_URL explicitly — silently writing tokens into site-packages
would be a confusing way to fail).
"""

import pytest

from auth.db import reset_for_tests
from auth.db.session import (
    DeploymentConfigError,
    _is_dev_checkout,
    default_db_url,
    get_engine,
)


def test_dev_checkout_is_detected(tmp_path):
    """A directory with Makefile or pyproject.toml is recognized as a checkout."""
    (tmp_path / "Makefile").touch()
    assert _is_dev_checkout(tmp_path) is True


def test_pyproject_alone_is_enough(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    assert _is_dev_checkout(tmp_path) is True


def test_empty_directory_is_not_a_checkout(tmp_path):
    assert _is_dev_checkout(tmp_path) is False


def test_default_db_url_raises_when_not_in_checkout(monkeypatch):
    """If the resolved repo_root has no markers, default_db_url refuses."""
    import auth.db.session as session_mod
    monkeypatch.setattr(session_mod, "_repo_root", lambda: __import__("pathlib").Path("/var/empty"))
    with pytest.raises(DeploymentConfigError, match="BOND_MCPS_DB_URL"):
        default_db_url()


def test_default_db_url_works_for_real_checkout():
    """In the real bond-mcps checkout this test runs from, default works."""
    url = default_db_url()
    assert url.startswith("sqlite:///")
    assert url.endswith("tokens.db")


def test_get_engine_propagates_deployment_error(monkeypatch):
    """get_engine should raise DeploymentConfigError too — not a confusing
    OperationalError after silently writing into a bad path."""
    import auth.db.session as session_mod
    monkeypatch.delenv("BOND_MCPS_DB_URL", raising=False)
    monkeypatch.setattr(
        session_mod, "_repo_root",
        lambda: __import__("pathlib").Path("/var/empty"),
    )
    reset_for_tests()
    try:
        with pytest.raises(DeploymentConfigError, match="BOND_MCPS_DB_URL"):
            get_engine()
    finally:
        reset_for_tests()


def test_doctor_reports_deployment_error(monkeypatch, capsys):
    """CLI doctor must surface DeploymentConfigError as a clear FAIL."""
    import auth.db.session as session_mod
    from auth.cli import main

    monkeypatch.delenv("BOND_MCPS_DB_URL", raising=False)
    monkeypatch.setattr(
        session_mod, "_repo_root",
        lambda: __import__("pathlib").Path("/var/empty"),
    )
    reset_for_tests()
    try:
        rc = main(["doctor"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "BOND_MCPS_DB_URL" in err
    finally:
        reset_for_tests()
