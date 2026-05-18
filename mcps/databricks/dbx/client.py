"""
Databricks SQL client wrapper.

The auth chain is split across an async/sync boundary on purpose:

  * `resolve_token_now()` runs SYNCHRONOUSLY in the FastMCP tool's call frame,
    where the request's `Authorization: Bearer` header is reachable via the
    contextvar `fastmcp.server.dependencies.get_http_headers`. This is the
    only safe place to read the request-scoped token.

  * `run_query(..., token=...)` (and the other tool-backed helpers) take the
    pre-resolved token and pass it to `sql.connect(access_token=token)`. They
    can be safely called from a worker thread (`asyncio.to_thread`) without
    losing the bearer context.

Telemetry is disabled (`enable_telemetry=False` AND `force_enable_telemetry=
False`) so the connector does not spawn a background daemon thread that would
call our auth path with no request context — that path would either fail
silently or attribute telemetry to whatever PAT/OAuth happens to be set on
the server process, leaking identity in a multi-tenant Bond-AI deployment.
Both flags are needed because `force_enable_telemetry=True` overrides
`enable_telemetry=False` (see databricks/sql/telemetry/telemetry_client.py
`is_telemetry_enabled`).

`_socket_timeout=300` caps each individual HTTP request to the warehouse at
5 minutes. This is NOT a query-duration timeout — a Databricks query
typically makes many short polling requests (ExecuteStatement, repeated
GetOperationStatus polls, FetchResults chunks), and each one is well under
the limit on its own. What this prevents is a single HTTP request hanging
at the TCP layer (a wedged load balancer, a stuck thrift call). Real query
timeouts are enforced by the MCP tool layer via `asyncio.wait_for`.
"""

import logging
import os
from typing import Any

from databricks import sql
from databricks.sql import exc as dbexc

from dbx.auth import AuthSource, get_databricks_token, resolve_token_now
from dbx.local_auth import host_without_scheme

# Re-export so `from dbx import client as db; db.resolve_token_now()` still
# works (single source of truth lives in dbx.auth — see its module docstring).
__all__ = ["DatabricksError", "MAX_FETCH_ROWS", "resolve_token_now",
           "run_query", "list_catalogs", "list_schemas", "list_tables"]

logger = logging.getLogger(__name__)

# Hard cap on rows pulled from the warehouse per call. Protects the MCP from
# OOM on `SELECT * FROM <huge>` while still leaving headroom for ad-hoc admin
# queries. Display formatting in databricks_mcp._format_result caps to a
# smaller preview number. Public — referenced by databricks_mcp.
MAX_FETCH_ROWS = 5000

# Per-HTTP-request socket timeout (seconds). Applies to each connect/read on
# the connector's internal HTTP calls, NOT to total query duration. A long
# query that makes many short polling calls is unaffected by this setting.
# Use the MCP tool layer's asyncio.wait_for wrapper for total-duration caps.
_SOCKET_TIMEOUT_S = 300


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


def _connect(token: str | None):
    """Open a SQL connection.

    If `token` is provided, it is used verbatim as the Bearer credential
    (no callback, no refresh). If None, `get_databricks_token()` is called
    once at connect time — only safe when running synchronously in a context
    where FastMCP's request contextvar is alive.
    """
    host, http_path = _require_env()
    if token is None:
        token = get_databricks_token()
    return sql.connect(
        server_hostname=host_without_scheme(host),
        http_path=http_path,
        access_token=token,
        user_agent_entry="bond-ai-databricks-mcp",
        # Both telemetry flags are required: force_enable_telemetry=True
        # overrides enable_telemetry=False (telemetry_client.py:121).
        enable_telemetry=False,
        force_enable_telemetry=False,
        _socket_timeout=_SOCKET_TIMEOUT_S,
    )


def _classify_error(exc: Exception) -> DatabricksError:
    """Map a connector / network exception to a DatabricksError with a
    coarse error_code.

    Auth markers (401/403/etc.) are checked across exception types because
    the connector wraps HTTP errors inconsistently. "Unreachable" is reserved
    for STRONG network signals only — transient server-side errors (e.g.
    deadlocks wrapped as RequestError) are surfaced as SQLError so the user
    sees the actual server message instead of a misleading "can't reach"
    note.
    """
    msg = str(exc)
    lower = msg.lower()

    # 1. Connector's own SQL error types — server-side execution failures.
    if isinstance(exc, (dbexc.ServerOperationError, dbexc.ProgrammingError)):
        return DatabricksError(msg, error_code="SQLError")

    # 2. Auth markers across all exception types. Connector wraps HTTP 401/403
    # inside RequestError / OperationalError / etc., so type alone is unreliable.
    if "401" in msg or "unauthorized" in lower or "invalid_grant" in lower:
        return DatabricksError(msg, error_code="Unauthorized")
    if "403" in msg or "forbidden" in lower or "permission" in lower:
        return DatabricksError(msg, error_code="Forbidden")

    # 3. Strong network signals only. RequestError without these markers is
    # NOT automatically "unreachable" — the connector also surfaces transient
    # backend errors (e.g. deadlocks) as RequestError.
    network_markers = (
        "could not resolve", "name or service not known", "connection refused",
        "connection reset", "name resolution", "no route to host",
        "network is unreachable",
    )
    if isinstance(exc, (ConnectionError, OSError)) \
            or any(m in lower for m in network_markers):
        return DatabricksError(msg, error_code="Unreachable")

    # 4. Anything from the connector hierarchy that didn't match above is a
    # server-side issue worth surfacing verbatim (deadlocks, timeouts, retry
    # exhaustion on a remote operation, etc.).
    if isinstance(exc, dbexc.Error):
        return DatabricksError(msg, error_code="SQLError")
    return DatabricksError(msg, error_code="Unknown")


def run_query(query: str, *, token: str | None = None) -> dict[str, Any]:
    """Execute a SQL query and return columns + rows.

    Caps fetched rows at `MAX_FETCH_ROWS`. Sets `truncated=True` when the
    cursor still had more rows past the cap.

    Returns:
        {"columns": [str, ...], "rows": [[val, ...], ...], "truncated": bool}
    """
    try:
        with _connect(token) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                # Pull one extra so we can detect-and-discard the (n+1)th row
                # without doing a second round trip if we hit exactly cap.
                fetched = cur.fetchmany(MAX_FETCH_ROWS + 1)
                truncated = len(fetched) > MAX_FETCH_ROWS
                rows = fetched[:MAX_FETCH_ROWS]
                columns = [d[0] for d in (cur.description or [])]
        return {
            "columns": columns,
            "rows": [list(r) for r in rows],
            "truncated": truncated,
        }
    except DatabricksError:
        raise
    except Exception as exc:
        raise _classify_error(exc) from exc


def list_catalogs(*, token: str | None = None) -> list[str]:
    result = run_query("SHOW CATALOGS", token=token)
    return [row[0] for row in result["rows"]]


def list_schemas(catalog: str, *, token: str | None = None) -> list[str]:
    catalog_quoted = _quote_identifier(catalog)
    result = run_query(f"SHOW SCHEMAS IN {catalog_quoted}", token=token)
    return [row[0] for row in result["rows"]]


def list_tables(
    catalog: str, schema: str, *, token: str | None = None
) -> list[dict[str, str]]:
    catalog_quoted = _quote_identifier(catalog)
    schema_quoted = _quote_identifier(schema)
    result = run_query(
        f"SHOW TABLES IN {catalog_quoted}.{schema_quoted}", token=token
    )
    out = []
    for row in result["rows"]:
        out.append({
            "database": row[0] if len(row) > 0 else "",
            "table": row[1] if len(row) > 1 else "",
            "is_temporary": bool(row[2]) if len(row) > 2 else False,
        })
    return out


def _quote_identifier(name: str) -> str:
    """Backtick-quote a Databricks identifier, escaping embedded backticks."""
    return "`" + name.replace("`", "``") + "`"
