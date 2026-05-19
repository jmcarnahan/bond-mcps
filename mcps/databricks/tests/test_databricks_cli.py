"""Tests for databricks_cli.py — CLI subcommands with mocked client."""

import sys
from unittest.mock import patch

import pytest

from tests.conftest import HTTP_PATH, WORKSPACE_HOST


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-test")


def _run_cli(argv, capsys):
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
        result = {"columns": ["user"], "rows": [["alice@example.com"]], "truncated": False}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["whoami"], capsys)
        assert "PAT" in out.out
        assert "alice@example.com" in out.out
        assert "Connection OK" in out.out

    def test_oauth_mode(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        result = {"columns": ["user"], "rows": [["bob@example.com"]], "truncated": False}
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
        result = {
            "columns": ["id", "name"],
            "rows": [[1, "alpha"], [2, "beta"]],
            "truncated": False,
        }
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["query", "SELECT id, name FROM t"], capsys)
        # Pipe-delimited (matches MCP server formatting).
        assert "id|name" in out.out
        assert "alpha" in out.out
        assert "(2 row(s))" in out.out

    def test_none_values_rendered_empty(self, capsys):
        result = {"columns": ["x"], "rows": [[None]], "truncated": False}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["query", "SELECT NULL"], capsys)
        assert "None" not in out.out

    def test_truncated_marker_in_row_count(self, capsys):
        result = {"columns": ["x"], "rows": [[i] for i in range(3)], "truncated": True}
        with patch("databricks_cli.db.run_query", return_value=result):
            out = _run_cli(["query", "SELECT x FROM big"], capsys)
        assert "truncated" in out.out


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
        with patch(
            "databricks_cli.db.list_tables",
            return_value=[
                {"database": "default", "table": "events", "is_temporary": False},
                {"database": "default", "table": "tmp_join", "is_temporary": True},
            ],
        ):
            out = _run_cli(["tables", "main", "default"], capsys)
        assert "default.events" in out.out
        assert "tmp_join (temp)" in out.out


class TestLogout:
    def test_clears_existing_cache(self, capsys):
        """TokenStore is DB-backed now; test the public API contract:
        if get_token() returns something, we call clear() and report it."""
        with patch("databricks_cli.TokenStore") as MockStore:
            store = MockStore.return_value
            store.get_token.return_value = {"access_token": "x"}
            out = _run_cli(["logout"], capsys)
        store.clear.assert_called_once()
        assert "Cleared" in out.out

    def test_reports_nothing_to_clear_when_empty(self, capsys):
        with patch("databricks_cli.TokenStore") as MockStore:
            store = MockStore.return_value
            store.get_token.return_value = None
            out = _run_cli(["logout"], capsys)
        store.clear.assert_not_called()
        assert "No cached" in out.out

    def test_warns_when_pat_env_still_set(self, capsys):
        with patch("databricks_cli.TokenStore") as MockStore:
            MockStore.return_value.get_token.return_value = None
            out = _run_cli(["logout"], capsys)
        assert "DATABRICKS_ACCESS_TOKEN" in out.out


class TestFriendlyErrors:
    """Round-2 fix: CLI must surface the same friendly messages as the MCP.
    Previously the CLI printed raw DatabricksError messages, which would have
    given a user with a sql-scope-missing PAT a connector stack trace instead
    of the helpful "your PAT may be missing the `sql` scope" guidance."""

    def test_unauthorized_in_pat_mode_hints_sql_scope(self, capsys):
        from dbx.client import DatabricksError

        with patch(
            "databricks_cli.db.run_query",
            side_effect=DatabricksError("HTTP 401", error_code="Unauthorized"),
        ):
            with pytest.raises(SystemExit):
                _run_cli(["query", "SELECT 1"], capsys)
        err = capsys.readouterr().err
        assert "DATABRICKS_ACCESS_TOKEN" in err
        assert "sql" in err.lower()

    def test_unauthorized_in_oauth_mode_hints_login(self, monkeypatch, capsys):
        from dbx.client import DatabricksError

        monkeypatch.delenv("DATABRICKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "id")
        with patch(
            "databricks_cli.db.run_query",
            side_effect=DatabricksError("HTTP 401", error_code="Unauthorized"),
        ):
            with pytest.raises(SystemExit):
                _run_cli(["query", "SELECT 1"], capsys)
        err = capsys.readouterr().err
        assert "make login-databricks" in err

    def test_sql_error_rendered_with_fence(self, capsys):
        from dbx.client import DatabricksError

        with patch(
            "databricks_cli.db.run_query",
            side_effect=DatabricksError("TABLE_OR_VIEW_NOT_FOUND: x.y", error_code="SQLError"),
        ):
            with pytest.raises(SystemExit):
                _run_cli(["query", "SELECT * FROM x.y"], capsys)
        err = capsys.readouterr().err
        assert "SQL error" in err
        assert "TABLE_OR_VIEW_NOT_FOUND" in err
