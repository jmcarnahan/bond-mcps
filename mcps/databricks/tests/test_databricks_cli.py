"""Tests for databricks_cli.py — CLI subcommands with mocked client."""

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from tests.conftest import WORKSPACE_HOST, HTTP_PATH


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-test")


def _run_cli(argv, capsys):
    """Invoke databricks_cli.main() with the given argv, return captured output."""
    from databricks_cli import main
    old_argv = sys.argv[:]
    sys.argv = ["databricks-cli", *argv]
    try:
        main()
    finally:
        sys.argv = old_argv
    return capsys.readouterr()


class TestWhoami:
    def test_pat_mode(self, capsys):
        result = {"columns": ["user"], "rows": [["alice@example.com"]]}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["whoami"], capsys)
        assert "PAT" in out.out
        assert "alice@example.com" in out.out
        assert "Connection OK" in out.out

    def test_oauth_mode(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        result = {"columns": ["user"], "rows": [["bob@example.com"]]}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["whoami"], capsys)
        assert "OAuth" in out.out
        assert "bob@example.com" in out.out

    def test_no_auth_exits_with_message(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc:
            _run_cli(["whoami"], capsys)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "authorization required" in err.lower()


class TestQuery:
    def test_prints_columns_and_rows(self, capsys):
        result = {"columns": ["id", "name"], "rows": [[1, "alpha"], [2, "beta"]]}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["query", "SELECT id, name FROM t"], capsys)
        assert "id" in out.out and "name" in out.out
        assert "alpha" in out.out
        assert "(2 row(s))" in out.out

    def test_none_values_rendered_empty(self, capsys):
        result = {"columns": ["x"], "rows": [[None]]}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["query", "SELECT NULL"], capsys)
        # None becomes empty string in output (no 'None' literal)
        assert "None" not in out.out


class TestList:
    def test_catalogs(self, capsys):
        with patch("databricks_cli.db.list_catalogs", return_value=["main", "samples"]):
            out = _run_cli(["catalogs"], capsys)
        assert "main" in out.out
        assert "samples" in out.out

    def test_schemas(self, capsys):
        with patch("databricks_cli.db.list_schemas", return_value=["a", "b"]):
            out = _run_cli(["schemas", "main"], capsys)
        assert "a\nb" in out.out

    def test_tables_marks_temp(self, capsys):
        with patch("databricks_cli.db.list_tables", return_value=[
            {"database": "default", "table": "events", "is_temporary": False},
            {"database": "default", "table": "tmp_join", "is_temporary": True},
        ]):
            out = _run_cli(["tables", "main", "default"], capsys)
        assert "default.events" in out.out
        assert "tmp_join (temp)" in out.out


class TestLogout:
    def test_clears_existing_cache(self, tmp_path, monkeypatch, capsys):
        # Point TokenStore at a temp dir with a fake cache file
        from auth import TokenStore
        monkeypatch.setattr(
            "auth.token_store.DEFAULT_CACHE_DIR", tmp_path,
        )
        cache_file = tmp_path / "databricks.json"
        cache_file.write_text('{"access_token": "x"}')

        out = _run_cli(["logout"], capsys)
        assert not cache_file.exists()
        assert "Cleared" in out.out

    def test_warns_when_pat_env_still_set(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "auth.token_store.DEFAULT_CACHE_DIR", tmp_path,
        )
        # DATABRICKS_ACCESS_TOKEN already set by the autouse fixture
        out = _run_cli(["logout"], capsys)
        assert "DATABRICKS_ACCESS_TOKEN" in out.out
