#!/usr/bin/env python3
"""
Databricks CLI — verify connectivity and run ad-hoc queries.

Used by `make login-databricks` (whoami) and direct dev use. In OAuth mode the
first invocation opens a browser; in PAT mode it just runs the query. Backend
(Bearer-header) mode is unreachable from the CLI by design.

Setup:
    1. Set DATABRICKS_HOST and DATABRICKS_HTTP_PATH (always required).
    2. EITHER set DATABRICKS_CLIENT_ID (+ optional secret) for OAuth,
       OR set DATABRICKS_ACCESS_TOKEN for PAT.
    3. For OAuth: start the auth proxy (`make dev` or `cd auth && poetry run python -m auth`).

Usage:
    databricks-cli whoami                       # auth check + SELECT current_user()
    databricks-cli query "SELECT 1"             # ad-hoc SQL
    databricks-cli catalogs                     # SHOW CATALOGS
    databricks-cli schemas <catalog>            # SHOW SCHEMAS IN <catalog>
    databricks-cli tables <catalog> <schema>    # SHOW TABLES IN <catalog>.<schema>
    databricks-cli logout                       # clear cached OAuth token
"""

import argparse
import csv
import io
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from auth import TokenStore
from dbx import client as db
from dbx.auth import AuthSource, get_auth_source
from dbx.client import DatabricksError


def _print_auth_mode() -> AuthSource:
    """Print the active auth mode and return it. Raises PermissionError if
    nothing is configured (caller catches and exits)."""
    source = get_auth_source()
    label = {
        AuthSource.OAUTH: "OAuth (DATABRICKS_CLIENT_ID)",
        AuthSource.PAT:   "PAT (DATABRICKS_ACCESS_TOKEN)",
        AuthSource.BEARER: "backend Bearer header",
    }[source]
    print(f"Databricks auth mode: {label}")
    return source


def _format_table(header, rows) -> str:
    """Pipe-delimited table, matching the MCP server's output format."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="|", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().rstrip("\r\n")


def _stringify(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def cmd_whoami(args):
    source = _print_auth_mode()
    if source is AuthSource.PAT:
        print("PAT mode — no browser login needed.")
    result = db.run_query("SELECT current_user() AS user")
    if result["rows"]:
        print(f"Authenticated as: {result['rows'][0][0]}")
    print("Connection OK.")


def cmd_query(args):
    result = db.run_query(args.sql)
    columns = result["columns"]
    rows = result["rows"]
    if not columns:
        print("(no result set)")
        return
    table = _format_table(
        columns, [[_stringify(v) for v in row] for row in rows]
    )
    print(table)
    suffix = ""
    if result.get("truncated"):
        suffix = " — truncated; refine with LIMIT"
    print(f"\n({len(rows)} row(s){suffix})")


def cmd_catalogs(args):
    for c in db.list_catalogs():
        print(c)


def cmd_schemas(args):
    for s in db.list_schemas(args.catalog):
        print(s)


def cmd_tables(args):
    for t in db.list_tables(args.catalog, args.schema):
        temp = " (temp)" if t.get("is_temporary") else ""
        print(f"{t['database']}.{t['table']}{temp}")


def cmd_logout(args):
    store = TokenStore("databricks")
    if store.cache_file.exists():
        store.clear()
        print("Cleared cached Databricks OAuth token.")
    else:
        print("No cached Databricks OAuth token to clear.")
    if os.environ.get("DATABRICKS_ACCESS_TOKEN"):
        print(
            "Note: DATABRICKS_ACCESS_TOKEN is set in your environment / .env. "
            "Unset it in your shell or remove it from .env to fully sign out."
        )


def main():
    parser = argparse.ArgumentParser(
        description="Databricks CLI — SQL warehouse queries with OAuth or PAT auth."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("whoami", help="Verify connectivity and print the current user")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("query", help="Run a single SQL statement")
    p.add_argument("sql", help="SQL statement to execute")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("catalogs", help="List catalogs (SHOW CATALOGS)")
    p.set_defaults(func=cmd_catalogs)

    p = sub.add_parser("schemas", help="List schemas in a catalog")
    p.add_argument("catalog")
    p.set_defaults(func=cmd_schemas)

    p = sub.add_parser("tables", help="List tables in catalog.schema")
    p.add_argument("catalog")
    p.add_argument("schema")
    p.set_defaults(func=cmd_tables)

    p = sub.add_parser("logout", help="Clear cached OAuth tokens")
    p.set_defaults(func=cmd_logout)

    args = parser.parse_args()

    try:
        args.func(args)
    except PermissionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except DatabricksError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


if __name__ == "__main__":
    main()
