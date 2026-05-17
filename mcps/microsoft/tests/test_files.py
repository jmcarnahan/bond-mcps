"""Tests for file/drive operations (sync and async)."""

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph import files
from .conftest import (
    SAMPLE_DRIVE_CHILDREN_RESPONSE,
    SAMPLE_DRIVE_ITEM_BINARY,
    SAMPLE_DRIVE_ITEM_FILE,
    SAMPLE_DRIVE_ITEM_FOLDER,
    SAMPLE_DRIVE_ITEM_LARGE_TEXT,
    SAMPLE_DRIVE_ITEM_WORD,
    SAMPLE_UPLOADED_FILE,
    SAMPLE_COPY_IN_PROGRESS,
    SAMPLE_COPY_COMPLETED,
    SAMPLE_COPY_FAILED,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_SEARCH_RESPONSE_EMPTY,
    SAMPLE_SITE,
    SAMPLE_SITES_RESPONSE,
)


class TestFilesSync:
    """Synchronous file operation tests."""

    @respx.mock
    def test_list_drive_children_root(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        with GraphClient("tok") as client:
            items = files.list_drive_children(client)

        assert len(items) == 3
        assert items[0]["name"] == "Documents"
        assert items[1]["name"] == "report.csv"

    @respx.mock
    def test_list_drive_children_subfolder(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root:/Documents:/children").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with GraphClient("tok") as client:
            items = files.list_drive_children(client, folder_path="Documents")

        assert len(items) == 1
        assert items[0]["name"] == "report.csv"

    @respx.mock
    def test_list_drive_children_sharepoint(self):
        respx.get(f"{GRAPH_BASE_URL}/sites/site-id-001/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        with GraphClient("tok") as client:
            items = files.list_drive_children(client, site_id="site-id-001")

        assert len(items) == 3

    @respx.mock
    def test_get_drive_item(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with GraphClient("tok") as client:
            item = files.get_drive_item(client, "file-id-001")

        assert item["name"] == "report.csv"
        assert item["size"] == 1024

    @respx.mock
    def test_get_drive_item_content_text(self):
        csv_content = b"header1,header2\nvalue1,value2\n"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=csv_content)
        )
        with GraphClient("tok") as client:
            item, content = files.get_drive_item_content(client, "file-id-001")

        assert item["name"] == "report.csv"
        assert content == "header1,header2\nvalue1,value2\n"

    @respx.mock
    def test_get_drive_item_content_binary(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-002").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_BINARY)
        )
        with GraphClient("tok") as client:
            item, content = files.get_drive_item_content(client, "file-id-002")

        assert item["name"] == "presentation.pptx"
        assert content is None

    @respx.mock
    def test_get_drive_item_content_too_large(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-003").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_LARGE_TEXT)
        )
        with GraphClient("tok") as client:
            item, content = files.get_drive_item_content(client, "file-id-003")

        assert item["name"] == "huge-log.txt"
        assert content is None  # Too large, skipped

    @respx.mock
    def test_search_drive(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/search(q='report')").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with GraphClient("tok") as client:
            results = files.search_drive(client, "report")

        assert len(results) == 1
        assert results[0]["name"] == "report.csv"

    @respx.mock
    def test_search_files_unified(self):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        with GraphClient("tok") as client:
            results = files.search_files_unified(client, "budget")

        assert len(results) == 2
        assert results[0]["name"] == "Q4-budget.xlsx"
        assert results[0]["_searchSummary"] == "Q4 <c0>budget</c0> projections for 2025"
        assert results[1]["name"] == "budget-notes.md"

    @respx.mock
    def test_search_files_unified_empty(self):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE_EMPTY)
        )
        with GraphClient("tok") as client:
            results = files.search_files_unified(client, "nonexistent")

        assert results == []

    @respx.mock
    def test_list_sites(self):
        respx.get(f"{GRAPH_BASE_URL}/sites").mock(
            return_value=httpx.Response(200, json=SAMPLE_SITES_RESPONSE)
        )
        with GraphClient("tok") as client:
            sites = files.list_sites(client, query="engineering")

        assert len(sites) == 2
        assert sites[0]["displayName"] == "Engineering Hub"

    @respx.mock
    def test_search_files_unified_consumer_fallback(self):
        """Consumer accounts get 400 from /search/query — should fall back to per-drive search."""
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"code": "BadRequest", "message": "This API is not supported for MSA accounts"}},
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/search(q='report')").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with GraphClient("tok") as client:
            results = files.search_files_unified(client, "report")

        assert len(results) == 1
        assert results[0]["name"] == "report.csv"

    @respx.mock
    def test_list_sites_followed(self):
        respx.get(f"{GRAPH_BASE_URL}/me/followedSites").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_SITE]})
        )
        with GraphClient("tok") as client:
            sites = files.list_sites(client)

        assert len(sites) == 1
        assert sites[0]["displayName"] == "Engineering Hub"


class TestFilesAsync:
    """Async file operation tests."""

    @respx.mock
    async def test_alist_drive_children_root(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            items = await files.alist_drive_children(client)

        assert len(items) == 3

    @respx.mock
    async def test_alist_drive_children_subfolder(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root:/Projects:/children").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        async with AsyncGraphClient("tok") as client:
            items = await files.alist_drive_children(client, folder_path="Projects")

        assert len(items) == 1

    @respx.mock
    async def test_alist_drive_children_sharepoint(self):
        respx.get(f"{GRAPH_BASE_URL}/sites/site-id-001/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            items = await files.alist_drive_children(client, site_id="site-id-001")

        assert len(items) == 3

    @respx.mock
    async def test_aget_drive_item(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            item = await files.aget_drive_item(client, "file-id-001")

        assert item["name"] == "report.csv"

    @respx.mock
    async def test_aget_drive_item_content_text(self):
        csv_content = b"col1,col2\na,b\n"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=csv_content)
        )
        async with AsyncGraphClient("tok") as client:
            item, content = await files.aget_drive_item_content(client, "file-id-001")

        assert content == "col1,col2\na,b\n"

    @respx.mock
    async def test_aget_drive_item_content_binary(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-002").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_BINARY)
        )
        async with AsyncGraphClient("tok") as client:
            item, content = await files.aget_drive_item_content(client, "file-id-002")

        assert content is None

    @respx.mock
    async def test_aget_drive_item_content_too_large(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-003").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_LARGE_TEXT)
        )
        async with AsyncGraphClient("tok") as client:
            item, content = await files.aget_drive_item_content(client, "file-id-003")

        assert content is None

    @respx.mock
    async def test_asearch_drive(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/search(q='report')").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        async with AsyncGraphClient("tok") as client:
            results = await files.asearch_drive(client, "report")

        assert len(results) == 1

    @respx.mock
    async def test_asearch_files_unified(self):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            results = await files.asearch_files_unified(client, "budget")

        assert len(results) == 2
        assert "_searchSummary" in results[0]

    @respx.mock
    async def test_asearch_files_unified_empty(self):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE_EMPTY)
        )
        async with AsyncGraphClient("tok") as client:
            results = await files.asearch_files_unified(client, "nonexistent")

        assert results == []

    @respx.mock
    async def test_alist_sites(self):
        respx.get(f"{GRAPH_BASE_URL}/sites").mock(
            return_value=httpx.Response(200, json=SAMPLE_SITES_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            sites = await files.alist_sites(client, query="engineering")

        assert len(sites) == 2

    @respx.mock
    async def test_asearch_files_unified_consumer_fallback(self):
        """Consumer accounts get 400 from /search/query — should fall back to per-drive search."""
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"code": "BadRequest", "message": "This API is not supported for MSA accounts"}},
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/search(q='report')").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        async with AsyncGraphClient("tok") as client:
            results = await files.asearch_files_unified(client, "report")

        assert len(results) == 1
        assert results[0]["name"] == "report.csv"

    @respx.mock
    async def test_alist_sites_followed(self):
        respx.get(f"{GRAPH_BASE_URL}/me/followedSites").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_SITE]})
        )
        async with AsyncGraphClient("tok") as client:
            sites = await files.alist_sites(client)

        assert len(sites) == 1


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------

class TestUploadSync:
    """Synchronous upload_file tests."""

    @respx.mock
    def test_upload_to_root(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/report.md:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with GraphClient("tok") as client:
            item = files.upload_file(client, folder_path="", filename="report.md", content="# Hello")

        assert item["name"] == "report.md"
        assert route.calls[0].request.headers["Content-Type"] == "text/markdown"
        assert route.calls[0].request.content == b"# Hello"

    @respx.mock
    def test_upload_to_subfolder(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Documents/data.csv:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with GraphClient("tok") as client:
            files.upload_file(client, folder_path="Documents", filename="data.csv", content="a,b\n1,2")

        assert route.called
        assert route.calls[0].request.headers["Content-Type"] == "text/csv"

    @respx.mock
    def test_upload_to_sharepoint(self):
        site_id = "site-id-001"
        route = respx.put(
            f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root:/Shared Documents/notes.txt:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        with GraphClient("tok") as client:
            files.upload_file(
                client,
                folder_path="Shared Documents",
                filename="notes.txt",
                content="hello",
                site_id=site_id,
            )
        assert route.called
        assert route.calls[0].request.headers["Content-Type"] == "text/plain"

    @respx.mock
    def test_upload_overwrites_existing(self):
        """200 response means file was updated (overwritten)."""
        respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/existing.json:/content").mock(
            return_value=httpx.Response(200, json=SAMPLE_UPLOADED_FILE)
        )
        with GraphClient("tok") as client:
            item = files.upload_file(client, folder_path="", filename="existing.json", content="{}")
        assert item is not None

    @respx.mock
    def test_upload_unknown_extension_uses_octet_stream(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/file.bin:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with GraphClient("tok") as client:
            files.upload_file(client, folder_path="", filename="file.bin", content="data")
        assert route.calls[0].request.headers["Content-Type"] == "application/octet-stream"

    @pytest.mark.parametrize("ext,expected_ct", [
        ("report.txt",  "text/plain"),
        ("page.html",   "text/html"),
        ("data.json",   "application/json"),
        ("config.yaml", "application/yaml"),
        ("config.yml",  "application/yaml"),
        ("schema.xml",  "application/xml"),
    ])
    @respx.mock
    def test_upload_content_type_inference(self, ext, expected_ct):
        route = respx.put(url__regex=r"/content$").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with GraphClient("tok") as client:
            files.upload_file(client, folder_path="", filename=ext, content="x")
        assert route.called
        ct = route.calls[0].request.headers["Content-Type"]
        assert ct == expected_ct

    def test_upload_rejects_content_over_4mb(self):
        """Content exceeding 4 MB raises ValueError before making an API call."""
        big_content = "x" * 4_000_001
        with pytest.raises(ValueError, match="4 MB"):
            with GraphClient("tok") as client:
                files.upload_file(client, folder_path="", filename="big.txt", content=big_content)

    async def test_aupload_rejects_content_over_4mb(self):
        """Async upload also rejects content over 4 MB."""
        big_content = "x" * 4_000_001
        with pytest.raises(ValueError, match="4 MB"):
            async with AsyncGraphClient("tok") as client:
                await files.aupload_file(client, folder_path="", filename="big.txt", content=big_content)


class TestUploadAsync:
    """Asynchronous aupload_file tests."""

    @respx.mock
    async def test_aupload_to_root(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/notes.md:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            item = await files.aupload_file(client, folder_path="", filename="notes.md", content="hello")
        assert item["id"] == SAMPLE_UPLOADED_FILE["id"]
        assert route.calls[0].request.headers["Content-Type"] == "text/markdown"

    @respx.mock
    async def test_aupload_to_subfolder(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Reports/summary.csv:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            await files.aupload_file(client, folder_path="Reports", filename="summary.csv", content="x,y")
        assert route.called
        assert route.calls[0].request.headers["Content-Type"] == "text/csv"

    @respx.mock
    async def test_aupload_to_sharepoint(self):
        site_id = "site-id-001"
        route = respx.put(
            f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root:/Docs/page.html:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        async with AsyncGraphClient("tok") as client:
            await files.aupload_file(
                client, folder_path="Docs", filename="page.html", content="<p>hi</p>", site_id=site_id
            )
        assert route.called

    @respx.mock
    async def test_aupload_bytes_pdf(self):
        """aupload_bytes uploads raw binary with the specified content-type."""
        pdf_bytes = b"%PDF-1.4 fake content"
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Power BI Exports/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            item = await files.aupload_bytes(
                client,
                folder_path="Power BI Exports",
                filename="report.pdf",
                data=pdf_bytes,
                content_type="application/pdf",
            )

        assert item["id"] == SAMPLE_UPLOADED_FILE["id"]
        assert route.calls[0].request.headers["Content-Type"] == "application/pdf"
        assert route.calls[0].request.content == pdf_bytes

    async def test_aupload_bytes_rejects_over_4mb(self):
        with pytest.raises(ValueError, match="4 MB"):
            async with AsyncGraphClient("tok") as client:
                await files.aupload_bytes(
                    client,
                    folder_path="",
                    filename="big.pdf",
                    data=b"x" * 4_000_001,
                    content_type="application/pdf",
                )


# ---------------------------------------------------------------------------
# Copy tests
# ---------------------------------------------------------------------------

MONITOR_URL = "https://api.onedrive.com/v1.0/monitor/copy-op-token"


class TestCopySync:
    """Synchronous copy_drive_item tests."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_sleep):
        pass

    @respx.mock
    def test_copy_succeeds_after_one_poll_200(self):
        """200 monitor response with completed status."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        with GraphClient("tok") as client:
            result = files.copy_drive_item(client, item_id=item_id, new_name="template-copy.docx")

        assert result["status"] == "completed"
        assert result["resourceId"] == SAMPLE_COPY_COMPLETED["resourceId"]

    @respx.mock
    def test_copy_succeeds_via_303(self):
        """303 monitor response (real SharePoint behavior) — completed status in body."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(303, json=SAMPLE_COPY_COMPLETED)
        )
        with GraphClient("tok") as client:
            result = files.copy_drive_item(client, item_id=item_id, new_name="template-copy.docx")

        assert result["status"] == "completed"
        assert result["resourceId"] == SAMPLE_COPY_COMPLETED["resourceId"]

    @respx.mock
    def test_copy_polls_until_completed(self):
        """In-progress response is polled past before completed fires."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        # First poll → inProgress (200), second poll → completed (303)
        respx.get(MONITOR_URL).mock(
            side_effect=[
                httpx.Response(200, json=SAMPLE_COPY_IN_PROGRESS),
                httpx.Response(303, json=SAMPLE_COPY_COMPLETED),
            ]
        )
        with GraphClient("tok") as client:
            result = files.copy_drive_item(client, item_id=item_id, new_name="copy.docx")

        assert result["status"] == "completed"
        assert respx.calls.call_count == 4  # GET item + POST copy + 2x GET monitor

    @respx.mock
    def test_copy_raises_on_failure(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_FAILED)
        )
        with pytest.raises(GraphError, match="accessDenied"):
            with GraphClient("tok") as client:
                files.copy_drive_item(client, item_id=item_id, new_name="copy.docx")

    @respx.mock
    def test_copy_with_explicit_destination_folder(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        dest_folder_id = "folder-id-archive"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        copy_route = respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        with GraphClient("tok") as client:
            files.copy_drive_item(
                client, item_id=item_id, new_name="archived.docx",
                destination_folder_id=dest_folder_id,
            )

        copy_body = json.loads(copy_route.calls[0].request.content)
        assert copy_body["parentReference"]["id"] == dest_folder_id

    @respx.mock
    def test_copy_on_sharepoint(self):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        with GraphClient("tok") as client:
            result = files.copy_drive_item(
                client, item_id=item_id, new_name="sp-copy.docx", site_id=site_id
            )
        assert result["status"] == "completed"

    @respx.mock
    def test_copy_passes_drive_id_from_source(self):
        """driveId from source parentReference is included in the copy request."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        source_drive_id = SAMPLE_DRIVE_ITEM_WORD["parentReference"]["driveId"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        copy_route = respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        with GraphClient("tok") as client:
            files.copy_drive_item(client, item_id=item_id, new_name="copy.docx")

        copy_body = json.loads(copy_route.calls[0].request.content)
        assert copy_body["parentReference"]["driveId"] == source_drive_id
        assert copy_body["name"] == "copy.docx"

    @respx.mock
    def test_copy_raises_on_missing_location_header(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202)  # No Location header
        )
        with pytest.raises(GraphError, match="NoLocation"):
            with GraphClient("tok") as client:
                files.copy_drive_item(client, item_id=item_id, new_name="copy.docx")


    @respx.mock
    def test_copy_times_out(self):
        """Raises CopyTimeout when the operation never completes within the deadline."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_IN_PROGRESS)
        )
        import ms_graph.files as files_mod
        with patch.object(files_mod, "_COPY_POLL_TIMEOUT", 0):
            with pytest.raises(GraphError, match="CopyTimeout"):
                with GraphClient("tok") as client:
                    files.copy_drive_item(client, item_id=item_id, new_name="copy.docx")


class TestCopyAsync:
    """Asynchronous acopy_drive_item tests."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_sleep):
        pass

    @respx.mock
    async def test_acopy_succeeds_200(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        async with AsyncGraphClient("tok") as client:
            result = await files.acopy_drive_item(client, item_id=item_id, new_name="async-copy.docx")

        assert result["status"] == "completed"
        assert result["resourceId"] == SAMPLE_COPY_COMPLETED["resourceId"]

    @respx.mock
    async def test_acopy_succeeds_303(self):
        """303 monitor response (real SharePoint behavior) — completed status in body."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(303, json=SAMPLE_COPY_COMPLETED)
        )
        async with AsyncGraphClient("tok") as client:
            result = await files.acopy_drive_item(client, item_id=item_id, new_name="async-copy.docx")

        assert result["status"] == "completed"
        assert result["resourceId"] == SAMPLE_COPY_COMPLETED["resourceId"]

    @respx.mock
    async def test_acopy_raises_on_failure(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_FAILED)
        )
        with pytest.raises(GraphError, match="accessDenied"):
            async with AsyncGraphClient("tok") as client:
                await files.acopy_drive_item(client, item_id=item_id, new_name="copy.docx")

    @respx.mock
    async def test_acopy_with_explicit_destination(self):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        dest = "folder-id-target"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        copy_route = respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED)
        )
        async with AsyncGraphClient("tok") as client:
            await files.acopy_drive_item(
                client, item_id=item_id, new_name="copy.docx", destination_folder_id=dest
            )

        copy_body = json.loads(copy_route.calls[0].request.content)
        assert copy_body["parentReference"]["id"] == dest

    @respx.mock
    async def test_acopy_times_out(self):
        """Async copy raises CopyTimeout when operation never completes."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_COPY_IN_PROGRESS)
        )
        import ms_graph.files as files_mod
        with patch.object(files_mod, "_COPY_POLL_TIMEOUT", 0):
            with pytest.raises(GraphError, match="CopyTimeout"):
                async with AsyncGraphClient("tok") as client:
                    await files.acopy_drive_item(client, item_id=item_id, new_name="copy.docx")


# ---------------------------------------------------------------------------
# Rename tests
# ---------------------------------------------------------------------------

SAMPLE_RENAMED_FILE = {**SAMPLE_DRIVE_ITEM_FILE, "name": "renamed-report.csv"}
SAMPLE_RENAMED_FOLDER = {**SAMPLE_DRIVE_ITEM_FOLDER, "name": "Archive-2025"}


class TestRenameSync:
    """Synchronous rename_drive_item tests."""

    @respx.mock
    def test_rename_file(self):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        route = respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_RENAMED_FILE)
        )
        with GraphClient("tok") as client:
            item = files.rename_drive_item(client, item_id=item_id, new_name="renamed-report.csv")

        assert item["name"] == "renamed-report.csv"
        body = json.loads(route.calls[0].request.content)
        assert body == {"name": "renamed-report.csv"}

    @respx.mock
    def test_rename_folder(self):
        item_id = SAMPLE_DRIVE_ITEM_FOLDER["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_RENAMED_FOLDER)
        )
        with GraphClient("tok") as client:
            item = files.rename_drive_item(client, item_id=item_id, new_name="Archive-2025")

        assert item["name"] == "Archive-2025"

    @respx.mock
    def test_rename_on_sharepoint(self):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        route = respx.patch(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_RENAMED_FILE)
        )
        with GraphClient("tok") as client:
            item = files.rename_drive_item(
                client, item_id=item_id, new_name="renamed-report.csv", site_id=site_id
            )

        assert item["name"] == "renamed-report.csv"
        assert route.called

    @respx.mock
    def test_rename_propagates_graph_error(self):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "ResourceNotFound", "message": "Item not found."}
            })
        )
        with pytest.raises(GraphError, match="ResourceNotFound"):
            with GraphClient("tok") as client:
                files.rename_drive_item(client, item_id=item_id, new_name="new.csv")


class TestRenameAsync:
    """Asynchronous arename_drive_item tests."""

    @respx.mock
    async def test_arename_file(self):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        route = respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_RENAMED_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            item = await files.arename_drive_item(client, item_id=item_id, new_name="renamed-report.csv")

        assert item["name"] == "renamed-report.csv"
        body = json.loads(route.calls[0].request.content)
        assert body == {"name": "renamed-report.csv"}

    @respx.mock
    async def test_arename_on_sharepoint(self):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        route = respx.patch(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_DRIVE_ITEM_WORD, "name": "final-doc.docx"})
        )
        async with AsyncGraphClient("tok") as client:
            item = await files.arename_drive_item(
                client, item_id=item_id, new_name="final-doc.docx", site_id=site_id
            )

        assert item["name"] == "final-doc.docx"
        assert route.called

    @respx.mock
    async def test_arename_propagates_graph_error(self):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(403, json={
                "error": {"code": "AccessDenied", "message": "Cannot rename."}
            })
        )
        with pytest.raises(GraphError, match="AccessDenied"):
            async with AsyncGraphClient("tok") as client:
                await files.arename_drive_item(client, item_id=item_id, new_name="x.csv")
