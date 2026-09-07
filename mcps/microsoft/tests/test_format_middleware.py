"""FormatNegotiation against the real server: one dict, two renderings.

Every other MCP test module runs one side of this split — test_mcp_server.py
pins the desktop JSON contract via an autouse header patch, the markdown
modules exercise str tools that the middleware never touches. This module is
where both sides of a single tool are asserted together.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
import respx
from fastmcp import Client
from ms_graph import mail_policy
from ms_graph.graph_client import GRAPH_BASE_URL

from .conftest import SAMPLE_EXTERNAL_MESSAGE, SAMPLE_MESSAGE, SAMPLE_MESSAGES_RESPONSE

CONNECT_URL = "https://auth.example.com/connect/microsoft?ticket=t"
INBOX_URL = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages"


def _mock_token(token: str = "test-ms-token"):
    return patch("ms_graph_mcp.get_graph_token", return_value=token)


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
        result = await _call(mcp_server, "manage_file", {"item_id": "x", "action": "bogus"})

        assert result.structured_content == {"result": _get_text(result)}
        assert _get_text(result).startswith("Invalid action 'bogus'")

    @respx.mock
    async def test_list_emails_renders_as_pipe_csv(self, mcp_server):
        """The columns a model reads, and the count line under them."""
        respx.get(INBOX_URL).mock(return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE))
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        assert result.structured_content is None
        lines = _get_text(result).split("\n")
        assert lines[0] == (
            "date|from_name|from_address|to|subject|is_read|body_preview|has_attachments|id"
        )
        assert lines[1] == (
            "2025-12-15T10:30:00Z|Alice Smith|alice@example.com|bob@example.com|"
            "Weekly Report|false|Here is the weekly report. Best, Alice||AAMkAGI2TG93AAA="
        )
        # Zero survives as a real answer; the empty query and notice drop out.
        assert lines[3:] == ["count: 2", "folder: inbox", "marked_read: 0"]

    @respx.mock
    async def test_list_emails_notice_line_is_verbatim_under_the_policy(
        self, mcp_server, monkeypatch
    ):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        respx.get(INBOX_URL).mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE]}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        assert _get_text(result).endswith(f"\nnotice: {mail_policy.POLICY_NOTICE}")


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
        args = {"item_id": "x", "action": "bogus"}
        compact = await _call(mcp_server, "manage_file", args)
        with _desktop_headers():
            desktop = await _call(mcp_server, "manage_file", args)

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
            "list_emails",
            "send_email",
            "manage_inbox_rules",
            "manage_mail_folders",
            "list_calendar_events",
            "get_calendar_event",
            "create_calendar_event",
            "check_availability",
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
