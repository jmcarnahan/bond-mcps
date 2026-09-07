"""Integration tests for the edit_excel_workbook MCP tool."""

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from ms_graph.graph_client import GRAPH_BASE_URL

ITEM_ID = "file-id-xlsx-001"

SAMPLE_DRIVE_ITEM_XLSX = {
    "id": ITEM_ID,
    "name": "budget.xlsx",
    "size": 40_000,
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "webUrl": "https://contoso.sharepoint.com/sites/finance/budget.xlsx",
    "parentReference": {"driveId": "drive-001", "path": "/drive/root:"},
}

WORKSHEETS = {"value": [{"name": "Sheet1", "position": 0}]}


@pytest.fixture(autouse=True)
def _desktop_client_header():
    """Pin this module to the desktop JSON contract.

    In-process clients carry no HTTP headers, so FormatNegotiation would
    render every dict tool compactly and `_structured` would have no dict to
    read. The compact rendering is covered by test_format_middleware.py.
    """
    with patch(
        "bond_common.middleware.get_http_headers",
        return_value={"x-bond-client": "desktop"},
    ):
        yield


def _mock_token(token: str = "test-ms-token"):
    return patch("ms_graph_mcp.get_graph_token", return_value=token)


def _structured(result) -> dict:
    """The dict edit_document returned, however the client surfaced it."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _wb(base: str) -> str:
    return f"{GRAPH_BASE_URL}{base}/items/{ITEM_ID}/workbook"


def _mock_workbook_session(base: str = "/me/drive"):
    wb = _wb(base)
    respx.post(f"{wb}/createSession").mock(
        return_value=httpx.Response(201, json={"id": "session-xyz"})
    )
    respx.post(f"{wb}/closeSession").mock(return_value=httpx.Response(204))
    respx.get(f"{wb}/worksheets").mock(return_value=httpx.Response(200, json=WORKSHEETS))


@pytest.fixture
def mcp_server():
    from ms_graph_mcp import mcp

    return mcp


async def _call(mcp_server, args):
    from fastmcp import Client

    with _mock_token():
        async with Client(mcp_server) as client:
            return await client.call_tool("edit_document", args)


class TestEditExcelWorkbookTool:
    @respx.mock
    async def test_set_cell_in_place(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session()
        patch_route = respx.patch(
            f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='B2')"
        ).mock(return_value=httpx.Response(200, json={}))

        edits = json.dumps([{"op": "set_cell", "cell": "B2", "value": "42"}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})

        assert _structured(result) == {
            "ok": True,
            "kind": "excel",
            "name": "budget.xlsx",
            "operations": 1,
            "ops": "set_cell",
            "default_sheet": "Sheet1",
            "worksheets": "Sheet1",
            "web_url": SAMPLE_DRIVE_ITEM_XLSX["webUrl"],
        }
        assert patch_route.called
        # No re-upload PUT of the whole file
        assert not any(c.request.method == "PUT" for c in respx.calls)

    @respx.mock
    async def test_add_column(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session()
        respx.get(f"{_wb('/me/drive')}/worksheets('Sheet1')/usedRange").mock(
            return_value=httpx.Response(
                200, json={"address": "Sheet1!A1:B3", "values": [["a", "b"]]}
            )
        )
        patch_route = respx.patch(
            f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='C1:C3')"
        ).mock(return_value=httpx.Response(200, json={}))

        edits = json.dumps(
            [{"op": "add_column", "header": "Total", "values": ["=A2*B2", "=A3*B3"]}]
        )
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})

        assert _structured(result)["ok"] is True
        assert patch_route.called

    @respx.mock
    async def test_unsupported_extension_rejected(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_DRIVE_ITEM_XLSX, "name": "legacy.xls"})
        )
        edits = json.dumps([{"op": "set_cell", "cell": "A1", "value": "x"}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})
        data = _structured(result)
        assert data["error"] == "invalid_arguments"
        assert "not an editable document" in data["reason"]

    @respx.mock
    async def test_insert_columns(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session()
        insert_route = respx.post(
            f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='C:C')/insert"
        ).mock(return_value=httpx.Response(200, json={}))

        edits = json.dumps([{"op": "insert_columns", "at": 3}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})

        assert _structured(result)["ok"] is True
        assert json.loads(insert_route.calls[0].request.content) == {"shift": "Right"}

    @respx.mock
    async def test_multiple_operations(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session()
        respx.patch(f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='A1')").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='2:2')/insert").mock(
            return_value=httpx.Response(200, json={})
        )

        edits = json.dumps(
            [
                {"op": "set_cell", "cell": "A1", "value": "Header"},
                {"op": "insert_rows", "at": 2},
            ]
        )
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})

        data = _structured(result)
        assert data["operations"] == 2
        assert data["ops"] == "set_cell, insert_rows"

    @respx.mock
    async def test_invalid_edits_json(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": "not json{"})
        data = _structured(result)
        assert data["error"] == "invalid_arguments"
        assert data["reason"].startswith("Invalid edits:")

    @respx.mock
    async def test_empty_edits(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": "[]"})
        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "No edit operations provided.",
        }

    @respx.mock
    async def test_file_not_found(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        )
        edits = json.dumps([{"op": "set_cell", "cell": "A1", "value": "x"}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})
        assert _structured(result) == {
            "error": "not_found",
            "reason": f"File not found: {ITEM_ID}",
        }

    @respx.mock
    async def test_sharepoint_site_id(self, mcp_server):
        site_id = "site-123"
        base = f"/sites/{site_id}/drive"
        respx.get(f"{GRAPH_BASE_URL}{base}/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session(base)
        patch_route = respx.patch(f"{_wb(base)}/worksheets('Sheet1')/range(address='A1')").mock(
            return_value=httpx.Response(200, json={})
        )

        edits = json.dumps([{"op": "set_cell", "cell": "A1", "value": "x"}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits, "site_id": site_id})

        assert _structured(result)["ok"] is True
        assert patch_route.called

    @respx.mock
    async def test_graph_edit_error_surfaced(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_XLSX)
        )
        _mock_workbook_session()
        respx.patch(f"{_wb('/me/drive')}/worksheets('Sheet1')/range(address='ZZ99')").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": "InvalidArgument", "message": "bad range"}}
            )
        )
        edits = json.dumps([{"op": "set_cell", "cell": "ZZ99", "value": "x"}])
        result = await _call(mcp_server, {"item_id": ITEM_ID, "edits": edits})
        data = _structured(result)
        assert data["error"] == "edit_failed"
        assert "InvalidArgument" in data["reason"]
