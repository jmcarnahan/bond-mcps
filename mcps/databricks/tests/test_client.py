"""Tests for dbx/client.py — SQL wrapper using mocked connector."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import WORKSPACE_HOST, HTTP_PATH


def _mock_connection(rows, columns):
    """Build a context-manager-friendly mock connector return value."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.description = [(c,) for c in columns]
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestRequireEnv:
    def test_raises_when_host_missing(self):
        from dbx.client import _require_env, DatabricksError
        with pytest.raises(DatabricksError, match="DATABRICKS_HOST"):
            _require_env()

    def test_raises_when_http_path_missing(self, monkeypatch):
        from dbx.client import _require_env, DatabricksError
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
        with pytest.raises(DatabricksError, match="DATABRICKS_HTTP_PATH"):
            _require_env()

    def test_lists_all_missing_in_one_error(self):
        from dbx.client import _require_env, DatabricksError
        with pytest.raises(DatabricksError) as exc:
            _require_env()
        assert "DATABRICKS_HOST" in str(exc.value)
        assert "DATABRICKS_HTTP_PATH" in str(exc.value)


class TestRunQuery:
    def _env(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-tok")

    def test_returns_columns_and_rows(self, monkeypatch):
        from dbx.client import run_query
        self._env(monkeypatch)
        conn, cursor = _mock_connection(
            rows=[("a", 1), ("b", 2)], columns=["name", "val"],
        )
        with patch("dbx.client.sql.connect", return_value=conn):
            result = run_query("SELECT name, val FROM t")
        assert result == {
            "columns": ["name", "val"],
            "rows": [["a", 1], ["b", 2]],
        }
        cursor.execute.assert_called_once_with("SELECT name, val FROM t")

    def test_empty_result(self, monkeypatch):
        from dbx.client import run_query
        self._env(monkeypatch)
        conn, _ = _mock_connection(rows=[], columns=["x"])
        with patch("dbx.client.sql.connect", return_value=conn):
            result = run_query("SELECT x FROM t WHERE 1=0")
        assert result == {"columns": ["x"], "rows": []}

    def test_unauthorized_classified(self, monkeypatch):
        from dbx.client import run_query, DatabricksError
        self._env(monkeypatch)
        with patch("dbx.client.sql.connect",
                   side_effect=Exception("HTTP 401 Unauthorized")):
            with pytest.raises(DatabricksError) as exc:
                run_query("SELECT 1")
        assert exc.value.error_code == "Unauthorized"

    def test_forbidden_classified(self, monkeypatch):
        from dbx.client import run_query, DatabricksError
        self._env(monkeypatch)
        with patch("dbx.client.sql.connect",
                   side_effect=Exception("Permission denied for table")):
            with pytest.raises(DatabricksError) as exc:
                run_query("SELECT 1")
        assert exc.value.error_code == "Forbidden"

    def test_unreachable_classified(self, monkeypatch):
        from dbx.client import run_query, DatabricksError
        self._env(monkeypatch)
        with patch("dbx.client.sql.connect",
                   side_effect=Exception("Could not resolve host")):
            with pytest.raises(DatabricksError) as exc:
                run_query("SELECT 1")
        assert exc.value.error_code == "Unreachable"


class TestListHelpers:
    def _env(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
        monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat")

    def test_list_catalogs(self, monkeypatch):
        from dbx.client import list_catalogs
        self._env(monkeypatch)
        conn, _ = _mock_connection(rows=[("main",), ("samples",)], columns=["catalog"])
        with patch("dbx.client.sql.connect", return_value=conn):
            assert list_catalogs() == ["main", "samples"]

    def test_list_schemas_quotes_identifier(self, monkeypatch):
        from dbx.client import list_schemas
        self._env(monkeypatch)
        conn, cursor = _mock_connection(
            rows=[("default",), ("bronze",)], columns=["databaseName"],
        )
        with patch("dbx.client.sql.connect", return_value=conn):
            assert list_schemas("main") == ["default", "bronze"]
        cursor.execute.assert_called_once_with("SHOW SCHEMAS IN `main`")

    def test_list_schemas_escapes_backticks(self, monkeypatch):
        """Identifier with an embedded backtick must be safely escaped."""
        from dbx.client import list_schemas
        self._env(monkeypatch)
        conn, cursor = _mock_connection(rows=[], columns=["databaseName"])
        with patch("dbx.client.sql.connect", return_value=conn):
            list_schemas("evil`name")
        cursor.execute.assert_called_once_with("SHOW SCHEMAS IN `evil``name`")

    def test_list_tables(self, monkeypatch):
        from dbx.client import list_tables
        self._env(monkeypatch)
        conn, cursor = _mock_connection(
            rows=[
                ("default", "events", False),
                ("default", "tmp_join", True),
            ],
            columns=["database", "tableName", "isTemporary"],
        )
        with patch("dbx.client.sql.connect", return_value=conn):
            tables = list_tables("main", "default")
        cursor.execute.assert_called_once_with("SHOW TABLES IN `main`.`default`")
        assert tables == [
            {"database": "default", "table": "events", "is_temporary": False},
            {"database": "default", "table": "tmp_join", "is_temporary": True},
        ]


class TestCredentialsProvider:
    def test_provider_returns_header_factory(self, monkeypatch):
        """The connector calls our outer fn to get a header_factory, then calls
        that for each HTTP request. Verify both layers compose correctly."""
        from dbx.client import _credentials_provider
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "tok-abc")
        factory = _credentials_provider()
        headers = factory()
        assert headers == {"Authorization": "Bearer tok-abc"}

    def test_provider_refreshes_via_get_databricks_token(self, monkeypatch):
        """If token resolution changes (e.g., OAuth refresh), the next factory
        call must reflect it — proves we re-resolve per call, not just cache."""
        from dbx.client import _credentials_provider

        tokens = iter(["tok-1", "tok-2"])
        with patch("dbx.client.get_databricks_token", side_effect=lambda: next(tokens)):
            factory = _credentials_provider()
            assert factory() == {"Authorization": "Bearer tok-1"}
            assert factory() == {"Authorization": "Bearer tok-2"}
