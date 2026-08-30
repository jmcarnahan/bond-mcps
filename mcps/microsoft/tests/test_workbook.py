"""Unit tests for the low-level Graph Workbook API wrappers (ms_graph.workbook)."""

import httpx
import pytest
import respx
from ms_graph import workbook
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient

ITEM_ID = "wb-item-001"
WB = f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}/workbook"


class TestPureHelpers:
    def test_workbook_base(self):
        assert workbook._workbook_base("/me/drive", "x") == "/me/drive/items/x/workbook"
        assert workbook._workbook_base("/sites/s1/drive", "x") == "/sites/s1/drive/items/x/workbook"

    def test_session_headers(self):
        assert workbook._session_headers("sess-1") == {"workbook-session-id": "sess-1"}
        assert workbook._session_headers(None) is None
        assert workbook._session_headers("") is None

    @pytest.mark.parametrize(
        "name,escaped",
        [
            ("Sheet1", "Sheet1"),
            ("O'Brien", "O''Brien"),
            ("Q1 2025", "Q1 2025"),
            ("a'b'c", "a''b''c"),
            ("''", "''''"),
        ],
    )
    def test_escape_sheet(self, name, escaped):
        assert workbook._escape_sheet(name) == escaped


class TestSession:
    @respx.mock
    async def test_create_session_returns_id(self):
        route = respx.post(f"{WB}/createSession").mock(
            return_value=httpx.Response(201, json={"id": "sess-xyz"})
        )
        async with AsyncGraphClient("t") as client:
            sid = await workbook.acreate_session(client, "/me/drive", ITEM_ID)
        assert sid == "sess-xyz"
        import json

        assert json.loads(route.calls[0].request.content) == {"persistChanges": True}

    @respx.mock
    async def test_create_session_none_when_no_body(self):
        respx.post(f"{WB}/createSession").mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("t") as client:
            sid = await workbook.acreate_session(client, "/me/drive", ITEM_ID)
        assert sid is None

    @respx.mock
    async def test_close_session_sends_header(self):
        route = respx.post(f"{WB}/closeSession").mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("t") as client:
            await workbook.aclose_session(client, "/me/drive", ITEM_ID, "sess-1")
        assert route.calls[0].request.headers.get("workbook-session-id") == "sess-1"


class TestRangeOps:
    @respx.mock
    async def test_list_worksheets(self):
        respx.get(f"{WB}/worksheets").mock(
            return_value=httpx.Response(200, json={"value": [{"name": "Sheet1"}]})
        )
        async with AsyncGraphClient("t") as client:
            ws = await workbook.alist_worksheets(client, "/me/drive", ITEM_ID)
        assert ws == [{"name": "Sheet1"}]

    @respx.mock
    async def test_get_used_range_selects_fields(self):
        route = respx.get(f"{WB}/worksheets('Sheet1')/usedRange").mock(
            return_value=httpx.Response(200, json={"address": "Sheet1!A1:B2"})
        )
        async with AsyncGraphClient("t") as client:
            await workbook.aget_used_range(client, "/me/drive", ITEM_ID, "Sheet1")
        assert route.calls[0].request.url.params["$select"] == "address,rowCount,columnCount,values"

    @respx.mock
    async def test_set_range_escapes_sheet_name(self):
        # A sheet named O'Brien must reach the API as O''Brien in the path.
        route = respx.patch(f"{WB}/worksheets('O''Brien')/range(address='A1')").mock(
            return_value=httpx.Response(200, json={})
        )
        async with AsyncGraphClient("t") as client:
            await workbook.aset_range(
                client, "/me/drive", ITEM_ID, "O'Brien", "A1", [["v"]], "sess-1"
            )
        assert route.called
        assert route.calls[0].request.headers.get("workbook-session-id") == "sess-1"

    @respx.mock
    async def test_insert_range_shift(self):
        route = respx.post(f"{WB}/worksheets('Sheet1')/range(address='2:2')/insert").mock(
            return_value=httpx.Response(200, json={})
        )
        async with AsyncGraphClient("t") as client:
            await workbook.ainsert_range(client, "/me/drive", ITEM_ID, "Sheet1", "2:2", "Down")
        import json

        assert json.loads(route.calls[0].request.content) == {"shift": "Down"}

    @respx.mock
    async def test_delete_range_shift(self):
        route = respx.post(f"{WB}/worksheets('Sheet1')/range(address='B:B')/delete").mock(
            return_value=httpx.Response(200, json={})
        )
        async with AsyncGraphClient("t") as client:
            await workbook.adelete_range(client, "/me/drive", ITEM_ID, "Sheet1", "B:B", "Left")
        import json

        assert json.loads(route.calls[0].request.content) == {"shift": "Left"}
