"""Shared error formatting + table rendering for both MCP server and CLI.

Previously `databricks_mcp.py` had `_friendly_error/_format_table/_stringify`
and `databricks_cli.py` had near-duplicates of `_format_table/_stringify`
plus no friendly mapping at all. Lifting them here means:

  * The CLI surfaces the same "missing `sql` scope" / "run make
    login-databricks" guidance as the MCP — important because the CLI is
    often the first thing users run during `make login-databricks`.
  * Output formatting is identical across surfaces — a query run via CLI
    looks the same as the same query run via the MCP tool.
"""

import csv
import io
from collections.abc import Sequence

from dbx.auth import AuthSource
from dbx.client import DatabricksError


def friendly_error(err: DatabricksError, source: AuthSource | None) -> str:
    """Map a DatabricksError code to a user-readable message.

    `source` is the AuthSource captured at the call site (tool entry for the
    MCP, command start for the CLI). Threading it through avoids re-reading
    env vars at error-formatting time, which could give a message that doesn't
    match the actual auth path used.

    `source=None` is acceptable — used when nothing is configured yet.
    """
    code = err.error_code

    if code == "MissingConfig":
        return f"Databricks not configured: {err}"

    if code == "Unauthorized":
        if source is AuthSource.OAUTH:
            return (
                "Databricks authentication failed. Run `make login-databricks` "
                "to reconnect, or check that DATABRICKS_HOST matches the "
                "workspace where the OAuth app is registered."
            )
        if source is AuthSource.PAT:
            return (
                "Databricks PAT rejected. Your DATABRICKS_ACCESS_TOKEN may be "
                "expired, revoked, missing the `sql` scope, or scoped to a "
                "different workspace than DATABRICKS_HOST."
            )
        if source is AuthSource.BEARER:
            return (
                "Databricks authentication failed. The forwarded Bearer token "
                "may be expired or scoped to a different workspace."
            )
        return (
            "Databricks rejected the token. No auth source is currently "
            "configured — see README for setup."
        )

    if code == "Forbidden":
        return (
            "Databricks permission denied. Your user lacks access to the SQL "
            "warehouse at DATABRICKS_HTTP_PATH, or to the requested "
            "catalog/schema."
        )

    if code == "Unreachable":
        return f"Cannot reach Databricks at DATABRICKS_HOST. Check the workspace " f"URL.\n({err})"

    if code == "SQLError":
        return f"Databricks SQL error:\n```\n{err}\n```"

    return f"Databricks error: {err}"


def stringify(val) -> str:
    """Convert a SQL value to a stable string for table output."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def format_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Pipe-delimited CSV table — matches the atlassian MCP table format
    so output across MCPs is visually consistent."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().rstrip("\r\n")
