"""
Databricks SQL client wrapper.

Wraps databricks-sql-connector with a credentials_provider callback so token
refresh is transparent — the connector calls back per HTTP request, and the
callback re-resolves the token (cached/refreshed for OAuth, returned as-is
for PAT or backend Bearer).

One connection per query is intentional: MCP usage is interactive, low-QPS,
and pooling would re-introduce the token-expiry-vs-pool race that PAT-only
code paths never had to worry about.
"""

import logging
import os
from typing import Any

from databricks import sql
from databricks.sql.exc import Error as DatabricksSQLError

from dbx.auth import AuthSource, get_auth_source, get_databricks_token
from dbx.local_auth import host_without_scheme

logger = logging.getLogger(__name__)


class DatabricksError(Exception):
    """Wraps SQL connector and auth errors with a coarse error code."""

    def __init__(self, message: str, error_code: str = "Unknown"):
        super().__init__(message)
        self.error_code = error_code


def _require_env() -> tuple[str, str]:
    host = os.environ.get("DATABRICKS_HOST")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    missing = []
    if not host:
        missing.append("DATABRICKS_HOST")
    if not http_path:
        missing.append("DATABRICKS_HTTP_PATH")
    if missing:
        raise DatabricksError(
            f"Missing required environment variables: {', '.join(missing)}.",
            error_code="MissingConfig",
        )
    return host, http_path


def _credentials_provider():
    """Build the header_factory callable expected by databricks-sql-connector.

    The connector invokes the returned factory on every HTTP request, so
    OAuth refresh happens transparently via TokenStore.
    """

    def header_factory():
        token = get_databricks_token()
        return {"Authorization": f"Bearer {token}"}

    return header_factory


def _connect():
    host, http_path = _require_env()
    return sql.connect(
        server_hostname=host_without_scheme(host),
        http_path=http_path,
        credentials_provider=_credentials_provider,
        user_agent_entry="bond-ai-databricks-mcp",
    )


def _classify_error(exc: Exception) -> DatabricksError:
    """Map low-level errors to friendly DatabricksError codes."""
    msg = str(exc)
    lower = msg.lower()
    if "401" in msg or "unauthorized" in lower or "invalid_grant" in lower:
        return DatabricksError(msg, error_code="Unauthorized")
    if "403" in msg or "forbidden" in lower or "permission" in lower:
        return DatabricksError(msg, error_code="Forbidden")
    if "could not resolve" in lower or "name or service not known" in lower \
            or "connection refused" in lower:
        return DatabricksError(msg, error_code="Unreachable")
    if isinstance(exc, DatabricksSQLError):
        return DatabricksError(msg, error_code="SQLError")
    return DatabricksError(msg, error_code="Unknown")


def run_query(query: str) -> dict[str, Any]:
    """Execute a SQL query and return columns + rows.

    Returns:
        {"columns": [str, ...], "rows": [[val, ...], ...]}
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                columns = [d[0] for d in (cur.description or [])]
        return {
            "columns": columns,
            "rows": [list(r) for r in rows],
        }
    except DatabricksError:
        raise
    except Exception as exc:
        raise _classify_error(exc) from exc


def list_catalogs() -> list[str]:
    result = run_query("SHOW CATALOGS")
    return [row[0] for row in result["rows"]]


def list_schemas(catalog: str) -> list[str]:
    catalog_quoted = _quote_identifier(catalog)
    result = run_query(f"SHOW SCHEMAS IN {catalog_quoted}")
    # SHOW SCHEMAS returns one column (databaseName / namespace)
    return [row[0] for row in result["rows"]]


def list_tables(catalog: str, schema: str) -> list[dict[str, str]]:
    catalog_quoted = _quote_identifier(catalog)
    schema_quoted = _quote_identifier(schema)
    result = run_query(f"SHOW TABLES IN {catalog_quoted}.{schema_quoted}")
    # SHOW TABLES columns: database, tableName, isTemporary
    out = []
    for row in result["rows"]:
        out.append({
            "database": row[0] if len(row) > 0 else "",
            "table": row[1] if len(row) > 1 else "",
            "is_temporary": bool(row[2]) if len(row) > 2 else False,
        })
    return out


def current_auth_source() -> AuthSource:
    """Pass-through to dbx.auth.get_auth_source for use by the MCP server / CLI."""
    return get_auth_source()


def _quote_identifier(name: str) -> str:
    """Backtick-quote a Databricks identifier, escaping embedded backticks."""
    return "`" + name.replace("`", "``") + "`"
