"""Tests for Power BI operations (sync and async)."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from ms_graph.graph_client import GraphError
from ms_graph.power_bi import (
    POWERBI_BASE_URL,
    PowerBIClient,
    AsyncPowerBIClient,
    _format_dax_results,
    _workspace_base,
)
from ms_graph import power_bi as pbi
from .conftest import (
    SAMPLE_PBI_WORKSPACE,
    SAMPLE_PBI_WORKSPACE_2,
    SAMPLE_PBI_WORKSPACES_RESPONSE,
    SAMPLE_PBI_DATASET,
    SAMPLE_PBI_DATASETS_RESPONSE,
    SAMPLE_PBI_REPORT,
    SAMPLE_PBI_REPORTS_RESPONSE,
    SAMPLE_PBI_DASHBOARD,
    SAMPLE_PBI_DASHBOARDS_RESPONSE,
    SAMPLE_PBI_DAX_RESULT,
    SAMPLE_PBI_DAX_EMPTY,
    SAMPLE_PBI_REFRESH_HISTORY,
    SAMPLE_PBI_EXPORT_IN_PROGRESS,
    SAMPLE_PBI_EXPORT_SUCCEEDED,
    SAMPLE_PBI_EXPORT_FAILED,
)

WORKSPACE_ID = "ws-id-001"
DATASET_ID = "ds-id-001"
REPORT_ID = "rpt-id-001"
EXPORT_ID = "export-id-001"


@pytest.fixture(autouse=False)
def no_pbi_sleep(monkeypatch):
    """Patch time.sleep and asyncio.sleep in ms_graph.power_bi for polling tests."""
    monkeypatch.setattr("ms_graph.power_bi.time.sleep", lambda _: None)
    monkeypatch.setattr("ms_graph.power_bi.asyncio.sleep", AsyncMock(return_value=None))


# ---------------------------------------------------------------------------
# _format_dax_results helper
# ---------------------------------------------------------------------------

class TestWorkspaceBase:
    def test_empty_string_is_my_workspace(self):
        assert _workspace_base("") == ""

    def test_me_is_my_workspace(self):
        assert _workspace_base("me") == ""

    def test_ME_case_insensitive(self):
        assert _workspace_base("ME") == ""

    def test_named_workspace_returns_groups_prefix(self):
        assert _workspace_base("ws-id-001") == "/groups/ws-id-001"

    def test_uuid_workspace(self):
        assert _workspace_base("1f7cdf01-efac-4705-9343-1037c7dd4d05") == "/groups/1f7cdf01-efac-4705-9343-1037c7dd4d05"


class TestFormatDaxResults:
    def test_formats_rows_as_csv(self):
        result = _format_dax_results(SAMPLE_PBI_DAX_RESULT)
        assert "[Region]" in result
        assert "West" in result
        assert "1234567.89" in result

    def test_empty_rows(self):
        result = _format_dax_results(SAMPLE_PBI_DAX_EMPTY)
        assert result == "(no rows returned)"

    def test_missing_results_key(self):
        result = _format_dax_results({})
        assert result == "(no rows returned)"

    def test_csv_has_header_row(self):
        result = _format_dax_results(SAMPLE_PBI_DAX_RESULT)
        lines = result.strip().splitlines()
        assert len(lines) == 4  # header + 3 data rows
        assert "[Region]" in lines[0]
        assert "[Sales Amount]" in lines[0]

    def test_multiple_rows_all_present(self):
        result = _format_dax_results(SAMPLE_PBI_DAX_RESULT)
        assert "West" in result
        assert "East" in result
        assert "Central" in result

    def test_sparse_rows_inconsistent_keys(self):
        """Power BI omits null columns from rows — DictWriter must handle missing keys."""
        sparse = {
            "results": [{"tables": [{"rows": [
                {"[Region]": "West", "[Sales]": 1000},       # [Units] absent (null)
                {"[Region]": "East", "[Sales]": 500, "[Units]": 200},
            ]}]}]
        }
        result = _format_dax_results(sparse)
        lines = result.strip().splitlines()
        assert "[Region]" in lines[0]
        assert "[Sales]" in lines[0]
        assert "[Units]" in lines[0]   # column present even though first row lacks it
        assert "West" in result
        assert "200" in result

    def test_null_values_render_as_empty(self):
        """None values in rows render as empty CSV cells."""
        data = {
            "results": [{"tables": [{"rows": [
                {"[A]": "x", "[B]": None},
                {"[A]": None, "[B]": "y"},
            ]}]}]
        }
        result = _format_dax_results(data)
        lines = result.strip().splitlines()
        assert lines[1] == "x,"    # B is None → empty
        assert lines[2] == ",y"    # A is None → empty


# ---------------------------------------------------------------------------
# Synchronous tests
# ---------------------------------------------------------------------------

class TestPowerBISync:
    """Synchronous Power BI operation tests."""

    @respx.mock
    def test_list_workspaces(self):
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_WORKSPACES_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            workspaces = pbi.list_workspaces(client)

        assert len(workspaces) == 2
        assert workspaces[0]["name"] == "Analytics Hub"
        assert workspaces[1]["name"] == "Finance Reports"

    @respx.mock
    def test_list_datasets_my_workspace(self):
        """workspace_id='' uses root /datasets endpoint (My workspace)."""
        route = respx.get(f"{POWERBI_BASE_URL}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            datasets = pbi.list_datasets(client, "")

        assert len(datasets) == 2
        assert route.called
        assert "/groups/" not in str(route.calls[0].request.url)

    @respx.mock
    def test_list_reports_my_workspace(self):
        """workspace_id='me' also uses root endpoint."""
        route = respx.get(f"{POWERBI_BASE_URL}/reports").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_REPORTS_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            reports = pbi.list_reports(client, "me")

        assert len(reports) == 2
        assert route.called

    @respx.mock
    def test_execute_dax_my_workspace(self):
        """DAX query against My workspace dataset uses root /datasets endpoint."""
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{DATASET_ID}/executeQueries").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT)
        )
        with PowerBIClient("tok") as client:
            result = pbi.execute_dax_query(client, "", DATASET_ID, "EVALUATE 'Sales'")

        assert route.called
        assert "/groups/" not in str(route.calls[0].request.url)

    @respx.mock
    def test_trigger_refresh_my_workspace(self):
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{DATASET_ID}/refreshes").mock(
            return_value=httpx.Response(202)
        )
        with PowerBIClient("tok") as client:
            pbi.trigger_refresh(client, "me", DATASET_ID)

        assert route.called
        assert "/groups/" not in str(route.calls[0].request.url)

    @respx.mock
    def test_list_workspaces_empty(self):
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with PowerBIClient("tok") as client:
            workspaces = pbi.list_workspaces(client)
        assert workspaces == []

    @respx.mock
    def test_list_datasets(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            datasets = pbi.list_datasets(client, WORKSPACE_ID)

        assert len(datasets) == 2
        assert datasets[0]["name"] == "Sales"

    @respx.mock
    def test_list_reports(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_REPORTS_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            reports = pbi.list_reports(client, WORKSPACE_ID)

        assert len(reports) == 2
        assert reports[0]["name"] == "Q4 Dashboard"

    @respx.mock
    def test_list_dashboards(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/dashboards").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DASHBOARDS_RESPONSE)
        )
        with PowerBIClient("tok") as client:
            dashboards = pbi.list_dashboards(client, WORKSPACE_ID)

        assert len(dashboards) == 1
        assert dashboards[0]["displayName"] == "Executive Overview"

    @respx.mock
    def test_execute_dax_query(self):
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/executeQueries"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT))
        with PowerBIClient("tok") as client:
            result = pbi.execute_dax_query(
                client, WORKSPACE_ID, DATASET_ID,
                "EVALUATE TOPN(3, 'Sales', 'Sales'[Amount], DESC)"
            )

        assert route.called
        assert result["results"][0]["tables"][0]["rows"][0]["[Region]"] == "West"
        # Verify the query was sent in the payload
        body = json.loads(route.calls[0].request.content)
        assert "TOPN" in body["queries"][0]["query"]

    @respx.mock
    def test_execute_dax_query_400_syntax_error(self):
        respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/executeQueries"
        ).mock(return_value=httpx.Response(400, json={
            "error": {"code": "BadRequest", "message": "DAX syntax error near 'EVALUATEE'"}
        }))
        with pytest.raises(GraphError, match="BadRequest"):
            with PowerBIClient("tok") as client:
                pbi.execute_dax_query(client, WORKSPACE_ID, DATASET_ID, "EVALUATEE")

    @respx.mock
    def test_trigger_refresh(self):
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        ).mock(return_value=httpx.Response(202))
        with PowerBIClient("tok") as client:
            pbi.trigger_refresh(client, WORKSPACE_ID, DATASET_ID)
        assert route.called

    @respx.mock
    def test_get_refresh_history(self):
        route = respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_REFRESH_HISTORY))
        with PowerBIClient("tok") as client:
            history = pbi.get_refresh_history(client, WORKSPACE_ID, DATASET_ID)

        assert len(history) == 2
        assert history[0]["status"] == "Completed"
        assert "top=5" in str(route.calls[0].request.url)

    @respx.mock
    def test_start_export(self):
        export_location = f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/ExportTo"
        ).mock(return_value=httpx.Response(202, headers={"Location": export_location}))
        with PowerBIClient("tok") as client:
            export_id = pbi.start_export(client, WORKSPACE_ID, REPORT_ID, "PDF")

        assert export_id == EXPORT_ID
        body = json.loads(route.calls[0].request.content)
        assert body["format"] == "PDF"

    @respx.mock
    def test_start_export_with_pages(self):
        export_location = f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/ExportTo"
        ).mock(return_value=httpx.Response(202, headers={"Location": export_location}))
        with PowerBIClient("tok") as client:
            pbi.start_export(client, WORKSPACE_ID, REPORT_ID, "PDF", pages=["ReportSection1"])

        body = json.loads(route.calls[0].request.content)
        assert body["powerBIReportConfiguration"]["pages"][0]["pageName"] == "ReportSection1"

    @respx.mock
    def test_get_export_status(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_IN_PROGRESS))
        with PowerBIClient("tok") as client:
            status = pbi.get_export_status(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert status["status"] == "Running"
        assert status["percentComplete"] == 40

    @respx.mock
    def test_download_export(self):
        pdf_bytes = b"%PDF-1.4 fake pdf content"
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}/file"
        ).mock(return_value=httpx.Response(200, content=pdf_bytes))
        with PowerBIClient("tok") as client:
            data = pbi.download_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert data == pdf_bytes

    @respx.mock
    def test_error_propagates_as_graph_error(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "PowerBIEntityNotFound", "message": "Workspace not found."}
            })
        )
        with pytest.raises(GraphError) as exc_info:
            with PowerBIClient("tok") as client:
                pbi.list_reports(client, WORKSPACE_ID)

        assert exc_info.value.status_code == 404
        assert exc_info.value.error_code == "PowerBIEntityNotFound"

    @respx.mock
    def test_403_premium_capacity_error(self):
        respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/executeQueries"
        ).mock(return_value=httpx.Response(403, json={
            "error": {"code": "Forbidden", "message": "DAX query requires Premium or PPU capacity."}
        }))
        with pytest.raises(GraphError) as exc_info:
            with PowerBIClient("tok") as client:
                pbi.execute_dax_query(client, WORKSPACE_ID, DATASET_ID, "EVALUATE 'Sales'")

        assert exc_info.value.status_code == 403


class TestPollExportSync:
    """Tests for the synchronous poll_export polling loop."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_pbi_sleep):
        pass

    @respx.mock
    def test_wait_succeeds_after_one_poll(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED))
        with PowerBIClient("tok") as client:
            status = pbi.poll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert status["status"] == "Succeeded"
        assert status["percentComplete"] == 100

    @respx.mock
    def test_wait_polls_through_running_to_succeeded(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(side_effect=[
            httpx.Response(200, json=SAMPLE_PBI_EXPORT_IN_PROGRESS),
            httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED),
        ])
        with PowerBIClient("tok") as client:
            status = pbi.poll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert status["status"] == "Succeeded"

    @respx.mock
    def test_wait_raises_on_failed(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_FAILED))
        with pytest.raises(GraphError, match="ExportFailed|PowerBIEntityNotFound"):
            with PowerBIClient("tok") as client:
                pbi.poll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

    @respx.mock
    def test_wait_raises_on_timeout(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_IN_PROGRESS))
        import ms_graph.power_bi as pbi_mod
        with patch.object(pbi_mod, "_EXPORT_POLL_TIMEOUT", 0):
            with pytest.raises(GraphError, match="ExportTimeout"):
                with PowerBIClient("tok") as client:
                    pbi.poll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)


# ---------------------------------------------------------------------------
# Asynchronous tests
# ---------------------------------------------------------------------------

class TestPowerBIAsync:
    """Asynchronous Power BI operation tests."""

    @respx.mock
    async def test_alist_workspaces(self):
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_WORKSPACES_RESPONSE)
        )
        async with AsyncPowerBIClient("tok") as client:
            workspaces = await pbi.alist_workspaces(client)

        assert len(workspaces) == 2
        assert workspaces[0]["id"] == SAMPLE_PBI_WORKSPACE["id"]

    @respx.mock
    async def test_alist_datasets(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        async with AsyncPowerBIClient("tok") as client:
            datasets = await pbi.alist_datasets(client, WORKSPACE_ID)

        assert len(datasets) == 2

    @respx.mock
    async def test_alist_reports(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_REPORTS_RESPONSE)
        )
        async with AsyncPowerBIClient("tok") as client:
            reports = await pbi.alist_reports(client, WORKSPACE_ID)

        assert len(reports) == 2

    @respx.mock
    async def test_alist_dashboards(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/dashboards").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DASHBOARDS_RESPONSE)
        )
        async with AsyncPowerBIClient("tok") as client:
            dashboards = await pbi.alist_dashboards(client, WORKSPACE_ID)

        assert len(dashboards) == 1

    @respx.mock
    async def test_aexecute_dax_query(self):
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/executeQueries"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT))
        async with AsyncPowerBIClient("tok") as client:
            result = await pbi.aexecute_dax_query(
                client, WORKSPACE_ID, DATASET_ID, "EVALUATE 'Sales'"
            )

        assert route.called
        assert len(result["results"][0]["tables"][0]["rows"]) == 3

    @respx.mock
    async def test_atrigger_refresh(self):
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        ).mock(return_value=httpx.Response(202))
        async with AsyncPowerBIClient("tok") as client:
            await pbi.atrigger_refresh(client, WORKSPACE_ID, DATASET_ID)
        assert route.called

    @respx.mock
    async def test_aget_refresh_history(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_REFRESH_HISTORY))
        async with AsyncPowerBIClient("tok") as client:
            history = await pbi.aget_refresh_history(client, WORKSPACE_ID, DATASET_ID)

        assert len(history) == 2

    @respx.mock
    async def test_astart_export(self):
        export_location = f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        respx.post(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/ExportTo"
        ).mock(return_value=httpx.Response(202, headers={"Location": export_location}))
        async with AsyncPowerBIClient("tok") as client:
            export_id = await pbi.astart_export(client, WORKSPACE_ID, REPORT_ID, "PPTX")

        assert export_id == EXPORT_ID

    @respx.mock
    async def test_adownload_export(self):
        pdf_bytes = b"%PDF-1.4 fake content"
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}/file"
        ).mock(return_value=httpx.Response(200, content=pdf_bytes))
        async with AsyncPowerBIClient("tok") as client:
            data = await pbi.adownload_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert data == pdf_bytes

    @respx.mock
    async def test_async_error_propagates(self):
        respx.get(f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/datasets").mock(
            return_value=httpx.Response(401, json={
                "error": {"code": "Unauthorized", "message": "Token expired."}
            })
        )
        with pytest.raises(GraphError) as exc_info:
            async with AsyncPowerBIClient("tok") as client:
                await pbi.alist_datasets(client, WORKSPACE_ID)

        assert exc_info.value.status_code == 401


class TestApollExportAsync:
    """Tests for the async apoll_export polling loop."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_pbi_sleep):
        pass

    @respx.mock
    async def test_await_succeeds(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED))
        async with AsyncPowerBIClient("tok") as client:
            status = await pbi.apoll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert status["status"] == "Succeeded"

    @respx.mock
    async def test_await_polls_through_running(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(side_effect=[
            httpx.Response(200, json=SAMPLE_PBI_EXPORT_IN_PROGRESS),
            httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED),
        ])
        async with AsyncPowerBIClient("tok") as client:
            status = await pbi.apoll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

        assert status["status"] == "Succeeded"

    @respx.mock
    async def test_await_raises_on_failed(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_FAILED))
        with pytest.raises(GraphError, match="ExportFailed|PowerBIEntityNotFound"):
            async with AsyncPowerBIClient("tok") as client:
                await pbi.apoll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)

    @respx.mock
    async def test_await_raises_on_timeout(self):
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}/exports/{EXPORT_ID}"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_IN_PROGRESS))
        import ms_graph.power_bi as pbi_mod
        with patch.object(pbi_mod, "_EXPORT_POLL_TIMEOUT", 0):
            with pytest.raises(GraphError, match="ExportTimeout"):
                async with AsyncPowerBIClient("tok") as client:
                    await pbi.apoll_export(client, WORKSPACE_ID, REPORT_ID, EXPORT_ID)
