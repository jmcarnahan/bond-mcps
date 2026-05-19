"""Tests for the FastMCP server using in-process client.

In-process FastMCP clients don't have HTTP request context, so we mock
get_graph_token() directly instead of get_http_headers().
"""

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from ms_graph.graph_client import GRAPH_BASE_URL
from ms_graph.power_bi import POWERBI_BASE_URL

from .conftest import (
    GRAPH_ERROR_403,
    SAMPLE_CHANNEL_MESSAGES_RESPONSE,
    SAMPLE_CHANNELS_RESPONSE,
    SAMPLE_CHAT_MESSAGE_SENT,
    SAMPLE_CHAT_MESSAGES_RESPONSE,
    SAMPLE_CHATS_RESPONSE,
    SAMPLE_COPY_COMPLETED,
    SAMPLE_COPY_FAILED,
    SAMPLE_DRIVE_CHILDREN_RESPONSE,
    SAMPLE_DRIVE_ITEM_BINARY,
    SAMPLE_DRIVE_ITEM_FILE,
    SAMPLE_DRIVE_ITEM_LARGE_TEXT,
    SAMPLE_DRIVE_ITEM_WORD,
    SAMPLE_MAILBOX_SETTINGS,
    SAMPLE_MESSAGE,
    SAMPLE_MESSAGE_2,
    SAMPLE_MESSAGES_RESPONSE,
    SAMPLE_PBI_DASHBOARDS_RESPONSE,
    SAMPLE_PBI_DATASETS_RESPONSE,
    SAMPLE_PBI_DAX_RESULT,
    SAMPLE_PBI_EXPORT_SUCCEEDED,
    SAMPLE_PBI_REPORTS_RESPONSE,
    SAMPLE_PBI_WORKSPACES_RESPONSE,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_SEARCH_RESPONSE_EMPTY,
    SAMPLE_SITES_RESPONSE,
    SAMPLE_TEAMS_RESPONSE,
    SAMPLE_UPLOADED_FILE,
    SAMPLE_USER_PROFILE,
)

MONITOR_URL = "https://api.onedrive.com/v1.0/monitor/copy-op-token"
PBI_EXPORT_MONITOR_URL = (
    f"{POWERBI_BASE_URL}/groups/ws-id-001/reports/rpt-id-001/exports/export-id-001"
)


def _mock_token(token: str = "test-ms-token"):
    """Patch get_graph_token to return a test token."""
    return patch("ms_graph_mcp.get_graph_token", return_value=token)


def _mock_pbi_token(token: str = "test-pbi-token"):
    """Patch get_powerbi_token to return a test token."""
    return patch("ms_graph_mcp.get_powerbi_token", return_value=token)


def _get_text(result) -> str:
    """Extract text from FastMCP CallToolResult."""
    return result.content[0].text


@pytest.fixture
def mcp_server():
    """Import and return the MCP server instance."""
    from ms_graph_mcp import mcp

    return mcp


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class TestMCPProfileTools:
    """Test user profile MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_get_user_profile_with_mailbox_address(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("get_user_profile", {})

        text = _get_text(result)
        assert "Test User" in text
        assert "user@example.com" in text
        assert "Mailbox Address" in text
        assert "mailbox@example.com" in text

    @respx.mock
    async def test_get_user_profile_without_mailbox_scope(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(
                403, json={"error": {"code": "ErrorAccessDenied", "message": "Access denied"}}
            )
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("get_user_profile", {})

        text = _get_text(result)
        assert "Test User" in text
        assert "Mailbox Address" not in text


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class TestMCPEmailTools:
    """Test consolidated email MCP tools."""

    @respx.mock
    async def test_list_emails_no_query(self, mcp_server):
        """No query → lists inbox."""
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_emails", {"top": 10})

        text = _get_text(result)
        assert "Weekly Report" in text
        assert "2 message(s)" in text

    @respx.mock
    async def test_list_emails_with_query(self, mcp_server):
        """query set → search mode."""
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_emails", {"query": "weekly"})

        text = _get_text(result)
        assert "1 result(s)" in text
        assert "Weekly Report" in text

    @respx.mock
    async def test_list_emails_search_no_results(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_emails", {"query": "nonexistent"})

        text = _get_text(result)
        assert "No messages found" in text
        assert "nonexistent" in text

    @respx.mock
    async def test_list_emails_empty_inbox(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_emails", {})

        assert "No messages found" in _get_text(result)

    @respx.mock
    async def test_list_emails_custom_folder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_emails", {"folder": "sentitems"})

        assert "Weekly Report" in _get_text(result)

    @respx.mock
    async def test_read_email_plain_body(self, mcp_server):
        msg_id = "AAMkAGI2TG93AAA="
        respx.get(f"{GRAPH_BASE_URL}/me/messages/{msg_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("read_email", {"message_id": msg_id})

        text = _get_text(result)
        assert "Weekly Report" in text
        assert "Alice Smith" in text
        assert "Here is the weekly report" in text

    @respx.mock
    async def test_read_email_html_body(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/messages/{SAMPLE_MESSAGE_2['id']}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_2)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "read_email", {"message_id": SAMPLE_MESSAGE_2["id"]}
                )

        text = _get_text(result)
        assert "HTML content" in text
        assert "Charlie Brown" in text

    @respx.mock
    async def test_send_email_plain_text(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "send_email",
                    {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
                )

        assert "sent" in _get_text(result).lower()
        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_html_body_auto_detected(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {
                        "to": "alice@example.com",
                        "subject": "HTML test",
                        "body": "<p>Hello <strong>Alice</strong>! Click <a href='https://example.com'>here</a>.</p>",
                    },
                )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    async def test_send_email_placeholder_not_mistaken_for_html(self, mcp_server):
        """'Dear <FirstName>,' must stay Text."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {
                        "to": "alice@example.com",
                        "subject": "Template",
                        "body": "Dear <FirstName>, thanks for reaching out.",
                    },
                )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_with_cc(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "send_email",
                    {
                        "to": "alice@example.com",
                        "subject": "Hi",
                        "body": "Hello!",
                        "cc": "bob@example.com",
                    },
                )

        text = _get_text(result)
        assert "CC" in text
        payload = json.loads(route.calls[0].request.content)
        assert len(payload["message"]["ccRecipients"]) == 1

    @respx.mock
    async def test_send_email_multiple_recipients(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {"to": "alice@example.com, bob@example.com", "subject": "Hi", "body": "Hello!"},
                )

        payload = json.loads(route.calls[0].request.content)
        assert len(payload["message"]["toRecipients"]) == 2

    @respx.mock
    async def test_send_email_explicit_text_overrides_html(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {
                        "to": "alice@example.com",
                        "subject": "S",
                        "body": "<p>HTML</p>",
                        "body_type": "Text",
                    },
                )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_no_from_by_default(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
                )

        payload = json.loads(route.calls[0].request.content)
        assert "from" not in payload["message"]

    @respx.mock
    async def test_send_email_with_from_address(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "send_email",
                    {
                        "to": "alice@example.com",
                        "subject": "Hi",
                        "body": "Hello!",
                        "from_address": "mailbox@example.com",
                    },
                )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["from"]["emailAddress"]["address"] == "mailbox@example.com"

    @respx.mock
    async def test_graph_error_propagates_from_email_tool(self, mcp_server):
        from fastmcp.exceptions import ToolError

        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken", "message": "Token expired"}},
            )
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="InvalidAuthenticationToken"):
                    await client.call_tool("list_emails", {})


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


class TestMCPTeamsTools:
    """Test consolidated Teams MCP tools."""

    @respx.mock
    async def test_list_teams_all(self, mcp_server):
        """No team_id → returns all joined teams."""
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_teams", {})

        text = _get_text(result)
        assert "Engineering" in text
        assert "2 team(s)" in text

    @respx.mock
    async def test_list_teams_channels(self, mcp_server):
        """team_id set → returns channels for that team."""
        team_id = "team-id-001"
        respx.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNELS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_teams", {"team_id": team_id})

        text = _get_text(result)
        assert "General" in text
        assert "2 channel(s)" in text
        assert team_id in text

    @respx.mock
    async def test_list_teams_empty(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_teams", {})

        assert "No teams found" in _get_text(result)

    @respx.mock
    async def test_list_teams_not_available(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_teams", {})

        assert "not available" in _get_text(result).lower()

    @respx.mock
    async def test_list_chats(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_chats", {})

        text = _get_text(result)
        assert "3 chat(s)" in text
        assert "Alice Smith" in text

    @respx.mock
    async def test_list_chats_empty(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_chats", {})

        assert "No chats found" in _get_text(result)

    async def test_list_chats_invalid_type(self, mcp_server):
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_chats", {"chat_type": "invalid"})

        assert "Invalid chat_type" in _get_text(result)

    @respx.mock
    async def test_read_teams_messages_channel(self, mcp_server):
        """team_id + channel_id → reads channel messages."""
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        respx.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "read_teams_messages",
                    {"team_id": team_id, "channel_id": channel_id},
                )

        text = _get_text(result)
        assert "2 message(s)" in text
        assert channel_id in text

    @respx.mock
    async def test_read_teams_messages_chat(self, mcp_server):
        """chat_id → reads chat messages."""
        chat_id = "chat-1on1-001"
        respx.get(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "read_teams_messages",
                    {"chat_id": chat_id},
                )

        text = _get_text(result)
        assert "1 message(s)" in text
        assert chat_id in text

    @respx.mock
    async def test_read_teams_messages_chat_takes_priority(self, mcp_server):
        """When both chat_id and team_id+channel_id are set, chat_id takes priority."""
        chat_id = "chat-1on1-001"
        respx.get(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "read_teams_messages",
                    {"chat_id": chat_id, "team_id": "team-id-001", "channel_id": "channel-id-001"},
                )

        text = _get_text(result)
        assert chat_id in text

    async def test_read_teams_messages_no_ids(self, mcp_server):
        """No IDs provided → returns helpful error."""
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("read_teams_messages", {})

        assert "Provide either chat_id" in _get_text(result)

    async def test_read_teams_messages_only_team_id(self, mcp_server):
        """Only team_id (no channel_id) → returns helpful error."""
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("read_teams_messages", {"team_id": "t1"})

        assert "Provide either chat_id" in _get_text(result)

    @respx.mock
    async def test_send_teams_message_to_channel(self, mcp_server):
        """team_id + channel_id → sends to channel."""
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        route = respx.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "send_teams_message",
                    {"message": "Hello!", "team_id": team_id, "channel_id": channel_id},
                )

        text = _get_text(result)
        assert "channel" in text.lower()
        assert route.called

    @respx.mock
    async def test_send_teams_message_to_chat(self, mcp_server):
        """chat_id → sends to chat."""
        chat_id = "chat-1on1-001"
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "send_teams_message",
                    {"message": "Hello!", "chat_id": chat_id},
                )

        text = _get_text(result)
        assert "chat" in text.lower()
        assert route.called

    async def test_send_teams_message_no_ids(self, mcp_server):
        """No IDs → helpful error, no API call made."""
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("send_teams_message", {"message": "Hello!"})

        assert "Provide either chat_id" in _get_text(result)

    @respx.mock
    async def test_get_teams_activity(self, mcp_server):
        # Wire up all the calls the activity scanner makes
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("get_teams_activity", {"hours": 1})

        text = _get_text(result)
        assert "No Teams activity" in text

    @respx.mock
    async def test_get_teams_activity_not_available(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("get_teams_activity", {})

        assert "not available" in _get_text(result).lower()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class TestMCPFileTools:
    """Test consolidated file MCP tools."""

    @respx.mock
    async def test_list_sharepoint_sites(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/sites").mock(
            return_value=httpx.Response(200, json=SAMPLE_SITES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_sharepoint_sites", {"query": "engineering"})

        text = _get_text(result)
        assert "Engineering Hub" in text
        assert "2" in text

    @respx.mock
    async def test_list_sharepoint_sites_empty(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/sites").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_sharepoint_sites", {"query": "nope"})

        assert "No SharePoint sites found" in _get_text(result)

    @respx.mock
    async def test_list_files_onedrive_browse(self, mcp_server):
        """No site_id, no query → OneDrive root browse."""
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {})

        text = _get_text(result)
        assert "Documents" in text
        assert "report.csv" in text

    @respx.mock
    async def test_list_files_onedrive_subfolder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root:/Documents:/children").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {"folder_path": "Documents"})

        text = _get_text(result)
        assert "report.csv" in text
        assert "Documents" in text

    @respx.mock
    async def test_list_files_sharepoint_browse(self, mcp_server):
        """site_id set, no query → SharePoint browse."""
        site_id = "site-id-001"
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {"site_id": site_id})

        text = _get_text(result)
        assert "Documents" in text

    @respx.mock
    async def test_list_files_search_query(self, mcp_server):
        """query set → search mode, site_id and folder_path ignored."""
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {"query": "budget"})

        text = _get_text(result)
        assert "Q4-budget.xlsx" in text
        assert "budget-notes.md" in text

    @respx.mock
    async def test_list_files_search_no_results(self, mcp_server):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE_EMPTY)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {"query": "nonexistent"})

        assert "No files found" in _get_text(result)

    @respx.mock
    async def test_list_files_empty_folder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_files", {})

        assert "No files found" in _get_text(result)

    @respx.mock
    async def test_inspect_file_metadata_only_default(self, mcp_server):
        """Default (no read_content arg) → metadata only, no download."""
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{SAMPLE_DRIVE_ITEM_FILE['id']}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file",
                    {"item_id": SAMPLE_DRIVE_ITEM_FILE["id"]},
                )

        text = _get_text(result)
        assert "report.csv" in text
        assert "Alice Smith" in text
        assert "---" not in text

    @respx.mock
    async def test_inspect_file_metadata_only_explicit(self, mcp_server):
        """read_content=False → metadata only, no download."""
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{SAMPLE_DRIVE_ITEM_FILE['id']}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file",
                    {"item_id": SAMPLE_DRIVE_ITEM_FILE["id"], "read_content": False},
                )

        text = _get_text(result)
        assert "report.csv" in text
        assert "---" not in text

    @respx.mock
    async def test_inspect_file_with_content(self, mcp_server):
        """read_content=True → downloads text content."""
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/content").mock(
            return_value=httpx.Response(200, content=b"col1,col2\n1,2\n")
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file",
                    {"item_id": item_id, "read_content": True},
                )

        text = _get_text(result)
        assert "report.csv" in text
        assert "col1,col2" in text
        assert "---" in text

    @respx.mock
    async def test_inspect_file_binary_returns_message(self, mcp_server):
        """Binary files (PPTX, 2.5 MB) report they cannot be shown as text."""
        item_id = SAMPLE_DRIVE_ITEM_BINARY["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_BINARY)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file", {"item_id": item_id, "read_content": True}
                )

        text = _get_text(result)
        # PPTX is 2.5 MB so it hits the "too large" branch (> 512 KB)
        assert "presentation.pptx" in text
        assert "too large" in text.lower() or "binary" in text.lower()

    @respx.mock
    async def test_inspect_file_too_large(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_LARGE_TEXT["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_LARGE_TEXT)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file", {"item_id": item_id, "read_content": True}
                )

        text = _get_text(result)
        assert "too large" in text.lower()

    @respx.mock
    async def test_inspect_file_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}/content").mock(
            return_value=httpx.Response(200, content=b"data")
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "inspect_file",
                    {"item_id": item_id, "site_id": site_id, "read_content": True},
                )

        assert "report.csv" in _get_text(result)

    @respx.mock
    async def test_graph_error_propagates_from_file_tool(self, mcp_server):
        from fastmcp.exceptions import ToolError

        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": "InvalidAuthenticationToken", "message": "Token expired"}},
            )
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="InvalidAuthenticationToken"):
                    await client.call_tool("list_files", {})


class TestMCPUploadTool:
    """Tests for the upload_file MCP tool."""

    @respx.mock
    async def test_upload_creates_file_in_root(self, mcp_server):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/notes.md:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "upload_file",
                    {"filename": "notes.md", "content": "# Hello"},
                )

        text = _get_text(result)
        assert "notes.md" in text
        assert "uploaded" in text.lower()
        assert route.called

    @respx.mock
    async def test_upload_to_subfolder(self, mcp_server):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Documents/data.csv:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "upload_file",
                    {"filename": "data.csv", "content": "a,b\n1,2", "folder_path": "Documents"},
                )

        assert "uploaded" in _get_text(result).lower()
        assert route.calls[0].request.headers["Content-Type"] == "text/csv"

    @respx.mock
    async def test_upload_to_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        route = respx.put(
            f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root:/Shared Documents/report.html:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "upload_file",
                    {
                        "filename": "report.html",
                        "content": "<h1>Hello</h1>",
                        "folder_path": "Shared Documents",
                        "site_id": site_id,
                    },
                )

        assert route.called
        assert route.calls[0].request.headers["Content-Type"] == "text/html"

    @respx.mock
    async def test_upload_result_includes_id_and_url(self, mcp_server):
        respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/readme.txt:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "upload_file", {"filename": "readme.txt", "content": "hello"}
                )

        text = _get_text(result)
        assert SAMPLE_UPLOADED_FILE["id"] in text
        assert SAMPLE_UPLOADED_FILE["webUrl"] in text


class TestMCPCopyOrRenameTool:
    """Tests for the consolidated copy_or_rename_file MCP tool."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_sleep):
        pass

    @respx.mock
    async def test_copy_action(self, mcp_server):
        """action='copy' → server-side copy."""
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "copy_or_rename_file",
                    {"item_id": item_id, "new_name": "template-copy.docx", "action": "copy"},
                )

        text = _get_text(result)
        assert "copied" in text.lower()
        assert "template-copy.docx" in text
        assert SAMPLE_COPY_COMPLETED["resourceId"] in text

    @respx.mock
    async def test_copy_with_destination_folder(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        dest = "folder-id-archive"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        copy_route = respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool(
                    "copy_or_rename_file",
                    {
                        "item_id": item_id,
                        "new_name": "archived.docx",
                        "action": "copy",
                        "destination_folder_id": dest,
                    },
                )

        copy_body = json.loads(copy_route.calls[0].request.content)
        assert copy_body["parentReference"]["id"] == dest

    @respx.mock
    async def test_copy_error_propagates(self, mcp_server):
        from fastmcp.exceptions import ToolError

        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_FAILED))
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="accessDenied"):
                    await client.call_tool(
                        "copy_or_rename_file",
                        {"item_id": item_id, "new_name": "copy.docx", "action": "copy"},
                    )

    @respx.mock
    async def test_rename_action(self, mcp_server):
        """action='rename' (default) → PATCH rename."""
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        renamed = {**SAMPLE_DRIVE_ITEM_FILE, "name": "final-report.csv"}
        route = respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=renamed)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "copy_or_rename_file",
                    {"item_id": item_id, "new_name": "final-report.csv"},
                )

        text = _get_text(result)
        assert "final-report.csv" in text
        assert "renamed" in text.lower()
        body = json.loads(route.calls[0].request.content)
        assert body == {"name": "final-report.csv"}

    @respx.mock
    async def test_rename_on_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        renamed = {**SAMPLE_DRIVE_ITEM_WORD, "name": "final-doc.docx"}
        respx.patch(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=renamed)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "copy_or_rename_file",
                    {"item_id": item_id, "new_name": "final-doc.docx", "site_id": site_id},
                )

        assert "final-doc.docx" in _get_text(result)

    async def test_invalid_action(self, mcp_server):
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "copy_or_rename_file",
                    {"item_id": "x", "new_name": "y", "action": "move"},
                )

        text = _get_text(result)
        assert "Invalid action" in text
        assert "move" in text

    @respx.mock
    async def test_rename_error_propagates(self, mcp_server):
        from fastmcp.exceptions import ToolError

        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "ResourceNotFound", "message": "Item not found."}}
            )
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="ResourceNotFound"):
                    await client.call_tool(
                        "copy_or_rename_file",
                        {"item_id": item_id, "new_name": "x.csv"},
                    )


# ---------------------------------------------------------------------------
# Power BI
# ---------------------------------------------------------------------------


class TestMCPPowerBITools:
    """Tests for the Power BI MCP tools."""

    @pytest.fixture(autouse=True)
    def patch_sleep(self, no_sleep):
        pass

    @respx.mock
    async def test_list_powerbi_workspaces(self, mcp_server):
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_WORKSPACES_RESPONSE)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_powerbi_workspaces", {})

        text = _get_text(result)
        assert "Analytics Hub" in text
        assert "Finance Reports" in text
        assert "My workspace" in text
        assert "3 workspace(s)" in text

    @respx.mock
    async def test_list_powerbi_workspaces_empty(self, mcp_server):
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_powerbi_workspaces", {})

        # Even with no named workspaces, My workspace is always shown
        text = _get_text(result)
        assert "My workspace" in text
        assert "1 workspace(s)" in text

    @respx.mock
    async def test_list_powerbi_content_all(self, mcp_server):
        """content_type=all returns datasets, reports, and dashboards."""
        ws_id = "ws-id-001"
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/reports").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_REPORTS_RESPONSE)
        )
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/dashboards").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DASHBOARDS_RESPONSE)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_powerbi_content", {"workspace_id": ws_id})

        text = _get_text(result)
        assert "Datasets" in text
        assert "Sales" in text
        assert "Reports" in text
        assert "Q4 Dashboard" in text
        assert "Dashboards" in text
        assert "Executive Overview" in text

    @respx.mock
    async def test_list_powerbi_content_datasets_only(self, mcp_server):
        ws_id = "ws-id-001"
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "list_powerbi_content",
                    {"workspace_id": ws_id, "content_type": "datasets"},
                )

        text = _get_text(result)
        assert "Datasets" in text
        assert "Sales" in text
        assert "Reports" not in text

    @respx.mock
    async def test_list_powerbi_workspaces_includes_my_workspace(self, mcp_server):
        """list_powerbi_workspaces always includes My workspace with id='me'."""
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_WORKSPACES_RESPONSE)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_powerbi_workspaces", {})

        text = _get_text(result)
        assert "My workspace" in text
        assert "me" in text  # sentinel ID
        assert "3 workspace(s)" in text  # 1 My + 2 named

    @respx.mock
    async def test_list_powerbi_content_my_workspace(self, mcp_server):
        """workspace_id='me' routes to root /datasets endpoint, not /groups/me/."""
        respx.get(f"{POWERBI_BASE_URL}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        respx.get(f"{POWERBI_BASE_URL}/reports").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_REPORTS_RESPONSE)
        )
        respx.get(f"{POWERBI_BASE_URL}/dashboards").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DASHBOARDS_RESPONSE)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("list_powerbi_content", {"workspace_id": "me"})

        text = _get_text(result)
        assert "Sales" in text
        assert "Q4 Dashboard" in text

    async def test_list_powerbi_content_invalid_type(self, mcp_server):
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "list_powerbi_content",
                    {"workspace_id": "ws-id-001", "content_type": "tiles"},
                )

        assert "Invalid content_type" in _get_text(result)

    @respx.mock
    async def test_query_dataset(self, mcp_server):
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/executeQueries"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT))
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "query_dataset",
                    {"workspace_id": ws_id, "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
                )

        text = _get_text(result)
        assert "3 row(s)" in text
        assert "West" in text
        assert "[Region]" in text
        body = json.loads(route.calls[0].request.content)
        assert body["queries"][0]["query"] == "EVALUATE 'Sales'"

    @respx.mock
    async def test_refresh_dataset(self, mcp_server):
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/refreshes").mock(
            return_value=httpx.Response(202)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "refresh_dataset",
                    {"workspace_id": ws_id, "dataset_id": ds_id},
                )

        text = _get_text(result)
        assert "Refresh triggered" in text
        assert route.called

    @respx.mock
    async def test_query_dataset_my_workspace(self, mcp_server):
        """workspace_id='me' routes to root /datasets/... not /groups/me/..."""
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{ds_id}/executeQueries").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "query_dataset",
                    {"workspace_id": "me", "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
                )

        assert "3 row(s)" in _get_text(result)
        assert route.called
        assert "/groups/" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_refresh_dataset_my_workspace(self, mcp_server):
        """workspace_id='me' routes to root /datasets/... not /groups/me/..."""
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{ds_id}/refreshes").mock(
            return_value=httpx.Response(202)
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "refresh_dataset",
                    {"workspace_id": "me", "dataset_id": ds_id},
                )

        assert "Refresh triggered" in _get_text(result)
        assert route.called
        assert "/groups/" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_export_report_onedrive_upload_failure_degrades_gracefully(self, mcp_server):
        """If Graph token is missing, export still succeeds with a helpful message."""
        ws_id = "ws-id-001"
        rpt_id = "rpt-id-001"
        export_id = "export-id-001"
        export_location = f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}"

        respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/ExportTo").mock(
            return_value=httpx.Response(202, headers={"Location": export_location})
        )
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED)
        )
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}/file"
        ).mock(return_value=httpx.Response(200, content=b"%PDF fake"))
        with (
            _mock_pbi_token(),
            patch("ms_graph_mcp.get_graph_token", side_effect=PermissionError("Not connected.")),
        ):
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "export_report",
                    {"workspace_id": ws_id, "report_id": rpt_id, "export_format": "PDF"},
                )

        text = _get_text(result)
        assert "exported" in text.lower()
        assert "OneDrive" in text
        assert "Microsoft auth" in text
        assert "PDF" in text

    @respx.mock
    async def test_export_report_success(self, mcp_server):
        """Export flow: PBI export → download bytes → upload to OneDrive → return URL."""
        ws_id = "ws-id-001"
        rpt_id = "rpt-id-001"
        export_id = "export-id-001"
        export_location = f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}"
        fake_pdf = b"%PDF-1.4 fake content"

        # PBI: start export → poll → download
        respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/ExportTo").mock(
            return_value=httpx.Response(202, headers={"Location": export_location})
        )
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_EXPORT_SUCCEEDED)
        )
        respx.get(
            f"{POWERBI_BASE_URL}/groups/{ws_id}/reports/{rpt_id}/exports/{export_id}/file"
        ).mock(return_value=httpx.Response(200, content=fake_pdf))
        # Graph: upload to OneDrive
        upload_route = respx.put(url__regex=r"/content$").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )

        with _mock_pbi_token(), _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "export_report",
                    {"workspace_id": ws_id, "report_id": rpt_id, "export_format": "PDF"},
                )

        text = _get_text(result)
        assert "exported" in text.lower()
        assert "PDF" in text
        assert "OneDrive" in text
        assert SAMPLE_UPLOADED_FILE["webUrl"] in text
        # Verify correct content-type was sent for PDF
        assert upload_route.calls[0].request.headers["Content-Type"] == "application/pdf"
        # Verify the bytes uploaded match what was downloaded
        assert upload_route.calls[0].request.content == fake_pdf

    async def test_export_report_invalid_format(self, mcp_server):
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "export_report",
                    {"workspace_id": "ws-1", "report_id": "rpt-1", "export_format": "DOCX"},
                )

        assert "Invalid export_format" in _get_text(result)

    @respx.mock
    async def test_powerbi_auth_error_propagates(self, mcp_server):
        from fastmcp.exceptions import ToolError

        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(
                401, json={"error": {"code": "Unauthorized", "message": "Token expired."}}
            )
        )
        with _mock_pbi_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="Unauthorized"):
                    await client.call_tool("list_powerbi_workspaces", {})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestMCPAuth:
    """Test authentication behavior."""

    async def test_missing_token_raises_error(self, mcp_server):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        with patch(
            "ms_graph_mcp.get_graph_token", side_effect=PermissionError("Authorization required.")
        ):
            async with Client(mcp_server) as client:
                with pytest.raises(ToolError, match="Authorization required"):
                    await client.call_tool("list_emails", {})

    async def test_all_tools_require_auth(self, mcp_server):
        """Verify every tool rejects unauthenticated requests."""
        from fastmcp import Client
        from fastmcp.exceptions import ToolError

        # Graph tools — blocked by get_graph_token failing
        graph_tools = [
            ("get_user_profile", {}),
            ("list_emails", {}),
            ("read_email", {"message_id": "fake-id"}),
            ("send_email", {"to": "a@b.com", "subject": "S", "body": "B"}),
            ("list_teams", {}),
            ("list_chats", {}),
            ("read_teams_messages", {"chat_id": "c1"}),
            ("send_teams_message", {"message": "Hi", "chat_id": "c1"}),
            ("get_teams_activity", {}),
            ("list_sharepoint_sites", {}),
            ("list_files", {}),
            ("inspect_file", {"item_id": "x"}),
            ("upload_file", {"filename": "x.txt", "content": "y"}),
            ("copy_or_rename_file", {"item_id": "x", "new_name": "y"}),
        ]
        with patch(
            "ms_graph_mcp.get_graph_token", side_effect=PermissionError("Authorization required.")
        ):
            async with Client(mcp_server) as client:
                for tool_name, args in graph_tools:
                    with pytest.raises(ToolError, match="Authorization required"):
                        await client.call_tool(tool_name, args)

        # Power BI tools — blocked by get_powerbi_token failing
        pbi_tools = [
            ("list_powerbi_workspaces", {}),
            ("list_powerbi_content", {"workspace_id": "ws-1"}),
            (
                "query_dataset",
                {"workspace_id": "ws-1", "dataset_id": "ds-1", "dax_query": "EVALUATE {1}"},
            ),
            ("refresh_dataset", {"workspace_id": "ws-1", "dataset_id": "ds-1"}),
            ("export_report", {"workspace_id": "ws-1", "report_id": "rpt-1"}),
        ]
        with patch(
            "ms_graph_mcp.get_powerbi_token", side_effect=PermissionError("Authorization required.")
        ):
            async with Client(mcp_server) as client:
                for tool_name, args in pbi_tools:
                    with pytest.raises(ToolError, match="Authorization required"):
                        await client.call_tool(tool_name, args)
