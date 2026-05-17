"""Shared fixtures + sample data for Databricks MCP tests.

Tests are hermetic: no live Databricks workspace required. OAuth HTTP exchanges
are mocked with respx; the SQL connector (which uses raw sockets) is mocked at
the dbx.client._connect boundary.
"""

import os

import pytest

WORKSPACE_HOST = "https://dbc-test-12345.cloud.databricks.com"
HTTP_PATH = "/sql/1.0/warehouses/abcdef1234567890"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all DATABRICKS_* env vars before each test so tests are isolated.

    Individual tests opt back in via monkeypatch.setenv as needed.
    """
    for var in (
        "DATABRICKS_HOST",
        "DATABRICKS_HTTP_PATH",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_ACCESS_TOKEN",
        "BOND_AUTH_PROXY_PORT",
    ):
        monkeypatch.delenv(var, raising=False)


SAMPLE_CATALOG_RESULT = {
    "columns": ["catalog"],
    "rows": [["main"], ["samples"], ["hive_metastore"]],
}

SAMPLE_SCHEMA_RESULT = {
    "columns": ["databaseName"],
    "rows": [["default"], ["bronze"], ["silver"], ["gold"]],
}

SAMPLE_TABLE_RESULT = {
    "columns": ["database", "tableName", "isTemporary"],
    "rows": [
        ["default", "events", False],
        ["default", "users", False],
        ["default", "tmp_join", True],
    ],
}

SAMPLE_SELECT_RESULT = {
    "columns": ["id", "name", "amount"],
    "rows": [
        [1, "alpha", 10.5],
        [2, "beta", 20.0],
        [3, None, 0.0],
    ],
}

SAMPLE_TOKEN_RESPONSE = {
    "access_token": "dbx-oauth-tok-abc",
    "refresh_token": "dbx-refresh-tok-xyz",
    "expires_in": 3600,
    "token_type": "Bearer",
}
