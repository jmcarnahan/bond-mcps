"""Tests for github_cli.py -- the thin shell over github.local_auth.

The CLI's _get_token() is meant to be a one-line delegation to
github.local_auth.get_local_token. If a typo breaks the delegation, all 16
command functions silently fail. These tests pin the contract.
"""

from unittest.mock import patch

import pytest


def test_get_token_delegates_to_local_auth():
    """_get_token() must return whatever github.local_auth.get_local_token returns."""
    import github_cli

    with patch("github.local_auth.get_local_token", return_value="sentinel_token_abc"):
        assert github_cli._get_token() == "sentinel_token_abc"


def test_get_token_surfaces_permission_error_via_exit(capsys):
    """If local_auth raises PermissionError, CLI prints to stderr and exits 1."""
    import github_cli

    with patch(
        "github.local_auth.get_local_token",
        side_effect=PermissionError("no creds"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            github_cli._get_token()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "no creds" in captured.err


def test_get_token_surfaces_runtime_error_via_exit(capsys):
    """RuntimeError (e.g. auth proxy unreachable) also exits 1 with a clean message."""
    import github_cli

    with patch(
        "github.local_auth.get_local_token",
        side_effect=RuntimeError("auth proxy not running"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            github_cli._get_token()
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert "auth proxy not running" in captured.err
