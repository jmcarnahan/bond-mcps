"""FormatNegotiation against the real server: one dict, two renderings.

Every other MCP test module runs one side of this split — test_mcp_server.py
pins the desktop JSON contract via an autouse header patch, the markdown
modules exercise str tools that the middleware never touches. This module is
where both sides of a single tool are asserted together.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import respx
from fastmcp import Client

CONNECT_URL = "https://auth.example.com/connect/microsoft?ticket=t"


@contextmanager
def _desktop_headers():
    """Stand in for a request carrying `X-Bond-Client: desktop`.

    In-process clients have no HTTP context, so fastmcp's get_http_headers()
    returns {} and calls take the compact path unless patched here.
    """
    with patch(
        "bond_common.middleware.get_http_headers",
        return_value={"x-bond-client": "desktop"},
    ):
        yield


def _get_text(result) -> str:
    return result.content[0].text


def _mock_missing_connection(connect_url=CONNECT_URL):
    from auth import MissingProviderConnection

    return patch(
        "ms_graph_mcp.get_graph_token",
        side_effect=MissingProviderConnection(
            provider="microsoft", user_key="u", connect_url=connect_url
        ),
    )


async def _call(mcp_server, name, args=None):
    async with Client(mcp_server) as client:
        return await client.call_tool(name, args or {})


@pytest.fixture
def mcp_server():
    from ms_graph_mcp import mcp

    return mcp


class TestCompactPath:
    """No desktop header — the model-facing rendering."""

    async def test_status_dict_renders_compactly(self, mcp_server):
        result = await _call(mcp_server, "connection_status")

        assert result.structured_content is None
        # `scopes: []` makes this an (empty) table, so the list key leads and
        # the null connect_url/account lines are dropped.
        assert _get_text(result) == "scopes: (none)\nconnected: false"

    @respx.mock
    async def test_empty_table_renders_as_the_none_marker(self, mcp_server):
        """Mirrors the blank-query contract test in test_mcp_server.py."""
        result = await _call(mcp_server, "search_people", {"query": "   "})

        assert result.structured_content is None
        assert _get_text(result) == "people: (none)"

    async def test_error_dict_renders_as_a_leading_error_line(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_profile")

        assert result.structured_content is None
        text = _get_text(result)
        assert text.startswith("error: not_connected")
        assert CONNECT_URL in text

    async def test_str_tool_is_left_alone(self, mcp_server):
        """Markdown tools advertise a generated output schema and must keep
        their structured content, or the client rejects the result."""
        result = await _call(mcp_server, "manage_inbox_rules", {"action": "bogus"})

        assert result.structured_content == {"result": _get_text(result)}
        assert _get_text(result).startswith("Unknown action 'bogus'")


class TestDesktopPath:
    """`X-Bond-Client: desktop` — the canonical dict contract."""

    @respx.mock
    async def test_dict_tool_keeps_structured_content(self, mcp_server):
        with _desktop_headers():
            result = await _call(mcp_server, "search_people", {"query": "   "})

        assert result.structured_content == {"people": []}
        assert json.loads(_get_text(result)) == {"people": []}

    async def test_error_dict_stays_a_dict(self, mcp_server):
        with _desktop_headers(), _mock_missing_connection():
            result = await _call(mcp_server, "get_profile")

        assert result.structured_content == {
            "error": "not_connected",
            "connect_url": CONNECT_URL,
        }

    async def test_str_tool_is_identical_under_both_paths(self, mcp_server):
        args = {"action": "bogus"}
        compact = await _call(mcp_server, "manage_inbox_rules", args)
        with _desktop_headers():
            desktop = await _call(mcp_server, "manage_inbox_rules", args)

        assert _get_text(compact) == _get_text(desktop)
        assert desktop.structured_content == compact.structured_content


class TestOutputSchemas:
    """The declaration that opts a tool into compact rendering."""

    async def test_every_dict_tool_advertises_no_output_schema(self, mcp_server):
        async with Client(mcp_server) as client:
            tools = await client.list_tools()

        no_schema = {tool.name for tool in tools if tool.outputSchema is None}
        assert no_schema == {
            "get_profile",
            "search_people",
            "sync_mail",
            "get_mail_detail",
            "get_mail_attachment_json",
            "create_reply_draft_json",
            "create_draft_json",
            "update_draft_body",
            "add_draft_attachment_json",
            "send_draft",
            "mark_mail_read",
            "list_chats_page",
            "get_chat_members",
            "ensure_chat",
            "list_chat_messages_page",
            "get_chat_attachment_json",
            "mark_chat_read",
            "send_chat_message_json",
            "inspect_file",
            "connection_status",
        }
