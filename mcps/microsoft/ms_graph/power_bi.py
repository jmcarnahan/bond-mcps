"""
Power BI REST API operations.

Uses the Power BI API at https://api.powerbi.com/v1.0/myorg — separate from
Microsoft Graph. Requires a token scoped to the Power BI resource
(https://analysis.windows.net/powerbi/api/.default), not the Graph resource.

All functions accept a PowerBIClient or AsyncPowerBIClient and return parsed dicts.
"""

import asyncio
import csv
import io
import time
from typing import Any, Dict, List, Optional

import httpx

from .graph_client import GraphError, _raise_for_graph_error

POWERBI_BASE_URL = "https://api.powerbi.com/v1.0/myorg"

_EXPORT_POLL_INTERVAL = 3   # seconds between export status polls
_EXPORT_POLL_TIMEOUT = 120  # seconds before giving up on an export


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class PowerBIClient:
    """Synchronous Power BI REST API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=POWERBI_BASE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._client.get(path, params=params)
        _raise_for_graph_error(response)
        return response.json()

    def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    def post_with_location(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> str:
        """POST that expects 202 + Location header (async PBI operations like export)."""
        response = self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(response.status_code, "UnexpectedStatus",
                             f"Expected 202 Accepted, got {response.status_code}")
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(response.status_code, "NoLocation",
                             "Expected Location header in 202 response")
        return location

    def get_bytes(self, path: str) -> bytes:
        """GET request returning raw bytes (for export file download)."""
        response = self._client.get(path, follow_redirects=True)
        _raise_for_graph_error(response)
        return response.content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PowerBIClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class AsyncPowerBIClient:
    """Asynchronous Power BI REST API client."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=POWERBI_BASE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = await self._client.get(path, params=params)
        _raise_for_graph_error(response)
        return response.json()

    async def post(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = await self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code == 202 or not response.content:
            return None
        return response.json()

    async def post_with_location(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> str:
        """POST that expects 202 + Location header (async PBI operations like export)."""
        response = await self._client.post(path, json=json_data)
        _raise_for_graph_error(response)
        if response.status_code != 202:
            raise GraphError(response.status_code, "UnexpectedStatus",
                             f"Expected 202 Accepted, got {response.status_code}")
        location = response.headers.get("Location", "")
        if not location:
            raise GraphError(response.status_code, "NoLocation",
                             "Expected Location header in 202 response")
        return location

    async def get_bytes(self, path: str) -> bytes:
        """GET request returning raw bytes (for export file download)."""
        response = await self._client.get(path, follow_redirects=True)
        _raise_for_graph_error(response)
        return response.content

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncPowerBIClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace_base(workspace_id: str) -> str:
    """Return the API base path for a workspace.

    - Empty string or "me": My workspace (personal) → root paths (e.g. /datasets/...)
    - Any other ID: named workspace → /groups/{workspace_id}/... paths
    """
    if not workspace_id or workspace_id.lower() == "me":
        return ""
    return f"/groups/{workspace_id}"


def _format_dax_results(data: Dict[str, Any]) -> str:
    """Flatten Power BI DAX query results to a CSV string.

    Input shape: {"results": [{"tables": [{"rows": [{col: val, ...}]}]}]}

    Handles sparse rows — Power BI omits null-valued columns from row dicts,
    so column sets may differ between rows. All unique columns across all rows
    are included as headers; missing values render as empty cells.
    """
    try:
        rows = data["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError):
        return "(no rows returned)"

    if not rows:
        return "(no rows returned)"

    # Collect all column names in order of first appearance across all rows
    all_keys = list(dict.fromkeys(k for row in rows for k in row.keys()))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=all_keys, extrasaction="ignore", restval="")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().strip()


# ---------------------------------------------------------------------------
# Synchronous operations
# ---------------------------------------------------------------------------

def list_workspaces(client: PowerBIClient) -> List[Dict[str, Any]]:
    """List all workspaces (groups) the authenticated user has access to."""
    data = client.get("/groups")
    return data.get("value", [])


def list_datasets(client: PowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all datasets in a workspace. Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = client.get(f"{base}/datasets")
    return data.get("value", [])


def list_reports(client: PowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all reports in a workspace. Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = client.get(f"{base}/reports")
    return data.get("value", [])


def list_dashboards(client: PowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all dashboards in a workspace. Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = client.get(f"{base}/dashboards")
    return data.get("value", [])


def execute_dax_query(
    client: PowerBIClient,
    workspace_id: str,
    dataset_id: str,
    dax_query: str,
) -> Dict[str, Any]:
    """Execute a DAX query against a dataset. Returns the raw API response."""
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }
    base = _workspace_base(workspace_id)
    result = client.post(f"{base}/datasets/{dataset_id}/executeQueries",
                         json_data=payload)
    return result or {}


def trigger_refresh(
    client: PowerBIClient,
    workspace_id: str,
    dataset_id: str,
) -> None:
    """Trigger an on-demand dataset refresh (fire-and-forget POST)."""
    base = _workspace_base(workspace_id)
    client.post(f"{base}/datasets/{dataset_id}/refreshes")


def get_refresh_history(
    client: PowerBIClient,
    workspace_id: str,
    dataset_id: str,
    top: int = 5,
) -> List[Dict[str, Any]]:
    """Get recent refresh history for a dataset."""
    base = _workspace_base(workspace_id)
    data = client.get(f"{base}/datasets/{dataset_id}/refreshes",
                      params={"$top": top})
    return data.get("value", [])


def start_export(
    client: PowerBIClient,
    workspace_id: str,
    report_id: str,
    export_format: str = "PDF",
    pages: Optional[List[str]] = None,
) -> str:
    """Start an async report export. Returns the export ID (parsed from Location header)."""
    payload: Dict[str, Any] = {"format": export_format}
    if pages:
        payload["powerBIReportConfiguration"] = {
            "pages": [{"pageName": p} for p in pages]
        }
    base = _workspace_base(workspace_id)
    location = client.post_with_location(
        f"{base}/reports/{report_id}/ExportTo",
        json_data=payload,
    )
    # Location header format: .../reports/{reportId}/exports/{exportId}
    # Extract the last path segment as the export ID. If the format ever changes
    # (e.g., query params appended), the ID will still be correct as long as the
    # export ID remains the final path component.
    export_id = location.rstrip("/").rsplit("/", 1)[-1]
    return export_id


def get_export_status(
    client: PowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> Dict[str, Any]:
    """Poll the status of an async report export."""
    base = _workspace_base(workspace_id)
    return client.get(f"{base}/reports/{report_id}/exports/{export_id}")


def download_export(
    client: PowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> bytes:
    """Download the completed export file as raw bytes."""
    base = _workspace_base(workspace_id)
    return client.get_bytes(
        f"{base}/reports/{report_id}/exports/{export_id}/file"
    )


def poll_export(
    client: PowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> Dict[str, Any]:
    """Poll export status until complete or timeout. Returns the final status dict."""
    deadline = time.monotonic() + _EXPORT_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_EXPORT_POLL_INTERVAL)
        status = get_export_status(client, workspace_id, report_id, export_id)
        state = status.get("status", "")
        if state == "Succeeded":
            return status
        if state == "Failed":
            err = status.get("error", {})
            raise GraphError(500, err.get("code", "ExportFailed"),
                             err.get("message", "Export failed"))
    raise GraphError(504, "ExportTimeout",
                     f"Export did not complete within {_EXPORT_POLL_TIMEOUT}s")


# ---------------------------------------------------------------------------
# Asynchronous operations
# ---------------------------------------------------------------------------

async def alist_workspaces(client: AsyncPowerBIClient) -> List[Dict[str, Any]]:
    """List all workspaces the authenticated user has access to (async)."""
    data = await client.get("/groups")
    return data.get("value", [])


async def alist_datasets(client: AsyncPowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all datasets in a workspace (async). Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = await client.get(f"{base}/datasets")
    return data.get("value", [])


async def alist_reports(client: AsyncPowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all reports in a workspace (async). Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = await client.get(f"{base}/reports")
    return data.get("value", [])


async def alist_dashboards(client: AsyncPowerBIClient, workspace_id: str) -> List[Dict[str, Any]]:
    """List all dashboards in a workspace (async). Pass workspace_id="" for My workspace."""
    base = _workspace_base(workspace_id)
    data = await client.get(f"{base}/dashboards")
    return data.get("value", [])


async def aexecute_dax_query(
    client: AsyncPowerBIClient,
    workspace_id: str,
    dataset_id: str,
    dax_query: str,
) -> Dict[str, Any]:
    """Execute a DAX query against a dataset (async). Returns the raw API response."""
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }
    base = _workspace_base(workspace_id)
    result = await client.post(
        f"{base}/datasets/{dataset_id}/executeQueries",
        json_data=payload,
    )
    return result or {}


async def atrigger_refresh(
    client: AsyncPowerBIClient,
    workspace_id: str,
    dataset_id: str,
) -> None:
    """Trigger an on-demand dataset refresh (async)."""
    base = _workspace_base(workspace_id)
    await client.post(f"{base}/datasets/{dataset_id}/refreshes")


async def aget_refresh_history(
    client: AsyncPowerBIClient,
    workspace_id: str,
    dataset_id: str,
    top: int = 5,
) -> List[Dict[str, Any]]:
    """Get recent refresh history for a dataset (async)."""
    base = _workspace_base(workspace_id)
    data = await client.get(f"{base}/datasets/{dataset_id}/refreshes",
                            params={"$top": top})
    return data.get("value", [])


async def astart_export(
    client: AsyncPowerBIClient,
    workspace_id: str,
    report_id: str,
    export_format: str = "PDF",
    pages: Optional[List[str]] = None,
) -> str:
    """Start an async report export (async). Returns the export ID."""
    payload: Dict[str, Any] = {"format": export_format}
    if pages:
        payload["powerBIReportConfiguration"] = {
            "pages": [{"pageName": p} for p in pages]
        }
    base = _workspace_base(workspace_id)
    location = await client.post_with_location(
        f"{base}/reports/{report_id}/ExportTo",
        json_data=payload,
    )
    export_id = location.rstrip("/").rsplit("/", 1)[-1]
    return export_id


async def aget_export_status(
    client: AsyncPowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> Dict[str, Any]:
    """Poll the status of an async report export (async)."""
    base = _workspace_base(workspace_id)
    return await client.get(
        f"{base}/reports/{report_id}/exports/{export_id}"
    )


async def adownload_export(
    client: AsyncPowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> bytes:
    """Download the completed export file as raw bytes (async)."""
    base = _workspace_base(workspace_id)
    return await client.get_bytes(
        f"{base}/reports/{report_id}/exports/{export_id}/file"
    )


async def apoll_export(
    client: AsyncPowerBIClient,
    workspace_id: str,
    report_id: str,
    export_id: str,
) -> Dict[str, Any]:
    """Poll export status until complete or timeout (async)."""
    deadline = time.monotonic() + _EXPORT_POLL_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(_EXPORT_POLL_INTERVAL)
        status = await aget_export_status(client, workspace_id, report_id, export_id)
        state = status.get("status", "")
        if state == "Succeeded":
            return status
        if state == "Failed":
            err = status.get("error", {})
            raise GraphError(500, err.get("code", "ExportFailed"),
                             err.get("message", "Export failed"))
    raise GraphError(504, "ExportTimeout",
                     f"Export did not complete within {_EXPORT_POLL_TIMEOUT}s")
