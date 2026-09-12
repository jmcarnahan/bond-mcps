"""Tests for the FastMCP server using in-process client.

In-process FastMCP clients don't have HTTP request context, so we mock
get_graph_token() directly instead of get_http_headers().
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import pytest
import respx
from ms_graph import mail, mail_policy
from ms_graph.files import _encode_sharing_url
from ms_graph.graph_client import GRAPH_BASE_URL
from ms_graph.power_bi import POWERBI_BASE_URL

from auth.exceptions import MissingProviderConnection

from .conftest import (
    GRAPH_ERROR_400,
    GRAPH_ERROR_403,
    GRAPH_ERROR_404,
    GRAPH_ERROR_410,
    SAMPLE_CALENDAR_EVENT,
    SAMPLE_CALENDAR_EVENT_ALLDAY,
    SAMPLE_CALENDAR_EVENTS_RESPONSE,
    SAMPLE_CHANNEL_FILES_FOLDER,
    SAMPLE_CHANNEL_MESSAGES_RESPONSE,
    SAMPLE_CHANNELS_RESPONSE,
    SAMPLE_CHAT_CREATED,
    SAMPLE_CHAT_GROUP,
    SAMPLE_CHAT_MEMBERS_RESPONSE,
    SAMPLE_CHAT_MESSAGE_FULL,
    SAMPLE_CHAT_MESSAGE_SENT,
    SAMPLE_CHAT_MESSAGE_WITH_CARD,
    SAMPLE_CHAT_MESSAGE_WITH_FILE,
    SAMPLE_CHAT_MESSAGE_WITH_IMAGE,
    SAMPLE_CHAT_MESSAGE_WITH_JUNK_ATTACHMENTS,
    SAMPLE_CHAT_MESSAGES_PAGE,
    SAMPLE_CHAT_MESSAGES_PAGE_WITH_ATTACHMENTS,
    SAMPLE_CHAT_MESSAGES_RESPONSE,
    SAMPLE_CHAT_ONEONONE,
    SAMPLE_CHATS_PAGE,
    SAMPLE_CHATS_PAGE_NEXT_LINK,
    SAMPLE_CHATS_RESPONSE,
    SAMPLE_COPY_COMPLETED,
    SAMPLE_COPY_FAILED,
    SAMPLE_CREATED_ATTACHMENT,
    SAMPLE_CREATED_EVENT,
    SAMPLE_DELTA_LINK,
    SAMPLE_DELTA_MESSAGE,
    SAMPLE_DELTA_NEXT_LINK,
    SAMPLE_DELTA_PAGE_FINAL,
    SAMPLE_DELTA_PAGE_NEXT,
    SAMPLE_DELTA_TOMBSTONE,
    SAMPLE_DIRECTORY_USER,
    SAMPLE_DRAFT_FOR_SEND,
    SAMPLE_DRAFT_MESSAGE,
    SAMPLE_DRIVE_CHILDREN_RESPONSE,
    SAMPLE_DRIVE_ITEM_BINARY,
    SAMPLE_DRIVE_ITEM_FILE,
    SAMPLE_DRIVE_ITEM_FOLDER,
    SAMPLE_DRIVE_ITEM_LARGE_TEXT,
    SAMPLE_DRIVE_ITEM_WORD,
    SAMPLE_EXTERNAL_DELTA_MESSAGE,
    SAMPLE_EXTERNAL_ITEM_ATTACHMENT,
    SAMPLE_EXTERNAL_ITEM_ATTACHMENT_META,
    SAMPLE_EXTERNAL_MESSAGE,
    SAMPLE_EXTERNAL_MESSAGE_DETAIL,
    SAMPLE_FILE_ATTACHMENT,
    SAMPLE_FORWARDING_RULE,
    SAMPLE_HYDRATED_CHANNEL_MESSAGE,
    SAMPLE_HYDRATED_CHAT_MESSAGE,
    SAMPLE_INVITE_RESPONSE,
    SAMPLE_ITEM_ATTACHMENT,
    SAMPLE_MAIL_FOLDER,
    SAMPLE_MAIL_FOLDERS_RESPONSE,
    SAMPLE_MAILBOX_SETTINGS,
    SAMPLE_MESSAGE,
    SAMPLE_MESSAGE_DETAIL,
    SAMPLE_MESSAGE_DETAIL_NO_BODY,
    SAMPLE_MESSAGE_RULE,
    SAMPLE_MESSAGE_RULES_RESPONSE,
    SAMPLE_MESSAGES_PAGE1,
    SAMPLE_MESSAGES_PAGE2,
    SAMPLE_MESSAGES_RESPONSE,
    SAMPLE_NEW_DRAFT,
    SAMPLE_ONBEHALF_MESSAGE,
    SAMPLE_PBI_DASHBOARDS_RESPONSE,
    SAMPLE_PBI_DATASETS_RESPONSE,
    SAMPLE_PBI_DAX_EMPTY,
    SAMPLE_PBI_DAX_RESULT,
    SAMPLE_PBI_EXPORT_SUCCEEDED,
    SAMPLE_PBI_REPORTS_RESPONSE,
    SAMPLE_PBI_WORKSPACES_RESPONSE,
    SAMPLE_PHOTO_METADATA,
    SAMPLE_READ_DETAIL,
    SAMPLE_REFERENCE_ATTACHMENT,
    SAMPLE_REPLY_DRAFT,
    SAMPLE_SCHEDULE_RESPONSE,
    SAMPLE_SEARCH_CHANNEL_HIT,
    SAMPLE_SEARCH_CHAT_HIT,
    SAMPLE_SEARCH_MESSAGES_EMPTY,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_SEARCH_RESPONSE_EMPTY,
    SAMPLE_SENDER_ONLY_EXTERNAL,
    SAMPLE_SENDER_ONLY_INTERNAL,
    SAMPLE_SHARED_TEXT_FILE,
    SAMPLE_SITES_RESPONSE,
    SAMPLE_TEAMS_DRIVE_ITEM,
    SAMPLE_TEAMS_RESPONSE,
    SAMPLE_TEAMS_UPLOAD_RESPONSE,
    SAMPLE_TEAMS_UPLOADED_ITEM,
    SAMPLE_UNSENT_DRAFT,
    SAMPLE_UNSENT_DRAFT_DETAIL,
    SAMPLE_UPLOADED_FILE,
    SAMPLE_USER_PROFILE,
    SAMPLE_USERS_SEARCH_RESPONSE,
    SEARCH_CHANNEL_ID,
    SEARCH_CHAT_ID,
    SEARCH_TEAM_ID,
    TEAMS_FILE_ATTACHMENT_ID,
    TEAMS_FILE_URL,
    TEAMS_HOSTED_ID,
    TEAMS_HOSTED_URL,
    TEAMS_PPTX_MIME,
    TEAMS_UPLOAD_GUID,
    TEAMS_WEBDAV_URL,
    search_response,
)

# Teams message search. The index answers POST /search/query; every hit is then
# hydrated through GET /chats/{chatId}/messages/{id}, whose ids percent-encode.
TEAMS_SEARCH_URL = f"{GRAPH_BASE_URL}/search/query"
SEARCH_CHAT_HYDRATE = f"{GRAPH_BASE_URL}/chats/{quote(SEARCH_CHAT_ID, safe='')}/messages"
SEARCH_CHANNEL_HYDRATE = f"{GRAPH_BASE_URL}/chats/{quote(SEARCH_CHANNEL_ID, safe='')}/messages"
SEARCH_NOT_SUPPORTED_400 = {
    "error": {"code": "BadRequest", "message": "This API is not supported for MSA accounts"}
}
# A channel thread reply: the chat route refuses it and names the parent, so the
# hydrator retries on /teams/{team}/channels/{channel}/messages/{parent}/replies.
SEARCH_REPLY_ID = "1713933434104"
SEARCH_REPLY_PARENT_ID = "1713933312527"
SEARCH_REPLY_HIT = {
    "summary": "budget2026",
    "resource": {
        "id": SEARCH_REPLY_ID,
        "chatId": SEARCH_CHANNEL_ID,
        "channelIdentity": {"channelId": SEARCH_CHANNEL_ID, "teamId": SEARCH_TEAM_ID},
        "createdDateTime": "2024-04-24T04:37:15Z",
        "webLink": f"https://teams.microsoft.com/l/message/channel/{SEARCH_REPLY_ID}",
    },
}
SEARCH_IS_A_REPLY_400 = {
    "error": {
        "code": "BadRequest",
        "message": (
            f"The message '{SEARCH_REPLY_ID}' is a reply and is not supported on this route. "
            "Only root message identifiers are supported; retrieve replies via "
            f"/chats({SEARCH_CHANNEL_ID})/messages({SEARCH_REPLY_PARENT_ID})"
            f"/replies({SEARCH_REPLY_ID})."
        ),
    }
}
SEARCH_REPLY_ROUTE = (
    f"{GRAPH_BASE_URL}/teams/{quote(SEARCH_TEAM_ID, safe='')}"
    f"/channels/{quote(SEARCH_CHANNEL_ID, safe='')}"
    f"/messages/{SEARCH_REPLY_PARENT_ID}/replies/{SEARCH_REPLY_ID}"
)
SEARCH_REPLY_BODY = {
    "id": SEARCH_REPLY_ID,
    "replyToId": SEARCH_REPLY_PARENT_ID,
    "messageType": "message",
    "createdDateTime": "2024-04-24T04:37:15Z",
    "from": {"user": {"displayName": "Jimmy Wakimoto"}, "application": None},
    "body": {"contentType": "html", "content": "<p>Moving the #budget2026 thread here</p>"},
    "attachments": [],
}


def _search_hit(msg_id, created="2026-03-02T10:00:00Z", chat_id=SEARCH_CHAT_ID):
    """A chat search hit for one message id."""
    return {
        "summary": "budget2026",
        "resource": {
            "id": msg_id,
            "chatId": chat_id,
            "channelIdentity": {"channelId": chat_id},
            "createdDateTime": created,
            "webLink": f"https://teams.microsoft.com/l/message/chat/{msg_id}",
        },
    }


def _search_msg(msg_id, content="The #budget2026 numbers are in"):
    """A hydrated chat message for one search hit."""
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": "2026-03-02T10:00:00Z",
        "from": {"user": {"displayName": "Alice Smith"}, "application": None},
        "body": {"contentType": "text", "content": content},
        "attachments": [],
    }


def _mock_search_hydration(bodies):
    """Serve GET /chats/*/messages/<id> from a {message id: body} map; 404 otherwise."""

    def _handler(request):
        msg_id = str(request.url).rsplit("/", 1)[-1]
        body = bodies.get(msg_id)
        if body is None:
            return httpx.Response(404, json=GRAPH_ERROR_404)
        return httpx.Response(200, json=body)

    return respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(side_effect=_handler)


def _recorded_query_string() -> str:
    """The queryString of the first /search/query request respx saw."""
    for call in respx.calls:
        if call.request.url.path.endswith("/search/query"):
            return json.loads(call.request.content)["requests"][0]["query"]["queryString"]
    raise AssertionError("no /search/query request was made")


MONITOR_URL = "https://api.onedrive.com/v1.0/monitor/copy-op-token"
CONNECT_URL = "https://auth.example.com/connect/microsoft?ticket=t"
SOURCE_DRIVE_ID = SAMPLE_DRIVE_ITEM_WORD["parentReference"]["driveId"]
PBI_EXPORT_MONITOR_URL = (
    f"{POWERBI_BASE_URL}/groups/ws-id-001/reports/rpt-id-001/exports/export-id-001"
)

# Attachment URLs. aget_message interpolates the message ID raw (legacy), while
# every attachment path percent-encodes it — hence the two spellings.
ATT_MSG_ID = SAMPLE_MESSAGE["id"]
ATT_BASE = f"{GRAPH_BASE_URL}/me/messages/{quote(ATT_MSG_ID, safe='')}/attachments"
ATT_FILE_URL = f"{ATT_BASE}/{quote(SAMPLE_FILE_ATTACHMENT['id'], safe='')}"
ATT_ITEM_URL = f"{ATT_BASE}/{quote(SAMPLE_ITEM_ATTACHMENT['id'], safe='')}"
ATT_REF_URL = f"{ATT_BASE}/{quote(SAMPLE_REFERENCE_ATTACHMENT['id'], safe='')}"
DRAFT_BASE = f"{GRAPH_BASE_URL}/me/messages/{quote(SAMPLE_DRAFT_MESSAGE['id'], safe='')}"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Teams attachment routes. The sharing URL resolves through /shares/{token};
# the more specific content and thumbnail routes are exact URLs so registration
# order cannot shadow the metadata route.
TEAMS_CHAT_ID = "chat-1on1-001"
TEAMS_CHAT_MSGS = f"{GRAPH_BASE_URL}/chats/{TEAMS_CHAT_ID}/messages"
TEAMS_FILE_MSG_URL = f"{TEAMS_CHAT_MSGS}/chat-msg-file-001"
TEAMS_IMAGE_MSG_URL = f"{TEAMS_CHAT_MSGS}/chat-msg-image-001"
TEAMS_CARD_MSG_URL = f"{TEAMS_CHAT_MSGS}/chat-msg-card-001"
TEAMS_JUNK_MSG_URL = f"{TEAMS_CHAT_MSGS}/chat-msg-junk-001"
TEAMS_HOSTED_VALUE_URL = f"{TEAMS_IMAGE_MSG_URL}/hostedContents/{TEAMS_HOSTED_ID}/$value"
TEAMS_SHARE_BASE = f"{GRAPH_BASE_URL}/shares/{_encode_sharing_url(TEAMS_FILE_URL)}/driveItem"
TEAMS_SHARE_CONTENT_URL = f"{TEAMS_SHARE_BASE}/content"
TEAMS_SHARE_THUMB_URL = f"{TEAMS_SHARE_BASE}/thumbnails/0/medium/content"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-png"

# Sending a file into a chat: OneDrive upload, re-fetch, roster, share.
TEAMS_SEND_UPLOAD_URL = (
    f"{GRAPH_BASE_URL}/me/drive/root:/Microsoft%20Teams%20Chat%20Files/notes.txt:/content"
)
TEAMS_SEND_ITEM_URL = f"{GRAPH_BASE_URL}/me/drive/items/teams-upload-001"
TEAMS_SEND_MEMBERS_URL = f"{GRAPH_BASE_URL}/chats/{TEAMS_CHAT_ID}/members"
TEAMS_SEND_INVITE_URL = f"{TEAMS_SEND_ITEM_URL}/invite"


def _token_with_oid(oid: str) -> str:
    """An unsigned JWT whose payload carries the given oid claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"oid": oid}).encode()).rstrip(b"=")
    return f"eyJhbGciOiJub25lIn0.{payload.decode()}.sig"


def _invite_recipients() -> list:
    """The recipients of the one /invite call respx saw."""
    for call in respx.calls:
        if call.request.url.path.endswith("/invite"):
            return json.loads(call.request.content)["recipients"]
    raise AssertionError("no /invite request was made")


def _mock_chat_file_upload() -> None:
    """The four requests that put a file where a chat message can point at it."""
    respx.put(url__startswith=TEAMS_SEND_UPLOAD_URL).mock(
        return_value=httpx.Response(201, json=SAMPLE_TEAMS_UPLOAD_RESPONSE)
    )
    respx.get(url__startswith=TEAMS_SEND_ITEM_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_TEAMS_UPLOADED_ITEM)
    )
    respx.get(TEAMS_SEND_MEMBERS_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
    )
    respx.post(TEAMS_SEND_INVITE_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_INVITE_RESPONSE)
    )


def _graph_trail() -> list[tuple[str, str]]:
    """(method, path) for every request respx saw, in order."""
    return [(c.request.method, c.request.url.path) for c in respx.calls]


# The draft send_email builds, plus the two ids Graph stamps at creation — the
# only handle a caller ever gets on the copy that lands in Sent Items.
SENT_DRAFT = {
    **SAMPLE_DRAFT_MESSAGE,
    "conversationId": "AAQkAGI2conv777=",
    "internetMessageId": "<draft777@example.com>",
}

# The row list_emails builds from SAMPLE_MESSAGE.
MSG_ROW = {
    "date": "2025-12-15T10:30:00Z",
    "from_name": "Alice Smith",
    "from_address": "alice@example.com",
    "to": "bob@example.com",
    "subject": "Weekly Report",
    "is_read": False,
    "body_preview": "Here is the weekly report. Best, Alice",
    "has_attachments": None,
    "id": "AAMkAGI2TG93AAA=",
}


def _mock_send_email() -> respx.Route:
    """The two requests send_email always makes: create the draft, then send it."""
    create = respx.post(f"{GRAPH_BASE_URL}/me/messages").mock(
        return_value=httpx.Response(201, json=SENT_DRAFT)
    )
    respx.post(f"{DRAFT_BASE}/send").mock(return_value=httpx.Response(202))
    return create


def _sent_payload(route: respx.Route) -> dict:
    """The message body of the draft-creating POST send_email made."""
    return json.loads(route.calls[0].request.content)


def _docx_bytes() -> bytes:
    """A real docx so the text sink exercises the extractor, not a stub."""
    from ms_graph import document_create

    return document_create.markdown_to_docx("# Quarterly Title\n\nBody text here.")


@pytest.fixture(autouse=True)
def _desktop_client_header():
    """Pin this module to the desktop JSON contract.

    In-process clients carry no HTTP headers, so FormatNegotiation would
    render every dict tool compactly and `_structured` would have no dict to
    read. The compact rendering is covered by test_format_middleware.py.
    """
    with patch(
        "bond_common.middleware.get_http_headers",
        return_value={"x-bond-client": "desktop"},
    ):
        yield


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


DIRECTORY_USER_ID = "user-id-002"
USER_URL_PREFIX = f"{GRAPH_BASE_URL}/users/{DIRECTORY_USER_ID}"
USER_PHOTO_URL = f"{USER_URL_PREFIX}/photo"
USER_PHOTO_240_URL = f"{USER_URL_PREFIX}/photos/240x240/$value"
ME_PHOTO_URL = f"{GRAPH_BASE_URL}/me/photo"
ME_PHOTO_240_URL = f"{GRAPH_BASE_URL}/me/photos/240x240/$value"
ME_PHOTO_ORIGINAL_URL = f"{GRAPH_BASE_URL}/me/photo/$value"
PHOTO_BYTES = b"\x89PNGfake"
# The one dict every "deployed without User.ReadBasic.All" test pins.
SCOPE_MISSING = {
    "error": "directory_scope_missing",
    "reason": (
        "Could not retrieve the profile: this connection lacks the User.ReadBasic.All permission."
    ),
}


def _mock_me_with_photo(metadata):
    """Route /me, /me/mailboxSettings and /me/photo; metadata None means no
    photo is set (Graph answers 404 ImageNotFound)."""
    respx.get(f"{GRAPH_BASE_URL}/me").mock(
        return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
    )
    respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
        return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
    )
    respx.get(ME_PHOTO_URL).mock(
        return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        if metadata is None
        else httpx.Response(200, json=metadata)
    )


class TestMCPProfileTools:
    """Test user profile MCP tools via in-process FastMCP client."""

    @respx.mock
    async def test_get_profile_with_mailbox_address(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool("get_profile", {})

        assert _structured(result) == {
            "id": "user-id-001",
            "display_name": "Test User",
            "mail": "user@example.com",
            "user_principal_name": "user@example.com",
            "mailbox_address": "mailbox@example.com",
            "job_title": None,
        }

    @respx.mock
    async def test_get_profile_without_mailbox_scope(self, mcp_server):
        """mailbox_address stays present and null when the scope is missing."""
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
                result = await client.call_tool("get_profile", {})

        data = _structured(result)
        assert data["display_name"] == "Test User"
        assert data["mailbox_address"] is None

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_profile")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    # -- another directory user -------------------------------------------

    @respx.mock
    async def test_no_arguments_keep_todays_request_trail_and_keys(self, mcp_server):
        """The contract pin: extra params must not change the default call."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile")

        assert _graph_trail() == [("GET", "/v1.0/me"), ("GET", "/v1.0/me/mailboxSettings")]
        assert set(_structured(result)) == {
            "id",
            "display_name",
            "mail",
            "user_principal_name",
            "mailbox_address",
            "job_title",
        }

    @respx.mock
    async def test_a_user_is_read_from_the_directory(self, mcp_server):
        """No mailboxSettings call for someone else, so mailbox_address is null."""
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_USER)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"user": DIRECTORY_USER_ID})

        assert _structured(result) == {
            "id": "user-id-002",
            "display_name": "Ada Lovelace",
            "mail": "ada@example.com",
            "user_principal_name": "ada@example.com",
            "mailbox_address": None,
            "job_title": "Engineer",
        }
        assert _graph_trail() == [("GET", f"/v1.0/users/{DIRECTORY_USER_ID}")]

    @respx.mock
    async def test_an_unknown_user_is_user_not_found(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"user": DIRECTORY_USER_ID})

        assert _structured(result) == {"error": "user_not_found"}

    @respx.mock
    async def test_a_user_graph_cannot_parse_is_invalid_arguments(self, mcp_server):
        """A 400 is permanent, so it is an error dict rather than a tool error."""
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": "Request_BadRequest", "message": "bad id"}}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"user": DIRECTORY_USER_ID})

        data = _structured(result)
        assert data["error"] == "invalid_arguments"
        assert DIRECTORY_USER_ID in data["reason"]

    @respx.mock
    async def test_a_blank_user_is_the_signed_in_user(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"user": "   "})

        assert _structured(result)["id"] == "user-id-001"
        assert _graph_trail() == [("GET", "/v1.0/me"), ("GET", "/v1.0/me/mailboxSettings")]

    # -- deployed without User.ReadBasic.All -------------------------------
    #
    # Every one of these must return the scope error ALONE: no profile data,
    # no photo keys, no exception, whichever request 403s.

    @respx.mock
    async def test_a_403_on_the_user_lookup_is_the_scope_error_alone(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"user": DIRECTORY_USER_ID})

        assert _structured(result) == SCOPE_MISSING

    @respx.mock
    async def test_a_403_on_the_lookup_never_reaches_the_photo(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "get_profile", {"user": DIRECTORY_USER_ID, "photo": "bytes"}
            )

        assert _structured(result) == SCOPE_MISSING
        assert _graph_trail() == [("GET", f"/v1.0/users/{DIRECTORY_USER_ID}")]

    @respx.mock
    async def test_a_403_on_a_users_photo_metadata_drops_the_fetched_profile(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_USER)
        )
        respx.get(USER_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server, "get_profile", {"user": DIRECTORY_USER_ID, "photo": "metadata"}
            )

        assert _structured(result) == SCOPE_MISSING

    @respx.mock
    async def test_a_403_on_a_users_photo_bytes_drops_the_fetched_profile(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_USER)
        )
        respx.get(USER_PHOTO_URL).mock(return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA))
        respx.get(USER_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server, "get_profile", {"user": DIRECTORY_USER_ID, "photo": "bytes"}
            )

        assert _structured(result) == SCOPE_MISSING

    @respx.mock
    async def test_a_403_on_my_own_photo_metadata_is_the_scope_error_alone(self, mcp_server):
        """A tenant policy can deny even /me/photo; the profile is dropped."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "metadata"})

        assert _structured(result) == SCOPE_MISSING

    @respx.mock
    async def test_a_403_on_my_own_photo_bytes_is_the_scope_error_alone(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA))
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        assert _structured(result) == SCOPE_MISSING

    # -- photo metadata ----------------------------------------------------

    @respx.mock
    async def test_photo_metadata_adds_the_four_photo_keys(self, mcp_server):
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "metadata"})

        assert _structured(result) == {
            "id": "user-id-001",
            "display_name": "Test User",
            "mail": "user@example.com",
            "user_principal_name": "user@example.com",
            "mailbox_address": "mailbox@example.com",
            "job_title": None,
            "has_photo": True,
            "photo_width": 256,
            "photo_height": 256,
            "photo_content_type": "image/jpeg",
        }

    @respx.mock
    async def test_no_photo_set_is_a_clean_result_not_an_error(self, mcp_server):
        _mock_me_with_photo(None)
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "metadata"})

        data = _structured(result)
        assert data["has_photo"] is False
        assert data["photo_width"] is None
        assert data["photo_height"] is None
        assert data["photo_content_type"] is None
        assert _graph_trail()[-1] == ("GET", "/v1.0/me/photo")

    # -- photo bytes -------------------------------------------------------

    @respx.mock
    async def test_photo_bytes_default_to_the_240_variant(self, mcp_server):
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        data = _structured(result)
        assert _graph_trail() == [
            ("GET", "/v1.0/me"),
            ("GET", "/v1.0/me/mailboxSettings"),
            ("GET", "/v1.0/me/photo"),
            ("GET", "/v1.0/me/photos/240x240/$value"),
        ]
        assert base64.b64decode(data["content_base64"]) == PHOTO_BYTES
        assert data["content_type"] == "image/png"
        assert data["size"] == len(PHOTO_BYTES)
        assert data["photo_size"] == "240x240"
        assert set(data) == {
            "id",
            "display_name",
            "mail",
            "user_principal_name",
            "mailbox_address",
            "job_title",
            "has_photo",
            "photo_width",
            "photo_height",
            "photo_content_type",
            "content_base64",
            "content_type",
            "size",
            "photo_size",
        }

    @respx.mock
    async def test_photo_size_original_reads_the_stored_image(self, mcp_server):
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_ORIGINAL_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server, "get_profile", {"photo": "bytes", "photo_size": "original"}
            )

        assert _graph_trail()[-1] == ("GET", "/v1.0/me/photo/$value")
        assert _structured(result)["photo_size"] == "original"

    @respx.mock
    async def test_a_missing_content_type_falls_back_to_the_metadata_mime(self, mcp_server):
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(200, content=PHOTO_BYTES))
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        assert _structured(result)["content_type"] == "image/jpeg"

    @respx.mock
    async def test_photo_bytes_with_no_photo_leaves_every_added_key_null(self, mcp_server):
        _mock_me_with_photo(None)
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        data = _structured(result)
        assert data["has_photo"] is False
        assert data["content_base64"] is None
        assert data["content_type"] is None
        assert data["size"] is None
        assert data["photo_size"] is None
        assert not [path for _, path in _graph_trail() if path.endswith("$value")]

    @respx.mock
    async def test_a_photo_that_vanishes_between_requests_is_reported_absent(self, mcp_server):
        """Metadata said yes, $value said 404: every photo key agrees on "none"."""
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        data = _structured(result)
        assert data["has_photo"] is False
        assert data["photo_width"] is None
        assert data["photo_content_type"] is None
        assert data["content_base64"] is None
        assert data["photo_size"] is None

    @respx.mock
    async def test_a_users_photo_bytes_walk_the_users_paths(self, mcp_server):
        respx.get(url__startswith=f"{USER_URL_PREFIX}?").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_USER)
        )
        respx.get(USER_PHOTO_URL).mock(return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA))
        respx.get(USER_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server, "get_profile", {"user": DIRECTORY_USER_ID, "photo": "bytes"}
            )

        assert _graph_trail() == [
            ("GET", f"/v1.0/users/{DIRECTORY_USER_ID}"),
            ("GET", f"/v1.0/users/{DIRECTORY_USER_ID}/photo"),
            ("GET", f"/v1.0/users/{DIRECTORY_USER_ID}/photos/240x240/$value"),
        ]
        assert base64.b64decode(_structured(result)["content_base64"]) == PHOTO_BYTES

    @respx.mock
    async def test_the_photo_mode_word_tolerates_case_and_space(self, mcp_server):
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "Bytes "})

        assert _structured(result)["photo_size"] == "240x240"

    # -- rejected before the token -----------------------------------------

    @respx.mock
    async def test_an_unknown_photo_mode_is_rejected_without_a_token(self, mcp_server):
        result = await _call(mcp_server, "get_profile", {"photo": "thumbnail"})

        data = _structured(result)
        assert data["error"] == "invalid_photo"
        assert "metadata, bytes" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_a_size_graph_does_not_serve_is_rejected_without_a_token(self, mcp_server):
        result = await _call(mcp_server, "get_profile", {"photo": "bytes", "photo_size": "100x100"})

        data = _structured(result)
        assert data["error"] == "invalid_photo_size"
        assert "48x48" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_a_size_without_the_bytes_mode_is_rejected(self, mcp_server):
        result = await _call(
            mcp_server, "get_profile", {"photo": "metadata", "photo_size": "96x96"}
        )

        assert _structured(result) == {
            "error": "invalid_photo_size",
            "reason": "photo_size only applies to photo='bytes'",
        }
        assert _graph_trail() == []

    # -- caps and connection ------------------------------------------------

    @respx.mock
    async def test_a_photo_over_the_json_cap_is_refused(self, mcp_server, monkeypatch):
        from ms_graph import attachments as attachment_ops

        monkeypatch.setattr(attachment_ops, "MAX_JSON_ATTACHMENT_BYTES", 4)
        _mock_me_with_photo(SAMPLE_PHOTO_METADATA)
        respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        assert _structured(result) == {
            "error": "too_large",
            "size": len(PHOTO_BYTES),
            "limit": 4,
        }

    async def test_not_connected_with_a_photo_request(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_profile", {"photo": "bytes"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class TestMCPEmailTools:
    """Test consolidated email MCP tools."""

    @respx.mock
    async def test_list_emails_no_query(self, mcp_server):
        """No query → lists the inbox, one row per message."""
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        data = _structured(result)
        assert data["messages"][0] == MSG_ROW
        assert [row["subject"] for row in data["messages"]] == [
            "Weekly Report",
            "Re: Project Update",
        ]
        assert data["count"] == 2
        assert data["folder"] == "inbox"
        assert data["query"] == ""
        assert data["marked_read"] == 0
        assert data["notice"] == ""

    @respx.mock
    async def test_list_emails_with_query(self, mcp_server):
        """query set → search mode; the query comes back in the payload."""
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"query": "weekly"})

        data = _structured(result)
        assert data["messages"] == [MSG_ROW]
        assert data["count"] == 1
        assert data["query"] == "weekly"

    @respx.mock
    async def test_list_emails_search_no_results(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"query": "nonexistent"})

        data = _structured(result)
        assert data["messages"] == []
        assert data["count"] == 0
        assert data["query"] == "nonexistent"

    @respx.mock
    async def test_list_emails_empty_inbox(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {})

        assert _structured(result) == {
            "messages": [],
            "count": 0,
            "folder": "inbox",
            "query": "",
            "marked_read": 0,
            "notice": "",
        }

    @respx.mock
    async def test_list_emails_custom_folder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"folder": "sentitems"})

        data = _structured(result)
        assert data["count"] == 2
        assert data["folder"] == "sentitems"

    @respx.mock
    async def test_list_emails_custom_display_name_resolves(self, mcp_server):
        """A custom folder display name resolves to its ID, then lists messages (#54)."""
        folder_id = SAMPLE_MAIL_FOLDER["id"]  # "AQMkAGfolder-001", displayName "Projects"
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/{folder_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"folder": "projects"})

        data = _structured(result)
        assert data["count"] == 2
        assert data["folder"] == "projects"  # the payload keeps the display name

    @respx.mock
    async def test_list_emails_unknown_folder_returns_clear_error(self, mcp_server):
        """An unresolvable folder name returns a human-readable error, not a raw 400 (#54)."""
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"folder": "ghost"})

        assert _structured(result) == {
            "error": "folder_not_found",
            "reason": "Folder 'ghost' not found.",
        }

    @respx.mock
    async def test_list_emails_well_known_skips_folder_lookup(self, mcp_server):
        """Well-known folders resolve without hitting the mailFolders list endpoint (#54)."""
        list_route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool("list_emails", {"folder": "sentitems"})

        assert not list_route.called

    @respx.mock
    async def test_list_emails_query_scopes_to_custom_folder(self, mcp_server):
        """query + explicit custom folder scopes the search to that folder (#54)."""
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        scoped = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/{folder_id}/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                result = await client.call_tool(
                    "list_emails", {"query": "report", "folder": "projects"}
                )

        assert scoped.called
        scoped_url = str(scoped.calls[0].request.url)
        assert "$search" in scoped_url or "%24search" in scoped_url
        data = _structured(result)
        assert (data["count"], data["query"]) == (1, "report")

    @respx.mock
    async def test_list_emails_query_default_inbox_stays_global(self, mcp_server):
        """query with the default inbox folder searches globally, no folder lookup (#54)."""
        list_route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        global_route = respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        with _mock_token():
            from fastmcp import Client

            async with Client(mcp_server) as client:
                await client.call_tool("list_emails", {"query": "report"})

        assert global_route.called
        assert not list_route.called

    @respx.mock
    async def test_list_emails_pagination(self, mcp_server):
        """Pagination follows @odata.nextLink to fetch all pages."""
        responses = iter(
            [
                httpx.Response(200, json=SAMPLE_MESSAGES_PAGE1),
                httpx.Response(200, json=SAMPLE_MESSAGES_PAGE2),
            ]
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            side_effect=lambda req: next(responses)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 1000})

        data = _structured(result)
        assert [row["subject"] for row in data["messages"]] == [
            "Weekly Report",
            "Re: Project Update",
        ]
        assert data["count"] == 2

    @respx.mock
    async def test_list_emails_row_keys_and_values(self, mcp_server):
        """Every row key is spelled out, and the booleans stay booleans."""
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        rows = _structured(result)["messages"]
        assert list(rows[0]) == [
            "date",
            "from_name",
            "from_address",
            "to",
            "subject",
            "is_read",
            "body_preview",
            "has_attachments",
            "id",
        ]
        assert rows[0] == MSG_ROW
        assert rows[1]["subject"] == "Re: Project Update"
        assert rows[1]["is_read"] is True

    @respx.mock
    async def test_list_emails_requests_has_attachments(self, mcp_server):
        """The attachment flag has to be $selected or Graph omits it (#Phase 2)."""
        route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(
                200, json={"value": [{**SAMPLE_MESSAGE, "hasAttachments": True}]}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        select = parse_qs(urlparse(str(route.calls[0].request.url)).query)["$select"][0]
        assert "hasAttachments" in select
        assert _structured(result)["messages"][0]["has_attachments"] is True

    @respx.mock
    async def test_list_emails_pipe_in_subject_is_carried_verbatim(self, mcp_server):
        """Pipes are the compact renderer's delimiter; the dict keeps them raw."""
        msg_with_pipe = {
            **SAMPLE_MESSAGE,
            "subject": "Re: Q4 | Budget Review",
            "bodyPreview": "Preview with | pipe char",
        }
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json={"value": [msg_with_pipe]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        row = _structured(result)["messages"][0]
        assert row["subject"] == "Re: Q4 | Budget Review"
        assert row["body_preview"] == "Preview with | pipe char"

    @respx.mock
    async def test_list_emails_with_mark_as_read(self, mcp_server):
        msg_id = "AAMkAGI2TG93AAA="
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        patch_route = respx.patch(f"{GRAPH_BASE_URL}/me/messages/{msg_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MESSAGE, "isRead": True})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "list_emails",
                {"top": 10, "options": f'{{"mark_as_read": ["{msg_id}"]}}'},
            )

        data = _structured(result)
        assert data["count"] == 1
        assert data["marked_read"] == 1
        assert patch_route.called
        assert json.loads(patch_route.calls[0].request.content) == {"isRead": True}

    @respx.mock
    async def test_list_emails_mark_as_read_rejects_non_array(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        patch_route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "list_emails",
                {"top": 10, "options": '{"mark_as_read": "single-id"}'},
            )

        assert _structured(result) == {
            "error": "invalid_options",
            "reason": "Option 'mark_as_read' must be a JSON array of message IDs.",
        }
        assert not patch_route.called

    @respx.mock
    async def test_list_emails_bad_options_json(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"options": "not json"})

        assert _structured(result)["error"] == "invalid_options"
        assert _graph_trail() == []

    async def test_list_emails_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_emails", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_send_email_returns_the_sent_ids(self, mcp_server):
        """The ack carries every id the caller can match Sent Items on."""
        route = _mock_send_email()
        with _mock_token(), patch("ms_graph_mcp._utcnow_iso", return_value="2026-09-06T12:00:00Z"):
            result = await _call(
                mcp_server,
                "send_email",
                {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
            )

        assert _structured(result) == {
            "ok": True,
            "id": SENT_DRAFT["id"],
            "conversation_id": SENT_DRAFT["conversationId"],
            "internet_message_id": SENT_DRAFT["internetMessageId"],
            "subject": "Hi",
            "to": "alice@example.com",
            "cc": "",
            "bcc_count": 0,
            "from": "",
            "attachments": "",
            "sent_at": "2026-09-06T12:00:00Z",
        }
        assert _sent_payload(route)["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_always_goes_through_a_draft(self, mcp_server):
        """sendMail answers 202 with no body, so it can never report the ids."""
        send_mail = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(
            return_value=httpx.Response(202)
        )
        _mock_send_email()
        with _mock_token():
            await _call(
                mcp_server,
                "send_email",
                {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
            )

        assert not send_mail.called
        assert _graph_trail() == [
            ("POST", "/v1.0/me/messages"),
            ("POST", f"/v1.0/me/messages/{SENT_DRAFT['id']}/send"),
        ]

    async def test_send_email_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server, "send_email", {"to": "a@b.com", "subject": "S", "body": "B"}
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_send_email_html_body_auto_detected(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "HTML test",
                    "body": "<p>Hello <strong>Alice</strong>! Click <a href='https://example.com'>here</a>.</p>",
                },
            )

        assert _sent_payload(route)["body"]["contentType"] == "HTML"

    @respx.mock
    async def test_send_email_placeholder_not_mistaken_for_html(self, mcp_server):
        """'Dear <FirstName>,' must stay Text."""
        route = _mock_send_email()
        with _mock_token():
            await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Template",
                    "body": "Dear <FirstName>, thanks for reaching out.",
                },
            )

        assert _sent_payload(route)["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_with_cc(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"cc": "bob@example.com"}',
                },
            )

        assert _structured(result)["cc"] == "bob@example.com"
        assert len(_sent_payload(route)["ccRecipients"]) == 1

    @respx.mock
    async def test_send_email_with_bcc(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"bcc": "hidden@example.com,secret@example.com"}',
                },
            )

        data = _structured(result)
        assert data["bcc_count"] == 2
        # BCC addresses must not come back out of the tool.
        assert "hidden@example.com" not in json.dumps(data)
        payload = _sent_payload(route)
        assert len(payload["bccRecipients"]) == 2
        assert payload["bccRecipients"][0]["emailAddress"]["address"] == "hidden@example.com"

    @respx.mock
    async def test_send_email_multiple_recipients(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {"to": "alice@example.com, bob@example.com", "subject": "Hi", "body": "Hello!"},
            )

        assert _structured(result)["to"] == "alice@example.com, bob@example.com"
        assert len(_sent_payload(route)["toRecipients"]) == 2

    @respx.mock
    async def test_send_email_explicit_text_overrides_html(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "S",
                    "body": "<p>HTML</p>",
                    "options": '{"body_type": "Text"}',
                },
            )

        assert _sent_payload(route)["body"]["contentType"] == "Text"

    @respx.mock
    async def test_send_email_no_from_by_default(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            await _call(
                mcp_server,
                "send_email",
                {"to": "alice@example.com", "subject": "Hi", "body": "Hello!"},
            )

        assert "from" not in _sent_payload(route)

    @respx.mock
    async def test_send_email_with_from_address(self, mcp_server):
        route = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"from_address": "mailbox@example.com"}',
                },
            )

        assert _structured(result)["from"] == "mailbox@example.com"
        assert _sent_payload(route)["from"]["emailAddress"]["address"] == "mailbox@example.com"

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

    @respx.mock
    async def test_send_email_with_text_and_base64_attachments(self, mcp_server):
        """Attachments add an attach POST per file between create and send."""
        create = _mock_send_email()
        attach = respx.post(f"{DRAFT_BASE}/attachments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )

        specs = [
            {"name": "notes.txt", "text": "hello"},
            {"name": "img.png", "base64": base64.b64encode(b"\x89PNG").decode("ascii")},
        ]
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": json.dumps({"attachments": specs}),
                },
            )

        assert create.called
        payloads = [json.loads(call.request.content) for call in attach.calls]
        assert [p["name"] for p in payloads] == ["notes.txt", "img.png"]
        assert all(p["@odata.type"] == "#microsoft.graph.fileAttachment" for p in payloads)
        assert base64.b64decode(payloads[0]["contentBytes"]) == b"hello"

        data = _structured(result)
        assert data["ok"] is True
        assert data["attachments"] == "notes.txt (5 B), img.png (4 B)"
        draft_path = f"/v1.0/me/messages/{SENT_DRAFT['id']}"
        assert _graph_trail() == [
            ("POST", "/v1.0/me/messages"),
            ("POST", f"{draft_path}/attachments"),
            ("POST", f"{draft_path}/attachments"),
            ("POST", f"{draft_path}/send"),
        ]

    @respx.mock
    async def test_send_email_bad_attachment_spec_sends_nothing(self, mcp_server):
        create = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"attachments": [{"name": "x"}]}',
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_attachments"
        assert "attachments[0]:" in data["reason"]
        assert not create.called

    @respx.mock
    async def test_send_email_attachments_must_be_an_array(self, mcp_server):
        create = _mock_send_email()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"attachments": "nope"}',
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_attachments"
        assert "attachments must be a JSON array" in data["reason"]
        assert not create.called

    @respx.mock
    async def test_send_email_empty_attachment_list_attaches_nothing(self, mcp_server):
        """An empty list is not "attachments" — no attach request, no ack entry."""
        create = _mock_send_email()
        attach = respx.post(f"{DRAFT_BASE}/attachments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "alice@example.com",
                    "subject": "Hi",
                    "body": "Hello!",
                    "options": '{"attachments": []}',
                },
            )

        assert create.called
        assert not attach.called
        assert _structured(result)["attachments"] == ""


class TestMCPReadEmail:
    """read_email — one detail fetch, one canonical dict."""

    # Every read goes through the same percent-encoded detail URL; the body and
    # the attachment metadata arrive together on its $expand.
    DETAIL_URL = f"{GRAPH_BASE_URL}/me/messages/{quote(ATT_MSG_ID, safe='')}"
    PLAIN = {**SAMPLE_READ_DETAIL, "hasAttachments": False, "attachments": []}
    HEADERS = {
        "message-id": "<abc123@example.com>",
        "in-reply-to": "<parent@example.com>",
        # "Received" appeared twice with different casing; the first one wins.
        "received": "from mx1.example.com",
    }

    def _mock_detail(self, payload=None):
        return respx.get(self.DETAIL_URL).mock(
            return_value=httpx.Response(200, json=payload or self.PLAIN)
        )

    @respx.mock
    async def test_plain_body_flattens_the_whole_envelope(self, mcp_server):
        self._mock_detail()
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        assert _structured(result) == {
            "subject": "Weekly Report",
            "from_name": "Alice Smith",
            "from_address": "alice@example.com",
            "to": [{"name": "Bob Jones", "address": "bob@example.com"}],
            "cc": [{"name": None, "address": "carol@example.com"}],
            "received": "2025-12-15T10:30:00Z",
            "is_read": False,
            "is_draft": False,
            "body_text": "Here is the weekly report.\n\nBest,\nAlice",
            "headers": self.HEADERS,
            "has_attachments": False,
            "attachments": [],
            "attachment_count": 0,
        }

    @respx.mock
    async def test_attachments_ride_the_body_fetch(self, mcp_server):
        """One request carries body and attachment metadata; there is no listing call."""
        route = self._mock_detail(SAMPLE_READ_DETAIL)
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        data = _structured(result)
        assert route.call_count == 1
        assert _graph_trail() == [("GET", f"/v1.0/me/messages/{ATT_MSG_ID}")]
        assert data["has_attachments"] is True
        assert data["attachment_count"] == 3
        assert [a["name"] for a in data["attachments"]] == [
            "report.pdf",
            "logo.png",
            "Q4 Plan.docx",
        ]
        assert data["attachments"][1]["is_inline"] is True
        assert data["attachments"][2]["kind"] == "reference"

    @respx.mock
    async def test_mark_as_read_patches_and_acknowledges(self, mcp_server):
        self._mock_detail()
        patch_route = respx.patch(self.DETAIL_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_MESSAGE, "isRead": True})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {"message_id": ATT_MSG_ID, "options": '{"mark_as_read": true}'},
            )

        assert _structured(result)["marked_as_read"] is True
        assert json.loads(patch_route.calls[0].request.content) == {"isRead": True}
        assert _graph_trail() == [
            ("GET", f"/v1.0/me/messages/{ATT_MSG_ID}"),
            ("PATCH", f"/v1.0/me/messages/{ATT_MSG_ID}"),
        ]

    @respx.mock
    async def test_mark_as_unread_acknowledges_the_other_way(self, mcp_server):
        self._mock_detail()
        patch_route = respx.patch(self.DETAIL_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_MESSAGE, "isRead": False})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {"message_id": ATT_MSG_ID, "options": '{"mark_as_read": false}'},
            )

        assert _structured(result)["marked_as_read"] is False
        assert json.loads(patch_route.calls[0].request.content) == {"isRead": False}

    @respx.mock
    async def test_no_mark_option_leaves_the_key_off(self, mcp_server):
        self._mock_detail()
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        assert "marked_as_read" not in _structured(result)

    @respx.mock
    async def test_shaping_options_become_hints_and_leave_the_dict_whole(self, mcp_server):
        """The canonical body and header map are unchanged; the options only
        travel as hints for the compact renderer."""
        self._mock_detail(SAMPLE_READ_DETAIL)
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {
                    "message_id": ATT_MSG_ID,
                    "options": json.dumps(
                        {
                            "max_content_length": 5,
                            "include_headers": True,
                            "include_inline": True,
                        }
                    ),
                },
            )

        data = _structured(result)
        assert data["body_text"] == "Here is the weekly report.\n\nBest,\nAlice"
        assert data["headers"] == self.HEADERS
        assert data["max_content_length"] == 5
        assert data["include_headers"] is True
        assert data["include_inline"] is True

    @respx.mock
    async def test_unset_shaping_options_leave_no_hint_keys(self, mcp_server):
        self._mock_detail()
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        data = _structured(result)
        for key in ("max_content_length", "include_headers", "include_inline"):
            assert key not in data

    @respx.mock
    async def test_full_body_asks_for_the_thread_and_returns_it(self, mcp_server):
        route = self._mock_detail(
            {
                **self.PLAIN,
                "body": {"contentType": "text", "content": "Here is the weekly report.\n\n> older"},
            }
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {"message_id": ATT_MSG_ID, "options": '{"full_body": true}'},
            )

        assert "body" in _select_of(route.calls[0].request).split(",")
        assert _structured(result)["body_text"] == "Here is the weekly report.\n\n> older"

    @respx.mock
    async def test_mailbox_reads_the_shared_mailbox(self, mcp_server):
        route = respx.get(
            url__startswith=f"{GRAPH_BASE_URL}/users/support@example.com/messages"
        ).mock(return_value=httpx.Response(200, json=self.PLAIN))
        with _mock_token():
            await _call(
                mcp_server,
                "read_email",
                {"message_id": ATT_MSG_ID, "mailbox": "support@example.com"},
            )

        assert route.called

    @respx.mock
    async def test_invalid_options_returns_a_dict(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "read_email", {"message_id": ATT_MSG_ID, "options": "not json"}
            )

        assert _structured(result)["error"] == "invalid_options"
        assert _graph_trail() == []

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPGetMailAttachment:
    """get_mail_attachment — the modes and parameters, as canonical dicts."""

    @respx.mock
    async def test_metadata_mode_fetches_no_content(self, mcp_server):
        value_route = respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        respx.get(ATT_FILE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "metadata",
                },
            )

        assert _structured(result) == {
            "id": SAMPLE_FILE_ATTACHMENT["id"],
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size": 1_258_291,
            "is_inline": False,
            "content_id": None,
            "kind": "file",
            "source_url": None,
        }
        assert not value_route.called

    @respx.mock
    async def test_text_is_the_default_mode(self, mcp_server):
        docx = _docx_bytes()
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=docx, headers={"Content-Type": DOCX_MIME})
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "report.docx",
                    "contentType": DOCX_MIME,
                    "size": len(docx),
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]},
            )

        data = _structured(result)
        assert "Quarterly Title" in data["text"]
        assert data["truncated"] is False
        assert "reason" not in data
        assert data["name"] == "report.docx"
        assert data["kind"] == "file"

    @respx.mock
    async def test_text_mode_refuses_an_oversized_file_without_downloading(self, mcp_server):
        value_route = respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_FILE_ATTACHMENT, "size": 60_000_000})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]},
            )

        data = _structured(result)
        assert data["text"] is None
        assert data["reason"] == "too_large"
        assert data["truncated"] is False
        assert not value_route.called

    @respx.mock
    async def test_text_mode_on_an_html_item_falls_back_to_the_preview(self, mcp_server):
        inner = {
            "subject": "Budget draft",
            "bodyPreview": "Numbers attached",
            "body": {"contentType": "html", "content": "<p>Numbers attached</p>"},
        }

        def _respond(request):
            # Graph query strings arrive percent-encoded: "$" is "%24".
            if "expand" in str(request.url):
                return httpx.Response(200, json={**SAMPLE_ITEM_ATTACHMENT, "item": inner})
            return httpx.Response(200, json=SAMPLE_ITEM_ATTACHMENT)

        respx.get(url__startswith=ATT_ITEM_URL).mock(side_effect=_respond)
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"]},
            )

        assert _structured(result) == {
            "id": SAMPLE_ITEM_ATTACHMENT["id"],
            "name": "FW: Budget",
            "content_type": "",
            "size": 32_768,
            "is_inline": False,
            "content_id": None,
            "kind": "item",
            "source_url": None,
            "item_subject": "Budget draft",
            "item_from": None,
            "item_received": None,
            "text": "Numbers attached",
            "truncated": True,
        }

    @respx.mock
    async def test_onedrive_mode_uploads_and_returns_the_link(self, mcp_server):
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
            )
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_FILE_ATTACHMENT, "size": 8})
        )
        upload = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Attachments/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "onedrive",
                },
            )

        assert upload.calls[0].request.content == b"%PDF-1.7"
        assert _structured(result) == {
            "id": SAMPLE_FILE_ATTACHMENT["id"],
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size": 8,
            "is_inline": False,
            "content_id": None,
            "kind": "file",
            "source_url": None,
            "item_id": SAMPLE_UPLOADED_FILE["id"],
            "web_url": SAMPLE_UPLOADED_FILE["webUrl"],
        }
        assert [method for method, _ in _graph_trail()] == ["GET", "GET", "PUT"]

    @respx.mock
    async def test_onedrive_mode_saves_an_attached_message_as_eml(self, mcp_server):
        respx.get(f"{ATT_ITEM_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"From: dana@example.com")
        )
        respx.get(ATT_ITEM_URL).mock(
            return_value=httpx.Response(
                200, json={**SAMPLE_ITEM_ATTACHMENT, "name": "Budget", "size": 22}
            )
        )
        upload = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Attachments/Budget.eml:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"],
                    "mode": "onedrive",
                },
            )

        assert upload.called
        data = _structured(result)
        assert data["name"] == "Budget.eml"
        assert data["content_type"] == "message/rfc822"
        assert data["item_id"] == SAMPLE_UPLOADED_FILE["id"]
        assert data["web_url"] == SAMPLE_UPLOADED_FILE["webUrl"]

    @respx.mock
    async def test_onedrive_mode_honors_folder_path_option(self, mcp_server):
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7")
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_FILE_ATTACHMENT, "size": 8})
        )
        upload = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Inbox/Files/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "onedrive",
                    "options": '{"folder_path": "Inbox/Files"}',
                },
            )

        assert upload.called

    @respx.mock
    async def test_a_link_attachment_has_no_bytes_to_save(self, mcp_server):
        respx.get(ATT_REF_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_REFERENCE_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_REFERENCE_ATTACHMENT["id"],
                    "mode": "onedrive",
                },
            )

        assert _structured(result) == {
            "error": "reference",
            "source_url": SAMPLE_REFERENCE_ATTACHMENT["sourceUrl"],
        }

    @respx.mock
    async def test_base64_is_not_a_mode(self, mcp_server):
        """base64 is not one of this tool's modes; the four valid ones are."""
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": "a", "mode": "base64"},
            )

        assert _structured(result) == {
            "error": "invalid_mode",
            "reason": "mode must be one of: metadata, text, bytes, onedrive; got 'base64'",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_bad_options_json_is_refused_before_any_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": "a", "options": "not json"},
            )

        assert _structured(result)["error"] == "invalid_options"
        assert _graph_trail() == []

    @respx.mock
    async def test_shared_mailbox_reads_the_users_path(self, mcp_server):
        base = (
            f"{GRAPH_BASE_URL}/users/support@example.com/messages/"
            f"{quote(ATT_MSG_ID, safe='')}/attachments/"
            f"{quote(SAMPLE_FILE_ATTACHMENT['id'], safe='')}"
        )
        respx.get(f"{base}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(base).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "notes.txt",
                    "contentType": "text/plain",
                    "size": 5,
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mailbox": "support@example.com",
                },
            )

        assert _structured(result)["text"] == "hello"
        assert all(
            path.startswith("/v1.0/users/support@example.com/messages/")
            for _, path in _graph_trail()
        )

    @respx.mock
    async def test_an_external_sender_hides_the_attachment(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        meta_route = respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]},
            )

        assert _structured(result) == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert not meta_route.called

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": "a"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

_CALENDAR_VIEW_URL = f"{GRAPH_BASE_URL}/me/calendarView"
_EVENTS_URL = f"{GRAPH_BASE_URL}/me/events"
_SCHEDULE_URL = f"{GRAPH_BASE_URL}/me/calendar/getSchedule"


class TestMCPListCalendarEvents:
    """list_calendar_events — one row per event."""

    @respx.mock
    async def test_lists_events(self, mcp_server):
        respx.get(_CALENDAR_VIEW_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENTS_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "list_calendar_events",
                {"start_date": "2026-05-08T00:00:00Z", "end_date": "2026-05-09T00:00:00Z"},
            )

        data = _structured(result)
        assert data["count"] == 2
        assert data["events"][0] == {
            "subject": "Sprint Planning",
            "start": "2026-05-08T10:00:00.0000000",
            "end": "2026-05-08T11:00:00.0000000",
            "timezone": "UTC",
            "organizer": "Alice Smith",
            "location": "Conference Room A",
            "online_url": "https://teams.microsoft.com/meet/123",
            "is_all_day": False,
            "is_cancelled": False,
            "id": "AAMkAGI2-event-001",
        }
        assert data["events"][1]["is_all_day"] is True
        assert data["events"][1]["online_url"] == ""
        assert _graph_trail() == [("GET", "/v1.0/me/calendarView")]

    @respx.mock
    async def test_empty_range(self, mcp_server):
        respx.get(_CALENDAR_VIEW_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        with _mock_token():
            result = await _call(mcp_server, "list_calendar_events", {})

        assert _structured(result) == {"events": [], "count": 0}

    @respx.mock
    async def test_default_range_is_the_next_seven_days(self, mcp_server):
        """No dates given: start is now, end is start + 7 days."""
        route = respx.get(_CALENDAR_VIEW_URL).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            await _call(mcp_server, "list_calendar_events", {})

        params = route.calls[0].request.url.params
        start = datetime.fromisoformat(params["startDateTime"])
        end = datetime.fromisoformat(params["endDateTime"])
        assert (end - start) == timedelta(days=7)

    @respx.mock
    async def test_unparseable_start_date_is_an_error_dict(self, mcp_server):
        """The default end is computed from start_date, so a bad one fails here."""
        route = respx.get(_CALENDAR_VIEW_URL).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_calendar_events", {"start_date": "next tuesday"})

        assert _structured(result)["error"] == "invalid_date"
        assert not route.called

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_calendar_events", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPGetCalendarEvent:
    """get_calendar_event — the attendee table plus the event's own fields."""

    @respx.mock
    async def test_full_event(self, mcp_server):
        event_id = SAMPLE_CALENDAR_EVENT["id"]
        respx.get(f"{_EVENTS_URL}/{event_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_calendar_event", {"event_id": event_id})

        assert _structured(result) == {
            "attendees": [
                {"name": "Bob Jones", "address": "bob@example.com", "response": "accepted"}
            ],
            "subject": "Sprint Planning",
            "start": "2026-05-08T10:00:00.0000000",
            "end": "2026-05-08T11:00:00.0000000",
            "timezone": "UTC",
            "organizer_name": "Alice Smith",
            "organizer_address": "alice@example.com",
            "location": "Conference Room A",
            "online_url": "https://teams.microsoft.com/meet/123",
            "is_all_day": False,
            "recurrence": "",
            "id": event_id,
            "body_type": "text",
            "body_text": "Let's plan the sprint.\n\nAgenda:\n1. Review backlog",
        }

    @respx.mock
    async def test_no_attendees(self, mcp_server):
        event_id = SAMPLE_CALENDAR_EVENT_ALLDAY["id"]
        respx.get(f"{_EVENTS_URL}/{event_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT_ALLDAY)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_calendar_event", {"event_id": event_id})

        data = _structured(result)
        assert data["attendees"] == []
        assert data["is_all_day"] is True

    @respx.mock
    async def test_max_content_length_truncates_the_body(self, mcp_server):
        event_id = SAMPLE_CALENDAR_EVENT["id"]
        respx.get(f"{_EVENTS_URL}/{event_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_CALENDAR_EVENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_calendar_event",
                {"event_id": event_id, "options": '{"max_content_length": 10}'},
            )

        assert _structured(result)["body_text"] == "Let's plan"

    @respx.mock
    async def test_html_body_is_labelled_not_prefixed(self, mcp_server):
        """body_type carries the fact; the text stays exactly what Graph sent."""
        event_id = SAMPLE_CALENDAR_EVENT["id"]
        respx.get(f"{_EVENTS_URL}/{event_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_CALENDAR_EVENT,
                    "body": {"contentType": "html", "content": "<p>Agenda</p>"},
                },
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_calendar_event", {"event_id": event_id})

        data = _structured(result)
        assert data["body_type"] == "html"
        assert data["body_text"] == "<p>Agenda</p>"

    @respx.mock
    async def test_recurrence_is_summarised(self, mcp_server):
        event_id = SAMPLE_CALENDAR_EVENT["id"]
        respx.get(f"{_EVENTS_URL}/{event_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_CALENDAR_EVENT,
                    "recurrence": {"pattern": {"type": "weekly", "interval": 2}},
                },
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "get_calendar_event", {"event_id": event_id})

        assert _structured(result)["recurrence"] == "weekly (every 2)"

    async def test_bad_options(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "get_calendar_event", {"event_id": "e1", "options": "not json"}
            )

        assert _structured(result)["error"] == "invalid_options"

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_calendar_event", {"event_id": "e1"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPCreateCalendarEvent:
    """create_calendar_event — the ack a caller books against."""

    @respx.mock
    async def test_creates_and_acks(self, mcp_server):
        route = respx.post(_EVENTS_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_EVENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "create_calendar_event",
                {
                    "subject": "Design Review",
                    "start_datetime": "2026-05-09T14:00:00",
                    "end_datetime": "2026-05-09T15:00:00",
                    "timezone": "America/Los_Angeles",
                    "options": json.dumps(
                        {"attendees": "bob@example.com", "is_online_meeting": True}
                    ),
                },
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["attendees"][0]["emailAddress"]["address"] == "bob@example.com"
        assert payload["isOnlineMeeting"] is True
        assert _structured(result) == {
            "ok": True,
            "id": "AAMkAGI2-event-new-001",
            "subject": "Design Review",
            "start": "2026-05-09T14:00:00.0000000",
            "end": "2026-05-09T15:00:00.0000000",
            "timezone": "America/Los_Angeles",
            "online_meeting_url": "https://teams.microsoft.com/meet/789",
            "web_link": "",
        }
        assert _graph_trail() == [("POST", "/v1.0/me/events")]

    async def test_bad_options(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "create_calendar_event",
                {
                    "subject": "S",
                    "start_datetime": "2026-05-09T14:00:00",
                    "end_datetime": "2026-05-09T15:00:00",
                    "options": "not json",
                },
            )

        assert _structured(result)["error"] == "invalid_options"

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "create_calendar_event",
                {
                    "subject": "S",
                    "start_datetime": "2026-05-09T14:00:00",
                    "end_datetime": "2026-05-09T15:00:00",
                },
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPCheckAvailability:
    """check_availability — every busy block, plus a per-person free summary."""

    @respx.mock
    async def test_busy_rows_and_summary(self, mcp_server):
        respx.post(_SCHEDULE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SCHEDULE_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": "alice@example.com, bob@example.com",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        assert _structured(result) == {
            "busy": [
                {
                    "person": "alice@example.com",
                    "start": "2026-05-08T10:00:00.0000000",
                    "end": "2026-05-08T11:00:00.0000000",
                    "subject": "Sprint Planning",
                    "status": "busy",
                }
            ],
            "summary": (
                "alice@example.com: 75% free (12/16 slots); "
                "bob@example.com: 100% free (16/16 slots)"
            ),
            "busy_count": 1,
        }
        assert _graph_trail() == [("POST", "/v1.0/me/calendar/getSchedule")]

    @respx.mock
    async def test_every_block_is_listed(self, mcp_server):
        """The old renderer stopped at ten items; busy_count must be the truth."""
        items = [
            {
                "subject": f"Block {i}",
                "start": {"dateTime": f"2026-05-08T{i:02d}:00:00"},
                "end": {"dateTime": f"2026-05-08T{i:02d}:30:00"},
                "status": "busy",
            }
            for i in range(14)
        ]
        respx.post(_SCHEDULE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "scheduleId": "alice@example.com",
                            "availabilityView": "22",
                            "scheduleItems": items,
                        }
                    ]
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": "alice@example.com",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        data = _structured(result)
        assert data["busy_count"] == 14
        assert len(data["busy"]) == 14
        assert data["busy"][13]["subject"] == "Block 13"

    @respx.mock
    async def test_private_block_keeps_a_placeholder_subject(self, mcp_server):
        respx.post(_SCHEDULE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "scheduleId": "alice@example.com",
                            "availabilityView": "",
                            "scheduleItems": [
                                {"start": {"dateTime": "x"}, "end": {"dateTime": "y"}}
                            ],
                        }
                    ]
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": "alice@example.com",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        data = _structured(result)
        assert data["busy"][0]["subject"] == "(private)"
        assert data["summary"] == "alice@example.com: no slots"

    async def test_no_emails(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": " , ",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "No email addresses provided.",
        }

    @respx.mock
    async def test_no_schedules_returned(self, mcp_server):
        respx.post(_SCHEDULE_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        with _mock_token():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": "alice@example.com",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        assert _structured(result) == {
            "error": "no_data",
            "reason": "No availability information returned.",
        }

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "check_availability",
                {
                    "emails": "alice@example.com",
                    "start_datetime": "2026-05-08T09:00:00",
                    "end_datetime": "2026-05-08T17:00:00",
                },
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPTeamsTools:
    """Test consolidated Teams MCP tools."""

    @respx.mock
    async def test_list_teams_all(self, mcp_server):
        """No team_id → returns all joined teams."""
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_teams")

        assert _structured(result) == {
            "teams": [
                {"name": t["displayName"], "id": t["id"]} for t in SAMPLE_TEAMS_RESPONSE["value"]
            ],
            "count": 2,
        }

    @respx.mock
    async def test_list_teams_channels(self, mcp_server):
        """team_id set → returns channels for that team."""
        team_id = "team-id-001"
        respx.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNELS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_teams", {"team_id": team_id})

        assert _structured(result) == {
            "channels": [
                {"name": c["displayName"], "id": c["id"]} for c in SAMPLE_CHANNELS_RESPONSE["value"]
            ],
            "count": 2,
            "team_id": team_id,
        }

    @respx.mock
    async def test_list_teams_empty(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_teams")

        assert _structured(result) == {"teams": [], "count": 0}

    @respx.mock
    async def test_list_teams_not_available(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_teams")

        assert _structured(result) == {
            "error": "teams_not_available",
            "reason": (
                "Microsoft Teams is not available for this account. "
                "A Microsoft 365 license is required."
            ),
        }

    async def test_list_teams_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_teams")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_search_teams_messages_chat_hit(self, mcp_server):
        """A chat hit fills the seven columns with a chat: conversation label."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([SAMPLE_SEARCH_CHAT_HIT]))
        )
        respx.get(f"{SEARCH_CHAT_HYDRATE}/1750000000001").mock(
            return_value=httpx.Response(200, json=SAMPLE_HYDRATED_CHAT_MESSAGE)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result) == {
            "messages": [
                {
                    "timestamp": "2026-03-02T10:00:00Z",
                    "sender": "Alice Smith",
                    "conversation": f"chat:{SEARCH_CHAT_ID}",
                    "content": "The #budget2026 numbers are in",
                    "attachments": "",
                    "id": "1750000000001",
                    "link": "https://teams.microsoft.com/l/message/chat/1750000000001",
                }
            ],
            "count": 1,
            "query": "#budget2026",
            "since": "",
            "conversation_id": "",
            "skipped": 0,
            "notice": "",
        }

    @respx.mock
    async def test_search_teams_messages_channel_hit(self, mcp_server):
        """A channel hit labels the conversation channel:<team>/<channel>."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([SAMPLE_SEARCH_CHANNEL_HIT]))
        )
        respx.get(f"{SEARCH_CHANNEL_HYDRATE}/1750000000002").mock(
            return_value=httpx.Response(200, json=SAMPLE_HYDRATED_CHANNEL_MESSAGE)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        rows = _structured(result)["messages"]
        assert rows[0]["conversation"] == f"channel:{SEARCH_TEAM_ID}/{SEARCH_CHANNEL_ID}"

    @respx.mock
    async def test_search_teams_messages_channel_reply(self, mcp_server):
        """A channel thread reply is hydrated on the replies route, not skipped."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([SEARCH_REPLY_HIT]))
        )
        respx.get(f"{SEARCH_CHANNEL_HYDRATE}/{SEARCH_REPLY_ID}").mock(
            return_value=httpx.Response(400, json=SEARCH_IS_A_REPLY_400)
        )
        respx.get(SEARCH_REPLY_ROUTE).mock(return_value=httpx.Response(200, json=SEARCH_REPLY_BODY))
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        data = _structured(result)
        assert data["count"] == 1
        assert data["skipped"] == 0
        assert data["messages"][0] == {
            "timestamp": "2024-04-24T04:37:15Z",
            "sender": "Jimmy Wakimoto",
            "conversation": f"channel:{SEARCH_TEAM_ID}/{SEARCH_CHANNEL_ID}",
            "content": "Moving the #budget2026 thread here",
            "attachments": "",
            "id": SEARCH_REPLY_ID,
            "link": f"https://teams.microsoft.com/l/message/channel/{SEARCH_REPLY_ID}",
        }

    @respx.mock
    async def test_search_teams_messages_all_time_by_default(self, mcp_server):
        """No since means all time — and no sent>= clause reaches the index."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("m1")]))
        )
        _mock_search_hydration({"m1": _search_msg("m1")})
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result)["since"] == ""
        assert "sent>=" not in _recorded_query_string()

    @respx.mock
    async def test_search_teams_messages_since_is_normalized(self, mcp_server):
        """A bare date becomes midnight Zulu and a day-granular KQL clause."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("m1")]))
        )
        _mock_search_hydration({"m1": _search_msg("m1")})
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "since": "2026-01-01"},
            )

        assert _structured(result)["since"] == "2026-01-01T00:00:00Z"
        assert _recorded_query_string().endswith("sent>=2026-01-01")

    @respx.mock
    async def test_search_teams_messages_invalid_since(self, mcp_server):
        """A malformed cutoff is reported without touching Graph."""
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "since": "last tuesday"},
            )

        data = _structured(result)
        assert data["error"] == "invalid_date"
        assert "Invalid since format" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_search_teams_messages_empty_query(self, mcp_server):
        """A blank query is a caller error, not a search for everything."""
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "   "})

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Provide a search query.",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_search_teams_messages_invalid_options(self, mcp_server):
        """Options JSON is validated before any request."""
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "options": "not json"},
            )

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert "must be valid JSON" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_search_teams_messages_conversation_scope(self, mcp_server):
        """conversation_id filters hits client-side; the index cannot scope."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=search_response([SAMPLE_SEARCH_CHAT_HIT, SAMPLE_SEARCH_CHANNEL_HIT]),
            )
        )
        _mock_search_hydration(
            {
                "1750000000001": SAMPLE_HYDRATED_CHAT_MESSAGE,
                "1750000000002": SAMPLE_HYDRATED_CHANNEL_MESSAGE,
            }
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "conversation_id": SEARCH_CHAT_ID},
            )

        data = _structured(result)
        assert data["count"] == 1
        assert data["conversation_id"] == SEARCH_CHAT_ID
        assert len([m for m, _ in _graph_trail() if m == "GET"]) == 1

    @respx.mock
    async def test_search_teams_messages_exact_drops_stemmed_hit(self, mcp_server):
        """The index stems; the tool re-checks and says so when nothing survives."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("m1")]))
        )
        _mock_search_hydration({"m1": _search_msg("m1", content="budget20260 update")})
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        data = _structured(result)
        assert data["messages"] == []
        assert data["count"] == 0
        assert data["notice"] == (
            "The index matched 1 message(s) but none carried the hashtag literally; "
            'retry with {"exact": false} to see them.'
        )

    @respx.mock
    async def test_search_teams_messages_exact_false_keeps_it(self, mcp_server):
        """exact:false returns everything the index matched."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("m1")]))
        )
        _mock_search_hydration({"m1": _search_msg("m1", content="budget20260 update")})
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "options": '{"exact": false}'},
            )

        assert _structured(result)["count"] == 1

    @respx.mock
    async def test_search_teams_messages_max_content_length(self, mcp_server):
        """max_content_length truncates the content column."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("m1")]))
        )
        _mock_search_hydration({"m1": _search_msg("m1", content="#budget2026 " + "x" * 500)})
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "options": '{"max_content_length": 20}'},
            )

        content = _structured(result)["messages"][0]["content"]
        assert content.endswith("...")
        assert len(content) <= 23

    @respx.mock
    async def test_search_teams_messages_max_results(self, mcp_server):
        """max_results trims the rows and the notice says more may exist."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=search_response([_search_hit("m1"), _search_hit("m2"), _search_hit("m3")]),
            )
        )
        _mock_search_hydration(
            {"m1": _search_msg("m1"), "m2": _search_msg("m2"), "m3": _search_msg("m3")}
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "search_teams_messages",
                {"query": "#budget2026", "options": '{"max_results": 1}'},
            )

        data = _structured(result)
        assert data["count"] == 1
        assert data["notice"] == (
            "More results may exist. Narrow the search with since or "
            "conversation_id, or raise max_results."
        )

    @respx.mock
    async def test_search_teams_messages_attachments_column(self, mcp_server):
        """Shared files reach the attachments column as name [file:<id>]."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=search_response([_search_hit("chat-msg-file-001")])
            )
        )
        _mock_search_hydration({"chat-msg-file-001": SAMPLE_CHAT_MESSAGE_WITH_FILE})
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "notes"})

        assert "[file:" in _structured(result)["messages"][0]["attachments"]

    @respx.mock
    async def test_search_teams_messages_skipped_note(self, mcp_server):
        """A message that can no longer be read is counted, not fatal."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=search_response([_search_hit("m1"), _search_hit("gone")])
            )
        )
        _mock_search_hydration({"m1": _search_msg("m1")})
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        data = _structured(result)
        assert data["count"] == 1
        assert data["skipped"] == 1
        assert data["notice"] == (
            "1 matching message(s) could not be read "
            "(deleted, or no longer shared with you) and were skipped."
        )

    @respx.mock
    async def test_search_teams_messages_only_hit_deleted(self, mcp_server):
        """A lone unreadable hit is reported as skipped, not as a tool error."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=search_response([_search_hit("gone")]))
        )
        _mock_search_hydration({})
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        data = _structured(result)
        assert data["messages"] == []
        assert data["skipped"] == 1
        # Nothing was hydrated, so the stemming hint would be a lie.
        assert data["notice"] == (
            "1 matching message(s) could not be read "
            "(deleted, or no longer shared with you) and were skipped."
        )

    @respx.mock
    async def test_search_teams_messages_no_results(self, mcp_server):
        """An empty index answer reads as no results, with no hydration."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_MESSAGES_EMPTY)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result) == {
            "messages": [],
            "count": 0,
            "query": "#budget2026",
            "since": "",
            "conversation_id": "",
            "skipped": 0,
            "notice": "",
        }

    @respx.mock
    async def test_search_teams_messages_teams_unavailable(self, mcp_server):
        """A 403 on the index is the no-Teams-licence answer."""
        respx.post(TEAMS_SEARCH_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result) == {
            "error": "teams_not_available",
            "reason": "Microsoft Teams is not available for this account.",
        }

    @respx.mock
    async def test_search_teams_messages_consumer_account(self, mcp_server):
        """A consumer account gets the work/school explanation, not a stack trace."""
        respx.post(TEAMS_SEARCH_URL).mock(
            return_value=httpx.Response(400, json=SEARCH_NOT_SUPPORTED_400)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result) == {
            "error": "search_unsupported",
            "reason": (
                "Teams message search is not available for this account. Microsoft "
                "Search covers work and school accounts only. Read a specific "
                "conversation with read_teams_messages instead."
            ),
        }

    async def test_search_teams_messages_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "search_teams_messages", {"query": "#budget2026"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_get_teams_activity_quiet_window(self, mcp_server):
        # Wire up all the calls the activity scanner makes
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "get_teams_activity", {"hours": 1})

        assert _structured(result) == {
            "activity": [],
            "count": 0,
            "sources": 0,
            "hours": 1,
        }

    @respx.mock
    async def test_get_teams_activity_rows(self, mcp_server):
        """A chat's last message becomes one row, and the chat is one source."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat = {
            "id": "chat-1on1-001",
            "chatType": "oneOnOne",
            "topic": None,
            "members": [{"displayName": "Alice Smith"}],
            "lastMessagePreview": {
                "createdDateTime": recent,
                "body": {"content": "Standup in five"},
                "from": {"user": {"displayName": "Alice Smith"}},
            },
            "viewpoint": {"lastMessageReadDateTime": recent},
        }
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": [chat]})
        )
        with _mock_token():
            result = await _call(mcp_server, "get_teams_activity", {"hours": 24})

        assert _structured(result) == {
            "activity": [
                {
                    "source": "chat",
                    "source_name": "oneOnOne: Alice Smith",
                    "sender": "Alice Smith",
                    "timestamp": recent,
                    "preview": "Standup in five",
                }
            ],
            "count": 1,
            "sources": 1,
            "hours": 24,
        }

    @respx.mock
    async def test_get_teams_activity_not_available(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_teams_activity")

        assert _structured(result) == {
            "error": "teams_not_available",
            "reason": "Microsoft Teams is not available for this account.",
        }

    async def test_get_teams_activity_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_teams_activity")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


# The row list_chats builds from each chats fixture.
ONEONONE_ROW = {
    "unread": False,
    "chat_type": "oneOnOne",
    "topic": None,
    "members": "Alice Smith, Bob Jones",
    "last_sender": "Alice Smith",
    "last_preview": "Sounds good!",
    "last_preview_at": "2025-12-15T14:00:00Z",
    "last_read_at": "2025-12-15T14:00:00Z",
    "id": "chat-1on1-001",
}
GROUP_ROW = {
    "unread": True,
    "chat_type": "group",
    "topic": "Project Standup",
    "members": "Alice Smith, Bob Jones, Charlie Brown",
    "last_sender": "Bob Jones",
    "last_preview": "Meeting at 3pm",
    "last_preview_at": "2025-12-15T13:00:00Z",
    "last_read_at": "2025-12-15T12:00:00Z",
    "id": "chat-group-001",
}
MEETING_ROW = {
    "unread": True,
    "chat_type": "meeting",
    "topic": "Sprint Review",
    "members": "Alice Smith, Bob Jones",
    "last_sender": "Alice Smith",
    "last_preview": "Notes attached",
    "last_preview_at": "2025-12-15T10:00:00Z",
    "last_read_at": None,
    "id": "chat-meeting-001",
}


class TestMCPListChats:
    """list_chats: the merged listing, its paging, and mark_as_read."""

    @respx.mock
    async def test_rows_carry_every_column(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {})

        assert _structured(result) == {
            "chats": [ONEONONE_ROW, GROUP_ROW, MEETING_ROW],
            "count": 3,
            "next_cursor": "",
        }

    @respx.mock
    async def test_unread_edge_cases(self, mcp_server):
        """No messages is not unread; a null read timestamp is."""
        chat_no_messages = {
            "id": "chat-empty-001",
            "chatType": "oneOnOne",
            "topic": None,
            "members": [{"displayName": "Alice"}],
            "lastMessagePreview": None,
            "viewpoint": None,
        }
        chat_null_read = {
            "id": "chat-null-read-001",
            "chatType": "group",
            "topic": "Test",
            "members": [{"displayName": "Bob"}],
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T10:00:00Z",
                "body": {"content": "hello"},
                "from": {"user": {"displayName": "Bob"}},
            },
            "viewpoint": {"lastMessageReadDateTime": None},
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": [chat_no_messages, chat_null_read]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {})

        empty_chat, unread_chat = _structured(result)["chats"]
        assert empty_chat["unread"] is False
        assert empty_chat["last_preview"] is None
        assert empty_chat["last_preview_at"] is None
        assert unread_chat["unread"] is True

    @respx.mock
    async def test_empty_listing(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {})

        assert _structured(result) == {"chats": [], "count": 0, "next_cursor": ""}

    @respx.mock
    async def test_invalid_chat_type_makes_no_request(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {"chat_type": "invalid"})

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": (
                "Invalid chat_type: invalid. "
                "Must be one of: oneOnOne, group, meeting (or empty for all)."
            ),
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_a_bigger_top_pages_internally(self, mcp_server):
        """Graph caps /me/chats at 50 a page, so top=60 follows the nextLink."""
        first = {"@odata.nextLink": SAMPLE_CHATS_PAGE_NEXT_LINK, "value": [SAMPLE_CHAT_ONEONONE]}
        # The exact cursor route is registered first so the prefix route below
        # cannot shadow it — the cursor is itself a /me/chats URL.
        respx.get(SAMPLE_CHATS_PAGE_NEXT_LINK).mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_CHAT_GROUP]})
        )
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats?").mock(
            return_value=httpx.Response(200, json=first)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {"top": 60})

        assert len(_graph_trail()) == 2
        assert _structured(result) == {
            "chats": [ONEONONE_ROW, GROUP_ROW],
            "count": 2,
            "next_cursor": "",
        }

    @respx.mock
    async def test_a_cursor_is_fetched_verbatim_and_only_once(self, mcp_server):
        route = respx.get(SAMPLE_CHATS_PAGE_NEXT_LINK).mock(
            return_value=httpx.Response(
                200,
                json={"@odata.nextLink": SAMPLE_CHATS_PAGE_NEXT_LINK, "value": [SAMPLE_CHAT_GROUP]},
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server, "list_chats", {"cursor": SAMPLE_CHATS_PAGE_NEXT_LINK, "top": 60}
            )

        assert route.call_count == 1
        assert str(route.calls[0].request.url) == SAMPLE_CHATS_PAGE_NEXT_LINK
        assert _structured(result) == {
            "chats": [GROUP_ROW],
            "count": 1,
            "next_cursor": SAMPLE_CHATS_PAGE_NEXT_LINK,
        }

    @respx.mock
    async def test_mark_as_read_acknowledges_each_id(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        mark_route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/markChatReadForUser").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(
                mcp_server, "list_chats", {"options": '{"mark_as_read": ["chat-1on1-001"]}'}
            )

        assert json.loads(mark_route.calls[0].request.content) == {
            "user": {"id": "user-obj-id", "tenantId": "tenant-id-123"}
        }
        data = _structured(result)
        assert data["count"] == 3
        assert data["marked_as_read"] == 1

    @respx.mock
    async def test_mark_as_read_rejects_a_non_array(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {"options": '{"mark_as_read": true}'})

        assert _structured(result) == {
            "error": "invalid_options",
            "reason": "Option 'mark_as_read' must be a JSON array of chat IDs.",
        }
        assert _graph_trail() == []

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_chats", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPReadTeamsMessages:
    """read_teams_messages: both modes, and every refusal."""

    @respx.mock
    async def test_channel_messages(self, mcp_server):
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        respx.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"team_id": team_id, "channel_id": channel_id, "since": "2025-01-01"},
            )

        data = _structured(result)
        assert data["count"] == 2
        assert data["next_cursor"] == ""
        assert data["messages"][0]["id"] == "msg-user-001"
        assert data["messages"][0]["from_user_display"] == "Alice Smith"

    @respx.mock
    async def test_chat_messages(self, mcp_server):
        chat_id = "chat-1on1-001"
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "read_teams_messages", {"chat_id": chat_id, "since": "2025-01-01"}
            )

        assert _structured(result)["count"] == 1
        assert _graph_trail() == [("GET", f"/v1.0/chats/{chat_id}/messages")]

    @respx.mock
    async def test_chat_beats_channel_when_both_are_given(self, mcp_server):
        chat_id = "chat-1on1-001"
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": chat_id,
                    "team_id": "team-id-001",
                    "channel_id": "channel-id-001",
                    "since": "2025-01-01",
                },
            )

        assert _graph_trail() == [("GET", f"/v1.0/chats/{chat_id}/messages")]

    @respx.mock
    @pytest.mark.parametrize("args", [{}, {"team_id": "t1"}])
    async def test_no_usable_scope(self, mcp_server, args):
        with _mock_token():
            result = await _call(mcp_server, "read_teams_messages", args)

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_invalid_since(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "read_teams_messages", {"chat_id": "c", "since": "last tuesday"}
            )

        assert _structured(result) == {
            "error": "invalid_date",
            "reason": "Invalid since format: 'last tuesday'. Use YYYY-MM-DD or ISO datetime.",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_max_content_length_leaves_the_body_raw(self, mcp_server):
        """It is a rendering hint: the canonical body is never truncated."""
        chat_id = "chat-1on1-001"
        long_msg = {
            "id": "msg-long-001",
            "messageType": "message",
            "createdDateTime": "2025-12-15T12:00:00Z",
            "from": {"user": {"displayName": "Tim"}, "application": None},
            "body": {"contentType": "text", "content": "SELECT " + "x" * 2000},
            "attachments": [],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json={"value": [long_msg]})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": chat_id,
                    "since": "2025-01-01",
                    "options": '{"max_content_length": 50}',
                },
            )

        data = _structured(result)
        assert data["messages"][0]["body_content"] == "SELECT " + "x" * 2000
        assert data["max_content_length"] == 50

    @respx.mock
    async def test_page_mode_makes_one_request_filtered_on_last_modified(self, mcp_server):
        chat_id = "chat-1on1-001"
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "@odata.nextLink": SAMPLE_CHATS_PAGE_NEXT_LINK,
                    "value": SAMPLE_CHAT_MESSAGES_PAGE["value"],
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": chat_id,
                    "since": "2026-01-05T00:00:00Z",
                    "options": '{"page": true}',
                },
            )

        assert route.call_count == 1
        query = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert query["$filter"][0].split(" ")[0] == "lastModifiedDateTime"
        assert query["$orderby"][0].split(" ")[0] == "lastModifiedDateTime"
        assert _structured(result)["next_cursor"] == SAMPLE_CHATS_PAGE_NEXT_LINK

    @respx.mock
    async def test_paging_a_channel_is_refused(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "team_id": "team-id-001",
                    "channel_id": "channel-id-001",
                    "options": '{"page": true}',
                },
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Cursor paging is only supported for chats.",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_mark_as_read_marks_the_chat(self, mcp_server):
        chat_id = "chat-1on1-001"
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        mark_route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/markChatReadForUser").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": chat_id,
                    "since": "2025-01-01",
                    "options": '{"mark_as_read": true}',
                },
            )

        assert _graph_trail() == [
            ("GET", f"/v1.0/chats/{chat_id}/messages"),
            ("POST", f"/v1.0/chats/{chat_id}/markChatReadForUser"),
        ]
        assert json.loads(mark_route.calls[0].request.content) == {
            "user": {"id": "user-obj-id", "tenantId": "tenant-id-123"}
        }
        assert _structured(result)["marked_as_read"] is True

    @respx.mock
    async def test_mark_as_read_on_a_channel_is_refused(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "team_id": "team-id-001",
                    "channel_id": "channel-id-001",
                    "since": "2025-01-01",
                    "options": '{"mark_as_read": true}',
                },
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Option 'mark_as_read' is only supported for chats, not channels.",
        }
        assert _graph_trail() == []

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "read_teams_messages", {"chat_id": "c"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPSendTeamsMessage:
    """send_teams_message: the request shapes, and the dict that comes back."""

    @respx.mock
    async def test_sends_to_a_channel(self, mcp_server):
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        route = respx.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"message": "Hello!", "team_id": team_id, "channel_id": channel_id},
            )

        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "Hello!"}
        }
        data = _structured(result)
        assert data["sent_to"] == "channel"
        assert data["message"]["id"] == "chat-msg-sent-002"

    @respx.mock
    async def test_sends_to_a_chat(self, mcp_server):
        chat_id = "chat-1on1-001"
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "send_teams_message", {"message": "Hello!", "chat_id": chat_id}
            )

        assert route.called
        assert _structured(result)["sent_to"] == "chat"

    @respx.mock
    async def test_no_ids_makes_no_request(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "send_teams_message", {"message": "Hello!"})

        assert _structured(result) == {
            "message": None,
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_an_empty_message_with_no_files_is_refused(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "send_teams_message", {"message": "   ", "chat_id": "chat-1on1-001"}
            )

        assert _structured(result) == {
            "message": None,
            "error": "invalid_arguments",
            "reason": "message must not be empty",
        }
        assert _graph_trail() == []

    @respx.mock
    async def test_plain_text_travels_verbatim(self, mcp_server):
        """The default content_type is "text": newlines and "<" are not markup."""
        chat_id = "chat-1on1-001"
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "send_teams_message",
                {"message": "Hello\nWorld a < b", "chat_id": chat_id},
            )

        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "Hello\nWorld a < b"}
        }

    @respx.mock
    @pytest.mark.parametrize("content_type", ["html", "auto"])
    async def test_markup_needs_an_explicit_content_type(self, mcp_server, content_type):
        chat_id = "chat-1on1-001"
        html_msg = '<a href="https://example.com">Click here</a>'
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": html_msg,
                    "chat_id": chat_id,
                    "options": json.dumps({"content_type": content_type}),
                },
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == html_msg

    @respx.mock
    async def test_a_user_mention_builds_the_graph_payload(self, mcp_server):
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        route = respx.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "Hey check this out",
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "options": '{"mentions": [{"user_id": "aad-123", "name": "Alice"}]}',
                },
            )

        payload = json.loads(route.calls[0].request.content)
        assert len(payload["mentions"]) == 1
        assert payload["mentions"][0]["mentionText"] == "Alice"
        assert payload["mentions"][0]["mentioned"]["user"]["id"] == "aad-123"
        body_content = payload["body"]["content"]
        assert '<at id="0">Alice</at>' in body_content
        assert "Hey check this out" in body_content
        assert _structured(result)["sent_to"] == "channel"

    @respx.mock
    async def test_mention_everyone_builds_a_channel_mention(self, mcp_server):
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        route = respx.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "Important update",
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "options": '{"mention_everyone": true}',
                },
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["mentions"][0]["mentionText"] == "Everyone"
        assert (
            payload["mentions"][0]["mentioned"]["conversation"]["conversationIdentityType"]
            == "channel"
        )
        body_content = payload["body"]["content"]
        assert '<at id="0">Everyone</at>' in body_content
        assert "Important update" in body_content
        assert "note" not in _structured(result)

    @respx.mock
    async def test_mention_everyone_in_a_chat_is_noted_not_sent(self, mcp_server):
        chat_id = "chat-1on1-001"
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "Important update",
                    "chat_id": chat_id,
                    "options": '{"mention_everyone": true}',
                },
            )

        assert "mentions" not in json.loads(route.calls[0].request.content)
        assert _structured(result)["note"] == (
            "mention_everyone only works in channels, ignored here."
        )

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server, "send_teams_message", {"message": "hi", "chat_id": "c"}
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPSendTeamsMessageFiles:
    """send_teams_message with attachments and images in its options."""

    @respx.mock
    async def test_a_chat_file_is_uploaded_shared_then_posted(self, mcp_server):
        _mock_chat_file_upload()
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "here you go",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"attachments": [{"name": "notes.txt", "text": "hello"}]}',
                },
            )

        assert _graph_trail() == [
            ("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/notes.txt:/content"),
            ("GET", "/v1.0/me/drive/items/teams-upload-001"),
            ("GET", f"/v1.0/chats/{TEAMS_CHAT_ID}/members"),
            ("POST", "/v1.0/me/drive/items/teams-upload-001/invite"),
            ("POST", f"/v1.0/chats/{TEAMS_CHAT_ID}/messages"),
        ]
        payload = json.loads(post.calls[0].request.content)
        assert payload["attachments"] == [
            {
                "id": TEAMS_UPLOAD_GUID,
                "contentType": "reference",
                "contentUrl": TEAMS_WEBDAV_URL,
                "name": "notes.txt",
            }
        ]
        assert f'<attachment id="{TEAMS_UPLOAD_GUID}"></attachment>' in payload["body"]["content"]
        assert _structured(result)["sent_to"] == "chat"
        assert _structured(result)["message"]["id"] == "chat-msg-sent-001"

    @respx.mock
    async def test_an_image_rides_inside_the_message_with_no_upload(self, mcp_server):
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        encoded = base64.b64encode(PNG_BYTES).decode()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "look",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": json.dumps({"images": [{"name": "pic.png", "base64": encoded}]}),
                },
            )

        assert [method for method, _ in _graph_trail()] == ["POST"]
        payload = json.loads(post.calls[0].request.content)
        assert payload["hostedContents"][0]["@microsoft.graph.temporaryId"] == "1"
        assert payload["hostedContents"][0]["contentType"] == "image/png"
        assert base64.b64decode(payload["hostedContents"][0]["contentBytes"]) == PNG_BYTES
        assert '<img src="../hostedContents/1/$value">' in payload["body"]["content"]
        assert _structured(result)["sent_to"] == "chat"

    @respx.mock
    async def test_the_sender_from_the_token_is_not_invited(self, mcp_server):
        """The token's oid identifies the sender; they own the file already."""
        _mock_chat_file_upload()
        respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token(_token_with_oid("user-id-001")):
            await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "here you go",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"attachments": [{"name": "notes.txt", "text": "hello"}]}',
                },
            )

        assert _invite_recipients() == [{"email": "alice@example.com"}]

    @respx.mock
    async def test_a_failed_post_removes_the_uploaded_file(self, mcp_server):
        _mock_chat_file_upload()
        respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        )
        removed = respx.delete(f"{GRAPH_BASE_URL}/drives/drive-001/items/teams-upload-001").mock(
            return_value=httpx.Response(204)
        )
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="503"):
                await _call(
                    mcp_server,
                    "send_teams_message",
                    {
                        "message": "here you go",
                        "chat_id": TEAMS_CHAT_ID,
                        "options": '{"attachments": [{"name": "notes.txt", "text": "hello"}]}',
                    },
                )

        assert removed.call_count == 1

    @respx.mock
    async def test_files_and_images_are_both_named_in_the_confirmation(self, mcp_server):
        _mock_chat_file_upload()
        respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        encoded = base64.b64encode(PNG_BYTES).decode()
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "both",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": json.dumps(
                        {
                            "attachments": [{"name": "notes.txt", "text": "hello"}],
                            "images": [{"name": "pic.png", "base64": encoded}],
                        }
                    ),
                },
            )

        assert _structured(result)["sent_to"] == "chat"

    @respx.mock
    async def test_a_non_image_in_images_is_refused_before_anything_is_sent(self, mcp_server):
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "look",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"images": [{"name": "notes.txt", "text": "hi"}]}',
                },
            )

        data = _structured(result)
        assert data["message"] is None
        assert data["error"] == "invalid_attachments"
        assert "not an image" in data["reason"]
        assert not post.called

    @respx.mock
    async def test_a_bad_image_spec_is_reported_against_images_not_attachments(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "look",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"images": [{"text": "no name"}]}',
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_attachments"
        assert data["reason"].startswith("images[0]:")
        assert "attachments[" not in data["reason"]

    @respx.mock
    async def test_a_bad_attachment_spec_names_its_index(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "hi",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"attachments": [{"name": "a.txt", "text": "ok"}, {}]}',
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_attachments"
        assert data["reason"].startswith("attachments[1]:")

    @respx.mock
    async def test_a_403_on_the_upload_explains_the_missing_permission(self, mcp_server):
        respx.put(url__startswith=TEAMS_SEND_UPLOAD_URL).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "here",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"attachments": [{"name": "notes.txt", "text": "hello"}]}',
                },
            )

        data = _structured(result)
        assert data["message"] is None
        assert data["error"] == "files_scope_missing"
        assert "Files.ReadWrite" in data["reason"]
        assert not post.called

    @respx.mock
    async def test_a_channel_file_lands_in_the_channel_drive(self, mcp_server):
        team_id = "team-id-001"
        channel_id = "channel-id-001"
        respx.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/filesFolder").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_FILES_FOLDER)
        )
        respx.put(
            url__startswith=(
                f"{GRAPH_BASE_URL}/drives/drive-team-001/items/"
                "folder-channel-001:/notes.txt:/content"
            )
        ).mock(return_value=httpx.Response(201, json=SAMPLE_TEAMS_UPLOAD_RESPONSE))
        respx.get(
            url__startswith=f"{GRAPH_BASE_URL}/drives/drive-team-001/items/teams-upload-001"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_TEAMS_UPLOADED_ITEM))
        post = respx.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )

        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "deck",
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "options": '{"attachments": [{"name": "notes.txt", "text": "hello"}]}',
                },
            )

        assert _graph_trail() == [
            ("GET", f"/v1.0/teams/{team_id}/channels/{channel_id}/filesFolder"),
            ("PUT", "/v1.0/drives/drive-team-001/items/folder-channel-001:/notes.txt:/content"),
            ("GET", "/v1.0/drives/drive-team-001/items/teams-upload-001"),
            ("POST", f"/v1.0/teams/{team_id}/channels/{channel_id}/messages"),
        ]
        assert json.loads(post.calls[0].request.content)["attachments"][0]["name"] == "notes.txt"
        assert _structured(result)["sent_to"] == "channel"

    @respx.mock
    async def test_an_empty_attachments_list_still_takes_the_file_path(self, mcp_server):
        """An explicit [] means "no files", not "old code path" — and must still send."""
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "plain",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": '{"attachments": []}',
                },
            )

        assert json.loads(post.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "plain"}
        }
        assert _structured(result)["sent_to"] == "chat"


class TestMCPReadTeamsMessagesAttachments:
    """The canonical attachments list a message row carries."""

    @respx.mock
    async def test_attachments_flatten_file_image_and_card(self, mcp_server):
        page = {
            "value": [
                *SAMPLE_CHAT_MESSAGES_PAGE_WITH_ATTACHMENTS["value"][:3],
                SAMPLE_CHAT_MESSAGE_FULL,
            ]
        }
        respx.get(TEAMS_CHAT_MSGS).mock(return_value=httpx.Response(200, json=page))
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": TEAMS_CHAT_ID, "since": "2025-01-01"},
            )

        rows = {row["id"]: row for row in _structured(result)["messages"]}

        file_entry = rows["chat-msg-file-001"]["attachments"][0]
        assert (file_entry["kind"], file_entry["id"], file_entry["name"]) == (
            "file",
            TEAMS_FILE_ATTACHMENT_ID,
            "roadmap.pptx",
        )
        image_entry = rows["chat-msg-image-001"]["attachments"][0]
        assert (image_entry["kind"], image_entry["id"]) == ("image", TEAMS_HOSTED_ID)
        assert rows["chat-msg-card-001"]["attachments"][0]["kind"] == "card"
        # A message with nothing attached carries an empty list, never null.
        assert rows["chat-msg-001"]["attachments"] == []


class TestMCPGetTeamsAttachment:
    """get_teams_attachment: files, inline images, cards, and every refusal."""

    async def test_invalid_mode(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "pdf",
                },
            )

        assert _structured(result) == {
            "error": "invalid_mode",
            "reason": (
                "mode must be one of: metadata, text, bytes, onedrive, thumbnail; got 'pdf'"
            ),
        }

    @respx.mock
    async def test_bad_options_json_is_refused_before_any_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                    "options": "not json",
                },
            )

        assert _structured(result)["error"] == "invalid_options"
        assert _graph_trail() == []

    async def test_missing_ids(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {"message_id": "m1", "attachment_id": "a1"},
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Provide either chat_id, or both team_id and channel_id.",
        }

    @respx.mock
    async def test_unknown_id_lists_what_the_message_has(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": "nope",
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {
            "error": "not_found",
            "available": [{"kind": "file", "id": TEAMS_FILE_ATTACHMENT_ID, "name": "roadmap.pptx"}],
        }

    @respx.mock
    async def test_metadata_mode_returns_the_entry(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        share = respx.get(url__startswith=f"{GRAPH_BASE_URL}/shares/").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "metadata",
                },
            )

        assert _structured(result) == {
            "id": TEAMS_FILE_ATTACHMENT_ID,
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": "reference",
            "content_url": TEAMS_FILE_URL,
            "thumbnail_url": None,
            "card_text": None,
        }
        assert not share.called

    @respx.mock
    async def test_metadata_mode_describes_a_quoted_reference(self, mcp_server):
        """The kinds with no bytes are only reachable through metadata mode."""
        respx.get(TEAMS_JUNK_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_JUNK_ATTACHMENTS)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-junk-001",
                    "attachment_id": "ref-001",
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "metadata",
                },
            )

        assert _structured(result)["kind"] == "message_reference"

    @respx.mock
    async def test_a_quoted_reference_has_nothing_to_read(self, mcp_server):
        respx.get(TEAMS_JUNK_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_JUNK_ATTACHMENTS)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-junk-001",
                    "attachment_id": "ref-001",
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        data = _structured(result)
        assert data["error"] == "not_found"
        assert {"kind": "file", "id": TEAMS_FILE_ATTACHMENT_ID, "name": "roadmap.pptx"} in (
            data["available"]
        )

    @respx.mock
    async def test_file_text_mode_extracts_the_document(self, mcp_server):
        docx = _docx_bytes()
        docx_item = {
            **SAMPLE_TEAMS_DRIVE_ITEM,
            "name": "notes.docx",
            "file": {"mimeType": DOCX_MIME},
            "size": len(docx),
        }
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_CONTENT_URL).mock(return_value=httpx.Response(200, content=docx))
        respx.get(TEAMS_SHARE_BASE).mock(return_value=httpx.Response(200, json=docx_item))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        data = _structured(result)
        assert "Quarterly Title" in data.pop("text")
        assert data == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": DOCX_MIME,
            "size": len(docx),
            "truncated": False,
        }

    @respx.mock
    async def test_text_mode_refuses_an_oversized_file_before_downloading(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        content = respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"never")
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json={**SAMPLE_TEAMS_DRIVE_ITEM, "size": 60_000_000})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": TEAMS_PPTX_MIME,
            "size": 60_000_000,
            "text": None,
            "truncated": False,
            "reason": "too_large",
        }
        assert not content.called

    @respx.mock
    async def test_base64_still_means_bytes(self, mcp_server):
        """The str ancestor's word for raw bytes survives as a silent synonym."""
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"PPTXBYTES")
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "base64",
                },
            )

        assert _structured(result) == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": TEAMS_PPTX_MIME,
            "size": len(b"PPTXBYTES"),
            "content_base64": base64.b64encode(b"PPTXBYTES").decode("ascii"),
        }

    @respx.mock
    async def test_file_onedrive_mode_saves_a_copy(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"PPTXBYTES")
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        upload = respx.put(
            f"{GRAPH_BASE_URL}/me/drive/root:/Attachments/roadmap.pptx:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "onedrive",
                },
            )

        assert upload.called
        assert _structured(result) == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": TEAMS_PPTX_MIME,
            "size": len(b"PPTXBYTES"),
            "item_id": SAMPLE_UPLOADED_FILE["id"],
            "web_url": SAMPLE_UPLOADED_FILE["webUrl"],
        }

    @respx.mock
    async def test_403_on_the_sharing_link_is_access_denied(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_BASE).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {"error": "access_denied"}

    @respx.mock
    async def test_404_on_the_sharing_link_propagates_as_a_tool_error(self, mcp_server):
        """Only the 403 is permanent; everything else stays retryable."""
        from fastmcp.exceptions import ToolError

        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_BASE).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        with _mock_token():
            with pytest.raises(ToolError, match="404"):
                await _call(
                    mcp_server,
                    "get_teams_attachment",
                    {
                        "message_id": "chat-msg-file-001",
                        "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                        "chat_id": TEAMS_CHAT_ID,
                    },
                )

    @respx.mock
    async def test_a_shared_folder_is_refused(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {"error": "is_folder"}

    @respx.mock
    async def test_file_without_a_content_url_is_not_found(self, mcp_server):
        msg = {
            **SAMPLE_CHAT_MESSAGE_WITH_FILE,
            "attachments": [{"id": TEAMS_FILE_ATTACHMENT_ID, "contentType": "reference"}],
        }
        respx.get(TEAMS_FILE_MSG_URL).mock(return_value=httpx.Response(200, json=msg))
        share = respx.get(url__startswith=f"{GRAPH_BASE_URL}/shares/").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {"error": "not_found"}
        assert not share.called

    @respx.mock
    async def test_inline_image_text_mode_reports_a_binary(self, mcp_server):
        respx.get(TEAMS_IMAGE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_IMAGE)
        )
        respx.get(TEAMS_HOSTED_VALUE_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-image-001",
                    "attachment_id": TEAMS_HOSTED_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {
            "kind": "image",
            "name": "image-aWQ9eF8wLWN1.png",
            "content_type": "image/png",
            "size": len(PNG_BYTES),
            "text": None,
            "truncated": False,
            "reason": "binary",
        }

    @respx.mock
    async def test_card_text_mode_returns_the_card_text(self, mcp_server):
        respx.get(TEAMS_CARD_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-card-001",
                    "attachment_id": "card-att-001",
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {
            "kind": "card",
            "content_type": "application/vnd.microsoft.card.adaptive",
            "text": "Deploy finished",
            "truncated": False,
        }

    @respx.mock
    async def test_a_card_has_no_bytes(self, mcp_server):
        respx.get(TEAMS_CARD_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-card-001",
                    "attachment_id": "card-att-001",
                    "chat_id": TEAMS_CHAT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "not_found",
            "available": [{"kind": "card", "id": "card-att-001", "name": None}],
        }

    @respx.mock
    async def test_403_on_the_message_reports_teams_unavailable(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {"error": "teams_unavailable"}

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "chat_id": TEAMS_CHAT_ID,
                },
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_channel_form_reads_the_channel_message(self, mcp_server):
        route = respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages/chat-msg-card-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-card-001",
                    "attachment_id": "card-att-001",
                    "team_id": "t1",
                    "channel_id": "c1",
                },
            )

        assert route.called
        assert _graph_trail() == [("GET", "/v1.0/teams/t1/channels/c1/messages/chat-msg-card-001")]
        assert _structured(result)["text"] == "Deploy finished"

    @respx.mock
    async def test_chat_id_wins_over_a_team_and_channel(self, mcp_server):
        chat_route = respx.get(TEAMS_CARD_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        channel_route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/teams/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "message_id": "chat-msg-card-001",
                    "attachment_id": "card-att-001",
                    "chat_id": TEAMS_CHAT_ID,
                    "team_id": "t1",
                    "channel_id": "c1",
                },
            )

        assert chat_route.called
        assert not channel_route.called


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
            result = await _call(mcp_server, "list_sharepoint_sites", {"query": "engineering"})

        assert _structured(result) == {
            "sites": [
                {
                    "name": site["displayName"],
                    "id": site["id"],
                    "web_url": site["webUrl"],
                }
                for site in SAMPLE_SITES_RESPONSE["value"]
            ],
            "count": 2,
            "query": "engineering",
        }

    @respx.mock
    async def test_list_sharepoint_sites_empty(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/sites").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_sharepoint_sites", {"query": "nope"})

        assert _structured(result) == {"sites": [], "count": 0, "query": "nope"}

    async def test_list_sharepoint_sites_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_sharepoint_sites")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_list_files_onedrive_browse(self, mcp_server):
        """No site_id, no query → OneDrive root browse, folders and files as one table."""
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_CHILDREN_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files")

        assert _structured(result) == {
            "files": [
                {
                    "name": "Documents",
                    "type": "folder",
                    "size": 0,
                    "child_count": 5,
                    "id": "folder-id-001",
                },
                {"name": "report.csv", "type": "file", "size": 1024, "id": "file-id-001"},
                {
                    "name": "presentation.pptx",
                    "type": "file",
                    "size": 2_500_000,
                    "id": "file-id-002",
                },
            ],
            "count": 3,
            "folder_path": "",
            "query": "",
        }

    @respx.mock
    async def test_list_files_onedrive_subfolder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root:/Documents:/children").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"folder_path": "Documents"})

        assert _structured(result) == {
            "files": [{"name": "report.csv", "type": "file", "size": 1024, "id": "file-id-001"}],
            "count": 1,
            "folder_path": "Documents",
            "query": "",
        }

    @respx.mock
    async def test_list_files_sharepoint_browse(self, mcp_server):
        """site_id set, no query → SharePoint browse."""
        site_id = "site-id-001"
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root/children").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FOLDER]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"site_id": site_id})

        assert _structured(result) == {
            "files": [
                {
                    "name": "Documents",
                    "type": "folder",
                    "size": 0,
                    "child_count": 5,
                    "id": "folder-id-001",
                }
            ],
            "count": 1,
            "folder_path": "",
            "query": "",
        }

    @respx.mock
    async def test_list_files_search_query(self, mcp_server):
        """query set → search mode, which adds the summary and url columns."""
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"query": "budget"})

        assert _structured(result) == {
            "files": [
                {
                    "name": "Q4-budget.xlsx",
                    "type": "file",
                    "size": 45000,
                    "id": "search-file-001",
                    "summary": "Q4 <c0>budget</c0> projections for 2025",
                    "url": "https://contoso.sharepoint.com/sites/finance/Q4-budget.xlsx",
                },
                {
                    "name": "budget-notes.md",
                    "type": "file",
                    "size": 2048,
                    "id": "search-file-002",
                    "summary": "Notes on <c0>budget</c0> review meeting",
                    "url": "https://contoso.sharepoint.com/sites/finance/budget-notes.md",
                },
            ],
            "count": 2,
            "folder_path": "",
            "query": "budget",
        }

    @respx.mock
    async def test_list_files_search_no_results(self, mcp_server):
        respx.post(f"{GRAPH_BASE_URL}/search/query").mock(
            return_value=httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE_EMPTY)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"query": "nonexistent"})

        assert _structured(result) == {
            "files": [],
            "count": 0,
            "folder_path": "",
            "query": "nonexistent",
        }

    @respx.mock
    async def test_list_files_empty_folder(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/root/children").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files")

        assert _structured(result) == {
            "files": [],
            "count": 0,
            "folder_path": "",
            "query": "",
        }

    @respx.mock
    async def test_list_files_sharing_url(self, mcp_server):
        """url set → the shared folder's children, addressed by encoded share id."""
        share_url = f"{GRAPH_BASE_URL}/shares/{_encode_sharing_url(TEAMS_FILE_URL)}/root/children"
        respx.get(share_url).mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_DRIVE_ITEM_FILE]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"url": TEAMS_FILE_URL})

        assert _structured(result) == {
            "files": [{"name": "report.csv", "type": "file", "size": 1024, "id": "file-id-001"}],
            "count": 1,
            "folder_path": "",
            "query": "",
        }

    @pytest.mark.parametrize(
        ("status", "payload", "code"),
        [
            (403, GRAPH_ERROR_403, "access_denied"),
            (404, GRAPH_ERROR_404, "not_found"),
            (400, GRAPH_ERROR_400, "invalid_link"),
        ],
    )
    @respx.mock
    async def test_list_files_sharing_url_errors(self, mcp_server, status, payload, code):
        """The three link failures a caller can act on are permanent codes."""
        share_url = f"{GRAPH_BASE_URL}/shares/{_encode_sharing_url(TEAMS_FILE_URL)}/root/children"
        respx.get(share_url).mock(return_value=httpx.Response(status, json=payload))
        with _mock_token():
            result = await _call(mcp_server, "list_files", {"url": TEAMS_FILE_URL})

        assert _structured(result)["error"] == code

    @respx.mock
    async def test_list_files_sharing_url_other_error_propagates(self, mcp_server):
        """A 500 on the share route is transient and must stay a tool error."""
        from fastmcp.exceptions import ToolError

        share_url = f"{GRAPH_BASE_URL}/shares/{_encode_sharing_url(TEAMS_FILE_URL)}/root/children"
        respx.get(share_url).mock(
            return_value=httpx.Response(
                500, json={"error": {"code": "serviceNotAvailable", "message": "Try later"}}
            )
        )
        with _mock_token(), pytest.raises(ToolError, match="serviceNotAvailable"):
            await _call(mcp_server, "list_files", {"url": TEAMS_FILE_URL})

    async def test_list_files_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_files")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

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


class TestMCPInspectFile:
    """inspect_file: metadata by item id or sharing link, optionally with text."""

    @respx.mock
    async def test_by_item_id_returns_metadata_only(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            result = await _call(mcp_server, "inspect_file", {"item_id": "file-id-001"})

        assert _structured(result) == {
            "item_id": "file-id-001",
            "name": "report.csv",
            "size": 1024,
            "content_type": "text/csv",
            "web_url": SAMPLE_DRIVE_ITEM_FILE["webUrl"],
            "modified": "2025-12-15T10:30:00Z",
            "is_folder": False,
        }

    @respx.mock
    async def test_by_url_resolves_the_sharing_link(self, mcp_server):
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_SHARED_TEXT_FILE)
        )
        with _mock_token():
            result = await _call(mcp_server, "inspect_file", {"url": TEAMS_FILE_URL})

        assert _structured(result) == {
            "item_id": "shared-file-002",
            "name": "notes.md",
            "size": 256,
            "content_type": "text/markdown",
            "web_url": SAMPLE_SHARED_TEXT_FILE["webUrl"],
            "modified": "2025-12-21T14:00:00Z",
            "is_folder": False,
        }

    @respx.mock
    async def test_an_item_id_that_is_a_url_is_treated_as_one(self, mcp_server):
        route = respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_SHARED_TEXT_FILE)
        )
        with _mock_token():
            result = await _call(mcp_server, "inspect_file", {"item_id": TEAMS_FILE_URL})

        assert route.called
        assert _structured(result)["name"] == "notes.md"

    @respx.mock
    async def test_read_content_returns_the_text(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"col1,col2\n1,2\n")
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "read_content": "true"},
            )

        assert _structured(result)["text"] == "col1,col2\n1,2\n"

    @respx.mock
    async def test_a_folder_reads_as_a_folder_with_no_text(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/folder-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "folder-id-001", "read_content": "true"},
            )

        data = _structured(result)
        assert data["is_folder"] is True
        assert data["text"] is None

    @respx.mock
    async def test_read_content_false_downloads_nothing(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "read_content": "false"},
            )

        assert "text" not in _structured(result)
        assert _graph_trail() == [("GET", "/v1.0/me/drive/items/file-id-001")]

    @respx.mock
    async def test_a_binary_file_has_no_text(self, mcp_server):
        """An image is neither decodable as text nor extractable as a document."""
        image_item = {
            "id": "file-id-img-001",
            "name": "diagram.png",
            "size": 500_000,
            "file": {"mimeType": "image/png"},
            "lastModifiedDateTime": "2025-12-15T10:00:00Z",
            "lastModifiedBy": {"user": {"displayName": "Alice Smith", "id": "user-001"}},
            "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-img-001",
            "parentReference": {"driveId": "drive-001", "path": "/drive/root:"},
        }
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-img-001").mock(
            return_value=httpx.Response(200, json=image_item)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-img-001", "read_content": "true"}
            )

        data = _structured(result)
        assert data["name"] == "diagram.png"
        assert data["text"] is None

    @respx.mock
    async def test_pptx_text_is_extracted(self, mcp_server):
        import io

        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = "Test Slide Title"
        buf = io.BytesIO()
        prs.save(buf)

        item_id = SAMPLE_DRIVE_ITEM_BINARY["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_BINARY)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/content").mock(
            return_value=httpx.Response(200, content=buf.getvalue())
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": item_id, "read_content": "true"}
            )

        data = _structured(result)
        assert data["name"] == "presentation.pptx"
        assert "Test Slide Title" in data["text"]

    @respx.mock
    async def test_a_file_over_the_size_limit_has_no_text(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_LARGE_TEXT["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_LARGE_TEXT)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": item_id, "read_content": "true"}
            )

        assert _structured(result)["text"] is None

    @respx.mock
    async def test_site_id_reads_from_a_sharepoint_drive(self, mcp_server):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}/content").mock(
            return_value=httpx.Response(200, content=b"data")
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": item_id, "site_id": site_id, "read_content": "true"},
            )

        assert _structured(result)["name"] == "report.csv"
        assert _graph_trail()[0][1].startswith(f"/v1.0/sites/{site_id}/drive/items/")

    async def test_missing_target(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "inspect_file", {})

        assert _structured(result) == {"error": "missing_target"}

    @pytest.mark.parametrize(
        ("status", "error"),
        [(403, "access_denied"), (404, "not_found"), (400, "invalid_link")],
    )
    @respx.mock
    async def test_sharing_link_failures_map_to_sentinels(self, mcp_server, status, error):
        respx.get(TEAMS_SHARE_BASE).mock(return_value=httpx.Response(status, json=GRAPH_ERROR_404))
        with _mock_token():
            result = await _call(mcp_server, "inspect_file", {"url": TEAMS_FILE_URL})

        assert _structured(result) == {"error": error}

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "inspect_file", {"item_id": "file-id-001"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_an_unknown_item_id_propagates_as_a_tool_error(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/nope").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="404"):
                await _call(mcp_server, "inspect_file", {"item_id": "nope"})


class TestMCPInspectFileModes:
    """inspect_file bytes and thumbnail: a preview for a bare sharing link."""

    # An item Graph reported no file facet and no size for — the shape the
    # content_type fill and the arrived-bytes cap are there for.
    FACETLESS_ITEM = {
        "id": "file-id-pdf",
        "name": "contract.pdf",
        "lastModifiedDateTime": "2025-12-15T10:30:00Z",
        "webUrl": "https://onedrive.live.com/edit.aspx?resid=file-id-pdf",
    }

    @respx.mock
    async def test_bytes_from_a_sharing_url(self, mcp_server):
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_SHARED_TEXT_FILE)
        )
        respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"# notes")
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"url": TEAMS_FILE_URL, "mode": "bytes"}
            )

        data = _structured(result)
        assert base64.b64decode(data["content_base64"]) == b"# notes"
        assert data["item_id"] == "shared-file-002"
        assert data["content_type"] == "text/markdown"
        assert set(data) == {
            "item_id",
            "name",
            "size",
            "content_type",
            "web_url",
            "modified",
            "is_folder",
            "content_base64",
        }
        share_path = httpx.URL(TEAMS_SHARE_BASE).path
        assert _graph_trail() == [("GET", share_path), ("GET", f"{share_path}/content")]

    @respx.mock
    async def test_bytes_by_item_id(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"col1,col2\n")
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-001", "mode": "bytes"}
            )

        assert base64.b64decode(_structured(result)["content_base64"]) == b"col1,col2\n"
        assert _graph_trail() == [
            ("GET", "/v1.0/me/drive/items/file-id-001"),
            ("GET", "/v1.0/me/drive/items/file-id-001/content"),
        ]

    @respx.mock
    async def test_a_missing_content_type_is_guessed_from_the_name(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-pdf").mock(
            return_value=httpx.Response(200, json=self.FACETLESS_ITEM)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-pdf/content").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7")
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-pdf", "mode": "bytes"}
            )

        assert _structured(result)["content_type"] == "application/pdf"

    @respx.mock
    async def test_thumbnail_from_a_sharing_url(self, mcp_server):
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_SHARED_TEXT_FILE)
        )
        respx.get(TEAMS_SHARE_THUMB_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"url": TEAMS_FILE_URL, "mode": "thumbnail"}
            )

        data = _structured(result)
        assert base64.b64decode(data["content_base64"]) == PNG_BYTES
        assert data["thumbnail_content_type"] == "image/png"
        # The file's own type still describes the file, not its picture.
        assert data["content_type"] == "text/markdown"

    @respx.mock
    async def test_thumbnail_by_item_id_takes_its_size_from_options(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        route = respx.get(
            f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/thumbnails/0/large/content"
        ).mock(return_value=httpx.Response(200, content=PNG_BYTES, headers={"Content-Type": ""}))
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {
                    "item_id": "file-id-001",
                    "mode": "thumbnail",
                    "options": json.dumps({"thumbnail": "large"}),
                },
            )

        assert route.called
        data = _structured(result)
        assert base64.b64decode(data["content_base64"]) == PNG_BYTES
        # Graph named no type for the picture, so the JPEG default stands in.
        assert data["thumbnail_content_type"] == "image/jpeg"

    @respx.mock
    async def test_a_file_graph_renders_no_thumbnail_for(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/thumbnails/0/medium/content").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-001", "mode": "thumbnail"}
            )

        assert _structured(result) == {"error": "no_thumbnail"}

    @respx.mock
    async def test_a_folder_has_no_bytes(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/folder-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "folder-id-001", "mode": "bytes"}
            )

        assert _structured(result) == {"error": "is_folder"}

    @respx.mock
    async def test_too_large_is_decided_from_the_metadata(self, mcp_server):
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json={**SAMPLE_SHARED_TEXT_FILE, "size": 20_000_000})
        )
        content = respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"never")
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"url": TEAMS_FILE_URL, "mode": "bytes"}
            )

        assert _structured(result) == {
            "error": "too_large",
            "size": 20_000_000,
            "limit": 10_000_000,
        }
        assert not content.called

    @respx.mock
    async def test_bytes_over_the_cap_are_refused_on_arrival(self, mcp_server, monkeypatch):
        """An item that announced no size can still overshoot the cap."""
        from ms_graph import attachments as attachment_ops

        monkeypatch.setattr(attachment_ops, "MAX_JSON_ATTACHMENT_BYTES", 4)
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-pdf").mock(
            return_value=httpx.Response(200, json=self.FACETLESS_ITEM)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-pdf/content").mock(
            return_value=httpx.Response(200, content=b"hello world")
        )
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-pdf", "mode": "bytes"}
            )

        assert _structured(result) == {"error": "too_large", "size": 11, "limit": 4}

    @respx.mock
    async def test_an_unknown_mode_is_refused_before_any_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "inspect_file", {"item_id": "file-id-001", "mode": "preview"}
            )

        data = _structured(result)
        assert data["error"] == "invalid_mode"
        assert "preview" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_a_bad_thumbnail_size_is_refused_before_any_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {
                    "item_id": "file-id-001",
                    "mode": "thumbnail",
                    "options": json.dumps({"thumbnail": "huge"}),
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_thumbnail"
        assert "huge" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_unparseable_options_are_refused_before_any_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "mode": "bytes", "options": "not json"},
            )

        assert _structured(result)["error"] == "invalid_options"
        assert _graph_trail() == []

    @respx.mock
    async def test_an_explicit_mode_wins_over_read_content(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"col1,col2\n")
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "read_content": "true", "mode": "bytes"},
            )

        data = _structured(result)
        assert "text" not in data
        assert base64.b64decode(data["content_base64"]) == b"col1,col2\n"

    @respx.mock
    async def test_mode_metadata_downloads_nothing_even_with_read_content(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "read_content": "true", "mode": "metadata"},
            )

        assert "text" not in _structured(result)
        assert _graph_trail() == [("GET", "/v1.0/me/drive/items/file-id-001")]

    @respx.mock
    async def test_an_empty_mode_still_follows_read_content(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "inspect_file",
                {"item_id": "file-id-001", "read_content": "false", "mode": ""},
            )

        assert "text" not in _structured(result)
        assert _graph_trail() == [("GET", "/v1.0/me/drive/items/file-id-001")]


class TestMCPUploadTool:
    """manage_file(action="upload") — the create-or-overwrite branch."""

    @respx.mock
    async def test_upload_creates_file_in_root(self, mcp_server):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/notes.md:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"action": "upload", "filename": "notes.md", "content": "# Hello"},
            )

        assert _structured(result) == {
            "action": "upload",
            "id": SAMPLE_UPLOADED_FILE["id"],
            "name": "notes.md",
            "size": SAMPLE_UPLOADED_FILE["size"],
            "web_url": SAMPLE_UPLOADED_FILE["webUrl"],
        }
        assert route.calls[0].request.content == b"# Hello"

    @respx.mock
    async def test_upload_to_subfolder(self, mcp_server):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Documents/data.csv:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "data.csv",
                    "content": "a,b\n1,2",
                    "options": '{"folder_path": "Documents"}',
                },
            )

        assert _structured(result)["action"] == "upload"
        assert _graph_trail() == [("PUT", "/v1.0/me/drive/root:/Documents/data.csv:/content")]
        assert route.calls[0].request.headers["Content-Type"] == "text/csv"

    @respx.mock
    async def test_upload_to_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        route = respx.put(
            f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root:/Shared Documents/report.html:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "report.html",
                    "content": "<h1>Hello</h1>",
                    "options": json.dumps({"folder_path": "Shared Documents", "site_id": site_id}),
                },
            )

        assert _structured(result)["action"] == "upload"
        assert route.calls[0].request.headers["Content-Type"] == "text/html"

    @respx.mock
    async def test_upload_docx_converts_markdown(self, mcp_server):
        """Uploading a .docx file converts markdown content to Word format."""
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Review.docx:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "Review.docx",
                    "content": "# Title\n\nA paragraph.",
                },
            )

        assert _structured(result)["action"] == "upload"
        req = route.calls[0].request
        assert req.headers["Content-Type"] == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert req.content[:2] == b"PK"  # ZIP magic bytes = valid .docx

    @respx.mock
    async def test_upload_base64_binary(self, mcp_server):
        """content_encoding=base64 decodes and uploads the raw bytes."""
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        encoded = base64.b64encode(raw).decode()
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/image.png:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "image.png",
                    "content": encoded,
                    "content_encoding": "base64",
                },
            )

        assert _structured(result)["action"] == "upload"
        assert route.calls[0].request.content == raw

    @respx.mock
    async def test_upload_base64_beats_the_docx_extension(self, mcp_server):
        """A base64 .docx uploads its bytes rather than being generated again."""
        raw = b"PK\x03\x04already-a-docx"
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Ready.docx:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "Ready.docx",
                    "content": base64.b64encode(raw).decode(),
                    "content_encoding": "base64",
                },
            )

        assert route.calls[0].request.content == raw
        assert route.calls[0].request.headers["Content-Type"] == "application/octet-stream"

    @respx.mock
    async def test_upload_base64_invalid_content(self, mcp_server):
        """Invalid base64 is refused before any request."""
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "file.bin",
                    "content": "not-valid-base64!!!",
                    "content_encoding": "base64",
                },
            )

        data = _structured(result)
        assert data["error"] == "invalid_arguments"
        assert data["reason"].startswith("Failed to decode base64 content:")
        assert _graph_trail() == []

    @respx.mock
    async def test_upload_over_the_simple_limit_is_refused(self, mcp_server):
        """The ops layer caps simple uploads at 4 MB, before it sends anything."""
        from ms_graph import files as files_ops

        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "huge.txt",
                    "content": "a" * (files_ops.MAX_SIMPLE_UPLOAD_BYTES + 1),
                },
            )

        data = _structured(result)
        assert data["error"] == "too_large"
        assert data["limit"] == files_ops.MAX_SIMPLE_UPLOAD_BYTES
        assert "4 MB simple upload limit" in data["reason"]
        assert _graph_trail() == []

    @respx.mock
    async def test_upload_docx_to_sharepoint(self, mcp_server):
        """Docx upload works with SharePoint site_id."""
        site_id = "site-id-001"
        route = respx.put(
            f"{GRAPH_BASE_URL}/sites/{site_id}/drive/root:/Contracts/Review.docx:/content"
        ).mock(return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "action": "upload",
                    "filename": "Review.docx",
                    "content": "# Contract\n\n- Clause 1\n- Clause 2",
                    "options": json.dumps({"folder_path": "Contracts", "site_id": site_id}),
                },
            )

        assert _structured(result)["action"] == "upload"
        assert route.called

    async def test_upload_requires_a_filename(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_file", {"action": "upload", "content": "hi"})

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "filename is required for the 'upload' action.",
        }

    async def test_upload_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "manage_file",
                {"action": "upload", "filename": "x.txt", "content": "y"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPCopyOrRenameTool:
    """manage_file's copy/rename/delete branches."""

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
        respx.post(f"{GRAPH_BASE_URL}/drives/{SOURCE_DRIVE_ID}/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "new_name": "template-copy.docx", "action": "copy"},
            )

        assert _structured(result) == {
            "action": "copy",
            "id": SAMPLE_COPY_COMPLETED["resourceId"],
            "name": "template-copy.docx",
        }

    @respx.mock
    async def test_copy_with_destination_folder(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        dest = "folder-id-archive"
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        copy_route = respx.post(
            f"{GRAPH_BASE_URL}/drives/{SOURCE_DRIVE_ID}/items/{item_id}/copy"
        ).mock(return_value=httpx.Response(202, headers={"Location": MONITOR_URL}))
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_COMPLETED))
        with _mock_token():
            await _call(
                mcp_server,
                "manage_file",
                {
                    "item_id": item_id,
                    "new_name": "archived.docx",
                    "action": "copy",
                    "options": f'{{"destination_folder_id": "{dest}"}}',
                },
            )

        copy_body = json.loads(copy_route.calls[0].request.content)
        assert copy_body["parentReference"]["id"] == dest

    @respx.mock
    async def test_copy_failure_is_a_tool_error(self, mcp_server):
        """A failed copy monitor raises — the operation is not permanently bad."""
        from fastmcp.exceptions import ToolError

        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_WORD)
        )
        respx.post(f"{GRAPH_BASE_URL}/drives/{SOURCE_DRIVE_ID}/items/{item_id}/copy").mock(
            return_value=httpx.Response(202, headers={"Location": MONITOR_URL})
        )
        respx.get(MONITOR_URL).mock(return_value=httpx.Response(200, json=SAMPLE_COPY_FAILED))
        with _mock_token(), pytest.raises(ToolError, match="accessDenied"):
            await _call(
                mcp_server,
                "manage_file",
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
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "new_name": "final-report.csv"},
            )

        assert _structured(result) == {
            "action": "rename",
            "id": item_id,
            "name": "final-report.csv",
            "web_url": SAMPLE_DRIVE_ITEM_FILE["webUrl"],
        }
        assert json.loads(route.calls[0].request.content) == {"name": "final-report.csv"}

    @respx.mock
    async def test_rename_on_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        renamed = {**SAMPLE_DRIVE_ITEM_WORD, "name": "final-doc.docx"}
        respx.patch(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(200, json=renamed)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "item_id": item_id,
                    "new_name": "final-doc.docx",
                    "options": f'{{"site_id": "{site_id}"}}',
                },
            )

        assert _structured(result)["name"] == "final-doc.docx"
        assert _graph_trail() == [("PATCH", f"/v1.0/sites/{site_id}/drive/items/{item_id}")]

    async def test_invalid_action(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": "x", "new_name": "y", "action": "move"},
            )

        assert _structured(result) == {
            "error": "invalid_action",
            "reason": "Invalid action 'move'. Must be 'rename', 'copy', 'delete', or 'upload'.",
        }

    @respx.mock
    async def test_rename_not_found(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "ResourceNotFound", "message": "Item not found."}}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "new_name": "x.csv"},
            )

        assert _structured(result) == {
            "error": "not_found",
            "reason": f"File not found: {item_id}",
        }

    @respx.mock
    async def test_delete_not_found(self, mcp_server):
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.delete(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "itemNotFound", "message": "Item not found."}}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "action": "delete"},
            )

        assert _structured(result) == {
            "error": "not_found",
            "reason": f"File not found: {item_id}",
        }

    @respx.mock
    async def test_transient_graph_error_still_raises(self, mcp_server):
        """Only a 404 is permanent; a 503 must reach the client as a tool error."""
        from fastmcp.exceptions import ToolError

        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        respx.patch(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(
                503,
                json={"error": {"code": "serviceNotAvailable", "message": "Try again later."}},
            )
        )
        with _mock_token(), pytest.raises(ToolError, match="serviceNotAvailable"):
            await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "new_name": "x.csv"},
            )

    @respx.mock
    async def test_delete_action(self, mcp_server):
        """action='delete' → DELETE the item (204 No Content)."""
        item_id = SAMPLE_DRIVE_ITEM_FILE["id"]
        route = respx.delete(f"{GRAPH_BASE_URL}/me/drive/items/{item_id}").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": item_id, "action": "delete"},
            )

        assert route.called
        assert _structured(result) == {"action": "delete", "id": item_id}

    @respx.mock
    async def test_delete_on_sharepoint(self, mcp_server):
        site_id = "site-id-001"
        item_id = SAMPLE_DRIVE_ITEM_WORD["id"]
        route = respx.delete(f"{GRAPH_BASE_URL}/sites/{site_id}/drive/items/{item_id}").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {
                    "item_id": item_id,
                    "action": "delete",
                    "options": f'{{"site_id": "{site_id}"}}',
                },
            )

        assert route.called
        assert _structured(result) == {"action": "delete", "id": item_id}

    async def test_rename_requires_new_name(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_file",
                {"item_id": "x", "action": "rename"},
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "new_name is required for the 'rename' action.",
        }

    async def test_delete_requires_an_item_id(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_file", {"action": "delete"})

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "item_id is required for the 'delete' action.",
        }

    async def test_manage_file_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "manage_file", {"item_id": "x", "new_name": "y.csv"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


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
        """No workspace_id → the workspace list, led by the synthetic My workspace."""
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_WORKSPACES_RESPONSE)
        )
        with _mock_pbi_token():
            result = await _call(mcp_server, "list_powerbi")

        assert _structured(result) == {
            "workspaces": [
                {"name": "My workspace", "id": "me", "premium": False},
                {"name": "Analytics Hub", "id": "ws-id-001", "premium": True},
                {"name": "Finance Reports", "id": "ws-id-002", "premium": False},
            ],
            "count": 3,
        }

    @respx.mock
    async def test_list_powerbi_workspaces_empty(self, mcp_server):
        """My workspace has no group ID, so it is always prepended."""
        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_pbi_token():
            result = await _call(mcp_server, "list_powerbi")

        assert _structured(result) == {
            "workspaces": [{"name": "My workspace", "id": "me", "premium": False}],
            "count": 1,
        }

    @respx.mock
    async def test_list_powerbi_content_all(self, mcp_server):
        """A workspace_id → datasets, then reports, then dashboards, each tagged."""
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
            result = await _call(mcp_server, "list_powerbi", {"workspace_id": ws_id})

        assert _structured(result) == {
            "items": [
                {"kind": "dataset", "name": "Sales", "id": "ds-id-001", "refreshable": True},
                {
                    "kind": "dataset",
                    "name": "Marketing KPIs",
                    "id": "ds-id-002",
                    "refreshable": True,
                },
                {
                    "kind": "report",
                    "name": "Q4 Dashboard",
                    "id": "rpt-id-001",
                    "dataset_id": "ds-id-001",
                },
                {
                    "kind": "report",
                    "name": "Monthly Revenue",
                    "id": "rpt-id-002",
                    "dataset_id": "ds-id-002",
                },
                {"kind": "dashboard", "name": "Executive Overview", "id": "dash-id-001"},
            ],
            "count": 5,
            "workspace_id": ws_id,
        }

    @respx.mock
    async def test_list_powerbi_content_datasets_only(self, mcp_server):
        ws_id = "ws-id-001"
        respx.get(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DATASETS_RESPONSE)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "list_powerbi",
                {"workspace_id": ws_id, "content_type": "datasets"},
            )

        data = _structured(result)
        assert data["count"] == 2
        assert {row["kind"] for row in data["items"]} == {"dataset"}
        assert _graph_trail() == [("GET", f"/v1.0/myorg/groups/{ws_id}/datasets")]

    @respx.mock
    async def test_list_powerbi_content_my_workspace(self, mcp_server):
        """workspace_id='me' routes to the root endpoints, not /groups/me/."""
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
            result = await _call(mcp_server, "list_powerbi", {"workspace_id": "me"})

        data = _structured(result)
        assert data["workspace_id"] == "me"
        assert data["count"] == 5
        assert not any("/groups/" in path for _, path in _graph_trail())

    async def test_list_powerbi_invalid_content_type(self, mcp_server):
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "list_powerbi",
                {"workspace_id": "ws-id-001", "content_type": "tiles"},
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": (
                "Invalid content_type 'tiles'. Must be: datasets, reports, dashboards, or all."
            ),
        }

    async def test_list_powerbi_not_connected(self, mcp_server):
        with _mock_missing_pbi_connection():
            result = await _call(mcp_server, "list_powerbi")

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_query_dataset(self, mcp_server):
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        route = respx.post(
            f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/executeQueries"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT))
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": ws_id, "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
            )

        assert _structured(result) == {
            "rows": SAMPLE_PBI_DAX_RESULT["results"][0]["tables"][0]["rows"],
            "count": 3,
        }
        body = json.loads(route.calls[0].request.content)
        assert body["queries"][0]["query"] == "EVALUATE 'Sales'"

    @respx.mock
    async def test_query_dataset_keeps_sparse_rows(self, mcp_server):
        """Power BI omits null columns per row; the raw rows survive untouched,
        and the renderer unions the headers."""
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        sparse = {
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
        }
        respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/executeQueries").mock(
            return_value=httpx.Response(200, json=sparse)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": ws_id, "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
            )

        assert _structured(result) == {
            "rows": [
                {"[Region]": "West", "[Units]": 4200},
                {"[Region]": "East", "[Margin]": 0.12},
            ],
            "count": 2,
        }

    @respx.mock
    async def test_query_dataset_no_rows(self, mcp_server):
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/executeQueries").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_EMPTY)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": ws_id, "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
            )

        assert _structured(result) == {"rows": [], "count": 0}

    @respx.mock
    async def test_query_dataset_my_workspace(self, mcp_server):
        """workspace_id='me' routes to root /datasets/... not /groups/me/..."""
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{ds_id}/executeQueries").mock(
            return_value=httpx.Response(200, json=SAMPLE_PBI_DAX_RESULT)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": "me", "dataset_id": ds_id, "dax_query": "EVALUATE 'Sales'"},
            )

        assert _structured(result)["count"] == 3
        assert "/groups/" not in str(route.calls[0].request.url)

    async def test_query_dataset_not_connected(self, mcp_server):
        with _mock_missing_pbi_connection():
            result = await _call(
                mcp_server,
                "query_dataset",
                {"workspace_id": "ws-1", "dataset_id": "ds-1", "dax_query": "EVALUATE {1}"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_refresh_dataset(self, mcp_server):
        ws_id = "ws-id-001"
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/groups/{ws_id}/datasets/{ds_id}/refreshes").mock(
            return_value=httpx.Response(202)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "refresh_dataset",
                {"workspace_id": ws_id, "dataset_id": ds_id},
            )

        assert _structured(result) == {
            "ok": True,
            "dataset_id": ds_id,
            "workspace_id": ws_id,
        }
        assert route.called

    @respx.mock
    async def test_refresh_dataset_my_workspace(self, mcp_server):
        """workspace_id='me' routes to root /datasets/... not /groups/me/..."""
        ds_id = "ds-id-001"
        route = respx.post(f"{POWERBI_BASE_URL}/datasets/{ds_id}/refreshes").mock(
            return_value=httpx.Response(202)
        )
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "refresh_dataset",
                {"workspace_id": "me", "dataset_id": ds_id},
            )

        assert _structured(result) == {"ok": True, "dataset_id": ds_id, "workspace_id": "me"}
        assert "/groups/" not in str(route.calls[0].request.url)

    async def test_refresh_dataset_not_connected(self, mcp_server):
        with _mock_missing_pbi_connection():
            result = await _call(
                mcp_server,
                "refresh_dataset",
                {"workspace_id": "ws-1", "dataset_id": "ds-1"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_export_report_onedrive_upload_failure_degrades_gracefully(self, mcp_server):
        """The bytes are already downloaded, so a dead Graph connection reports
        the loss rather than raising mid-flight."""
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
        with _mock_pbi_token(), _mock_missing_connection():
            result = await _call(
                mcp_server,
                "export_report",
                {"workspace_id": ws_id, "report_id": rpt_id, "export_format": "PDF"},
            )

        assert _structured(result) == {
            "error": "not_connected",
            "connect_url": CONNECT_URL,
            "reason": (
                "Report exported as PDF (9 bytes) but could not be saved to OneDrive "
                "because the Microsoft connection is not active."
            ),
        }

    @respx.mock
    async def test_export_report_success(self, mcp_server):
        """Export flow: PBI export → download bytes → upload to OneDrive."""
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
            result = await _call(
                mcp_server,
                "export_report",
                {"workspace_id": ws_id, "report_id": rpt_id, "export_format": "PDF"},
            )

        assert _structured(result) == {
            "ok": True,
            "format": "PDF",
            "filename": f"report-{rpt_id}.pdf",
            "size": len(fake_pdf),
            "folder_path": "Power BI Exports",
            "item_id": SAMPLE_UPLOADED_FILE["id"],
            "web_url": SAMPLE_UPLOADED_FILE["webUrl"],
        }
        assert upload_route.calls[0].request.headers["Content-Type"] == "application/pdf"
        assert upload_route.calls[0].request.content == fake_pdf

    async def test_export_report_invalid_format(self, mcp_server):
        with _mock_pbi_token():
            result = await _call(
                mcp_server,
                "export_report",
                {"workspace_id": "ws-1", "report_id": "rpt-1", "export_format": "DOCX"},
            )

        assert _structured(result) == {
            "error": "invalid_arguments",
            "reason": "Invalid export_format 'DOCX'. Must be: PDF, PNG, or PPTX.",
        }

    async def test_export_report_not_connected(self, mcp_server):
        with _mock_missing_pbi_connection():
            result = await _call(
                mcp_server,
                "export_report",
                {"workspace_id": "ws-1", "report_id": "rpt-1"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_powerbi_graph_error_propagates(self, mcp_server):
        """A Power BI 401 is transient auth trouble, not a permanent answer."""
        from fastmcp.exceptions import ToolError

        respx.get(f"{POWERBI_BASE_URL}/groups").mock(
            return_value=httpx.Response(
                401, json={"error": {"code": "Unauthorized", "message": "Token expired."}}
            )
        )
        with _mock_pbi_token(), pytest.raises(ToolError, match="Unauthorized"):
            await _call(mcp_server, "list_powerbi")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


# TestMCPAuth lived here. No tool raises on a missing token any more: every one
# of them returns the not_connected dict, which each tool's own class pins.


# ---------------------------------------------------------------------------
# Desktop JSON tools
# ---------------------------------------------------------------------------


def _structured(result) -> dict:
    """Extract the dict a Desktop JSON tool returned.

    FastMCP surfaces dict returns as structuredContent; fall back to parsing
    the text block for client versions that do not populate it.
    """
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(_get_text(result))


def _mock_missing_connection(connect_url=CONNECT_URL):
    """Patch get_graph_token to raise as JWT mode does for an unconnected user."""
    return patch(
        "ms_graph_mcp.get_graph_token",
        side_effect=MissingProviderConnection(
            provider="microsoft", user_key="u", connect_url=connect_url
        ),
    )


def _mock_missing_pbi_connection(connect_url=CONNECT_URL):
    """The same, for the separate Power BI connection entry."""
    return patch(
        "ms_graph_mcp.get_powerbi_token",
        side_effect=MissingProviderConnection(
            provider="microsoft_powerbi", user_key="u", connect_url=connect_url
        ),
    )


async def _call(mcp_server, name, args=None):
    from fastmcp import Client

    async with Client(mcp_server) as client:
        return await client.call_tool(name, args or {})


class TestPostExchangeScopesKey:
    """The token-exchange shim must persist scopes under the storage key."""

    def test_scope_is_persisted_as_scopes(self):
        from ms_graph_mcp import _microsoft_post_exchange

        out = _microsoft_post_exchange(
            {"access_token": "a", "scope": "Mail.Read Chat.Read", "expires_in": 3600}
        )

        assert out["scopes"] == "Mail.Read Chat.Read"
        assert "scope" not in out
        assert out["access_token"] == "a"
        assert "expires_at" in out

    def test_absent_scope_is_omitted(self):
        from ms_graph_mcp import _microsoft_post_exchange

        out = _microsoft_post_exchange({"access_token": "a"})

        assert "scopes" not in out
        assert "scope" not in out


USERS_SEARCH_PREFIX = f"{GRAPH_BASE_URL}/users?"


def _users_query() -> dict:
    """The decoded query string of the one /users request respx saw."""
    for call in respx.calls:
        if call.request.url.path == "/v1.0/users":
            return parse_qs(urlparse(str(call.request.url)).query)
    raise AssertionError("no /users request was made")


class TestMCPSearchPeople:
    """search_people."""

    @respx.mock
    async def test_blank_query_returns_empty_without_graph_or_token(self, mcp_server):
        """A typeahead fires on every keystroke, so an empty box costs nothing."""
        result = await _call(mcp_server, "search_people", {"query": "   "})

        assert _structured(result) == {"people": []}
        assert _graph_trail() == []

    @respx.mock
    async def test_results_are_flattened_with_nulls_kept(self, mcp_server):
        route = respx.get(url__startswith=USERS_SEARCH_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_people", {"query": "smi"})

        assert _structured(result) == {
            "people": [
                {
                    "id": "user-id-002",
                    "display_name": "Alice Smith",
                    "mail": "alice@example.com",
                    "user_principal_name": "alice@example.com",
                    "job_title": "Engineer",
                },
                {
                    "id": "user-id-003",
                    "display_name": "Smith Room",
                    "mail": None,
                    "user_principal_name": "smithroom@example.com",
                    "job_title": None,
                },
            ]
        }
        assert route.calls[0].request.headers["ConsistencyLevel"] == "eventual"
        query = _users_query()
        assert query["$search"] == ['"displayName:smi" OR "mail:smi"']
        assert query["$top"] == ["10"]

    @respx.mock
    async def test_top_is_passed_and_clamped(self, mcp_server):
        respx.get(url__startswith=USERS_SEARCH_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with _mock_token():
            await _call(mcp_server, "search_people", {"query": "smi", "top": 500})

        assert _users_query()["$top"] == ["50"]

    @respx.mock
    async def test_nothing_searchable_returns_empty_without_graph(self, mcp_server):
        """ "&" is dropped by the escaper; what is left is blank, so no request."""
        with _mock_token():
            result = await _call(mcp_server, "search_people", {"query": " & "})

        assert _structured(result) == {"people": []}
        assert _graph_trail() == []

    @respx.mock
    async def test_query_is_stripped(self, mcp_server):
        respx.get(url__startswith=USERS_SEARCH_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with _mock_token():
            await _call(mcp_server, "search_people", {"query": "  smi  "})

        assert _users_query()["$search"] == ['"displayName:smi" OR "mail:smi"']

    @respx.mock
    async def test_403_is_directory_scope_missing(self, mcp_server):
        respx.get(url__startswith=USERS_SEARCH_PREFIX).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token():
            result = await _call(mcp_server, "search_people", {"query": "smi"})

        assert _structured(result) == {"error": "directory_scope_missing"}

    @respx.mock
    async def test_429_propagates_as_tool_error(self, mcp_server):
        """Throttling is transient, so it must not look like a permanent answer."""
        from fastmcp.exceptions import ToolError

        respx.get(url__startswith=USERS_SEARCH_PREFIX).mock(
            return_value=httpx.Response(
                429, json={"error": {"code": "TooManyRequests", "message": "throttled"}}
            )
        )
        with _mock_token():
            with pytest.raises(ToolError, match="429"):
                await _call(mcp_server, "search_people", {"query": "smi"})

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "search_people", {"query": "smi"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPSyncMail:
    """sync_mail."""

    @respx.mock
    async def test_terminal_run_passes_messages_through_raw(self, mcp_server):
        """A run that reaches a deltaLink returns the message raw, no next_cursor."""
        page = {"@odata.deltaLink": SAMPLE_DELTA_LINK, "value": [SAMPLE_DELTA_MESSAGE]}
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(200, json=page)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "sync_mail", {"folder": "inbox", "min_received": "2026-01-01T00:00:00Z"}
            )

        data = _structured(result)
        assert data["messages"] == [SAMPLE_DELTA_MESSAGE]
        assert data["next_cursor"] == ""
        assert data["delta_cursor"] == SAMPLE_DELTA_LINK
        assert data["has_more"] is False
        assert data["resync"] is False

    @respx.mock
    async def test_page_cap_surfaces_next_cursor_and_has_more(self, mcp_server):
        """A self-referential nextLink is drained only up to the page cap; the
        surviving cursor comes back as next_cursor with has_more=True."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"min_received": "2026-01-01T00:00:00Z"})

        data = _structured(result)
        assert data["next_cursor"] == SAMPLE_DELTA_NEXT_LINK
        assert data["has_more"] is True
        assert data["delta_cursor"] == ""
        # One SAMPLE_DELTA_MESSAGE per page, capped at mail._MAX_PAGES pages.
        assert len(data["messages"]) == mail._MAX_PAGES
        assert data["resync"] is False

    @respx.mock
    async def test_fresh_sync_defaults_to_seven_day_floor(self, mcp_server):
        """A cursorless call with no min_received must not enumerate the whole
        mailbox — it applies a receivedDateTime floor of 7 days ago."""
        route = respx.get(
            url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL))
        with _mock_token():
            await _call(mcp_server, "sync_mail", {})

        url = str(route.calls[0].request.url)
        assert "$filter=receivedDateTime%20ge%20" in url
        floor = unquote(url.split("$filter=receivedDateTime%20ge%20", 1)[1].split("&", 1)[0])
        floor_dt = datetime.fromisoformat(floor.replace("Z", "+00:00"))
        expected = datetime.now(timezone.utc) - timedelta(days=7)
        assert abs((floor_dt - expected).total_seconds()) < 120

    @pytest.mark.parametrize("bad", ["last tuesday", "2026-13-01", "2026-02-30T00:00:00Z"])
    async def test_invalid_min_received_returns_invalid_date(self, mcp_server, bad):
        """A semantically bad date is rejected up front, not silently dropped."""
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"min_received": bad})

        assert _structured(result)["error"] == "invalid_date"

    @respx.mock
    async def test_floor_applies_to_a_client_supplied_cursor(self, mcp_server):
        """The core contract: min_received on a continuation call drops older
        mail from the cursor-fetched page, keeping recent mail and tombstones."""
        old_message = {
            **SAMPLE_DELTA_MESSAGE,
            "id": "old002=",
            "receivedDateTime": "2025-11-15T09:00:00Z",
        }
        page = {
            "@odata.deltaLink": SAMPLE_DELTA_LINK,
            "value": [SAMPLE_DELTA_MESSAGE, old_message, SAMPLE_DELTA_TOMBSTONE],
        }
        route = respx.get(SAMPLE_DELTA_NEXT_LINK).mock(return_value=httpx.Response(200, json=page))
        with _mock_token():
            result = await _call(
                mcp_server,
                "sync_mail",
                {"cursor": SAMPLE_DELTA_NEXT_LINK, "min_received": "2026-01-01T00:00:00Z"},
            )

        # Cursor fetched verbatim (the floor is applied client-side, not rebuilt
        # into the opaque cursor URL).
        assert str(route.calls[0].request.url) == SAMPLE_DELTA_NEXT_LINK
        data = _structured(result)
        assert data["messages"] == [SAMPLE_DELTA_MESSAGE, SAMPLE_DELTA_TOMBSTONE]
        assert data["delta_cursor"] == SAMPLE_DELTA_LINK
        assert data["has_more"] is False

    @respx.mock
    async def test_continuation_without_floor_is_not_post_filtered(self, mcp_server):
        """A cursor call with no min_received neither injects the 7-day default
        nor post-filters: the page cap is the only bound, so old mail survives."""
        old_message = {
            **SAMPLE_DELTA_MESSAGE,
            "id": "old003=",
            "receivedDateTime": "2019-01-01T00:00:00Z",
        }
        page = {"@odata.deltaLink": SAMPLE_DELTA_LINK, "value": [old_message]}
        route = respx.get(SAMPLE_DELTA_NEXT_LINK).mock(return_value=httpx.Response(200, json=page))
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"cursor": SAMPLE_DELTA_NEXT_LINK})

        assert str(route.calls[0].request.url) == SAMPLE_DELTA_NEXT_LINK
        data = _structured(result)
        assert data["messages"] == [old_message]
        assert data["has_more"] is False

    @respx.mock
    async def test_floor_is_reapplied_to_every_drained_page(self, mcp_server, monkeypatch):
        """An older message surfacing on page 2 is dropped, tombstones and
        recent mail survive, and external senders stay hidden."""
        _policy_on(monkeypatch)
        old_message = {
            **SAMPLE_DELTA_MESSAGE,
            "id": "old001=",
            "receivedDateTime": "2025-12-01T09:00:00Z",
        }
        page2 = {
            "@odata.deltaLink": SAMPLE_DELTA_LINK,
            "value": [old_message, SAMPLE_DELTA_TOMBSTONE],
        }
        page1 = {
            "@odata.nextLink": SAMPLE_DELTA_NEXT_LINK,
            "value": [SAMPLE_DELTA_MESSAGE, SAMPLE_EXTERNAL_DELTA_MESSAGE],
        }
        # Register the exact nextLink route first so the page-2 fetch matches it
        # rather than the broad fresh-start route.
        respx.get(SAMPLE_DELTA_NEXT_LINK).mock(return_value=httpx.Response(200, json=page2))
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(200, json=page1)
        )
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"min_received": "2026-01-01T00:00:00Z"})

        data = _structured(result)
        assert data["messages"] == [SAMPLE_DELTA_MESSAGE, SAMPLE_DELTA_TOMBSTONE]
        assert data["delta_cursor"] == SAMPLE_DELTA_LINK
        assert data["has_more"] is False
        _assert_no_canary(data)

    @respx.mock
    async def test_tombstones_pass_through_untouched(self, mcp_server):
        """The client's fold logic owns @removed entries — do not filter them here."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {})

        data = _structured(result)
        assert data["messages"] == [SAMPLE_DELTA_TOMBSTONE]
        assert data["messages"][0]["@removed"] == {"reason": "deleted"}
        assert data["next_cursor"] == ""
        assert data["delta_cursor"] == SAMPLE_DELTA_LINK

    @respx.mock
    async def test_cursor_is_requested_verbatim(self, mcp_server):
        route = respx.get(SAMPLE_DELTA_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"cursor": SAMPLE_DELTA_NEXT_LINK})

        assert route.call_count == 1
        assert str(route.calls[0].request.url) == SAMPLE_DELTA_NEXT_LINK
        assert _structured(result)["delta_cursor"] == SAMPLE_DELTA_LINK

    @respx.mock
    async def test_expired_cursor_returns_resync(self, mcp_server):
        respx.get(SAMPLE_DELTA_LINK).mock(return_value=httpx.Response(410, json=GRAPH_ERROR_410))
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"cursor": SAMPLE_DELTA_LINK})

        assert _structured(result) == {
            "messages": [],
            "next_cursor": "",
            "delta_cursor": "",
            "has_more": False,
            "resync": True,
        }

    @respx.mock
    async def test_other_graph_errors_propagate(self, mcp_server):
        """A 500 is the client's "transient, retry later" signal — not a resync."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(500, json={"error": {"code": "x", "message": "boom"}})
        )
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="500"):
                await _call(mcp_server, "sync_mail", {})

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "sync_mail", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    async def test_plain_permission_error_has_no_connect_url(self, mcp_server):
        """Laptop (MSAL) mode raises a bare PermissionError with no URL to offer."""
        with patch("ms_graph_mcp.get_graph_token", side_effect=PermissionError("no auth")):
            result = await _call(mcp_server, "sync_mail", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": None}


class TestMCPGetMailDetailContract:
    """read_email — the five keys get_mail_detail froze.

    The merged payload grew a flat envelope around them, so each frozen key is
    pinned on its own rather than as a whole dict: adding keys is allowed,
    changing these is not.
    """

    @respx.mock
    async def test_lowercases_headers_and_keeps_first_occurrence(self, mcp_server):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_DETAIL)
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": SAMPLE_MESSAGE["id"]})

        data = _structured(result)
        assert data["body_text"] == "Here is the weekly report.\n\nBest,\nAlice"
        assert data["has_attachments"] is True
        assert data["headers"]["message-id"] == "<abc123@example.com>"
        assert data["headers"]["in-reply-to"] == "<parent@example.com>"
        # "Received" appeared twice with different casing; the first one wins.
        assert data["headers"]["received"] == "from mx1.example.com"

        # The attachments ride along on the $expand — no second request.
        assert route.call_count == 1
        assert data["attachment_count"] == 3
        assert data["attachments"][0] == {
            "id": SAMPLE_FILE_ATTACHMENT["id"],
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size": 1_258_291,
            "is_inline": False,
            "content_id": None,
            "kind": "file",
            "source_url": None,
        }
        assert data["attachments"][1]["is_inline"] is True
        assert data["attachments"][1]["content_id"] == "logo@company"
        assert data["attachments"][2]["kind"] == "reference"
        assert data["attachments"][2]["source_url"] == SAMPLE_REFERENCE_ATTACHMENT["sourceUrl"]

    @respx.mock
    async def test_attachment_list_is_capped_but_count_is_true(self, mcp_server):
        """A pathological message lists 50 and still reports how many there are."""
        many = [{**SAMPLE_FILE_ATTACHMENT, "id": f"att-{i}"} for i in range(51)]
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MESSAGE_DETAIL, "attachments": many})
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": SAMPLE_MESSAGE["id"]})

        data = _structured(result)
        assert len(data["attachments"]) == 50
        assert data["attachment_count"] == 51
        assert data["attachments"][-1]["id"] == "att-49"

    @respx.mock
    async def test_missing_unique_body_becomes_empty_string(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_DETAIL_NO_BODY)
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": SAMPLE_MESSAGE["id"]})

        data = _structured(result)
        assert data["body_text"] == ""
        assert data["headers"] == {}
        assert data["has_attachments"] is False
        assert data["attachments"] == []
        assert data["attachment_count"] == 0

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "read_email", {"message_id": "m"})

        assert _structured(result)["error"] == "not_connected"


class TestMCPGetMailAttachmentJsonContract:
    """get_mail_attachment — the dict contract get_mail_attachment_json froze."""

    @respx.mock
    async def test_metadata_mode_returns_the_summary_only(self, mcp_server):
        value_route = respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        respx.get(ATT_FILE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "metadata",
                },
            )

        assert _structured(result) == {
            "id": SAMPLE_FILE_ATTACHMENT["id"],
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size": 1_258_291,
            "is_inline": False,
            "content_id": None,
            "kind": "file",
            "source_url": None,
        }
        assert not value_route.called

    @respx.mock
    async def test_metadata_mode_adds_the_inner_message_fields(self, mcp_server):
        inner = {
            "subject": "Budget draft",
            "from": {"emailAddress": {"name": "Dana Lee", "address": "dana@example.com"}},
            "receivedDateTime": "2025-12-01T09:00:00Z",
        }

        def _respond(request):
            if "expand" in str(request.url):
                return httpx.Response(200, json={**SAMPLE_ITEM_ATTACHMENT, "item": inner})
            return httpx.Response(200, json=SAMPLE_ITEM_ATTACHMENT)

        respx.get(url__startswith=ATT_ITEM_URL).mock(side_effect=_respond)
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"],
                    "mode": "metadata",
                },
            )

        data = _structured(result)
        assert data["kind"] == "item"
        assert data["item_subject"] == "Budget draft"
        assert data["item_from"] == "dana@example.com"
        assert data["item_received"] == "2025-12-01T09:00:00Z"

    @respx.mock
    async def test_text_mode_decodes_a_plain_text_attachment(self, mcp_server):
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello there", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "notes.txt",
                    "contentType": "text/plain",
                    "size": 11,
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "text",
                },
            )

        data = _structured(result)
        assert data["text"] == "hello there"
        assert data["truncated"] is False
        assert "reason" not in data

    @respx.mock
    async def test_text_mode_extracts_a_word_document(self, mcp_server):
        docx = _docx_bytes()
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=docx, headers={"Content-Type": DOCX_MIME})
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "report.docx",
                    "contentType": DOCX_MIME,
                    "size": len(docx),
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "text",
                },
            )

        data = _structured(result)
        assert "Quarterly Title" in data["text"]
        assert data["content_type"] == DOCX_MIME

    @respx.mock
    async def test_text_mode_on_a_binary_reports_the_reason(self, mcp_server):
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"}
            )
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "logo.png",
                    "contentType": "image/png",
                    "size": 8,
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "text",
                },
            )

        data = _structured(result)
        assert data["text"] is None
        assert data["reason"] == "binary"
        assert data["truncated"] is False

    @respx.mock
    async def test_text_mode_on_a_link_attachment_reports_reference(self, mcp_server):
        respx.get(ATT_REF_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_REFERENCE_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_REFERENCE_ATTACHMENT["id"],
                    "mode": "text",
                },
            )

        data = _structured(result)
        assert data["text"] is None
        assert data["reason"] == "reference"
        assert data["source_url"] == SAMPLE_REFERENCE_ATTACHMENT["sourceUrl"]

    @respx.mock
    async def test_text_mode_on_an_item_returns_the_inner_body(self, mcp_server):
        inner = {
            "subject": "Budget draft",
            "from": {"emailAddress": {"address": "dana@example.com"}},
            "receivedDateTime": "2025-12-01T09:00:00Z",
            "bodyPreview": "Numbers attached",
            "body": {"contentType": "text", "content": "Numbers attached, see inside."},
        }

        def _respond(request):
            if "expand" in str(request.url):
                return httpx.Response(200, json={**SAMPLE_ITEM_ATTACHMENT, "item": inner})
            return httpx.Response(200, json=SAMPLE_ITEM_ATTACHMENT)

        respx.get(url__startswith=ATT_ITEM_URL).mock(side_effect=_respond)
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"],
                    "mode": "text",
                },
            )

        data = _structured(result)
        assert data["text"] == "Numbers attached, see inside."
        assert data["truncated"] is False
        assert data["item_subject"] == "Budget draft"

    @respx.mock
    async def test_bytes_mode_returns_the_content(self, mcp_server):
        payload = b"\x89PNG\r\n\x1a\n"
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=payload, headers={"Content-Type": "image/png"})
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "logo.png",
                    "contentType": "image/png",
                    "size": len(payload),
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "bytes",
                },
            )

        data = _structured(result)
        assert base64.b64decode(data["content_base64"]) == payload
        assert data["content_type"] == "image/png"
        assert data["kind"] == "file"

    @respx.mock
    async def test_bytes_mode_refuses_oversize_without_downloading(self, mcp_server):
        value_route = respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_FILE_ATTACHMENT, "size": 20_000_000})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "too_large",
            "size": 20_000_000,
            "limit": 10_000_000,
        }
        assert not value_route.called

    @respx.mock
    async def test_bytes_mode_on_a_link_attachment_returns_the_url(self, mcp_server):
        respx.get(ATT_REF_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_REFERENCE_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_REFERENCE_ATTACHMENT["id"],
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "reference",
            "source_url": SAMPLE_REFERENCE_ATTACHMENT["sourceUrl"],
        }

    @respx.mock
    async def test_bytes_mode_on_an_item_falls_back_to_rfc822(self, mcp_server):
        """An item attachment has no contentType, but its $value is a MIME message."""
        respx.get(f"{ATT_ITEM_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"From: dana@example.com")
        )
        respx.get(ATT_ITEM_URL).mock(
            return_value=httpx.Response(200, json={**SAMPLE_ITEM_ATTACHMENT, "size": 22})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"],
                    "mode": "bytes",
                },
            )

        data = _structured(result)
        assert base64.b64decode(data["content_base64"]) == b"From: dana@example.com"
        assert data["content_type"] == "message/rfc822"

    async def test_invalid_mode_makes_no_request(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": "a", "mode": "pdf"},
            )

        assert _structured(result) == {
            "error": "invalid_mode",
            "reason": "mode must be one of: metadata, text, bytes, onedrive; got 'pdf'",
        }

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": "a", "mode": "bytes"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_unknown_attachment_propagates_as_a_tool_error(self, mcp_server):
        respx.get(ATT_FILE_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="404"):
                await _call(
                    mcp_server,
                    "get_mail_attachment",
                    {
                        "message_id": ATT_MSG_ID,
                        "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                        "mode": "bytes",
                    },
                )


class TestMCPAddDraftAttachmentJsonContract:
    """manage_draft action="add_attachment" — the dict add_draft_attachment_json froze."""

    ACTION = {"action": "add_attachment"}

    @respx.mock
    async def test_attaches_and_returns_the_id(self, mcp_server):
        route = respx.post(f"{DRAFT_BASE}/attachments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": base64.b64encode(b"hello world").decode("ascii"),
                },
            )

        assert _structured(result) == {"attachment_id": SAMPLE_CREATED_ATTACHMENT["id"]}
        payload = json.loads(route.calls[0].request.content)
        assert payload["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert payload["name"] == "notes.txt"
        assert payload["contentType"] == "text/plain"
        assert base64.b64decode(payload["contentBytes"]) == b"hello world"

    @respx.mock
    async def test_explicit_content_type_wins_over_the_guess(self, mcp_server):
        route = respx.post(f"{DRAFT_BASE}/attachments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": base64.b64encode(b"hi").decode("ascii"),
                    "content_type": "text/markdown",
                },
            )

        assert json.loads(route.calls[0].request.content)["contentType"] == "text/markdown"

    @respx.mock
    async def test_empty_name_is_rejected(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "   ",
                    "content_base64": base64.b64encode(b"hi").decode("ascii"),
                },
            )

        assert _structured(result) == {"error": "empty_name"}
        assert _graph_trail() == []

    @respx.mock
    async def test_invalid_base64_is_rejected(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": "not base64!!",
                },
            )

        assert _structured(result) == {"error": "invalid_base64"}
        assert _graph_trail() == []

    @respx.mock
    async def test_empty_content_is_rejected(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": "",
                },
            )

        assert _structured(result) == {"error": "invalid_base64"}
        assert _graph_trail() == []

    @respx.mock
    async def test_oversize_is_rejected_before_any_request(self, mcp_server, monkeypatch):
        from ms_graph import attachments as attachment_ops

        monkeypatch.setattr(attachment_ops, "MAX_ATTACHMENT_BYTES", 4)
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": base64.b64encode(b"hello world").decode("ascii"),
                },
            )

        assert _structured(result) == {"error": "too_large", "size": 11, "limit": 4}
        assert _graph_trail() == []

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": base64.b64encode(b"hi").decode("ascii"),
                },
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPDraftFlow:
    """manage_draft: reply, then update_body, add_attachment, and send."""

    @respx.mock
    async def test_create_reply_draft_returns_id_and_link(self, mcp_server):
        respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    "action": "reply",
                    "message_id": SAMPLE_MESSAGE["id"],
                    "timezone": "America/New_York",
                },
            )

        assert _structured(result) == {
            "id": "AAMkAGI2draft001=",
            "web_link": "https://outlook.office.com/mail/deeplink/AAMkAGI2draft001",
            "conversation_id": "AAQkAGI2conv001=",
            "internet_message_id": "<draft001@example.com>",
            "subject": "Re: Weekly Report",
            "to": [{"name": "Alice Smith", "address": "alice@example.com"}],
            "cc": [],
        }

    @respx.mock
    async def test_create_reply_draft_retries_once_without_timezone_header(self, mcp_server):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            side_effect=[
                httpx.Response(400, json=GRAPH_ERROR_400),
                httpx.Response(201, json=SAMPLE_REPLY_DRAFT),
            ]
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {"action": "reply", "message_id": SAMPLE_MESSAGE["id"], "timezone": "Bad/Zone"},
            )

        assert route.call_count == 2
        assert _structured(result)["id"] == "AAMkAGI2draft001="

    @respx.mock
    async def test_create_reply_draft_missing_web_link_is_empty(self, mcp_server):
        respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json={"id": "d1"})
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {"action": "reply", "message_id": "m1"}
            )

        assert _structured(result) == {
            "id": "d1",
            "web_link": "",
            "conversation_id": None,
            "internet_message_id": None,
            "subject": None,
            "to": [],
            "cc": [],
        }

    @respx.mock
    async def test_update_draft_body_sends_text_content_type(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPLY_DRAFT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {"action": "update_body", "draft_id": "AAMkAGI2draft001=", "text": "On it."},
            )

        assert _structured(result) == {"ok": True}
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"body": {"contentType": "text", "content": "On it."}}

    @respx.mock
    async def test_send_draft_returns_the_ids_the_sent_copy_carries(self, mcp_server, monkeypatch):
        """The pre-send read is the only chance to learn them, so it happens first."""
        monkeypatch.setattr("ms_graph_mcp._utcnow_iso", lambda: "2026-09-06T12:00:00Z")
        get_route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRAFT_FOR_SEND)
        )
        respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(202)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {"action": "send", "draft_id": "AAMkAGI2draft001="}
            )

        data = _structured(result)
        assert data == {
            "ok": True,
            "id": "AAMkAGI2draft001=",
            "conversation_id": "AAQkAGI2conv001=",
            "internet_message_id": "<draft001@example.com>",
            "subject": "Re: Weekly Report",
            "to": [{"name": "Alice Smith", "address": "alice@example.com"}],
            # The malformed second cc entry drops out rather than sinking the send.
            "cc": [{"name": "Charlie Brown", "address": "charlie@example.com"}],
            "sent_at": "2026-09-06T12:00:00Z",
        }
        # A sent draft's deep link is dead, so no web_link is offered at all.
        assert "web_link" not in data
        assert _select_of(get_route.calls[0].request) == mail.DRAFT_SEND_SELECT

    @respx.mock
    async def test_send_draft_reads_before_it_sends(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRAFT_FOR_SEND)
        )
        respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(202)
        )
        with _mock_token():
            await _call(
                mcp_server, "manage_draft", {"action": "send", "draft_id": "AAMkAGI2draft001="}
            )

        methods_and_suffixes = [
            (method, path.split("/me/messages/")[-1]) for method, path in _graph_trail()
        ]
        assert methods_and_suffixes == [
            ("GET", "AAMkAGI2draft001="),
            ("POST", "AAMkAGI2draft001=/send"),
        ]

    @respx.mock
    async def test_send_draft_sends_nothing_when_the_read_fails(self, mcp_server):
        """A failed read is a tool error, and the draft is still there to retry."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        post_route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(202)
        )
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="404"):
                await _call(
                    mcp_server,
                    "manage_draft",
                    {"action": "send", "draft_id": "AAMkAGI2draft001="},
                )

        assert not post_route.called
        assert not any(method == "POST" for method, _ in _graph_trail())

    async def test_send_draft_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "manage_draft", {"action": "send", "draft_id": "d1"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPCreateDraftJsonContract:
    """manage_draft action="create" — the dict create_draft_json froze.

    A fresh outbound draft, not a reply.
    """

    URL = f"{GRAPH_BASE_URL}/me/messages"
    ACTION = {"action": "create"}
    EXPECTED = {
        "id": "AAMkAGI2draft888=",
        "web_link": "https://outlook.office.com/mail/deeplink/AAMkAGI2draft888",
        "conversation_id": "AAQkAGI2conv888=",
        "internet_message_id": "<draft888@example.com>",
        "subject": "Lunch?",
        "to": [
            {"name": "Alice Smith", "address": "alice@example.com"},
            {"name": "Bob Jones", "address": "bob@example.com"},
        ],
        "cc": [],
    }

    @respx.mock
    async def test_posts_a_text_draft_and_returns_the_draft_shape(self, mcp_server):
        route = respx.post(self.URL).mock(return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    **self.ACTION,
                    "to": "a@example.com, b@example.com",
                    "subject": "Lunch?",
                    "text": "1 < 2, so noon works.",
                    "cc": "c@example.com",
                },
            )

        assert _structured(result) == self.EXPECTED
        payload = json.loads(route.calls[0].request.content)
        # Pinned to text: a typed "<" must reach the recipient as a "<".
        assert payload["body"] == {"contentType": "Text", "content": "1 < 2, so noon works."}
        assert payload["toRecipients"] == [
            {"emailAddress": {"address": "a@example.com"}},
            {"emailAddress": {"address": "b@example.com"}},
        ]
        assert payload["ccRecipients"] == [{"emailAddress": {"address": "c@example.com"}}]
        # An absent bcc is omitted, not sent empty; and the draft is the user's own.
        assert "bccRecipients" not in payload
        assert "from" not in payload

    @respx.mock
    async def test_empty_to_still_creates_a_draft(self, mcp_server):
        """A skeleton the user finishes in Outlook through web_link."""
        route = respx.post(self.URL).mock(return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT))
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {**self.ACTION, "to": "", "subject": "Draft it"}
            )

        assert _structured(result) == self.EXPECTED
        payload = json.loads(route.calls[0].request.content)
        assert payload["toRecipients"] == []
        assert "ccRecipients" not in payload
        assert "bccRecipients" not in payload

    @respx.mock
    async def test_bcc_is_sent_when_given(self, mcp_server):
        route = respx.post(self.URL).mock(return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT))
        with _mock_token():
            await _call(
                mcp_server,
                "manage_draft",
                {**self.ACTION, "to": "a@example.com", "subject": "s", "bcc": " x@example.com , "},
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["bccRecipients"] == [{"emailAddress": {"address": "x@example.com"}}]

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server, "manage_draft", {**self.ACTION, "to": "a@example.com", "subject": "s"}
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPManageDraft:
    """The dispatch itself: what the merged surface refuses before it acts."""

    @respx.mock
    async def test_unknown_action_is_refused(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_draft", {"action": "discard"})

        assert _structured(result) == {
            "error": "invalid_action",
            "reason": (
                "Unknown action 'discard'. Must be: create, reply, update_body, "
                "add_attachment, or send."
            ),
        }
        assert _graph_trail() == []

    @respx.mock
    @pytest.mark.parametrize(
        ("args", "reason"),
        [
            ({"action": "reply"}, "message_id is required for the 'reply' action."),
            (
                {"action": "update_body", "text": "hi"},
                "draft_id is required for the 'update_body' action.",
            ),
            (
                {"action": "add_attachment", "name": "a.txt", "content_base64": "aGk="},
                "draft_id is required for the 'add_attachment' action.",
            ),
            ({"action": "send"}, "draft_id is required for the 'send' action."),
        ],
    )
    async def test_a_missing_id_is_refused_before_the_token(self, mcp_server, args, reason):
        with _mock_token():
            result = await _call(mcp_server, "manage_draft", args)

        assert _structured(result) == {"error": "invalid_arguments", "reason": reason}
        assert _graph_trail() == []

    @respx.mock
    async def test_create_needs_nothing_but_the_action(self, mcp_server):
        """An empty `to` is a legal skeleton draft, so create validates nothing."""
        respx.post(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT)
        )
        with _mock_token():
            result = await _call(mcp_server, "manage_draft", {"action": "create"})

        assert _structured(result)["id"] == SAMPLE_NEW_DRAFT["id"]

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server, "manage_draft", {"action": "update_body", "draft_id": "d", "text": "x"}
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPMarkMailRead:
    """mark_mail_read."""

    @respx.mock
    async def test_marks_every_id_read(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps(["msg-1", "msg-2"])},
            )

        assert _structured(result) == {"updated": 2, "failed": []}
        assert route.call_count == 2
        assert str(route.calls[0].request.url).endswith("/me/messages/msg-1")
        assert str(route.calls[1].request.url).endswith("/me/messages/msg-2")
        assert json.loads(route.calls[0].request.content) == {"isRead": True}

    @respx.mock
    async def test_is_read_false_marks_unread(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps(["msg-1"]), "is_read": "false"},
            )

        assert _structured(result) == {"updated": 1, "failed": []}
        assert json.loads(route.calls[0].request.content) == {"isRead": False}

    @respx.mock
    async def test_one_missing_message_does_not_sink_the_batch(self, mcp_server):
        """A deleted message is reported in failed; the rest still get patched."""
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            side_effect=[
                httpx.Response(404, json=GRAPH_ERROR_404),
                httpx.Response(200, json=SAMPLE_MESSAGE),
            ]
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps(["gone", "msg-2"])},
            )

        data = _structured(result)
        assert route.call_count == 2
        assert data["updated"] == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "gone"
        assert "404" in data["failed"][0]["error"]

    @respx.mock
    @pytest.mark.parametrize("bad", ["{oops", '"notanarray"', "[1, 2]"])
    async def test_malformed_message_ids_makes_no_graph_calls(self, mcp_server, bad):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(mcp_server, "mark_mail_read", {"message_ids": bad})

        assert _structured(result) == {
            "updated": 0,
            "failed": [],
            "error": "message_ids must be a JSON array of strings",
        }
        assert route.call_count == 0

    @respx.mock
    async def test_bad_is_read_value_is_an_error_not_a_default(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps(["msg-1"]), "is_read": "yes"},
            )

        assert _structured(result) == {
            "updated": 0,
            "failed": [],
            "error": 'is_read must be "true" or "false"',
        }
        assert route.call_count == 0

    @respx.mock
    async def test_is_read_is_case_insensitive(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps(["msg-1"]), "is_read": "False"},
            )

        assert _structured(result)["updated"] == 1
        assert json.loads(route.calls[0].request.content) == {"isRead": False}

    @respx.mock
    async def test_only_the_first_100_ids_are_processed(self, mcp_server):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "mark_mail_read",
                {"message_ids": json.dumps([f"msg-{i}" for i in range(150)])},
            )

        assert _structured(result) == {"updated": 100, "failed": []}
        assert route.call_count == 100

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server, "mark_mail_read", {"message_ids": json.dumps(["msg-1"])}
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


def _fake_jwt(claims: dict) -> str:
    """Build an unsigned JWT whose payload decode_token_claims can read."""
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"s").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


IDENTITY_TOKEN = _fake_jwt({"oid": "user-obj-id", "tid": "tenant-id-123"})

SAMPLE_CHAT_MESSAGE_CREATED = {
    "id": "chat-msg-sent-002",
    "messageType": "message",
    "createdDateTime": "2026-01-06T09:00:00Z",
    "lastModifiedDateTime": "2026-01-06T09:00:00Z",
    "from": {"user": {"id": "user-id-001", "displayName": "Test User"}},
    "body": {"contentType": "text", "content": "on my way"},
}

# What Graph echoes back once the file card is on the message: the body is the
# tag, and the reference attachment carries the name the desktop renders.
SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE = {
    **SAMPLE_CHAT_MESSAGE_CREATED,
    "body": {
        "contentType": "html",
        "content": f'on my way<attachment id="{TEAMS_UPLOAD_GUID}"></attachment>',
    },
    "attachments": [
        {
            "id": TEAMS_UPLOAD_GUID,
            "contentType": "reference",
            "contentUrl": TEAMS_WEBDAV_URL,
            "name": "notes.txt",
            "thumbnailUrl": None,
        }
    ],
}


CHATS_CREATE_URL = f"{GRAPH_BASE_URL}/chats"
USER_BIND = "https://graph.microsoft.com/v1.0/users"


def _chat_create_body() -> dict:
    """The JSON body of the one POST /chats respx saw."""
    for call in respx.calls:
        if call.request.method == "POST" and call.request.url.path == "/v1.0/chats":
            return json.loads(call.request.content)
    raise AssertionError("no POST /chats request was made")


class TestMCPEnsureChat:
    """ensure_chat."""

    @respx.mock
    async def test_one_id_makes_a_one_on_one_with_the_caller_first(self, mcp_server):
        respx.post(CHATS_CREATE_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_CREATED)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(
                mcp_server,
                "ensure_chat",
                {"user_ids": "bob@example.com", "topic": "ignored for a 1:1"},
            )

        assert _structured(result) == {
            "chat_id": "19:new-chat@thread.v2",
            "chat_type": "oneOnOne",
        }
        body = _chat_create_body()
        assert body["chatType"] == "oneOnOne"
        assert "topic" not in body
        assert [m["user@odata.bind"] for m in body["members"]] == [
            f"{USER_BIND}('user-obj-id')",
            f"{USER_BIND}('bob@example.com')",
        ]

    @respx.mock
    async def test_two_ids_make_a_group_with_topic(self, mcp_server):
        respx.post(CHATS_CREATE_URL).mock(
            return_value=httpx.Response(
                201, json={**SAMPLE_CHAT_CREATED, "chatType": "group", "topic": "Launch"}
            )
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(
                mcp_server,
                "ensure_chat",
                {"user_ids": "bob@example.com, carol@example.com", "topic": " Launch "},
            )

        assert _structured(result)["chat_type"] == "group"
        body = _chat_create_body()
        assert body["chatType"] == "group"
        assert body["topic"] == "Launch"
        assert [m["user@odata.bind"] for m in body["members"]] == [
            f"{USER_BIND}('user-obj-id')",
            f"{USER_BIND}('bob@example.com')",
            f"{USER_BIND}('carol@example.com')",
        ]

    @respx.mock
    async def test_blanks_and_duplicates_are_dropped(self, mcp_server):
        respx.post(CHATS_CREATE_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_CREATED)
        )
        with _mock_token(IDENTITY_TOKEN):
            await _call(
                mcp_server,
                "ensure_chat",
                {"user_ids": " bob@example.com,, bob@example.com , "},
            )

        body = _chat_create_body()
        assert body["chatType"] == "oneOnOne"
        assert [m["user@odata.bind"] for m in body["members"]] == [
            f"{USER_BIND}('user-obj-id')",
            f"{USER_BIND}('bob@example.com')",
        ]

    @respx.mock
    async def test_own_id_is_dropped_and_self_only_is_no_members(self, mcp_server):
        """Graph would answer 400 for a chat of one; say so without the round trip."""
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "ensure_chat", {"user_ids": "user-obj-id"})

        assert _structured(result) == {"error": "no_members"}
        assert _graph_trail() == []

    @respx.mock
    async def test_own_id_among_others_is_dropped(self, mcp_server):
        respx.post(CHATS_CREATE_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_CREATED)
        )
        with _mock_token(IDENTITY_TOKEN):
            await _call(
                mcp_server,
                "ensure_chat",
                {"user_ids": "user-obj-id, bob@example.com"},
            )

        body = _chat_create_body()
        assert body["chatType"] == "oneOnOne"
        assert [m["user@odata.bind"] for m in body["members"]] == [
            f"{USER_BIND}('user-obj-id')",
            f"{USER_BIND}('bob@example.com')",
        ]

    @respx.mock
    async def test_empty_user_ids_is_no_members(self, mcp_server):
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "ensure_chat", {"user_ids": " , "})

        assert _structured(result) == {"error": "no_members"}
        assert _graph_trail() == []

    @respx.mock
    async def test_invalid_member_makes_no_request(self, mcp_server):
        """The id lands inside users('…'), so it is checked before anything else."""
        result = await _call(mcp_server, "ensure_chat", {"user_ids": "bob@example.com, x'y"})

        assert _structured(result) == {"error": "invalid_members"}
        assert _graph_trail() == []

    @respx.mock
    async def test_token_without_claims_is_no_identity(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "ensure_chat", {"user_ids": "bob@example.com"})

        assert _structured(result) == {"error": "no_identity"}
        assert _graph_trail() == []

    @respx.mock
    async def test_teams_403_reports_unavailable(self, mcp_server):
        respx.post(CHATS_CREATE_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "ensure_chat", {"user_ids": "bob@example.com"})

        assert _structured(result) == {"error": "teams_unavailable"}

    @respx.mock
    async def test_unknown_user_400_propagates(self, mcp_server):
        from fastmcp.exceptions import ToolError

        respx.post(CHATS_CREATE_URL).mock(return_value=httpx.Response(400, json=GRAPH_ERROR_400))
        with _mock_token(IDENTITY_TOKEN):
            with pytest.raises(ToolError, match="400"):
                await _call(mcp_server, "ensure_chat", {"user_ids": "nobody@example.com"})

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "ensure_chat", {"user_ids": "bob@example.com"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPMarkChatRead:
    """mark_chat_read."""

    @respx.mock
    async def test_marks_the_chat_for_the_token_identity(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/markChatReadForUser").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "mark_chat_read", {"chat_id": "chat-1on1-001"})

        assert _structured(result) == {"ok": True}
        assert route.call_count == 1
        assert str(route.calls[0].request.url) == (
            f"{GRAPH_BASE_URL}/chats/chat-1on1-001/markChatReadForUser"
        )
        assert json.loads(route.calls[0].request.content) == {
            "user": {"id": "user-obj-id", "tenantId": "tenant-id-123"}
        }

    @respx.mock
    async def test_empty_chat_id_makes_no_graph_calls(self, mcp_server):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "mark_chat_read", {"chat_id": "   "})

        assert _structured(result) == {"ok": False, "error": "chat_id must not be empty"}
        assert route.call_count == 0

    @respx.mock
    async def test_token_without_claims_is_no_identity_not_a_blank_call(self, mcp_server):
        """A token we cannot decode would mark the chat for nobody — refuse it."""
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(204)
        )
        with _mock_token("test-ms-token"):
            result = await _call(mcp_server, "mark_chat_read", {"chat_id": "chat-1on1-001"})

        assert _structured(result) == {"ok": False, "error": "no_identity"}
        assert route.call_count == 0

    @respx.mock
    async def test_teams_403_reports_unavailable(self, mcp_server):
        respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/markChatReadForUser").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with _mock_token(IDENTITY_TOKEN):
            result = await _call(mcp_server, "mark_chat_read", {"chat_id": "chat-1on1-001"})

        assert _structured(result) == {"ok": False, "error": "teams_unavailable"}

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "mark_chat_read", {"chat_id": "chat-1on1-001"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPSendTeamsMessageFrozenContract:
    """The send payload shape the desktop parses, pinned at send_teams_message.

    The body travels as `message`; the created message comes back flattened
    with `sent_to`, and an attachment is uploaded and shared before the card
    is posted. Empty-input refusals are covered in TestMCPSendTeamsMessage.
    """

    @respx.mock
    async def test_sends_plain_text_and_returns_the_flat_message(self, mcp_server):
        route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"chat_id": "chat-1on1-001", "message": "on my way"},
            )

        assert route.call_count == 1
        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "on my way"}
        }
        assert _structured(result) == {
            "message": {
                "id": "chat-msg-sent-002",
                "message_type": "message",
                "from_user_id": "user-id-001",
                "from_user_display": "Test User",
                "from_application_id": None,
                "body_content": "on my way",
                "body_content_type": "text",
                "mentioned_user_ids": [],
                "created": "2026-01-06T09:00:00Z",
                "last_modified": "2026-01-06T09:00:00Z",
                "attachments": [],
            },
            "sent_to": "chat",
        }

    @respx.mock
    async def test_angle_bracket_is_sent_verbatim_not_as_markup(self, mcp_server):
        """content_type "text" — "auto" would treat a typed < as HTML."""
        route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "send_teams_message",
                {"chat_id": "chat-1on1-001", "message": "a < b"},
            )

        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "a < b"}
        }

    @respx.mock
    async def test_an_attachment_is_uploaded_shared_and_comes_back_on_the_message(self, mcp_server):
        _mock_chat_file_upload()
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message": "on my way",
                    "attachments": json.dumps(
                        [
                            {
                                "name": "notes.txt",
                                "content_base64": base64.b64encode(b"hello").decode(),
                            }
                        ]
                    ),
                },
            )

        assert _graph_trail() == [
            ("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/notes.txt:/content"),
            ("GET", "/v1.0/me/drive/items/teams-upload-001"),
            ("GET", f"/v1.0/chats/{TEAMS_CHAT_ID}/members"),
            ("POST", "/v1.0/me/drive/items/teams-upload-001/invite"),
            ("POST", f"/v1.0/chats/{TEAMS_CHAT_ID}/messages"),
        ]
        payload = json.loads(post.calls[0].request.content)
        assert payload["attachments"][0]["contentUrl"] == TEAMS_WEBDAV_URL
        attachment = _structured(result)["message"]["attachments"][0]
        assert attachment["kind"] == "file"
        assert attachment["name"] == "notes.txt"

    @respx.mock
    async def test_the_sender_from_the_token_is_not_invited(self, mcp_server):
        _mock_chat_file_upload()
        respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE)
        )
        with _mock_token(_token_with_oid("user-id-001")):
            await _call(
                mcp_server,
                "send_teams_message",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message": "notes attached",
                    "attachments": json.dumps(
                        [
                            {
                                "name": "notes.txt",
                                "content_base64": base64.b64encode(b"hello").decode(),
                            }
                        ]
                    ),
                },
            )

        assert _invite_recipients() == [{"email": "alice@example.com"}]

    @respx.mock
    async def test_the_content_type_is_guessed_from_the_name(self, mcp_server):
        _mock_chat_file_upload()
        respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "send_teams_message",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message": "on my way",
                    "attachments": json.dumps(
                        [
                            {
                                "name": "notes.txt",
                                "content_base64": base64.b64encode(b"hello").decode(),
                            }
                        ]
                    ),
                },
            )

        assert respx.calls[0].request.headers["Content-Type"] == "text/plain"

    @respx.mock
    async def test_a_file_can_travel_without_any_text(self, mcp_server):
        _mock_chat_file_upload()
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message": "",
                    "attachments": json.dumps(
                        [
                            {
                                "name": "notes.txt",
                                "content_base64": base64.b64encode(b"hello").decode(),
                            }
                        ]
                    ),
                },
            )

        assert json.loads(post.calls[0].request.content)["body"]["content"] == (
            f'<attachment id="{TEAMS_UPLOAD_GUID}"></attachment>'
        )
        assert _structured(result)["message"]["id"] == "chat-msg-sent-002"

    @respx.mock
    @pytest.mark.parametrize(
        "attachments,reason",
        [
            ("not json", "attachments must be a JSON array"),
            ('{"name": "a.txt"}', "attachments must be a JSON array"),
            ("[42]", "attachments[0]: not an object"),
            ('[{"content_base64": "aGk="}]', "attachments[0]: missing name"),
            ('[{"name": "  ", "content_base64": "aGk="}]', "attachments[0]: missing name"),
            ('[{"name": "a.txt"}]', "attachments[0]: missing content_base64"),
            ('[{"name": "a.txt", "content_base64": "!!!"}]', "attachments[0]: invalid base64"),
            ('[{"name": "a.txt", "content_base64": ""}]', "attachments[0]: invalid base64"),
            (
                '[{"name": "a.txt", "content_base64": "aGk=", "content_type": 7}]',
                "attachments[0]: content_type must be a string",
            ),
        ],
    )
    async def test_a_bad_attachments_payload_makes_no_graph_call(
        self, mcp_server, attachments, reason
    ):
        route = respx.route().mock(return_value=httpx.Response(200, json={}))
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"chat_id": TEAMS_CHAT_ID, "message": "hi", "attachments": attachments},
            )

        assert _structured(result) == {
            "message": None,
            "error": "invalid_attachments",
            "reason": reason,
        }
        assert not route.called

    @respx.mock
    async def test_a_403_on_the_upload_is_a_permanent_scope_error(self, mcp_server):
        respx.put(url__startswith=TEAMS_SEND_UPLOAD_URL).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        post = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message": "on my way",
                    "attachments": json.dumps(
                        [
                            {
                                "name": "notes.txt",
                                "content_base64": base64.b64encode(b"hello").decode(),
                            }
                        ]
                    ),
                },
            )

        data = _structured(result)
        assert data["message"] is None
        assert data["error"] == "files_scope_missing"
        # The code is frozen; the reason is additive.
        assert data["reason"]
        assert not post.called

    @respx.mock
    async def test_an_empty_attachments_string_sends_exactly_as_before(self, mcp_server):
        route = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_CREATED)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"chat_id": TEAMS_CHAT_ID, "message": "on my way", "attachments": ""},
            )

        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "on my way"}
        }
        assert _structured(result)["message"]["attachments"] == []

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {"chat_id": "chat-1on1-001", "message": "hello"},
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPListChatsPageContract:
    """The four row keys and two top-level keys list_chats_page froze.

    list_chats rows carry five more keys now, so each test pins the frozen
    subset per row rather than asserting the whole row.
    """

    @respx.mock
    async def test_maps_chats_and_tolerates_null_preview(self, mcp_server):
        """last_read_at comes off viewpoint, and is null on a chat without one."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_PAGE)
        )
        # top matches the page so the merged tool stops where the json ancestor
        # did — after one page, with the nextLink still on offer.
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {"top": 3})

        data = _structured(result)
        assert data["next_cursor"] == SAMPLE_CHATS_PAGE_NEXT_LINK
        frozen = [
            {key: row[key] for key in ("id", "topic", "last_preview_at", "last_read_at")}
            for row in data["chats"]
        ]
        assert frozen == [
            {
                "id": "chat-1on1-001",
                "topic": None,
                "last_preview_at": "2025-12-15T14:00:00Z",
                "last_read_at": "2025-12-15T14:00:00Z",
            },
            {
                "id": "chat-group-001",
                "topic": "Project Standup",
                "last_preview_at": "2025-12-15T13:00:00Z",
                "last_read_at": "2025-12-15T12:00:00Z",
            },
            {
                "id": "chat-empty-001",
                "topic": "Newly Created",
                "last_preview_at": None,
                "last_read_at": None,
            },
        ]

    @respx.mock
    async def test_cursor_is_requested_verbatim(self, mcp_server):
        route = respx.get(SAMPLE_CHATS_PAGE_NEXT_LINK).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_chats", {"cursor": SAMPLE_CHATS_PAGE_NEXT_LINK})

        assert str(route.calls[0].request.url) == SAMPLE_CHATS_PAGE_NEXT_LINK
        data = _structured(result)
        assert data["chats"] == []
        assert data["next_cursor"] == ""

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "list_chats", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


class TestMCPChatMembers:
    """get_chat_members."""

    @respx.mock
    async def test_chat_members_maps_user_id_and_display_name(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "get_chat_members", {"chat_id": "chat-1on1-001"})

        assert _structured(result) == {
            "members": [
                {"user_id": "user-id-001", "display_name": "Test User"},
                {"user_id": "user-id-002", "display_name": "Alice Smith"},
            ]
        }

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "get_chat_members", {"chat_id": "c"})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


PAGE_MODE = '{"page": true}'


class TestMCPChatMessagesPageContract:
    """The page-mode contract list_chat_messages_page froze, at the new name.

    The row shape is unchanged, so these whole-row asserts survive verbatim;
    {"page": true} is what pins the single-page, last-modified-filtered path.
    """

    @respx.mock
    async def test_flat_mapping_survives_null_sender_and_body(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": "chat-1on1-001", "options": PAGE_MODE},
            )

        data = _structured(result)
        assert data["next_cursor"] == ""
        user_msg, app_msg, system_msg = data["messages"]

        assert user_msg == {
            "id": "chat-msg-001",
            "message_type": "message",
            "from_user_id": "user-id-002",
            "from_user_display": "Alice Smith",
            "from_application_id": None,
            "body_content": "<p>Sounds good!</p>",
            "body_content_type": "html",
            "mentioned_user_ids": [],
            "created": "2026-01-05T14:00:00Z",
            "last_modified": "2026-01-05T14:05:00Z",
            "attachments": [],
        }
        assert app_msg["from_application_id"] == "app-id-001"
        assert app_msg["from_user_id"] is None
        # from and body are both null on a system event.
        assert system_msg["message_type"] == "systemEventMessage"
        assert system_msg["from_user_id"] is None
        assert system_msg["from_application_id"] is None
        assert system_msg["body_content"] is None

    @respx.mock
    async def test_mentions_flatten_to_user_ids_in_order(self, mcp_server):
        msg_with_mentions = {
            **SAMPLE_CHAT_MESSAGE_FULL,
            "mentions": [
                {
                    "id": 0,
                    "mentionText": "Test User",
                    "mentioned": {"user": {"id": "user-id-001", "displayName": "Test User"}},
                },
                {
                    "id": 1,
                    "mentionText": "Bob Jones",
                    "mentioned": {"user": {"id": "user-id-003", "displayName": "Bob Jones"}},
                },
            ],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json={"value": [msg_with_mentions]})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": "chat-1on1-001", "options": PAGE_MODE},
            )

        assert _structured(result)["messages"][0]["mentioned_user_ids"] == [
            "user-id-001",
            "user-id-003",
        ]

    @respx.mock
    async def test_mentions_without_a_user_are_dropped(self, mcp_server):
        """Channel and tag mentions have no user under mentioned."""
        msg_with_channel_mention = {
            **SAMPLE_CHAT_MESSAGE_FULL,
            "mentions": [
                {
                    "id": 0,
                    "mentionText": "Everyone",
                    "mentioned": {"conversation": {"id": "channel-001", "displayName": "Everyone"}},
                },
                {
                    "id": 1,
                    "mentionText": "Test User",
                    "mentioned": {"user": {"id": "user-id-001", "displayName": "Test User"}},
                },
            ],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json={"value": [msg_with_channel_mention]})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": "chat-1on1-001", "options": PAGE_MODE},
            )

        assert _structured(result)["messages"][0]["mentioned_user_ids"] == ["user-id-001"]

    @respx.mock
    async def test_malformed_mention_entries_are_dropped(self, mcp_server):
        """A mention nests three deep and every level is type-checked. Graph
        sends none of these shapes, but one bad entry must not sink the page —
        the real id at the end is what proves the walk kept going."""
        msg_with_junk_mentions = {
            **SAMPLE_CHAT_MESSAGE_FULL,
            "mentions": [
                "not-a-dict",
                {"id": 0, "mentionText": "No mentioned key"},
                {"id": 1, "mentioned": None},
                {"id": 2, "mentioned": "not-a-dict"},
                {"id": 3, "mentioned": {"user": None}},
                {"id": 4, "mentioned": {"user": {"id": ""}}},
                {"id": 5, "mentioned": {"user": {"id": "user-id-001"}}},
            ],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json={"value": [msg_with_junk_mentions]})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": "chat-1on1-001", "options": PAGE_MODE},
            )

        assert _structured(result)["messages"][0]["mentioned_user_ids"] == ["user-id-001"]

    @respx.mock
    async def test_since_filters_on_the_orderby_property(self, mcp_server):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "read_teams_messages",
                {
                    "chat_id": "chat-1on1-001",
                    "since": "2026-01-05T00:00:00Z",
                    "options": PAGE_MODE,
                },
            )

        query = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert query["$filter"][0].split(" ")[0] == query["$orderby"][0].split(" ")[0]


class TestMCPChatMessagePageAttachmentsContract:
    """_chat_message_json's attachments list, straight off the page."""

    @respx.mock
    async def test_attachments_flatten_file_image_card_and_junk(self, mcp_server):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE_WITH_ATTACHMENTS)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "read_teams_messages",
                {"chat_id": TEAMS_CHAT_ID, "options": PAGE_MODE},
            )

        file_msg, image_msg, card_msg, junk_msg = _structured(result)["messages"]

        assert file_msg["attachments"] == [
            {
                "id": TEAMS_FILE_ATTACHMENT_ID,
                "kind": "file",
                "name": "roadmap.pptx",
                "content_type": "reference",
                "content_url": TEAMS_FILE_URL,
                "thumbnail_url": None,
                "card_text": None,
            }
        ]
        assert image_msg["attachments"] == [
            {
                "id": TEAMS_HOSTED_ID,
                "kind": "image",
                "name": None,
                "content_type": None,
                "content_url": TEAMS_HOSTED_URL,
                "thumbnail_url": None,
                "card_text": None,
            }
        ]
        assert card_msg["attachments"][0]["card_text"] == "Deploy finished"
        # The malformed entries drop out and the real file at the end survives.
        assert junk_msg["attachments"][-1]["kind"] == "file"
        assert junk_msg["attachments"][-1]["name"] == "roadmap.pptx"


class TestMCPGetTeamsAttachmentJsonContract:
    """get_teams_attachment: the bytes and thumbnail contract its json ancestor froze."""

    @respx.mock
    async def test_file_bytes(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"PPTXBYTES")
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": TEAMS_PPTX_MIME,
            "size": len(b"PPTXBYTES"),
            "content_base64": base64.b64encode(b"PPTXBYTES").decode("ascii"),
        }

    @respx.mock
    async def test_inline_image_bytes(self, mcp_server):
        respx.get(TEAMS_IMAGE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_IMAGE)
        )
        respx.get(TEAMS_HOSTED_VALUE_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-image-001",
                    "attachment_id": TEAMS_HOSTED_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "kind": "image",
            "name": "image-aWQ9eF8wLWN1.png",
            "content_type": "image/png",
            "size": len(PNG_BYTES),
            "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
        }

    @respx.mock
    async def test_thumbnail_skips_the_driveitem_metadata(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_THUMB_URL).mock(
            return_value=httpx.Response(
                200, content=b"THUMB", headers={"Content-Type": "image/jpeg"}
            )
        )
        meta = respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "thumbnail",
                    "options": json.dumps({"thumbnail": "medium"}),
                },
            )

        assert not meta.called
        assert _structured(result) == {
            "kind": "file",
            "name": "roadmap.pptx",
            "content_type": "image/jpeg",
            "size": 5,
            "content_base64": base64.b64encode(b"THUMB").decode("ascii"),
        }

    @respx.mock
    async def test_no_thumbnail_when_graph_has_none(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_THUMB_URL).mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "thumbnail",
                    "options": json.dumps({"thumbnail": "medium"}),
                },
            )

        assert _structured(result) == {"error": "no_thumbnail"}

    @respx.mock
    async def test_invalid_thumbnail_is_decided_before_any_request(self, mcp_server):
        route = respx.get(url__startswith=GRAPH_BASE_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "thumbnail",
                    "options": json.dumps({"thumbnail": "enormous"}),
                },
            )

        assert _structured(result) == {
            "error": "invalid_thumbnail",
            "reason": "thumbnail must be one of: small, medium, large; got 'enormous'",
        }
        assert not route.called

    @respx.mock
    async def test_unknown_id_is_not_found(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": "nope",
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "not_found",
            "available": [{"kind": "file", "id": TEAMS_FILE_ATTACHMENT_ID, "name": "roadmap.pptx"}],
        }

    @respx.mock
    async def test_a_card_is_not_found_because_it_has_no_bytes(self, mcp_server):
        respx.get(TEAMS_CARD_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_CARD)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-card-001",
                    "attachment_id": "card-att-001",
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "not_found",
            "available": [{"kind": "card", "id": "card-att-001", "name": None}],
        }

    @respx.mock
    async def test_a_file_without_a_content_url_is_not_found(self, mcp_server):
        msg = {
            **SAMPLE_CHAT_MESSAGE_WITH_FILE,
            "attachments": [{"id": TEAMS_FILE_ATTACHMENT_ID, "contentType": "reference"}],
        }
        respx.get(TEAMS_FILE_MSG_URL).mock(return_value=httpx.Response(200, json=msg))
        share = respx.get(url__startswith=f"{GRAPH_BASE_URL}/shares/").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_DRIVE_ITEM)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {"error": "not_found"}
        assert not share.called

    @respx.mock
    async def test_403_on_the_sharing_link_is_access_denied(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_BASE).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {"error": "access_denied"}

    @respx.mock
    async def test_too_large_is_decided_from_the_driveitem_size(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        content = respx.get(TEAMS_SHARE_CONTENT_URL).mock(
            return_value=httpx.Response(200, content=b"never")
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json={**SAMPLE_TEAMS_DRIVE_ITEM, "size": 20_000_000})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {
            "error": "too_large",
            "size": 20_000_000,
            "limit": 10_000_000,
        }
        assert not content.called

    @respx.mock
    async def test_a_shared_folder_is_refused(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        respx.get(TEAMS_SHARE_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {"error": "is_folder"}

    @respx.mock
    async def test_403_on_the_message_is_teams_unavailable(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {"error": "teams_unavailable"}

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(
                mcp_server,
                "get_teams_attachment",
                {
                    "chat_id": TEAMS_CHAT_ID,
                    "message_id": "chat-msg-file-001",
                    "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                    "mode": "bytes",
                },
            )

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    @respx.mock
    async def test_an_unknown_message_propagates_as_a_tool_error(self, mcp_server):
        respx.get(TEAMS_FILE_MSG_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        from fastmcp.exceptions import ToolError

        with _mock_token():
            with pytest.raises(ToolError, match="404"):
                await _call(
                    mcp_server,
                    "get_teams_attachment",
                    {
                        "chat_id": TEAMS_CHAT_ID,
                        "message_id": "chat-msg-file-001",
                        "attachment_id": TEAMS_FILE_ATTACHMENT_ID,
                        "mode": "bytes",
                    },
                )


class TestMCPConnectionStatus:
    """connection_status."""

    @respx.mock
    async def test_connected_with_scopes_and_account(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        repo = MagicMock()
        repo.return_value.get_token.return_value = {
            "access_token": "a",
            "scopes": "https://graph.microsoft.com/Mail.Read Chat.Read offline_access",
        }
        with (
            _mock_token(),
            patch("auth.db.repository.TokenRepository", repo),
            patch("auth.resolve_user_key_for_request", return_value="user-key"),
        ):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is True
        # Resource prefixes stripped, names lowercased.
        assert data["scopes"] == ["mail.read", "chat.read", "offline_access"]
        assert data["connect_url"] is None
        assert data["account"]["id"] == "user-id-001"
        assert data["account"]["display_name"] == "Test User"

    @respx.mock
    async def test_legacy_row_with_null_scopes_reports_empty(self, mcp_server):
        """Rows written before the scopes-key fix have nothing recorded."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        repo = MagicMock()
        repo.return_value.get_token.return_value = {"access_token": "a", "scopes": None}
        with (
            _mock_token(),
            patch("auth.db.repository.TokenRepository", repo),
            patch("auth.resolve_user_key_for_request", return_value="user-key"),
        ):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is True
        assert data["scopes"] == []

    async def test_unexpected_auth_error_reports_disconnected(self, mcp_server):
        """A status probe must never surface a tool error.

        The local MSAL path can raise beyond the not-connected contract (e.g.
        AttributeError from a confidential client hitting the device-flow path
        on a stale cache); connection_status maps anything unexpected to a
        plain disconnected report instead of an isError result.
        """
        with patch("ms_graph_mcp.get_graph_token", side_effect=AttributeError("boom")):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is False
        assert data["scopes"] == []
        assert data["connect_url"] is None
        assert data["account"] is None

    @respx.mock
    async def test_scope_lookup_failure_is_swallowed(self, mcp_server):
        """Laptop (MSAL) mode has no token row and no DB — status must still answer."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        repo = MagicMock(side_effect=RuntimeError("no database configured"))
        with (
            _mock_token(),
            patch("auth.db.repository.TokenRepository", repo),
            patch("auth.resolve_user_key_for_request", return_value="user-key"),
        ):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is True
        assert data["scopes"] == []

    @respx.mock
    async def test_unreachable_profile_leaves_account_null(self, mcp_server):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(500, json={"error": {"code": "x", "message": "boom"}})
        )
        with (
            _mock_token(),
            patch("ms_graph_mcp._stored_graph_scopes", return_value=["mail.read"]),
        ):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is True
        assert data["account"] is None
        assert data["scopes"] == ["mail.read"]

    async def test_not_connected_returns_connect_url(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "connection_status")

        assert _structured(result) == {
            "connected": False,
            "scopes": [],
            "connect_url": CONNECT_URL,
            "account": None,
        }

    async def test_laptop_mode_permission_error_has_no_connect_url(self, mcp_server):
        with patch("ms_graph_mcp.get_graph_token", side_effect=PermissionError("no auth")):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is False
        assert data["connect_url"] is None


# ---------------------------------------------------------------------------
# Inbox rules
# ---------------------------------------------------------------------------

_RULES_URL = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messageRules"


class TestMCPInboxRuleTools:
    """Test the manage_inbox_rules tool end-to-end via the in-process client."""

    @respx.mock
    async def test_list_rules(self, mcp_server):
        respx.get(_RULES_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_RULES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "manage_inbox_rules", {})

        data = _structured(result)
        assert data["count"] == 2
        assert data["rules"][0] == {
            "id": SAMPLE_MESSAGE_RULE["id"],
            "display_name": "From partner",
            "sequence": 2,
            "is_enabled": True,
            "conditions": "senderContains",
            "actions": "moveToFolder, stopProcessingRules",
        }
        assert data["rules"][1]["display_name"] == "Newsletters to read later"
        assert data["rules"][1]["is_enabled"] is False

    @respx.mock
    async def test_list_rules_empty(self, mcp_server):
        respx.get(_RULES_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        with _mock_token():
            result = await _call(mcp_server, "manage_inbox_rules", {"action": "list"})

        assert _structured(result) == {"rules": [], "count": 0}

    @respx.mock
    async def test_get_rule(self, mcp_server):
        """get returns the whole Graph rule — condition values, not just keys."""
        rule_id = SAMPLE_MESSAGE_RULE["id"]
        respx.get(f"{_RULES_URL}/{rule_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_RULE)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_inbox_rules", {"action": "get", "rule_id": rule_id}
            )

        assert _structured(result) == {"rule": SAMPLE_MESSAGE_RULE}

    @respx.mock
    async def test_create_rule(self, mcp_server):
        rule = {
            "displayName": "From partner",
            "sequence": 2,
            "actions": {"markAsRead": True},
        }
        route = respx.post(_RULES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_MESSAGE_RULE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "create", "options": json.dumps(rule)},
            )

        assert json.loads(route.calls[0].request.content) == rule
        assert _structured(result) == {
            "action": "created",
            "id": SAMPLE_MESSAGE_RULE["id"],
            "display_name": "From partner",
        }

    @respx.mock
    async def test_update_rule(self, mcp_server):
        rule_id = SAMPLE_MESSAGE_RULE["id"]
        route = respx.patch(f"{_RULES_URL}/{rule_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MESSAGE_RULE, "isEnabled": False})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "update", "rule_id": rule_id, "options": '{"isEnabled": false}'},
            )

        assert json.loads(route.calls[0].request.content) == {"isEnabled": False}
        assert _structured(result) == {
            "action": "updated",
            "id": rule_id,
            "display_name": "From partner",
        }

    @respx.mock
    async def test_update_rule_minimal_graph_response_falls_back_to_the_id(self, mcp_server):
        """An empty PATCH body must still name the rule the caller asked about."""
        rule_id = SAMPLE_MESSAGE_RULE["id"]
        respx.patch(f"{_RULES_URL}/{rule_id}").mock(return_value=httpx.Response(200, json={}))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "update", "rule_id": rule_id, "options": '{"isEnabled": false}'},
            )

        assert _structured(result) == {
            "action": "updated",
            "id": rule_id,
            "display_name": None,
        }

    @respx.mock
    async def test_delete_rule(self, mcp_server):
        rule_id = SAMPLE_MESSAGE_RULE["id"]
        route = respx.delete(f"{_RULES_URL}/{rule_id}").mock(return_value=httpx.Response(204))
        with _mock_token():
            result = await _call(
                mcp_server, "manage_inbox_rules", {"action": "delete", "rule_id": rule_id}
            )

        assert route.called
        assert _structured(result) == {"action": "deleted", "id": rule_id}

    async def test_unknown_action(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_inbox_rules", {"action": "frobnicate"})

        data = _structured(result)
        assert data["error"] == "invalid_action"
        assert "Unknown action" in data["reason"]

    async def test_missing_rule_id(self, mcp_server):
        with _mock_token():
            for action in ("get", "update", "delete"):
                result = await _call(mcp_server, "manage_inbox_rules", {"action": action})
                data = _structured(result)
                assert data["error"] == "missing_rule_id"
                assert action in data["reason"]

    async def test_create_without_options(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_inbox_rules", {"action": "create"})

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert "options" in data["reason"]

    async def test_update_without_options(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "update", "rule_id": "some-id", "options": "{}"},
            )

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert "non-empty options" in data["reason"]

    async def test_create_missing_required_fields(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "create", "options": '{"displayName": "x"}'},
            )

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert data["reason"] == "Action 'create' requires: actions, sequence."

    async def test_create_with_unparseable_options(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "manage_inbox_rules", {"action": "create", "options": "not json"}
            )

        assert _structured(result)["error"] == "invalid_options"

    @respx.mock
    async def test_create_missing_readwrite_surfaces_error(self, mcp_server):
        """403 (ReadWrite scope not granted) surfaces to the caller as a ToolError."""
        from fastmcp.exceptions import ToolError

        respx.post(_RULES_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            with pytest.raises(ToolError, match="Authorization_RequestDenied"):
                await _call(
                    mcp_server,
                    "manage_inbox_rules",
                    {
                        "action": "create",
                        "options": '{"displayName": "x", "sequence": 1, "actions": {"markAsRead": true}}',
                    },
                )

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "manage_inbox_rules", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}


_FOLDERS_URL = f"{GRAPH_BASE_URL}/me/mailFolders"


class TestMCPFolderTools:
    """Test the manage_mail_folders tool end-to-end via the in-process client."""

    @respx.mock
    async def test_list_folders(self, mcp_server):
        respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "manage_mail_folders", {})

        data = _structured(result)
        assert data["count"] == 2
        assert data["folders"][0] == {
            "id": SAMPLE_MAIL_FOLDER["id"],
            "display_name": "Projects",
            "child_folder_count": 2,
            "total_item_count": 42,
            "unread_item_count": 3,
        }
        assert data["folders"][1]["display_name"] == "Receipts"

    @respx.mock
    async def test_list_folders_empty(self, mcp_server):
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        with _mock_token():
            result = await _call(mcp_server, "manage_mail_folders", {"action": "list"})

        assert _structured(result) == {"folders": [], "count": 0}

    @respx.mock
    async def test_list_child_folders(self, mcp_server):
        parent_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.get(f"{_FOLDERS_URL}/{parent_id}/childFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "list", "options": json.dumps({"parent_id": parent_id})},
            )

        assert route.called
        assert _structured(result)["count"] == 2

    @respx.mock
    async def test_get_folder(self, mcp_server):
        """get returns the whole Graph folder, ID included."""
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.get(f"{_FOLDERS_URL}/{folder_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_mail_folders", {"action": "get", "folder_id": folder_id}
            )

        assert _structured(result) == {"folder": SAMPLE_MAIL_FOLDER}

    @respx.mock
    async def test_create_folder(self, mcp_server):
        route = respx.post(_FOLDERS_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_MAIL_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "create", "options": json.dumps({"display_name": "Projects"})},
            )

        assert json.loads(route.calls[0].request.content) == {"displayName": "Projects"}
        assert _structured(result) == {
            "action": "created",
            "id": SAMPLE_MAIL_FOLDER["id"],
            "display_name": "Projects",
        }

    @respx.mock
    async def test_create_child_folder(self, mcp_server):
        parent_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.post(f"{_FOLDERS_URL}/{parent_id}/childFolders").mock(
            return_value=httpx.Response(201, json=SAMPLE_MAIL_FOLDER)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "create",
                    "options": json.dumps({"display_name": "Sub", "parent_id": parent_id}),
                },
            )

        assert route.called
        assert json.loads(route.calls[0].request.content) == {"displayName": "Sub"}

    @respx.mock
    async def test_rename_folder(self, mcp_server):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.patch(f"{_FOLDERS_URL}/{folder_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "displayName": "Renamed"})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "rename",
                    "folder_id": folder_id,
                    "options": json.dumps({"display_name": "Renamed"}),
                },
            )

        assert json.loads(route.calls[0].request.content) == {"displayName": "Renamed"}
        assert _structured(result) == {
            "action": "renamed",
            "id": folder_id,
            "display_name": "Renamed",
        }

    @respx.mock
    async def test_rename_folder_minimal_graph_response_falls_back_to_the_id(self, mcp_server):
        """Graph returns the updated folder; an empty dict must not lose the id."""
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.patch(f"{_FOLDERS_URL}/{folder_id}").mock(return_value=httpx.Response(200, json={}))
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "rename",
                    "folder_id": folder_id,
                    "options": json.dumps({"display_name": "Renamed"}),
                },
            )

        assert _structured(result) == {
            "action": "renamed",
            "id": folder_id,
            "display_name": None,
        }

    @respx.mock
    async def test_delete_folder(self, mcp_server):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.delete(f"{_FOLDERS_URL}/{folder_id}").mock(return_value=httpx.Response(204))
        with _mock_token():
            result = await _call(
                mcp_server, "manage_mail_folders", {"action": "delete", "folder_id": folder_id}
            )

        assert route.called
        assert _structured(result) == {"action": "deleted", "id": folder_id}

    async def test_unknown_action(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_mail_folders", {"action": "frobnicate"})

        data = _structured(result)
        assert data["error"] == "invalid_action"
        assert "Unknown action" in data["reason"]

    async def test_missing_folder_id(self, mcp_server):
        with _mock_token():
            for action in ("get", "rename", "delete"):
                result = await _call(mcp_server, "manage_mail_folders", {"action": action})
                data = _structured(result)
                assert data["error"] == "missing_folder_id"
                assert action in data["reason"]

    async def test_create_without_display_name(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_mail_folders", {"action": "create"})

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert "display_name" in data["reason"]

    async def test_unparseable_options(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server, "manage_mail_folders", {"action": "list", "options": "not json"}
            )

        assert _structured(result)["error"] == "invalid_options"

    @respx.mock
    async def test_create_missing_readwrite_surfaces_error(self, mcp_server):
        """403 (ReadWrite scope not granted) surfaces to the caller as a ToolError."""
        from fastmcp.exceptions import ToolError

        respx.post(_FOLDERS_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            with pytest.raises(ToolError, match="Authorization_RequestDenied"):
                await _call(
                    mcp_server,
                    "manage_mail_folders",
                    {"action": "create", "options": '{"display_name": "x"}'},
                )

    @respx.mock
    async def test_move_folder(self, mcp_server):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        dest_id = "AQMkAGfolder-002"
        route = respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "parentFolderId": dest_id})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "move",
                    "folder_id": folder_id,
                    "options": json.dumps({"destination_id": dest_id}),
                },
            )

        assert json.loads(route.calls[0].request.content) == {"destinationId": dest_id}
        assert _structured(result) == {
            "action": "moved",
            "id": folder_id,
            "parent_id": dest_id,
        }

    @respx.mock
    async def test_move_folder_minimal_graph_response_falls_back_to_the_destination(
        self, mcp_server
    ):
        """Graph returns the moved folder; an empty dict falls back to dest_id."""
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        dest_id = "drafts"
        respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(200, json={})
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "move",
                    "folder_id": folder_id,
                    "options": json.dumps({"destination_id": dest_id}),
                },
            )

        assert _structured(result) == {"action": "moved", "id": folder_id, "parent_id": dest_id}

    async def test_move_missing_folder_id(self, mcp_server):
        with _mock_token():
            result = await _call(mcp_server, "manage_mail_folders", {"action": "move"})

        data = _structured(result)
        assert data["error"] == "missing_folder_id"
        assert "folder_id" in data["reason"]

    async def test_move_missing_destination(self, mcp_server):
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "move", "folder_id": SAMPLE_MAIL_FOLDER["id"]},
            )

        data = _structured(result)
        assert data["error"] == "invalid_options"
        assert "destination_id" in data["reason"]

    async def test_not_connected(self, mcp_server):
        with _mock_missing_connection():
            result = await _call(mcp_server, "manage_mail_folders", {})

        assert _structured(result) == {"error": "not_connected", "connect_url": CONNECT_URL}

    # --- input coercion / hardening (commit 2) exercised through the tool -----

    @respx.mock
    async def test_list_non_string_parent_id_is_coerced_not_crashed(self, mcp_server):
        """A numeric parent_id in JSON options is coerced to a string path, not a TypeError."""
        route = respx.get(f"{_FOLDERS_URL}/123/childFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "list", "options": json.dumps({"parent_id": 123})},
            )

        assert route.called
        assert _structured(result)["count"] == 2

    @respx.mock
    async def test_create_non_string_parent_id_is_coerced_not_crashed(self, mcp_server):
        """A numeric parent_id on create is coerced to a string path, not a TypeError."""
        route = respx.post(f"{_FOLDERS_URL}/123/childFolders").mock(
            return_value=httpx.Response(201, json=SAMPLE_MAIL_FOLDER)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_mail_folders",
                {
                    "action": "create",
                    "options": json.dumps({"display_name": "Sub", "parent_id": 123}),
                },
            )

        assert route.called
        assert _structured(result)["action"] == "created"

    @respx.mock
    async def test_list_top_zero_is_clamped_to_at_least_one(self, mcp_server):
        """top=0 must not reach Graph as $top=0; the tool clamps it to >= 1."""
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "list", "options": json.dumps({"top": 0})},
            )

        assert route.called
        assert int(route.calls[0].request.url.params["$top"]) >= 1

    @respx.mock
    async def test_list_include_hidden_adds_query_param(self, mcp_server):
        """include_hidden=true surfaces the Graph includeHiddenFolders query param."""
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        with _mock_token():
            await _call(
                mcp_server,
                "manage_mail_folders",
                {"action": "list", "options": json.dumps({"include_hidden": True})},
            )

        assert route.called
        assert route.calls[0].request.url.params["includeHiddenFolders"] == "true"

    @respx.mock
    async def test_list_surfaces_graph_error(self, mcp_server):
        """A 403 on list surfaces to the caller as a ToolError."""
        from fastmcp.exceptions import ToolError

        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with _mock_token():
            with pytest.raises(ToolError, match="Authorization_RequestDenied"):
                await _call(mcp_server, "manage_mail_folders", {"action": "list"})

    @respx.mock
    async def test_move_surfaces_graph_error(self, mcp_server):
        """A 404 on move surfaces to the caller as a ToolError."""
        from fastmcp.exceptions import ToolError

        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            with pytest.raises(ToolError, match="ResourceNotFound"):
                await _call(
                    mcp_server,
                    "manage_mail_folders",
                    {
                        "action": "move",
                        "folder_id": folder_id,
                        "options": json.dumps({"destination_id": "inbox"}),
                    },
                )

    @respx.mock
    async def test_delete_surfaces_graph_error(self, mcp_server):
        """A 404 on delete surfaces to the caller as a ToolError."""
        from fastmcp.exceptions import ToolError

        respx.delete(f"{_FOLDERS_URL}/bad-id").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with _mock_token():
            with pytest.raises(ToolError, match="ResourceNotFound"):
                await _call(
                    mcp_server, "manage_mail_folders", {"action": "delete", "folder_id": "bad-id"}
                )


class TestConnectFlowScopes:
    """The /connect flow and local MSAL login share ONE scope policy.

    Requesting an admin-gated scope walls the whole consent bundle behind
    "Approval required", so the org default must be exactly the consented set;
    the connect flow only adds offline_access, which the code grant needs for
    its refresh token.
    """

    def test_org_connect_request_is_consented_set_plus_offline_access(self):
        import os
        from unittest.mock import patch as env_patch

        from ms_graph.local_auth import CONSENTED_ORG_SCOPES
        from ms_graph_mcp import _ms_graph_scopes

        with env_patch.dict(os.environ, {"MS_TENANT_ID": "t"}, clear=True):
            scopes = _ms_graph_scopes().split()
        assert scopes == [*CONSENTED_ORG_SCOPES, "offline_access"]

    def test_ms_scopes_override_reaches_the_connect_flow(self):
        import os
        from unittest.mock import patch as env_patch

        from ms_graph_mcp import _ms_graph_scopes

        with env_patch.dict(
            os.environ,
            {"MS_TENANT_ID": "t", "MS_SCOPES": "Mail.Read Chat.ReadWrite"},
            clear=True,
        ):
            scopes = _ms_graph_scopes().split()
        assert scopes == ["Mail.Read", "Chat.ReadWrite", "offline_access"]

    def test_offline_access_is_not_duplicated(self):
        import os
        from unittest.mock import patch as env_patch

        from ms_graph_mcp import _ms_graph_scopes

        with env_patch.dict(
            os.environ,
            {"MS_TENANT_ID": "t", "MS_SCOPES": "Mail.Read offline_access"},
            clear=True,
        ):
            assert _ms_graph_scopes().split() == ["Mail.Read", "offline_access"]

    def test_connect_config_resolves_scopes_at_request_time(self):
        """The config must carry the policy FUNCTION, not an import-time call.

        MS_TENANT_ID/MS_SCOPES can load after module import (same reason the
        config's authorize_url/token_url are lambdas over _ms_tenant); a
        captured string would hand an org tenant the consumer wish-list and
        rebuild the "Approval required" wall.
        """
        import os
        from unittest.mock import patch as env_patch

        from ms_graph.local_auth import CONSENTED_ORG_SCOPES
        from ms_graph_mcp import MICROSOFT_CONNECT_CONFIG

        assert callable(MICROSOFT_CONNECT_CONFIG.scopes)
        with env_patch.dict(os.environ, {"MS_TENANT_ID": "t"}, clear=True):
            org = MICROSOFT_CONNECT_CONFIG.resolved_scopes().split()
        with env_patch.dict(os.environ, {"MS_SCOPES": "Mail.Read"}, clear=True):
            overridden = MICROSOFT_CONNECT_CONFIG.resolved_scopes().split()
        assert org == [*CONSENTED_ORG_SCOPES, "offline_access"]
        assert overridden == ["Mail.Read", "offline_access"]


# ---------------------------------------------------------------------------
# External-sender mail policy
# ---------------------------------------------------------------------------

# The sender check reads the parent message by id, percent-encoded, with only
# id/from/sender selected.
SENDER_CHECK_URL = f"{GRAPH_BASE_URL}/me/messages/{quote(ATT_MSG_ID, safe='')}"
SENDER_CHECK_PATH = f"/v1.0/me/messages/{ATT_MSG_ID}"
ATT_EXT_ITEM_URL = f"{ATT_BASE}/{quote(SAMPLE_EXTERNAL_ITEM_ATTACHMENT['id'], safe='')}"
EXTERNAL_MSG_ID = SAMPLE_EXTERNAL_MESSAGE["id"]

# Every mail-touching tool must be classified. Decision 12: the ungated ones
# are writes on ids the caller must already hold (the gated surfaces never hand
# out an external id), draft ids that Exchange rejects on non-drafts, and
# folder metadata that carries no mail.
GATED_MAIL_TOOLS = frozenset(
    {
        "list_emails",
        "read_email",
        "get_mail_attachment",
        "send_email",
        "manage_inbox_rules",
        "sync_mail",
    }
)
DELIBERATELY_UNGATED_MAIL_TOOLS = frozenset(
    {
        "mark_mail_read",
        "manage_mail_folders",
    }
)
# manage_draft gates per action — reply is gated, create/update_body/
# add_attachment/send are deliberately not — so it belongs to neither list.
# Its gating is pinned by the per-action tests in TestMailSenderPolicy.
MIXED_GATING_MAIL_TOOLS = frozenset({"manage_draft"})
MAIL_TOOL_WORDS = ("mail", "email", "draft", "inbox")


def _policy_on(monkeypatch, value: str = "example.com") -> None:
    """Turn the policy on. Every sample sender is @example.com, i.e. internal."""
    monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, value)


def _assert_no_canary(result) -> None:
    """No field of a hidden message may appear in what a tool returned.

    Each conftest canary fixture carries a distinct CANARY-<FIELD> marker, so a
    failure here names the field that leaked.
    """
    text = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
    for marker in ("CANARY-", "mallory", "Mallory"):
        assert marker not in text, f"{marker!r} leaked into a policy-gated result: {text[:400]}"


def _select_of(request) -> str:
    """The $select query parameter of a recorded request."""
    return parse_qs(urlparse(str(request.url)).query).get("$select", [""])[0]


def _sender_checks() -> list[str]:
    """Every request respx saw that was a policy sender check, by path.

    The policy's one probe is identifiable by its $select alone, so this says
    "the policy looked" without depending on which message id it looked at.
    """
    return [
        c.request.url.path
        for c in respx.calls
        if _select_of(c.request) == mail_policy.SENDER_SELECT
    ]


class TestMailSenderPolicy:
    """Every read surface hides mail that arrived from outside the allowlist."""

    # -- list_emails --------------------------------------------------------

    @respx.mock
    async def test_list_emails_hides_external_and_appends_the_notice(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE]}
            )
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"top": 10})

        data = _structured(result)
        assert data["count"] == 1
        assert data["messages"][0]["from_address"] == "alice@example.com"
        assert data["notice"] == mail_policy.POLICY_NOTICE
        assert "sender" in _select_of(route.calls[0].request)
        _assert_no_canary(json.dumps(data))

    @respx.mock
    async def test_list_emails_select_asks_for_sender_even_when_off(self, mcp_server):
        """The select is static, so a stored cursor never lacks the field."""
        route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {})

        select = _select_of(route.calls[0].request)
        assert "from" in select and "sender" in select
        data = _structured(result)
        assert data["notice"] == ""
        _assert_no_canary(json.dumps(data))

    @respx.mock
    async def test_list_emails_search_hiding_everything_still_reports_no_results(
        self, mcp_server, monkeypatch
    ):
        _policy_on(monkeypatch)
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_EXTERNAL_MESSAGE]})
        )
        with _mock_token():
            result = await _call(mcp_server, "list_emails", {"query": "CANARY"})

        data = _structured(result)
        assert data["messages"] == []
        assert data["count"] == 0
        assert data["notice"] == mail_policy.POLICY_NOTICE
        _assert_no_canary(json.dumps(data))

    @respx.mock
    async def test_search_notice_is_identical_for_zero_and_many_hidden(
        self, mcp_server, monkeypatch
    ):
        """A hidden count on a $search query would be a content oracle."""
        _policy_on(monkeypatch)
        three = [{**SAMPLE_EXTERNAL_MESSAGE, "id": f"ext-{i}"} for i in range(3)]
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            side_effect=[
                httpx.Response(200, json={"value": [SAMPLE_EXTERNAL_MESSAGE]}),
                httpx.Response(200, json={"value": three}),
            ]
        )
        with _mock_token():
            one_hidden = _structured(await _call(mcp_server, "list_emails", {"query": "CANARY"}))
            many_hidden = _structured(await _call(mcp_server, "list_emails", {"query": "CANARY"}))

        assert one_hidden == many_hidden
        _assert_no_canary(json.dumps(one_hidden))

    @respx.mock
    async def test_list_emails_filters_a_shared_mailbox_too(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        respx.get(f"{GRAPH_BASE_URL}/users/support@example.com/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE]}
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server, "list_emails", {"mailbox": "support@example.com", "top": 10}
            )

        data = _structured(result)
        assert data["count"] == 1
        assert data["notice"] == mail_policy.POLICY_NOTICE
        _assert_no_canary(json.dumps(data))

    # -- read_email ---------------------------------------------------------

    @respx.mock
    async def test_read_email_refuses_external_before_marking(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_EXTERNAL_MESSAGE_DETAIL)
        )
        patch_route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_EXTERNAL_MESSAGE)
        )

        with _mock_token():
            result = await _call(
                mcp_server,
                "read_email",
                {"message_id": EXTERNAL_MSG_ID, "options": '{"mark_as_read": true}'},
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert not patch_route.called
        assert _graph_trail() == [("GET", f"/v1.0/me/messages/{EXTERNAL_MSG_ID}")]
        _assert_no_canary(data)

    @respx.mock
    async def test_read_email_refuses_mail_sent_on_behalf_of_an_insider(
        self, mcp_server, monkeypatch
    ):
        """from is internal, but an outside service pressed send."""
        _policy_on(monkeypatch)
        msg_id = SAMPLE_ONBEHALF_MESSAGE["id"]
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_ONBEHALF_MESSAGE)
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": msg_id})

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        _assert_no_canary(data)

    @respx.mock
    async def test_read_email_internal_message_is_unchanged(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_READ_DETAIL)
        )
        with _mock_token():
            result = await _call(mcp_server, "read_email", {"message_id": ATT_MSG_ID})

        data = _structured(result)
        assert data["subject"] == "Weekly Report"
        assert "Here is the weekly report" in data["body_text"]
        _assert_no_canary(data)

    # -- get_mail_attachment ------------------------------------------------

    @respx.mock
    async def test_get_mail_attachment_checks_the_mailbox_it_will_read(
        self, mcp_server, monkeypatch
    ):
        """A /me check before a /users/{mailbox} read would be the wrong check."""
        _policy_on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/users/support@example.com/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mailbox": "support@example.com",
                },
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert route.calls[0].request.url.path.startswith(
            "/v1.0/users/support@example.com/messages/"
        )
        _assert_no_canary(data)

    @respx.mock
    async def test_get_mail_attachment_internal_costs_one_extra_request(
        self, mcp_server, monkeypatch
    ):
        _policy_on(monkeypatch)
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello there", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "notes.txt",
                    "contentType": "text/plain",
                    "size": 11,
                },
            )
        )
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]},
            )

        data = _structured(result)
        assert data["text"] == "hello there"
        trail = _graph_trail()
        assert trail[0] == ("GET", SENDER_CHECK_PATH)
        assert len(trail) == 3
        _assert_no_canary(data)

    @respx.mock
    async def test_policy_off_issues_no_extra_requests(self, mcp_server):
        """Off must be byte-for-byte the old behaviour, including request count."""
        respx.get(f"{ATT_FILE_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello there", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(ATT_FILE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    **SAMPLE_FILE_ATTACHMENT,
                    "name": "notes.txt",
                    "contentType": "text/plain",
                    "size": 11,
                },
            )
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]},
            )

        data = _structured(result)
        assert data["text"] == "hello there"
        assert len(_graph_trail()) == 2
        _assert_no_canary(data)

    # -- forwarding an attachment out of an external message ----------------

    @respx.mock
    async def test_send_email_refuses_to_forward_an_external_attachment(
        self, mcp_server, monkeypatch
    ):
        _policy_on(monkeypatch)
        attachments_route = respx.get(url__startswith=ATT_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        create_route = _mock_send_email()
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        spec = {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "bob@example.com",
                    "subject": "fwd",
                    "body": "see attached",
                    "options": json.dumps({"attachments": [spec]}),
                },
            )

        assert _structured(result) == {
            "error": "invalid_attachments",
            "reason": f"attachments[0]: {mail_policy.EXTERNAL_SENDER_TEXT}",
        }
        assert not attachments_route.called
        assert not create_route.called
        assert all(method != "POST" for method, _ in _graph_trail())
        _assert_no_canary(json.dumps(_structured(result)))

    @respx.mock
    async def test_forward_spec_mailbox_is_the_mailbox_that_is_checked(
        self, mcp_server, monkeypatch
    ):
        _policy_on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/users/support@example.com/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        spec = {
            "message_id": ATT_MSG_ID,
            "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
            "mailbox": "support@example.com",
        }
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_email",
                {
                    "to": "bob@example.com",
                    "subject": "fwd",
                    "body": "see attached",
                    "options": json.dumps({"attachments": [spec]}),
                },
            )

        assert _structured(result) == {
            "error": "invalid_attachments",
            "reason": f"attachments[0]: {mail_policy.EXTERNAL_SENDER_TEXT}",
        }
        assert route.calls[0].request.url.path.startswith(
            "/v1.0/users/support@example.com/messages/"
        )
        _assert_no_canary(json.dumps(_structured(result)))

    @respx.mock
    async def test_send_teams_message_refuses_to_forward_an_external_attachment(
        self, mcp_server, monkeypatch
    ):
        """Teams is out of scope, but it must not become a laundering route."""
        _policy_on(monkeypatch)
        post_route = respx.post(TEAMS_CHAT_MSGS).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        spec = {"message_id": ATT_MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
        with _mock_token():
            result = await _call(
                mcp_server,
                "send_teams_message",
                {
                    "message": "fyi",
                    "chat_id": TEAMS_CHAT_ID,
                    "options": json.dumps({"attachments": [spec]}),
                },
            )

        assert _structured(result) == {
            "message": None,
            "error": "invalid_attachments",
            "reason": f"attachments[0]: {mail_policy.EXTERNAL_SENDER_TEXT}",
        }
        assert not post_route.called
        _assert_no_canary(json.dumps(_structured(result)))

    # -- Desktop JSON surfaces ---------------------------------------------

    @respx.mock
    async def test_sync_mail_hides_external_and_keeps_tombstones(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        # A terminal page (deltaLink, no nextLink) ends the drain after one page.
        # An explicit floor older than the sample messages keeps them, so this
        # test isolates the external-sender/tombstone behavior from date filtering.
        page = {
            "@odata.deltaLink": SAMPLE_DELTA_LINK,
            "value": [
                SAMPLE_DELTA_MESSAGE,
                SAMPLE_EXTERNAL_DELTA_MESSAGE,
                SAMPLE_DELTA_TOMBSTONE,
            ],
        }
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
            return_value=httpx.Response(200, json=page)
        )
        with _mock_token():
            result = await _call(mcp_server, "sync_mail", {"min_received": "2026-01-01T00:00:00Z"})

        data = _structured(result)
        assert data["messages"] == [SAMPLE_DELTA_MESSAGE, SAMPLE_DELTA_TOMBSTONE]
        assert set(data) == {"messages", "next_cursor", "delta_cursor", "has_more", "resync"}
        assert data["next_cursor"] == ""
        assert data["delta_cursor"] == SAMPLE_DELTA_LINK
        assert data["has_more"] is False
        assert data["resync"] is False
        _assert_no_canary(data)

    @respx.mock
    async def test_read_email_detail_select_carries_the_policy_fields(
        self, mcp_server, monkeypatch
    ):
        """The one fetch must ask for what the refusal is decided on."""
        _policy_on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_EXTERNAL_MESSAGE_DETAIL)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "read_email", {"message_id": SAMPLE_EXTERNAL_MESSAGE["id"]}
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        select = _select_of(route.calls[0].request).split(",")
        assert "isDraft" in select
        assert "from" in select and "sender" in select
        _assert_no_canary(data)

    @respx.mock
    @pytest.mark.parametrize("mode", ["metadata", "text", "bytes", "onedrive"])
    async def test_get_mail_attachment_refuses_an_external_parent(
        self, mcp_server, monkeypatch, mode
    ):
        _policy_on(monkeypatch)
        attachments_route = respx.get(url__startswith=ATT_BASE).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": mode,
                },
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert not attachments_route.called
        assert _graph_trail() == [("GET", SENDER_CHECK_PATH)]
        _assert_no_canary(data)

    @respx.mock
    @pytest.mark.parametrize("mode", ["metadata", "text", "bytes", "onedrive"])
    async def test_get_mail_attachment_refuses_an_external_attached_message(
        self, mcp_server, monkeypatch, mode
    ):
        _policy_on(monkeypatch)
        value_route = respx.get(f"{ATT_EXT_ITEM_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"raw-eml")
        )

        def _respond(request):
            if "expand" in str(request.url):
                return httpx.Response(200, json=SAMPLE_EXTERNAL_ITEM_ATTACHMENT)
            return httpx.Response(200, json=SAMPLE_EXTERNAL_ITEM_ATTACHMENT_META)

        respx.get(url__startswith=ATT_EXT_ITEM_URL).mock(side_effect=_respond)
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": ATT_MSG_ID,
                    "attachment_id": SAMPLE_EXTERNAL_ITEM_ATTACHMENT["id"],
                    "mode": mode,
                },
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert not value_route.called
        _assert_no_canary(data)

    @respx.mock
    async def test_manage_draft_reply_never_posts_create_reply(self, mcp_server, monkeypatch):
        """Graph would build a draft quoting the original, from = the user."""
        _policy_on(monkeypatch)
        post_route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        respx.get(SENDER_CHECK_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {"action": "reply", "message_id": ATT_MSG_ID}
            )

        data = _structured(result)
        assert data == {"error": mail_policy.EXTERNAL_SENDER_ERROR}
        assert not post_route.called
        assert _graph_trail() == [("GET", SENDER_CHECK_PATH)]
        _assert_no_canary(data)

    # -- outbound stays ungated --------------------------------------------

    @respx.mock
    async def test_manage_draft_create_is_not_gated(self, mcp_server, monkeypatch):
        """Composing the user's own mail reads nothing that arrived from anyone."""
        _policy_on(monkeypatch)
        respx.post(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_NEW_DRAFT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {"action": "create", "to": "a@example.com", "subject": "Lunch?", "text": "noon"},
            )

        data = _structured(result)
        assert data["id"] == SAMPLE_NEW_DRAFT["id"]
        assert not _sender_checks()
        assert SENDER_CHECK_PATH not in [path for _, path in _graph_trail()]

    @respx.mock
    async def test_manage_draft_update_body_is_not_gated(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPLY_DRAFT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {"action": "update_body", "draft_id": "AAMkAGI2draft001=", "text": "On it."},
            )

        assert _structured(result) == {"ok": True}
        assert not _sender_checks()

    @respx.mock
    async def test_manage_draft_add_attachment_is_not_gated(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        respx.post(f"{DRAFT_BASE}/attachments").mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_draft",
                {
                    "action": "add_attachment",
                    "draft_id": SAMPLE_DRAFT_MESSAGE["id"],
                    "name": "notes.txt",
                    "content_base64": base64.b64encode(b"hi").decode("ascii"),
                },
            )

        assert _structured(result) == {"attachment_id": SAMPLE_CREATED_ATTACHMENT["id"]}
        assert not _sender_checks()

    @respx.mock
    async def test_manage_draft_send_is_not_gated(self, mcp_server, monkeypatch):
        """The pre-send read selects no from/sender, so there is nothing to gate."""
        _policy_on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRAFT_FOR_SEND)
        )
        respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(202)
        )
        with _mock_token():
            result = await _call(
                mcp_server, "manage_draft", {"action": "send", "draft_id": "AAMkAGI2draft001="}
            )

        assert _structured(result)["ok"] is True
        assert not _sender_checks()
        assert SENDER_CHECK_PATH not in [path for _, path in _graph_trail()]

    # -- forwarding inbox rules --------------------------------------------

    @respx.mock
    async def test_forwarding_rule_is_refused_before_any_request(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "create", "options": json.dumps(SAMPLE_FORWARDING_RULE)},
            )

        assert _structured(result) == {
            "error": "forwarding_rule",
            "reason": mail_policy.FORWARDING_RULE_TEXT,
        }
        assert _graph_trail() == []
        _assert_no_canary(json.dumps(_structured(result)))

    @respx.mock
    async def test_updating_a_rule_to_redirect_is_refused(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        changes = {"actions": {"redirectTo": [{"emailAddress": {"address": "x@evil.example.net"}}]}}
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {
                    "action": "update",
                    "rule_id": SAMPLE_MESSAGE_RULE["id"],
                    "options": json.dumps(changes),
                },
            )

        assert _structured(result) == {
            "error": "forwarding_rule",
            "reason": mail_policy.FORWARDING_RULE_TEXT,
        }
        assert _graph_trail() == []
        _assert_no_canary(json.dumps(_structured(result)))

    @respx.mock
    async def test_non_forwarding_rules_still_work_while_the_policy_is_on(
        self, mcp_server, monkeypatch
    ):
        """Move/copy/markAsRead rules do not change from, so they stay allowed."""
        _policy_on(monkeypatch)
        rule = {"displayName": "Filed", "sequence": 2, "actions": {"moveToFolder": "AQMkAG"}}
        route = respx.post(_RULES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_MESSAGE_RULE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "create", "options": json.dumps(rule)},
            )

        assert route.called
        assert _structured(result)["action"] == "created"
        _assert_no_canary(json.dumps(_structured(result)))

    @respx.mock
    async def test_forwarding_rule_is_allowed_while_the_policy_is_off(self, mcp_server):
        """The gate closes the policy bypass; it is not an independent control."""
        route = respx.post(_RULES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_MESSAGE_RULE)
        )
        with _mock_token():
            result = await _call(
                mcp_server,
                "manage_inbox_rules",
                {"action": "create", "options": json.dumps(SAMPLE_FORWARDING_RULE)},
            )

        assert route.called
        assert _structured(result)["action"] == "created"
        _assert_no_canary(json.dumps(_structured(result)))

    # -- connection_status --------------------------------------------------

    @respx.mock
    @pytest.mark.parametrize("value, expected", [("example.com", True), (None, False)])
    async def test_connection_status_reports_the_policy_state(
        self, mcp_server, monkeypatch, value, expected
    ):
        if value:
            _policy_on(monkeypatch, value)
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(500, json={"error": {"code": "x", "message": "boom"}})
        )
        with (
            _mock_token(),
            patch("ms_graph_mcp._stored_graph_scopes", return_value=[]),
        ):
            result = await _call(mcp_server, "connection_status")

        data = _structured(result)
        assert data["connected"] is True
        assert data["mail_policy"] == {"enabled": expected}
        _assert_no_canary(data)

    @respx.mock
    async def test_connection_status_never_crashes_on_a_bad_allowlist(self, monkeypatch):
        """A status probe must answer even when the config would stop the pod.

        Called directly rather than through the client, because the lifespan
        refuses to start the server at all on a malformed allowlist.
        """
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "not a domain")
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(500, json={"error": {"code": "x", "message": "boom"}})
        )
        from ms_graph_mcp import connection_status

        with (
            _mock_token(),
            patch("ms_graph_mcp._stored_graph_scopes", return_value=[]),
        ):
            data = await connection_status()

        assert data["connected"] is True
        assert data["mail_policy"] == {"enabled": True, "error": "invalid_config"}
        _assert_no_canary(data)

    # -- misconfiguration fails closed --------------------------------------

    @respx.mock
    async def test_bad_config_fails_read_email_before_any_graph_call(self, monkeypatch):
        """The tool itself fails closed, independently of the boot check.

        Called directly rather than through the client: the in-process client
        runs the lifespan, which would refuse the allowlist first and prove
        nothing about the tool.
        """
        _policy_on(monkeypatch, "*")
        respx.get(f"{GRAPH_BASE_URL}/me/messages/{ATT_MSG_ID}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        from ms_graph_mcp import read_email

        with _mock_token(), pytest.raises(mail_policy.MailPolicyConfigError, match=r"'\*'"):
            await read_email(ATT_MSG_ID)

        assert _graph_trail() == []

    @respx.mock
    async def test_bad_config_is_not_mistaken_for_a_missing_connection(self, monkeypatch):
        """A gated tool raises on a bad allowlist instead of returning a dict."""
        _policy_on(monkeypatch, "*")
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        from ms_graph_mcp import manage_draft

        with _mock_token(), pytest.raises(mail_policy.MailPolicyConfigError):
            await manage_draft(action="reply", message_id=SAMPLE_EXTERNAL_MESSAGE["id"])

    async def test_lifespan_raises_on_bad_config(self, monkeypatch):
        """A pod that cannot parse its allowlist must never become ready."""
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "not a domain")
        from ms_graph_mcp import _lifespan

        with pytest.raises(mail_policy.MailPolicyConfigError):
            async with _lifespan(None):
                pass

    async def test_lifespan_starts_with_a_valid_allowlist(self, monkeypatch):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        from ms_graph_mcp import _lifespan

        async with _lifespan(None):
            pass

    # -- the invariant itself -----------------------------------------------

    async def test_every_mail_tool_is_classified(self, mcp_server):
        """A new mail tool must be gated, or deliberately listed as ungated."""
        from fastmcp import Client

        async with Client(mcp_server) as client:
            names = {t.name for t in await client.list_tools()}

        classified = GATED_MAIL_TOOLS | DELIBERATELY_UNGATED_MAIL_TOOLS
        mail_tools = {n for n in names if any(word in n.lower() for word in MAIL_TOOL_WORDS)}
        assert mail_tools - MIXED_GATING_MAIL_TOOLS - classified == set()
        # And every name we classified is still registered, so the lists cannot
        # rot into a false sense of coverage.
        assert classified - names == set()


# The 24 tool names that were retired when the server collapsed to its
# canonical dict tools. They must stay gone: no registration, no tag, no call.
DEPRECATED_TOOL_NAMES = [
    "get_user_profile",
    "get_profile_json",
    "search_people_json",
    "list_mail_delta",
    "mark_mail_read_json",
    "get_chat_members_json",
    "ensure_chat_json",
    "mark_chat_read_json",
    "inspect_file_json",
    "upload_file",
    "list_powerbi_workspaces",
    "list_powerbi_content",
    "get_email_attachment",
    "get_mail_attachment_json",
    "get_chat_attachment_json",
    "list_chats_page",
    "list_chat_messages_page",
    "send_chat_message_json",
    "get_mail_detail",
    "create_reply_draft_json",
    "create_draft_json",
    "update_draft_body",
    "add_draft_attachment_json",
    "send_draft",
]


class TestNoDeprecatedAliases:
    """The 24 old names are gone: not registered, not tagged, not callable."""

    async def test_no_old_name_is_registered(self, mcp_server):
        from fastmcp import Client

        # run_middleware=False bypasses HideDeprecatedAliases, so this sees
        # every registered tool, not just the ones a model is shown.
        registered = await mcp_server.list_tools(run_middleware=False)
        names = {tool.name for tool in registered}
        assert names & set(DEPRECATED_TOOL_NAMES) == set()
        assert [t.name for t in registered if "deprecated-alias" in (t.tags or set())] == []

        # Nothing is hidden any more: what is registered is what is offered.
        async with Client(mcp_server) as client:
            visible = {t.name for t in await client.list_tools()}
        assert visible == names

    async def test_an_old_name_is_not_callable(self, mcp_server):
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="Unknown tool"):
            await _call(mcp_server, "get_profile_json")


class TestCursorGuard:
    """A caller-supplied cursor that is not a Graph URL is refused with no request."""

    EVIL = "https://evil.example/v1.0/me/messages/delta?$deltatoken=x"

    @respx.mock
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("sync_mail", {"cursor": EVIL}),
            ("list_chats", {"cursor": EVIL}),
            ("read_teams_messages", {"chat_id": "19:chat", "cursor": EVIL}),
        ],
    )
    async def test_non_graph_cursor_is_refused(self, mcp_server, tool, args):
        with _mock_token():
            result = await _call(mcp_server, tool, args)
            assert _structured(result) == {"error": "invalid_cursor"}
            assert _graph_trail() == []

    @respx.mock
    async def test_graph_cursor_still_works(self, mcp_server):
        with _mock_token():
            cursor = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta?$deltatoken=abc"
            respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta").mock(
                return_value=httpx.Response(200, json={"value": [], "@odata.deltaLink": cursor})
            )
            result = await _call(mcp_server, "sync_mail", {"cursor": cursor})
            assert _structured(result)["delta_cursor"] == cursor


class TestDraftsUnderPolicy:
    """Drafts have no from; the policy must still show the user's own drafts."""

    @respx.mock
    async def test_list_emails_drafts_folder_keeps_drafts(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        with _mock_token():
            route = respx.get(
                url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/drafts/messages"
            ).mock(
                return_value=httpx.Response(
                    200, json={"value": [SAMPLE_UNSENT_DRAFT, SAMPLE_EXTERNAL_MESSAGE]}
                )
            )
            result = await _call(mcp_server, "list_emails", {"folder": "drafts"})
            text = _get_text(result)
            _assert_no_canary(text)
            assert "DRAFT-SUBJECT" in text
            assert "isDraft" in _select_of(route.calls[0].request).split(",")

    @respx.mock
    async def test_read_email_shows_a_draft(self, mcp_server, monkeypatch):
        _policy_on(monkeypatch)
        with _mock_token():
            respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
                return_value=httpx.Response(200, json=SAMPLE_UNSENT_DRAFT_DETAIL)
            )
            result = await _call(
                mcp_server, "read_email", {"message_id": SAMPLE_UNSENT_DRAFT["id"]}
            )
            data = _structured(result)
            assert data["body_text"] == "DRAFT-BODY"
            assert data["is_draft"] is True
            assert "error" not in data

    @respx.mock
    async def test_attachment_check_admits_a_draft(self, mcp_server, monkeypatch):
        """The id check's $select carries isDraft, so a draft's attachment is readable."""
        _policy_on(monkeypatch)
        with _mock_token():
            draft_id = SAMPLE_UNSENT_DRAFT["id"]
            respx.get(f"{GRAPH_BASE_URL}/me/messages/{draft_id}").mock(
                return_value=httpx.Response(200, json={"id": draft_id, "isDraft": True})
            )
            respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/{draft_id}/attachments/").mock(
                return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
            )
            result = await _call(
                mcp_server,
                "get_mail_attachment",
                {
                    "message_id": draft_id,
                    "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                    "mode": "metadata",
                },
            )
            data = _structured(result)
            assert "error" not in data
            assert data["id"] == SAMPLE_FILE_ATTACHMENT["id"]
