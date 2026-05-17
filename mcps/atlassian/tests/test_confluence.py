"""Tests for Confluence operations."""

import httpx
import pytest
import respx

from atlassian.atlassian_client import AsyncAtlassianClient
from .conftest import (
    CLOUD_ID,
    CONFLUENCE_V1_BASE,
    CONFLUENCE_V2_BASE,
    SAMPLE_SPACES_RESPONSE,
    SAMPLE_CONFLUENCE_SEARCH_RESPONSE,
    SAMPLE_PAGE,
    SAMPLE_CREATED_PAGE,
    SAMPLE_UPDATED_PAGE,
)

TOKEN = "test-token"


class TestListSpaces:
    @respx.mock
    async def test_list_spaces(self):
        respx.get(f"{CONFLUENCE_V2_BASE}/spaces").mock(
            return_value=httpx.Response(200, json=SAMPLE_SPACES_RESPONSE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            spaces = await confluence.alist_spaces(client)
        assert len(spaces) == 2
        assert spaces[0]["key"] == "DEV"

    @respx.mock
    async def test_list_spaces_empty(self):
        respx.get(f"{CONFLUENCE_V2_BASE}/spaces").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            spaces = await confluence.alist_spaces(client)
        assert spaces == []


class TestSearchContent:
    @respx.mock
    async def test_search_content(self):
        respx.get(f"{CONFLUENCE_V1_BASE}/content/search").mock(
            return_value=httpx.Response(200, json=SAMPLE_CONFLUENCE_SEARCH_RESPONSE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            results = await confluence.asearch_content(client, query='type = page')
        assert len(results) == 2
        assert results[0]["title"] == "Architecture Overview"

    @respx.mock
    async def test_search_empty(self):
        respx.get(f"{CONFLUENCE_V1_BASE}/content/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            results = await confluence.asearch_content(client, query="nonexistent")
        assert results == []


class TestGetPage:
    @respx.mock
    async def test_get_page(self):
        respx.get(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_PAGE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            page = await confluence.aget_page(client, "12345")
        assert page["title"] == "Architecture Overview"
        assert "microservices" in page["body"]["storage"]["value"]


class TestCreatePage:
    @respx.mock
    async def test_create_page(self):
        route = respx.post(f"{CONFLUENCE_V2_BASE}/pages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_PAGE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await confluence.acreate_page(
                client, space_id="65536", title="New Page", body="<p>Hello</p>"
            )
        assert result["id"] == "12400"
        assert result["title"] == "New Page"

    @respx.mock
    async def test_create_page_with_parent(self):
        route = respx.post(f"{CONFLUENCE_V2_BASE}/pages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CREATED_PAGE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await confluence.acreate_page(
                client,
                space_id="65536",
                title="Child Page",
                body="<p>Content</p>",
                parent_id="12345",
            )
        # Verify parent_id is in the request
        body = route.calls[0].request.content
        assert b"parentId" in body


class TestUpdatePage:
    @respx.mock
    async def test_update_page(self):
        respx.put(f"{CONFLUENCE_V2_BASE}/pages/12345").mock(
            return_value=httpx.Response(200, json=SAMPLE_UPDATED_PAGE)
        )
        from atlassian import confluence
        async with AsyncAtlassianClient(TOKEN, CLOUD_ID) as client:
            result = await confluence.aupdate_page(
                client,
                page_id="12345",
                title="Architecture Overview v2",
                body="<h1>Updated</h1>",
                version_number=4,
            )
        assert result["version"]["number"] == 4
