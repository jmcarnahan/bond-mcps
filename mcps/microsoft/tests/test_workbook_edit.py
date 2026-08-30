"""Unit tests for workbook edit orchestration and A1-notation helpers."""

import json

import httpx
import pytest
import respx
from ms_graph import workbook_edit
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient

ITEM_ID = "wb-item-001"
WB = f"{GRAPH_BASE_URL}/me/drive/items/{ITEM_ID}/workbook"

WORKSHEETS = {"value": [{"name": "Sheet1", "position": 0}, {"name": "Data", "position": 1}]}


# ---------------------------------------------------------------------------
# parse_workbook_edits
# ---------------------------------------------------------------------------


class TestParseWorkbookEdits:
    def test_valid_ops(self):
        edits = json.dumps(
            [
                {"op": "set_cell", "cell": "B2", "value": "42"},
                {"op": "set_range", "range": "A1:B2", "values": [["a", "b"], ["c", "d"]]},
                {"op": "add_column", "header": "Total", "values": ["=A2*B2"]},
                {"op": "insert_rows", "at": 5, "count": 2},
                {"op": "delete_columns", "at": 3},
            ]
        )
        ops = workbook_edit.parse_workbook_edits(edits)
        assert len(ops) == 5

    def test_not_a_list(self):
        with pytest.raises(ValueError, match="must be a JSON array"):
            workbook_edit.parse_workbook_edits('{"op": "set_cell"}')

    def test_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            workbook_edit.parse_workbook_edits("not json{")

    def test_unknown_op(self):
        with pytest.raises(ValueError, match="unknown op"):
            workbook_edit.parse_workbook_edits(json.dumps([{"op": "frobnicate"}]))

    def test_missing_required_field(self):
        with pytest.raises(ValueError, match="missing 'value'"):
            workbook_edit.parse_workbook_edits(json.dumps([{"op": "set_cell", "cell": "A1"}]))

    def test_set_range_values_not_2d(self):
        with pytest.raises(ValueError, match="2-D array"):
            workbook_edit.parse_workbook_edits(
                json.dumps([{"op": "set_range", "range": "A1", "values": ["a", "b"]}])
            )

    def test_insert_rows_bad_at(self):
        with pytest.raises(ValueError, match="positive integer"):
            workbook_edit.parse_workbook_edits(json.dumps([{"op": "insert_rows", "at": 0}]))

    def test_insert_rows_bad_count(self):
        with pytest.raises(ValueError, match="'count' must be"):
            workbook_edit.parse_workbook_edits(
                json.dumps([{"op": "insert_rows", "at": 2, "count": 0}])
            )


# ---------------------------------------------------------------------------
# A1-notation helpers
# ---------------------------------------------------------------------------


class TestA1Helpers:
    @pytest.mark.parametrize(
        "letters,idx",
        [("A", 1), ("B", 2), ("Z", 26), ("AA", 27), ("AB", 28), ("BA", 53)],
    )
    def test_col_roundtrip(self, letters, idx):
        assert workbook_edit._col_to_index(letters) == idx
        assert workbook_edit._index_to_col(idx) == letters

    def test_parse_used_range_basic(self):
        parsed = workbook_edit._parse_used_range({"address": "Sheet1!A1:C10", "values": [["x"]]})
        assert parsed["end_col"] == 3
        assert parsed["start_row"] == 1
        assert parsed["empty"] is False

    def test_parse_used_range_empty_sheet(self):
        parsed = workbook_edit._parse_used_range({"address": "Sheet1!A1", "values": [[None]]})
        assert parsed["empty"] is True

    def test_parse_used_range_no_sheet_prefix(self):
        parsed = workbook_edit._parse_used_range({"address": "B2:D5", "values": [["v"]]})
        assert parsed["end_col"] == 4
        assert parsed["start_row"] == 2


# ---------------------------------------------------------------------------
# apply_workbook_edits (respx-mocked Graph endpoints)
# ---------------------------------------------------------------------------


def _mock_session():
    respx.post(f"{WB}/createSession").mock(
        return_value=httpx.Response(201, json={"id": "session-abc"})
    )
    respx.post(f"{WB}/closeSession").mock(return_value=httpx.Response(204))
    respx.get(f"{WB}/worksheets").mock(return_value=httpx.Response(200, json=WORKSHEETS))


class TestApplyWorkbookEdits:
    @respx.mock
    async def test_set_cell_dispatch_and_session(self):
        _mock_session()
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='B2')").mock(
            return_value=httpx.Response(200, json={"address": "Sheet1!B2"})
        )

        ops = [{"op": "set_cell", "cell": "B2", "value": "42"}]
        async with AsyncGraphClient("t") as client:
            result = await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        assert result["operations"] == ["set_cell"]
        assert result["default_sheet"] == "Sheet1"
        # Session header propagated on the PATCH
        assert patch_route.calls[0].request.headers.get("workbook-session-id") == "session-abc"
        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [["42"]]}
        # Session was closed
        assert respx.calls.last.request.url.path.endswith("/closeSession")

    @respx.mock
    async def test_set_range(self):
        _mock_session()
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='A1:B2')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "set_range", "range": "A1:B2", "values": [["a", "b"], ["c", "d"]]}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [["a", "b"], ["c", "d"]]}

    @respx.mock
    async def test_add_column_after_used_range(self):
        _mock_session()
        respx.get(f"{WB}/worksheets('Sheet1')/usedRange").mock(
            return_value=httpx.Response(
                200, json={"address": "Sheet1!A1:B3", "values": [["h1", "h2"]]}
            )
        )
        # Used range ends at column B(2), so new column is C, header row 1.
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='C1:C3')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "add_column", "header": "Total", "values": ["=A2*B2", "=A3*B3"]}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [["Total"], ["=A2*B2"], ["=A3*B3"]]}

    @respx.mock
    async def test_add_column_empty_sheet(self):
        _mock_session()
        respx.get(f"{WB}/worksheets('Sheet1')/usedRange").mock(
            return_value=httpx.Response(200, json={"address": "Sheet1!A1", "values": [[None]]})
        )
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='A1:A1')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "add_column", "header": "Name"}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [["Name"]]}

    @respx.mock
    async def test_add_column_used_range_not_at_row_1(self):
        # Data offset to B5:D10 → new column E, header at E5, values E6/E7.
        _mock_session()
        respx.get(f"{WB}/worksheets('Sheet1')/usedRange").mock(
            return_value=httpx.Response(
                200, json={"address": "Sheet1!B5:D10", "values": [["x", "y", "z"]]}
            )
        )
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='E5:E7')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "add_column", "header": "Total", "values": [1, 2]}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [["Total"], [1], [2]]}

    @respx.mock
    async def test_insert_rows(self):
        _mock_session()
        insert_route = respx.post(f"{WB}/worksheets('Sheet1')/range(address='5:6')/insert").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "insert_rows", "at": 5, "count": 2}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(insert_route.calls[0].request.content)
        assert body == {"shift": "Down"}

    @respx.mock
    async def test_delete_columns(self):
        _mock_session()
        delete_route = respx.post(f"{WB}/worksheets('Sheet1')/range(address='C:D')/delete").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "delete_columns", "at": 3, "count": 2}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(delete_route.calls[0].request.content)
        assert body == {"shift": "Left"}

    @respx.mock
    async def test_target_named_sheet(self):
        _mock_session()
        patch_route = respx.patch(f"{WB}/worksheets('Data')/range(address='A1')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "set_cell", "cell": "A1", "value": "x", "sheet": "Data"}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        assert patch_route.called

    @respx.mock
    async def test_unknown_sheet_raises(self):
        _mock_session()
        ops = [{"op": "set_cell", "cell": "A1", "value": "x", "sheet": "Ghost"}]
        async with AsyncGraphClient("t") as client:
            with pytest.raises(workbook_edit.EditError, match="not found"):
                await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

    @respx.mock
    async def test_graph_error_maps_to_edit_error_with_index(self):
        _mock_session()
        respx.patch(f"{WB}/worksheets('Sheet1')/range(address='A1')").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": "InvalidArgument", "message": "bad range"}}
            )
        )
        ops = [{"op": "set_cell", "cell": "A1", "value": "x"}]
        async with AsyncGraphClient("t") as client:
            with pytest.raises(workbook_edit.EditError) as exc:
                await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)
        assert exc.value.op_index == 0

    @respx.mock
    async def test_set_cell_numeric_value(self):
        _mock_session()
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='B2')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "set_cell", "cell": "B2", "value": 42}]
        async with AsyncGraphClient("t") as client:
            await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"values": [[42]]}

    @respx.mock
    async def test_empty_operations_short_circuits(self):
        # No Graph calls should be made for an empty op list.
        async with AsyncGraphClient("t") as client:
            result = await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, [])
        assert result["operations"] == []
        assert len(respx.calls) == 0

    @respx.mock
    async def test_session_closed_when_op_fails(self):
        _mock_session()
        close_route = respx.post(f"{WB}/closeSession").mock(return_value=httpx.Response(204))
        respx.patch(f"{WB}/worksheets('Sheet1')/range(address='A1')").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": "InvalidArgument", "message": "bad"}}
            )
        )
        ops = [{"op": "set_cell", "cell": "A1", "value": "x"}]
        async with AsyncGraphClient("t") as client:
            with pytest.raises(workbook_edit.EditError):
                await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)
        # The finally block must still close the session even though the op failed.
        assert close_route.called

    @respx.mock
    async def test_no_worksheets_raises(self):
        respx.post(f"{WB}/createSession").mock(
            return_value=httpx.Response(201, json={"id": "session-abc"})
        )
        respx.post(f"{WB}/closeSession").mock(return_value=httpx.Response(204))
        respx.get(f"{WB}/worksheets").mock(return_value=httpx.Response(200, json={"value": []}))

        ops = [{"op": "set_cell", "cell": "A1", "value": "x"}]
        async with AsyncGraphClient("t") as client:
            with pytest.raises(workbook_edit.EditError, match="no worksheets"):
                await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

    @respx.mock
    async def test_session_fallback_when_create_fails(self):
        # createSession fails -> session-less mode; edits still applied.
        respx.post(f"{WB}/createSession").mock(
            return_value=httpx.Response(500, json={"error": {"code": "x", "message": "y"}})
        )
        respx.get(f"{WB}/worksheets").mock(return_value=httpx.Response(200, json=WORKSHEETS))
        patch_route = respx.patch(f"{WB}/worksheets('Sheet1')/range(address='A1')").mock(
            return_value=httpx.Response(200, json={})
        )

        ops = [{"op": "set_cell", "cell": "A1", "value": "x"}]
        async with AsyncGraphClient("t") as client:
            result = await workbook_edit.apply_workbook_edits(client, "/me/drive", ITEM_ID, ops)

        assert result["operations"] == ["set_cell"]
        # No session header when session-less
        assert patch_route.calls[0].request.headers.get("workbook-session-id") is None
