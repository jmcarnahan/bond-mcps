"""Unit tests for atlassian_cli.py subcommands.

The previously-existing test file (test_integration_cli.py) is gated behind
real Atlassian credentials and is skipped in CI. This file covers the
CLI commands that can be exercised hermetically — currently `logout`, which
caught a real regression (cache_file attribute removed in the DB-backed
token store migration).
"""

import sys
from unittest.mock import patch


def _run_cli(argv, capsys):
    """Invoke atlassian_cli.main() with the given argv. Returns captured I/O."""
    from atlassian_cli import main

    old_argv = sys.argv[:]
    sys.argv = ["atlassian-cli", *argv]
    try:
        main()
    finally:
        sys.argv = old_argv
    return capsys.readouterr()


class TestLogout:
    """The cmd_logout flow against the DB-backed TokenStore.

    Pre-DB-migration this CLI used `store.cache_file.exists()`. That attribute
    was removed when the token store moved to SQLAlchemy. There was no unit
    test, so CI didn't catch it — running `atlassian-cli logout` on main today
    raises AttributeError. These tests lock in the public-API contract:
    get_token() → clear()."""

    def test_clears_existing_token(self, capsys):
        with patch("atlassian_cli.TokenStore") as MockStore:
            store = MockStore.return_value
            store.get_token.return_value = {"access_token": "x"}
            out = _run_cli(["logout"], capsys)
        store.clear.assert_called_once()
        assert "Logged out" in out.out

    def test_reports_no_token_when_empty(self, capsys):
        with patch("atlassian_cli.TokenStore") as MockStore:
            store = MockStore.return_value
            store.get_token.return_value = None
            out = _run_cli(["logout"], capsys)
        store.clear.assert_not_called()
        assert "No cached tokens" in out.out

    def test_does_not_call_removed_cache_file_method(self):
        """Regression guard: TokenStore.cache_file was removed in PR #4. If
        anyone re-introduces `.cache_file.exists()` (e.g. by copying from a
        pre-migration branch), this test fails immediately with a clear hint."""
        import inspect

        import atlassian_cli

        src = inspect.getsource(atlassian_cli.cmd_logout)
        # Look for the actual usage pattern, not the bare word — my own fix
        # comment legitimately mentions `cache_file` to explain the migration.
        assert ".cache_file" not in src, (
            "atlassian_cli.cmd_logout calls .cache_file on the TokenStore, "
            "but that attribute was removed in PR #4 (db-backed-token-cache). "
            "Use store.get_token() instead."
        )
