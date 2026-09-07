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
from ms_graph.power_bi import POWERBI_BASE_URL

from .conftest import (
    GRAPH_ERROR_404,
    SAMPLE_CHAT_MESSAGE_SYSTEM,
    SAMPLE_CHAT_MESSAGE_WITH_FILE,
    SAMPLE_DRIVE_ITEM_FILE,
    SAMPLE_DRIVE_ITEM_FOLDER,
    SAMPLE_EXTERNAL_MESSAGE,
    SAMPLE_MAILBOX_SETTINGS,
    SAMPLE_MESSAGE,
    SAMPLE_MESSAGES_RESPONSE,
    SAMPLE_NEW_DRAFT,
    SAMPLE_READ_DETAIL,
    SAMPLE_USER_PROFILE,
    TEAMS_FILE_ATTACHMENT_ID,
)

CONNECT_URL = "https://auth.example.com/connect/microsoft?ticket=t"
INBOX_URL = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages"


def _mock_token(token: str = "test-ms-token"):
    return patch("ms_graph_mcp.get_graph_token", return_value=token)


def _mock_pbi_token(token: str = "test-pbi-token"):
    return patch("ms_graph_mcp.get_powerbi_token", return_value=token)


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


@pytest.fixture
def str_specimen_server(mcp_server):
    """A str-returning tool to stand in for the ones production no longer has.

    Every production tool now returns a dict, but the middleware still has to
    leave a schema-advertising tool alone — fastmcp wraps a str result as
    {"result": ...} and a client rejects a result that drops it. The fixture is
    function-scoped, so the registration cannot leak into another test.
    """

    @mcp_server.tool(name="str_specimen")
    async def str_specimen() -> str:
        return "plain prose"

    yield mcp_server
    # mcp_server hands back the module-level singleton, so the registration has
    # to be undone by hand rather than by fixture scope.
    mcp_server.local_provider.remove_tool("str_specimen")


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

    @respx.mock
    async def test_profile_photo_keys_render_as_flat_lines(self, mcp_server):
        """Every photo key is a scalar, so nothing nests and has_photo shows."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/photo").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "metadata"})

        assert result.structured_content is None
        text = _get_text(result)
        assert "has_photo: false" in text.splitlines()
        assert "{" not in text

    async def test_str_tool_is_left_alone(self, str_specimen_server):
        """A tool that advertises an output schema must keep its structured
        content, or the client rejects the result."""
        result = await _call(str_specimen_server, "str_specimen")

        assert _get_text(result) == "plain prose"
        assert result.structured_content == {"result": "plain prose"}

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
    async def test_list_files_renders_ragged_rows_under_one_header(self, mcp_server):
        """A folder carries child_count and a file does not; the header is the
        union of both, and the file's missing cell renders empty."""
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root:/Documents:/children").mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_DRIVE_ITEM_FOLDER, SAMPLE_DRIVE_ITEM_FILE]}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"folder_path": "Documents"})

        assert result.structured_content is None
        assert _get_text(result) == (
            "name|type|size|child_count|id\n"
            "Documents|folder|0|5|folder-id-001\n"
            "report.csv|file|1024||file-id-001\n"
            "count: 2\n"
            "folder_path: Documents"
        )

    @respx.mock
    async def test_query_dataset_renders_sparse_dax_rows(self, mcp_server):
        """Power BI omits a null column from a row entirely. The header unions
        the keys in first-seen order and the omissions render as empty cells."""
        url = f"{POWERBI_BASE_URL}/groups/ws-1/datasets/ds-1/executeQueries"
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "tables": [
                                {
                                    "rows": [
                                        {"[Region]": "West", "[Units]": 4200},
                                        {"[Region]": "East", "[Margin]": 0.12},
                                    ]
                                }
                            ]
                        }
                    ]
                },
            )
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": "ws-1", "dataset_id": "ds-1", "dax_query": "EVALUATE 'Sales'"},
            )

        assert result.structured_content is None
        assert _get_text(result) == ("[Region]|[Units]|[Margin]\nWest|4200|\nEast||0.12\ncount: 2")

    @respx.mock
    async def test_read_teams_messages_renders_stripped_rows(self, mcp_server):
        """The override flattens the nested rows the default rules would have
        dumped as JSON: HTML stripped, attachments named, senders labelled."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(
                200,
                json={"value": [SAMPLE_CHAT_MESSAGE_WITH_FILE, SAMPLE_CHAT_MESSAGE_SYSTEM]},
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": "chat-1on1-001", "since": "2025-01-01"},
            )

        assert result.structured_content is None
        lines = _get_text(result).split("\n")
        assert lines[0] == "timestamp|sender|content|attachments|id"
        assert lines[1] == (
            "2026-02-01T10:00:00Z|Alice Smith|Here is the deck [File: roadmap.pptx]|"
            f"roadmap.pptx [file:{TEAMS_FILE_ATTACHMENT_ID}]|chat-msg-file-001"
        )
        # A system event has no sender and no body at all.
        assert lines[2] == "2026-01-05T12:00:00Z|(system)|(empty)||chat-msg-003"
        # next_cursor is empty outside page mode, so only the count survives.
        assert lines[3:] == ["count: 2"]

    @respx.mock
    async def test_read_teams_messages_truncates_without_leaking_the_hint(self, mcp_server):
        """max_content_length shortens the content column and is itself dropped
        — it is a rendering hint, not a result."""
        long_msg = {
            "id": "msg-long-001",
            "messageType": "message",
            "createdDateTime": "2025-12-15T12:00:00Z",
            "from": {"user": {"displayName": "Tim"}, "application": None},
            "body": {"contentType": "text", "content": "x" * 500},
            "attachments": [],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json={"value": [long_msg]})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": "chat-1on1-001",
                    "since": "2025-01-01",
                    "options": '{"max_content_length": 20}',
                },
            )

        text = _get_text(result)
        assert f"|{'x' * 20}...|" in text
        assert "max_content_length" not in text

    async def test_read_teams_messages_error_still_leads_with_the_code(self, mcp_server):
        """An override preempts the error rule, so it must delegate back to it."""
        result = await _call(mcp_server, "read_teams_messages", {})

        assert result.structured_content is None
        assert _get_text(result).split("\n")[0] == "error: invalid_arguments"

    @respx.mock
    async def test_send_teams_message_renders_a_confirmation(self, mcp_server):
        respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"message": "here you go", "chat_id": "chat-1on1-001"},
            )

        assert result.structured_content is None
        assert _get_text(result) == (
            "Message sent to Teams chat.\nid: chat-msg-file-001\nfiles: roadmap.pptx"
        )

    @respx.mock
    async def test_read_email_renders_the_envelope_body_and_attachments(self, mcp_server):
        """The inline logo is counted, not listed, and no headers show by default."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_READ_DETAIL)
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": SAMPLE_MESSAGE["id"]})

        assert result.structured_content is None
        text = _get_text(result)
        lines = text.split("\n")
        assert lines[0] == "subject: Weekly Report"
        assert lines[1] == "from: Alice Smith <alice@example.com>"
        assert lines[2] == "to: Bob Jones <bob@example.com>"
        # The cc entry has no name, so it renders as the bare address.
        assert lines[3] == "cc: carol@example.com"
        assert lines[4] == "received: 2025-12-15T10:30:00Z"
        assert lines[5] == "is_read: false"
        assert "Here is the weekly report." in text
        assert "name|content_type|size|kind|id" in text
        assert "report.pdf|application/pdf|1258291|file|AAMkAttachFile001=" in text
        assert "logo.png" not in text
        assert "inline: 1 not listed" in text
        assert "headers:" not in text
        assert "message-id" not in text

    @respx.mock
    async def test_read_email_include_headers_prints_them_without_the_hint(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_READ_DETAIL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {"message_id": SAMPLE_MESSAGE["id"], "options": '{"include_headers": true}'},
            )

        text = _get_text(result)
        assert "headers:" in text
        assert "message-id: <abc123@example.com>" in text
        # The hint is consumed by the renderer, never printed.
        assert "include_headers" not in text

    async def test_read_email_error_still_leads_with_the_code(self, mcp_server):
        """An override preempts the error rule, so it must delegate back to it."""
        with _mock_missing_connection():
            result = await _call(mcp_server, "read_email", {"message_id": "m"})

        assert result.structured_content is None
        assert _get_text(result).split("\n")[0] == "error: not_connected"

    @respx.mock
    async def test_manage_draft_update_body_renders_as_one_line(self, mcp_server):
        respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json={"id": "d1"})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {"action": "update_body", "draft_id": "d1", "text": "On it."},
            )

        assert result.structured_content is None
        assert _get_text(result) == "ok: true"

    @respx.mock
    async def test_manage_draft_create_falls_back_to_compact_json(self, mcp_server):
        """A draft nests its to/cc lists, so the hierarchical fallback applies."""
        respx.post(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {"action": "create", "to": "", "subject": "Lunch?"}
            )

        assert result.structured_content is None
        data = json.loads(_get_text(result))
        assert data["id"] == SAMPLE_NEW_DRAFT["id"]
        assert data["web_link"] == SAMPLE_NEW_DRAFT["webLink"]

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

    async def test_str_tool_is_identical_under_both_paths(self, str_specimen_server):
        compact = await _call(str_specimen_server, "str_specimen")
        with _desktop_headers():
            desktop = await _call(str_specimen_server, "str_specimen")

        assert _get_text(compact) == _get_text(desktop) == "plain prose"
        assert desktop.structured_content == compact.structured_content == {"result": "plain prose"}


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
            "read_email",
            "get_mail_attachment",
            "manage_draft",
            "mark_mail_read",
            "list_chats",
            "get_chat_members",
            "ensure_chat",
            "read_teams_messages",
            "get_teams_attachment",
            "mark_chat_read",
            "send_teams_message",
            "inspect_file",
            "connection_status",
            "list_teams",
            "search_teams_messages",
            "get_teams_activity",
            "list_sharepoint_sites",
            "list_files",
            "edit_document",
            "manage_file",
            "list_powerbi",
            "query_dataset",
            "refresh_dataset",
            "export_report",
        }
