"""Tests for the bond-mcps CLI subcommands."""

import io
import logging
from contextlib import redirect_stderr, redirect_stdout

import pytest

from auth.cli import build_parser, main


def test_generate_key_prints_decodable_key(capsys):
    rc = main(["generate-key"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    # The key is on the LAST stdout line (so `bond-mcps generate-key | pbcopy` works).
    key = out[-1]
    import base64
    raw = base64.urlsafe_b64decode(key.encode("ascii"))
    assert len(raw) == 32


def test_generate_key_warns_when_env_var_already_set(monkeypatch, capsys):
    monkeypatch.setenv("BOND_MCPS_ENCRYPTION_KEY", "anything")
    main(["generate-key"])
    err = capsys.readouterr().err
    assert "already set" in err.lower()


def test_generate_key_no_warning_when_env_var_unset(monkeypatch, capsys):
    monkeypatch.delenv("BOND_MCPS_ENCRYPTION_KEY", raising=False)
    main(["generate-key"])
    err = capsys.readouterr().err
    assert "already set" not in err.lower()


def test_clear_rejects_unknown_provider():
    """argparse choices must enforce {github, atlassian, microsoft}."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["clear", "--provider", "typo"])


def test_clear_accepts_each_known_provider():
    parser = build_parser()
    for provider in ("github", "atlassian", "microsoft"):
        args = parser.parse_args(["clear", "--provider", provider])
        assert args.provider == provider


def test_clear_provider_token(repo, capsys):
    """`bond-mcps clear --provider github --user alice` deletes the row."""
    repo.save_token("alice", "github", {"access_token": "x"})
    rc = main(["clear", "--provider", "github", "--user", "alice"])
    assert rc == 0
    assert repo.get_token("alice", "github") is None
    out = capsys.readouterr().out
    assert "Cleared github token" in out


def test_clear_microsoft_routes_to_msal_cache(repo, capsys):
    """The 'microsoft' provider clears msal_token_caches, NOT provider_tokens."""
    repo.save_msal_cache("alice", '{"v": 1}')
    rc = main(["clear", "--provider", "microsoft", "--user", "alice"])
    assert rc == 0
    assert repo.get_msal_cache("alice") is None


def test_migrate_db_subcommand_runs_to_head(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    from auth.db import reset_for_tests
    reset_for_tests()
    try:
        rc = main(["migrate-db"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "head" in out.lower()
    finally:
        reset_for_tests()


def test_doctor_passes_on_fresh_setup(repo, capsys, monkeypatch):
    """Doctor should report all-green when DB is at head and key is set."""
    # repo fixture sets up the DB and key; we just need alembic_version present.
    from auth.alembic_config import upgrade_head
    from auth.db import reset_for_tests

    # Switch the DB to a fresh tmp file to avoid colliding with repo fixture
    # state (which uses ORM create_all, not alembic).
    import tempfile, os as _os
    db_path = tempfile.mktemp()
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{db_path}")
    reset_for_tests()
    try:
        upgrade_head()
        reset_for_tests()
        rc = main(["doctor"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "All checks passed" in out
    finally:
        reset_for_tests()
        if _os.path.exists(db_path):
            _os.unlink(db_path)


def test_doctor_fails_on_missing_schema(tmp_path, monkeypatch, capsys):
    """Doctor reports schema-out-of-date for an un-migrated DB."""
    monkeypatch.setenv("BOND_MCPS_DB_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    from auth.db import reset_for_tests
    reset_for_tests()
    try:
        rc = main(["doctor"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "migrate-db" in err
    finally:
        reset_for_tests()
