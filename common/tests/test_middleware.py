"""FormatNegotiation and HideDeprecatedAliases against a real FastMCP server."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from bond_common import (
    DEPRECATED_ALIAS_TAG,
    DESKTOP_HEADER,
    DESKTOP_HEADER_VALUE,
    FormatNegotiation,
    HideDeprecatedAliases,
)
from fastmcp import Client, FastMCP


@contextmanager
def _desktop_headers():
    """Stand in for an HTTP request carrying the desktop header.

    In-process clients have no HTTP context, so fastmcp's real
    get_http_headers() returns {} and every call takes the compact path.
    """
    with patch(
        "bond_common.middleware.get_http_headers",
        return_value={DESKTOP_HEADER: DESKTOP_HEADER_VALUE},
    ):
        yield


@pytest.fixture
def server():
    mcp = FastMCP("format-negotiation-test")
    mcp.add_middleware(FormatNegotiation())
    mcp.add_middleware(HideDeprecatedAliases())

    @mcp.tool(output_schema=None)
    def list_things() -> dict:
        return {"things": [{"id": "1", "name": "one"}], "count": 1}

    @mcp.tool(output_schema=None)
    def failing_thing() -> dict:
        return {"error": "not_connected", "connect_url": "https://x/connect"}

    @mcp.tool()
    def markdown_thing() -> str:
        return "| a | b |"

    @mcp.tool()
    def schema_thing() -> dict:
        return {"things": [{"id": "1", "name": "one"}], "count": 1}

    @mcp.tool(output_schema=None, tags={DEPRECATED_ALIAS_TAG})
    def old_list_things() -> dict:
        return {"things": [{"id": "1", "name": "one"}], "count": 1}

    return mcp


def _text(result) -> str:
    return "".join(block.text for block in result.content)


class TestCompactPath:
    async def test_dict_tool_renders_compactly_without_structured_content(self, server):
        async with Client(server) as client:
            result = await client.call_tool("list_things", {})

        assert result.structured_content is None
        assert _text(result) == "id|name\n1|one\ncount: 1"

    async def test_error_dict_renders_as_an_error_line(self, server):
        async with Client(server) as client:
            result = await client.call_tool("failing_thing", {})

        assert result.structured_content is None
        assert _text(result) == "error: not_connected\nconnect_url: https://x/connect"

    async def test_str_tool_passes_through_untouched(self, server):
        """fastmcp auto-wraps a str return as {"result": ...} behind a
        generated schema; only the declared schema separates the surfaces."""
        async with Client(server) as client:
            result = await client.call_tool("markdown_thing", {})

        assert result.structured_content == {"result": "| a | b |"}
        assert _text(result) == "| a | b |"

    async def test_dict_tool_that_advertises_a_schema_passes_through(self, server):
        async with Client(server) as client:
            result = await client.call_tool("schema_thing", {})

        assert result.structured_content == {"things": [{"id": "1", "name": "one"}], "count": 1}
        assert json.loads(_text(result)) == result.structured_content


class TestDesktopPath:
    async def test_dict_tool_keeps_structured_content_and_json_text(self, server):
        with _desktop_headers():
            async with Client(server) as client:
                result = await client.call_tool("list_things", {})

        assert result.structured_content == {"things": [{"id": "1", "name": "one"}], "count": 1}
        assert json.loads(_text(result)) == result.structured_content

    async def test_str_tool_is_identical_under_both_paths(self, server):
        with _desktop_headers():
            async with Client(server) as client:
                desktop = await client.call_tool("markdown_thing", {})
        async with Client(server) as client:
            compact = await client.call_tool("markdown_thing", {})

        assert _text(desktop) == _text(compact) == "| a | b |"

    async def test_a_non_desktop_header_value_still_gets_the_compact_path(self, server):
        with patch(
            "bond_common.middleware.get_http_headers",
            return_value={DESKTOP_HEADER: "some-other-client"},
        ):
            async with Client(server) as client:
                result = await client.call_tool("list_things", {})

        assert result.structured_content is None


class TestHideDeprecatedAliases:
    async def test_tagged_tool_is_hidden_from_discovery(self, server):
        async with Client(server) as client:
            names = [tool.name for tool in await client.list_tools()]

        assert "list_things" in names
        assert "old_list_things" not in names

    async def test_hidden_tool_is_still_callable(self, server):
        async with Client(server) as client:
            result = await client.call_tool("old_list_things", {})

        assert _text(result) == "id|name\n1|one\ncount: 1"


class TestOutputSchema:
    async def test_dict_tools_advertise_no_output_schema(self, server):
        """The compact path omits structuredContent, which the spec forbids
        for a tool that advertises an outputSchema."""
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}

        assert tools["list_things"].outputSchema is None
