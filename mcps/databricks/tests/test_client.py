"""Tests for dbx/client.py — SQL wrapper using mocked connector."""

from unittest.mock import MagicMock, patch

import pytest
from databricks.sql import exc as dbexc

from tests.conftest import WORKSPACE_HOST, HTTP_PATH


def _mock_connection(rows, columns):
    """Build a context-manager-friendly mock connector return value.

    fetchmany(n) returns up to n rows from the queue — matching what the
    real connector does, so client.run_query's `fetchmany(_MAX_FETCH_ROWS+1)`
    truncation detection runs the same code path the test exercises.
    """
    remaining = list(rows)

    def fetchmany(n):
        out, remaining[:] = remaining[:n], remaining[n:]
        return out

    cursor = MagicMock()
    cursor.fetchmany.side_effect = fetchmany
    cursor.description = [(c,) for c in columns]
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE_HOST)
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", HTTP_PATH)
    monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "pat-tok")


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


class TestConnect:
    def test_connect_uses_provided_token_via_access_token_kwarg(self, monkeypatch):
        """The pre-resolved token must reach `sql.connect(access_token=...)`
        verbatim. Crucial for Bearer-mode handoff to worker threads — there
        must be NO callback that would later re-resolve from a stale context."""
        from dbx import client
        _env(monkeypatch)
        with patch("dbx.client.sql.connect") as mock_connect:
            client._connect(token="explicit-bearer-token")
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["access_token"] == "explicit-bearer-token"
        # Critical: no credentials_provider callback that could fire from a
        # worker thread with no FastMCP context.
        assert "credentials_provider" not in kwargs
        # Multi-tenant safety: BOTH telemetry flags off. enable_telemetry alone
        # is insufficient because force_enable_telemetry=True overrides it
        # (telemetry_client.py is_telemetry_enabled). If a future config layer
        # passes force_enable_telemetry=True via env or wrapper, our protection
        # would silently break.
        assert kwargs["enable_telemetry"] is False
        assert kwargs["force_enable_telemetry"] is False
        # Per-HTTP-request hang protection (NOT a query-duration timeout;
        # that's enforced by the MCP tool layer via asyncio.wait_for).
        assert kwargs["_socket_timeout"] == 300

    def test_connect_resolves_token_when_none_given(self, monkeypatch):
        """CLI path: _connect(None) falls through to get_databricks_token()."""
        from dbx import client
        _env(monkeypatch)
        with patch("dbx.client.sql.connect") as mock_connect, \
             patch("dbx.client.get_databricks_token", return_value="env-pat"):
            client._connect(token=None)
        assert mock_connect.call_args.kwargs["access_token"] == "env-pat"


class TestResolveTokenNow:
    def test_returns_token_and_source(self, monkeypatch):
        from dbx.client import resolve_token_now
        from dbx.auth import AuthSource
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "the-pat")
        with patch("fastmcp.server.dependencies.get_http_headers",
                   side_effect=Exception("no ctx")):
            token, source = resolve_token_now()
        assert token == "the-pat"
        assert source is AuthSource.PAT

    def test_captures_bearer_header(self, monkeypatch):
        """The token from `Authorization: Bearer` must take precedence — this
        is the call site where the contextvar is still alive."""
        from dbx.client import resolve_token_now
        from dbx.auth import AuthSource
        monkeypatch.setenv("DATABRICKS_ACCESS_TOKEN", "would-leak-if-used")
        with patch("fastmcp.server.dependencies.get_http_headers",
                   return_value={"authorization": "Bearer request-tok"}):
            token, source = resolve_token_now()
        assert token == "request-tok"
        assert source is AuthSource.BEARER


class TestRunQuery:
    def test_returns_columns_and_rows(self, monkeypatch):
        from dbx.client import run_query
        _env(monkeypatch)
        conn, cursor = _mock_connection(
            rows=[("a", 1), ("b", 2)], columns=["name", "val"],
        )
        with patch("dbx.client.sql.connect", return_value=conn):
            result = run_query("SELECT name, val FROM t")
        assert result == {
            "columns": ["name", "val"],
            "rows": [["a", 1], ["b", 2]],
            "truncated": False,
        }
        cursor.execute.assert_called_once_with("SELECT name, val FROM t")

    def test_empty_result(self, monkeypatch):
        from dbx.client import run_query
        _env(monkeypatch)
        conn, _ = _mock_connection(rows=[], columns=["x"])
        with patch("dbx.client.sql.connect", return_value=conn):
            result = run_query("SELECT x FROM t WHERE 1=0")
        assert result == {"columns": ["x"], "rows": [], "truncated": False}

    def test_passes_token_to_connect(self, monkeypatch):
        from dbx.client import run_query
        _env(monkeypatch)
        conn, _ = _mock_connection(rows=[], columns=["x"])
        with patch("dbx.client.sql.connect", return_value=conn) as mock_connect:
            run_query("SELECT 1", token="passed-tok")
        assert mock_connect.call_args.kwargs["access_token"] == "passed-tok"

    def test_truncates_at_max_fetch_rows(self, monkeypatch):
        """Cursor returns more than _MAX_FETCH_ROWS rows; result must be
        capped with truncated=True. Protects MCP from OOM on huge SELECTs."""
        from dbx import client
        _env(monkeypatch)
        # 5001 fetched → 5000 kept, truncated=True
        too_many = [(i,) for i in range(client._MAX_FETCH_ROWS + 1)]
        conn, _ = _mock_connection(rows=too_many, columns=["i"])
        with patch("dbx.client.sql.connect", return_value=conn):
            result = client.run_query("SELECT i FROM big")
        assert len(result["rows"]) == client._MAX_FETCH_ROWS
        assert result["truncated"] is True

    def test_exact_max_fetch_rows_not_truncated(self, monkeypatch):
        """If the result is exactly _MAX_FETCH_ROWS, truncated must be False —
        no false positives."""
        from dbx import client
        _env(monkeypatch)
        exact = [(i,) for i in range(client._MAX_FETCH_ROWS)]
        conn, _ = _mock_connection(rows=exact, columns=["i"])
        with patch("dbx.client.sql.connect", return_value=conn):
            result = client.run_query("SELECT i FROM big")
        assert len(result["rows"]) == client._MAX_FETCH_ROWS
        assert result["truncated"] is False


class TestClassifyError:
    """Type-based dispatch first; substring fallback for HTTP-status-in-message
    cases that the type system can't disambiguate."""

    def test_server_operation_error_is_sql_error(self):
        from dbx.client import _classify_error
        err = dbexc.ServerOperationError("TABLE_OR_VIEW_NOT_FOUND")
        result = _classify_error(err)
        assert result.error_code == "SQLError"

    def test_programming_error_is_sql_error(self):
        from dbx.client import _classify_error
        err = dbexc.ProgrammingError("parse error")
        result = _classify_error(err)
        assert result.error_code == "SQLError"

    def test_request_error_with_401_is_unauthorized(self):
        from dbx.client import _classify_error
        err = dbexc.RequestError("HTTP 401 Unauthorized")
        assert _classify_error(err).error_code == "Unauthorized"

    def test_request_error_with_403_is_forbidden(self):
        from dbx.client import _classify_error
        err = dbexc.RequestError("HTTP 403 Forbidden — permission denied")
        assert _classify_error(err).error_code == "Forbidden"

    def test_bare_request_error_is_sql_error(self):
        """Connector wraps transient backend issues (e.g. deadlocks) as
        RequestError. Don't blanket-classify those as Unreachable — the user
        would think their network is broken when it's a server-side hiccup."""
        from dbx.client import _classify_error
        err = dbexc.RequestError(
            "Error during request to server: Deadlock found when trying to "
            "get lock; try restarting transaction"
        )
        result = _classify_error(err)
        assert result.error_code == "SQLError"
        assert "Deadlock" in str(result)

    def test_network_marker_in_message_is_unreachable(self):
        """Strong network signal in the message → Unreachable, even when
        wrapped in a connector exception type."""
        from dbx.client import _classify_error
        err = dbexc.RequestError("could not resolve host dbc-x.cloud.databricks.com")
        assert _classify_error(err).error_code == "Unreachable"

    def test_connection_error_is_unreachable(self):
        from dbx.client import _classify_error
        assert _classify_error(ConnectionError("refused")).error_code == "Unreachable"

    def test_os_error_is_unreachable(self):
        from dbx.client import _classify_error
        assert _classify_error(OSError("network unreachable")).error_code == "Unreachable"

    def test_unknown_exception_falls_back(self):
        from dbx.client import _classify_error
        assert _classify_error(RuntimeError("???")).error_code == "Unknown"

    def test_substring_fallback_for_untyped_401(self):
        """A generic Exception with '401' in the message should still classify
        as Unauthorized (covers transport layers that don't raise typed errors)."""
        from dbx.client import _classify_error
        assert _classify_error(Exception("got HTTP 401")).error_code == "Unauthorized"


class TestListHelpers:
    def test_list_catalogs(self, monkeypatch):
        from dbx.client import list_catalogs
        _env(monkeypatch)
        conn, _ = _mock_connection(rows=[("main",), ("samples",)], columns=["catalog"])
        with patch("dbx.client.sql.connect", return_value=conn):
            assert list_catalogs() == ["main", "samples"]

    def test_list_schemas_quotes_identifier(self, monkeypatch):
        from dbx.client import list_schemas
        _env(monkeypatch)
        conn, cursor = _mock_connection(
            rows=[("default",), ("bronze",)], columns=["databaseName"],
        )
        with patch("dbx.client.sql.connect", return_value=conn):
            assert list_schemas("main") == ["default", "bronze"]
        cursor.execute.assert_called_once_with("SHOW SCHEMAS IN `main`")

    def test_list_schemas_escapes_backticks(self, monkeypatch):
        """Identifier with an embedded backtick must be safely escaped."""
        from dbx.client import list_schemas
        _env(monkeypatch)
        conn, cursor = _mock_connection(rows=[], columns=["databaseName"])
        with patch("dbx.client.sql.connect", return_value=conn):
            list_schemas("evil`name")
        cursor.execute.assert_called_once_with("SHOW SCHEMAS IN `evil``name`")

    def test_list_tables(self, monkeypatch):
        from dbx.client import list_tables
        _env(monkeypatch)
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

    def test_list_helpers_thread_token_through(self, monkeypatch):
        """list_* must thread the provided token down to sql.connect — same
        contract as run_query. Otherwise the worker thread would re-resolve
        and lose the request-scoped Bearer."""
        from dbx.client import list_catalogs, list_schemas, list_tables
        _env(monkeypatch)
        conn, _ = _mock_connection(rows=[], columns=["x"])
        for call in (
            lambda: list_catalogs(token="t1"),
            lambda: list_schemas("main", token="t2"),
            lambda: list_tables("main", "default", token="t3"),
        ):
            with patch("dbx.client.sql.connect", return_value=conn) as mc:
                call()
            assert mc.call_args.kwargs["access_token"] in ("t1", "t2", "t3")
