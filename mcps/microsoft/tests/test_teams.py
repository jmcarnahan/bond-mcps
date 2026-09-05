"""Tests for Teams operations (sync and async)."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from ms_graph import teams
from ms_graph.attachments import ResolvedAttachment
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph.teams import (
    FilesScopeMissingError,
    TeamsNotAvailableError,
    _attachment_from_drive_item,
    _hosted_contents,
    _member_emails,
    _message_base,
    _prepare_teams_body,
    extract_message_sender,
    extract_message_text,
    parse_message_attachments,
)

from .conftest import (
    GRAPH_ERROR_403,
    GRAPH_ERROR_404,
    SAMPLE_CHANNEL_FILES_FOLDER,
    SAMPLE_CHANNEL_MESSAGE,
    SAMPLE_CHANNEL_MESSAGE_BOT,
    SAMPLE_CHANNEL_MESSAGE_USER,
    SAMPLE_CHANNEL_MESSAGES_RESPONSE,
    SAMPLE_CHANNELS_RESPONSE,
    SAMPLE_CHAT_MEMBERS_RESPONSE,
    SAMPLE_CHAT_MESSAGE_SENT,
    SAMPLE_CHAT_MESSAGE_WITH_CARD,
    SAMPLE_CHAT_MESSAGE_WITH_FILE,
    SAMPLE_CHAT_MESSAGE_WITH_IMAGE,
    SAMPLE_CHAT_MESSAGE_WITH_JUNK_ATTACHMENTS,
    SAMPLE_CHAT_MESSAGES_PAGE,
    SAMPLE_CHAT_MESSAGES_RESPONSE,
    SAMPLE_CHATS_PAGE,
    SAMPLE_CHATS_PAGE_NEXT_LINK,
    SAMPLE_CHATS_RESPONSE,
    SAMPLE_INVITE_RESPONSE,
    SAMPLE_TEAMS_RESPONSE,
    SAMPLE_TEAMS_UPLOAD_RESPONSE,
    SAMPLE_TEAMS_UPLOADED_ITEM,
    TEAMS_FILE_ATTACHMENT_ID,
    TEAMS_FILE_URL,
    TEAMS_HOSTED_ID,
    TEAMS_HOSTED_URL,
    TEAMS_UPLOAD_GUID,
    TEAMS_WEBDAV_URL,
)

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestExtractMessageText:
    def test_plain_text(self):
        msg = {"body": {"contentType": "text", "content": "Hello world"}, "attachments": []}
        assert extract_message_text(msg) == "Hello world"

    def test_html_strips_tags(self):
        msg = {
            "body": {"contentType": "html", "content": "<p>Hello <b>world</b></p>"},
            "attachments": [],
        }
        assert extract_message_text(msg) == "Hello world"

    def test_adaptive_card(self):
        result = extract_message_text(SAMPLE_CHANNEL_MESSAGE_BOT)
        assert "Build completed successfully" in result
        assert "Pipeline: main-deploy" in result
        assert result.startswith("[Card]")

    def test_empty_body_no_attachments(self):
        msg = {"body": {"contentType": "text", "content": ""}, "attachments": []}
        assert extract_message_text(msg) == ""

    def test_null_body(self):
        msg = {"body": None, "attachments": []}
        assert extract_message_text(msg) == ""

    def test_missing_body(self):
        msg = {}
        assert extract_message_text(msg) == ""

    def test_truncation(self):
        msg = {"body": {"contentType": "text", "content": "x" * 600}, "attachments": []}
        result = extract_message_text(msg, max_length=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_no_truncation_by_default(self):
        long_content = "x" * 5000
        msg = {"body": {"contentType": "text", "content": long_content}, "attachments": []}
        result = extract_message_text(msg)
        assert result == long_content

    def test_negative_one_means_no_limit(self):
        long_content = "y" * 10000
        msg = {"body": {"contentType": "text", "content": long_content}, "attachments": []}
        result = extract_message_text(msg, max_length=-1)
        assert result == long_content

    def test_zero_means_no_limit(self):
        msg = {"body": {"contentType": "text", "content": "x" * 1000}, "attachments": []}
        assert len(extract_message_text(msg, max_length=0)) == 1000

    def test_malformed_adaptive_card(self):
        msg = {
            "body": {"contentType": "html", "content": ""},
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": "not valid json",
                }
            ],
        }
        assert extract_message_text(msg) == ""

    def test_file_marker_when_body_has_only_attachment_tag(self):
        """A file-only message used to render as "(empty)"."""
        msg = {
            **SAMPLE_CHAT_MESSAGE_WITH_FILE,
            "body": {
                "contentType": "html",
                "content": f'<attachment id="{TEAMS_FILE_ATTACHMENT_ID}"></attachment>',
            },
        }
        assert extract_message_text(msg) == "[File: roadmap.pptx]"

    def test_image_marker_appended(self):
        assert extract_message_text(SAMPLE_CHAT_MESSAGE_WITH_IMAGE) == "Look: [Image]"

    def test_markers_survive_truncation(self):
        """max_length caps the body a person wrote, not the attachment marker."""
        msg = {
            "body": {"contentType": "text", "content": "x" * 600},
            "attachments": SAMPLE_CHAT_MESSAGE_WITH_FILE["attachments"],
        }
        result = extract_message_text(msg, max_length=100)

        assert result.endswith("... [File: roadmap.pptx]")
        assert len(result.split(" [File:")[0]) == 103

    def test_unnamed_file_marker(self):
        msg = {
            "body": {"contentType": "html", "content": ""},
            "attachments": [{"id": "a1", "contentType": "reference", "contentUrl": "https://x"}],
        }
        assert extract_message_text(msg) == "[File: (unnamed)]"


class TestParseMessageAttachments:
    """One unified list: Graph attachments first, then body images."""

    def test_file_entry(self):
        assert parse_message_attachments(SAMPLE_CHAT_MESSAGE_WITH_FILE) == [
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

    def test_image_entry_is_parsed_from_the_body(self):
        assert parse_message_attachments(SAMPLE_CHAT_MESSAGE_WITH_IMAGE) == [
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

    def test_graph_entries_precede_body_images(self):
        msg = {
            **SAMPLE_CHAT_MESSAGE_WITH_FILE,
            "body": {
                "contentType": "html",
                "content": f'<p>deck</p><img src="{TEAMS_HOSTED_URL}">',
            },
        }
        assert [e["kind"] for e in parse_message_attachments(msg)] == ["file", "image"]

    def test_card_entry_carries_its_text(self):
        entry = parse_message_attachments(SAMPLE_CHAT_MESSAGE_WITH_CARD)[0]

        assert entry["kind"] == "card"
        assert entry["card_text"] == "Deploy finished"

    def test_message_reference_kind(self):
        msg = {"attachments": [{"id": "r1", "contentType": "messageReference"}]}
        assert parse_message_attachments(msg)[0]["kind"] == "message_reference"

    def test_unknown_content_type_is_other(self):
        msg = {"attachments": [{"id": "i1", "contentType": "image/png"}]}
        assert parse_message_attachments(msg)[0]["kind"] == "other"

    def test_junk_entries_are_skipped_and_the_walk_keeps_going(self):
        entries = parse_message_attachments(SAMPLE_CHAT_MESSAGE_WITH_JUNK_ATTACHMENTS)

        assert [e["kind"] for e in entries] == ["other", "message_reference", "other", "file"]
        # The id-less, type-less dict is still a dict, so it survives as "other"
        # with every field None.
        assert entries[0] == {
            "id": None,
            "kind": "other",
            "name": None,
            "content_type": None,
            "content_url": None,
            "thumbnail_url": None,
            "card_text": None,
        }
        assert entries[2]["content_url"] == "https://x/y.png"
        assert entries[3]["name"] == "roadmap.pptx"

    def test_duplicate_img_tags_yield_one_entry(self):
        msg = {
            "body": {
                "contentType": "html",
                "content": f'<img src="{TEAMS_HOSTED_URL}"><img src="{TEAMS_HOSTED_URL}">',
            }
        }
        assert len(parse_message_attachments(msg)) == 1

    def test_thumbnail_url_passes_through(self):
        msg = {
            "attachments": [
                {
                    "id": "f1",
                    "contentType": "reference",
                    "thumbnailUrl": "https://thumb/x.png",
                }
            ]
        }
        assert parse_message_attachments(msg)[0]["thumbnail_url"] == "https://thumb/x.png"

    @pytest.mark.parametrize("msg", [{}, {"body": None}, {"attachments": "nope"}])
    def test_malformed_messages_yield_nothing(self, msg):
        assert parse_message_attachments(msg) == []


class TestMessageBase:
    """One helper decides chat vs channel, and refuses to guess."""

    def test_chat_path(self):
        assert _message_base(chat_id="chat-1") == "/chats/chat-1/messages"

    def test_channel_path(self):
        assert _message_base(team_id="t1", channel_id="c1") == "/teams/t1/channels/c1/messages"

    def test_ids_are_percent_encoded(self):
        assert _message_base(chat_id="19:abc/def") == "/chats/19%3Aabc%2Fdef/messages"

    def test_neither_target_is_refused(self):
        with pytest.raises(ValueError, match="Provide chat_id"):
            _message_base()

    def test_both_targets_are_refused(self):
        with pytest.raises(ValueError, match="not both"):
            _message_base(chat_id="chat-1", team_id="t1", channel_id="c1")


class TestGetMessage:
    """Fetching one message by id, in a chat or a channel."""

    @respx.mock
    def test_sync_chat_message(self):
        route = respx.get(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages/chat-msg-file-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        with GraphClient("tok") as client:
            msg = teams.get_message(client, "chat-msg-file-001", chat_id="chat-1on1-001")

        assert route.called
        assert msg["id"] == "chat-msg-file-001"

    @respx.mock
    async def test_async_chat_message(self):
        route = respx.get(f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages/chat-msg-file-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            msg = await teams.aget_message(client, "chat-msg-file-001", chat_id="chat-1on1-001")

        assert route.called
        assert msg["id"] == "chat-msg-file-001"

    @respx.mock
    async def test_channel_form(self):
        route = respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages/msg-1").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGE_WITH_FILE)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.aget_message(client, "msg-1", team_id="t1", channel_id="c1")

        assert route.called

    @respx.mock
    async def test_403_means_teams_is_unavailable(self):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.aget_message(client, "m1", chat_id="chat-1")

    @respx.mock
    async def test_404_propagates(self):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await teams.aget_message(client, "m1", chat_id="chat-1")

        assert exc.value.status_code == 404


class TestGetHostedContent:
    """Inline image bytes come only from $value."""

    HOSTED_PATH = (
        f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages/chat-msg-image-001"
        f"/hostedContents/{TEAMS_HOSTED_ID}/$value"
    )

    @respx.mock
    def test_sync_returns_bytes_and_type(self):
        respx.get(self.HOSTED_PATH).mock(
            return_value=httpx.Response(
                200, content=b"\x89PNG...", headers={"Content-Type": "image/png"}
            )
        )
        with GraphClient("tok") as client:
            assert teams.get_hosted_content(
                client, "chat-msg-image-001", TEAMS_HOSTED_ID, chat_id="chat-1on1-001"
            ) == (b"\x89PNG...", "image/png")

    @respx.mock
    async def test_async_returns_bytes_and_type(self):
        respx.get(self.HOSTED_PATH).mock(
            return_value=httpx.Response(
                200, content=b"\x89PNG...", headers={"Content-Type": "image/png"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            assert await teams.aget_hosted_content(
                client, "chat-msg-image-001", TEAMS_HOSTED_ID, chat_id="chat-1on1-001"
            ) == (b"\x89PNG...", "image/png")

    @respx.mock
    async def test_403_means_teams_is_unavailable(self):
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.aget_hosted_content(client, "m1", "hosted-1", chat_id="chat-1")


class TestExtractMessageSender:
    def test_user_sender(self):
        assert extract_message_sender(SAMPLE_CHANNEL_MESSAGE_USER) == "Alice Smith"

    def test_bot_sender(self):
        assert extract_message_sender(SAMPLE_CHANNEL_MESSAGE_BOT) == "Power Automate"

    def test_null_from(self):
        msg = {"from": None}
        assert extract_message_sender(msg) == "(system)"

    def test_missing_from(self):
        msg = {}
        assert extract_message_sender(msg) == "(system)"

    def test_empty_user_and_app(self):
        msg = {"from": {"user": None, "application": None}}
        assert extract_message_sender(msg) == "(system)"


class TestPrepareTeamsBody:
    """Tests for _prepare_teams_body helper."""

    def test_auto_plain_text_converts_newlines(self):
        result = _prepare_teams_body("Hello\nWorld")
        assert result == {"contentType": "html", "content": "Hello<br>World"}

    def test_auto_plain_text_escapes_entities(self):
        result = _prepare_teams_body("if x<y and z>w")
        assert result["contentType"] == "html"
        assert "&lt;" in result["content"]
        assert "&gt;" in result["content"]

    def test_auto_detects_html_passthrough(self):
        html_msg = '<a href="https://example.com">Click here</a>'
        result = _prepare_teams_body(html_msg)
        assert result == {"contentType": "html", "content": html_msg}

    def test_auto_html_with_newlines_converts(self):
        msg = 'Update:\n- Done: <a href="https://example.com">link</a>\n- Next: review'
        result = _prepare_teams_body(msg)
        assert result["contentType"] == "html"
        assert "\n" not in result["content"]
        assert "<br>" in result["content"]
        assert '<a href="https://example.com">link</a>' in result["content"]

    def test_auto_detects_br_tags(self):
        result = _prepare_teams_body("Line 1<br>Line 2")
        assert result == {"contentType": "html", "content": "Line 1<br>Line 2"}

    def test_explicit_html_mode(self):
        result = _prepare_teams_body("plain text", content_type="html")
        assert result == {"contentType": "html", "content": "plain text"}

    def test_explicit_text_mode(self):
        result = _prepare_teams_body("Hello\nWorld", content_type="text")
        assert result == {"contentType": "text", "content": "Hello\nWorld"}

    def test_invalid_content_type_raises(self):
        with pytest.raises(ValueError, match="content_type"):
            _prepare_teams_body("msg", content_type="xml")

    def test_auto_multiline_with_entities(self):
        result = _prepare_teams_body("Dear <Team>,\nPlease review & approve.")
        assert result["contentType"] == "html"
        assert "Dear &lt;Team&gt;," in result["content"]
        assert "<br>" in result["content"]
        assert "&amp;" in result["content"]

    def test_case_insensitive_content_type(self):
        result = _prepare_teams_body("test", content_type="HTML")
        assert result["contentType"] == "html"
        result2 = _prepare_teams_body("test", content_type="Auto")
        assert result2["contentType"] == "html"

    def test_auto_normalizes_crlf(self):
        result = _prepare_teams_body("Hello\r\nWorld\rDone")
        assert result == {"contentType": "html", "content": "Hello<br>World<br>Done"}


# ---------------------------------------------------------------------------
# Synchronous operation tests
# ---------------------------------------------------------------------------


class TestTeamsSync:
    """Synchronous Teams operation tests."""

    @respx.mock
    def test_list_joined_teams(self):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = teams.list_joined_teams(client)

        assert len(result) == 2
        assert result[0]["displayName"] == "Engineering"

    @respx.mock
    def test_list_channels(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/team-id-001/channels").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNELS_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = teams.list_channels(client, "team-id-001")

        assert len(result) == 2
        assert result[0]["displayName"] == "General"

    @respx.mock
    def test_send_channel_message(self):
        respx.post(f"{GRAPH_BASE_URL}/teams/team-id-001/channels/channel-id-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        with GraphClient("tok") as client:
            result = teams.send_channel_message(client, "team-id-001", "channel-id-001", "Hello!")

        assert result["id"] == "msg-001"

    @respx.mock
    def test_list_channel_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_MESSAGES_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = teams.list_channel_messages(client, "t1", "c1")

        assert len(result) == 2
        assert result[0]["id"] == "msg-user-001"

    @respx.mock
    def test_list_chats(self):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = teams.list_chats(client)

        assert len(result) == 3
        assert result[0]["chatType"] == "oneOnOne"

    @respx.mock
    def test_list_chats_with_filter(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with GraphClient("tok") as client:
            teams.list_chats(client, chat_type="group")

        assert "chatType" in str(route.calls[0].request.url) and "group" in str(
            route.calls[0].request.url
        )

    @respx.mock
    def test_list_chat_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = teams.list_chat_messages(client, "chat-1")

        assert len(result) == 1

    @respx.mock
    def test_send_chat_message(self):
        respx.post(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with GraphClient("tok") as client:
            result = teams.send_chat_message(client, "chat-1", "Hi!")

        assert result["id"] == "chat-msg-sent-001"

    @respx.mock
    def test_send_channel_message_auto_converts_newlines(self):
        route = respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        with GraphClient("tok") as client:
            teams.send_channel_message(client, "t1", "c1", "Hello\nWorld")

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == "Hello<br>World"

    @respx.mock
    def test_send_channel_message_auto_html_passthrough(self):
        route = respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        html_msg = '<a href="https://example.com">link</a>'
        with GraphClient("tok") as client:
            teams.send_channel_message(client, "t1", "c1", html_msg)

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == html_msg

    @respx.mock
    def test_send_channel_message_explicit_text(self):
        route = respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        with GraphClient("tok") as client:
            teams.send_channel_message(client, "t1", "c1", "Hello!", content_type="text")

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "text"
        assert payload["body"]["content"] == "Hello!"

    @respx.mock
    def test_send_chat_message_auto_newlines(self):
        route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with GraphClient("tok") as client:
            teams.send_chat_message(client, "chat-1", "Line 1\nLine 2\nLine 3")

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == "Line 1<br>Line 2<br>Line 3"

    @respx.mock
    def test_teams_403_raises_not_available(self):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.list_joined_teams(client)

    @respx.mock
    def test_channels_403_raises_not_available(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.list_channels(client, "t1")

    @respx.mock
    def test_send_message_403_raises_not_available(self):
        respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.send_channel_message(client, "t1", "c1", "Hello!")

    @respx.mock
    def test_list_channel_messages_403(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.list_channel_messages(client, "t1", "c1")

    @respx.mock
    def test_list_chats_403(self):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.list_chats(client)

    @respx.mock
    def test_list_chat_messages_403(self):
        respx.get(f"{GRAPH_BASE_URL}/chats/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.list_chat_messages(client, "c1")

    @respx.mock
    def test_send_chat_message_403(self):
        respx.post(f"{GRAPH_BASE_URL}/chats/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                teams.send_chat_message(client, "c1", "Hi!")


# ---------------------------------------------------------------------------
# Async operation tests
# ---------------------------------------------------------------------------


class TestTeamsAsync:
    """Async Teams operation tests."""

    @respx.mock
    async def test_alist_joined_teams(self):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json=SAMPLE_TEAMS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_joined_teams(client)

        assert len(result) == 2

    @respx.mock
    async def test_alist_channels(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/team-id-001/channels").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNELS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_channels(client, "team-id-001")

        assert len(result) == 2

    @respx.mock
    async def test_asend_channel_message(self):
        respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.asend_channel_message(client, "t1", "c1", "Hello!")

        assert result["id"] == "msg-001"

    @respx.mock
    async def test_alist_channel_messages(self):
        route = respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_MESSAGES_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_channel_messages(
                client, "t1", "c1", since="2025-01-01T00:00:00Z"
            )

        assert len(result) == 2
        request_url = str(route.calls[0].request.url)
        assert "$orderby" not in request_url

    @respx.mock
    async def test_alist_chats(self):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_chats(client)

        assert len(result) == 3

    @respx.mock
    async def test_alist_chats_with_filter(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        async with AsyncGraphClient("tok") as client:
            await teams.alist_chats(client, chat_type="meeting")

        assert "chatType" in str(route.calls[0].request.url) and "meeting" in str(
            route.calls[0].request.url
        )

    @respx.mock
    async def test_alist_chat_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_chat_messages(client, "chat-1", since="2025-01-01T00:00:00Z")

        assert len(result) == 1

    @respx.mock
    async def test_asend_chat_message(self):
        respx.post(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.asend_chat_message(client, "chat-1", "Hi!")

        assert result["id"] == "chat-msg-sent-001"

    @respx.mock
    async def test_asend_channel_message_auto_newlines(self):
        route = respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHANNEL_MESSAGE)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_channel_message(client, "t1", "c1", "A\nB")

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == "A<br>B"

    @respx.mock
    async def test_asend_chat_message_html_passthrough(self):
        route = respx.post(f"{GRAPH_BASE_URL}/chats/chat-1/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        html_msg = "<p>Hello <strong>world</strong></p>"
        async with AsyncGraphClient("tok") as client:
            await teams.asend_chat_message(client, "chat-1", html_msg)

        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "html"
        assert payload["body"]["content"] == html_msg

    @respx.mock
    async def test_async_teams_403_raises_not_available(self):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.alist_joined_teams(client)

    @respx.mock
    async def test_async_channels_403_raises_not_available(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.alist_channels(client, "t1")

    @respx.mock
    async def test_async_send_message_403_raises_not_available(self):
        respx.post(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.asend_channel_message(client, "t1", "c1", "Hello!")

    @respx.mock
    async def test_async_channel_messages_403(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.alist_channel_messages(client, "t1", "c1")

    @respx.mock
    async def test_async_chats_403(self):
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.alist_chats(client)

    @respx.mock
    async def test_async_chat_messages_403(self):
        respx.get(f"{GRAPH_BASE_URL}/chats/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.alist_chat_messages(client, "c1")

    @respx.mock
    async def test_async_send_chat_message_403(self):
        respx.post(f"{GRAPH_BASE_URL}/chats/c1/messages").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.asend_chat_message(client, "c1", "Hi!")

    @respx.mock
    async def test_non_403_error_propagates(self):
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "ResourceNotFound", "message": "Not found"}}
            )
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await teams.alist_joined_teams(client)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Chat pagination tests
# ---------------------------------------------------------------------------


class TestChatPagination:
    """Tests for alist_chats internal pagination."""

    @respx.mock
    async def test_alist_chats_paginates_across_pages(self):
        """Fetches multiple pages when top exceeds the per-page limit."""
        page1_chats = [{"id": f"chat-{i}", "chatType": "oneOnOne"} for i in range(50)]
        page2_chats = [{"id": f"chat-{i}", "chatType": "oneOnOne"} for i in range(50, 75)]

        responses = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "value": page1_chats,
                        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/chats?$skip=50",
                    },
                ),
                httpx.Response(200, json={"value": page2_chats}),
            ]
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(side_effect=lambda req: next(responses))

        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_chats(client, top=100)

        assert len(result) == 75

    @respx.mock
    async def test_alist_chats_single_page_no_next_link(self):
        """Does not paginate when no @odata.nextLink is present."""
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_chats(client, top=100)

        assert len(result) == 3
        assert respx.calls.call_count == 1

    @respx.mock
    async def test_alist_chats_with_filter_paginates(self):
        """Filter parameter is preserved during pagination."""
        page1 = [{"id": f"chat-{i}", "chatType": "oneOnOne"} for i in range(50)]
        page2 = [{"id": f"chat-{i}", "chatType": "oneOnOne"} for i in range(50, 60)]

        responses = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "value": page1,
                        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/chats?$skip=50",
                    },
                ),
                httpx.Response(200, json={"value": page2}),
            ]
        )
        route = respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            side_effect=lambda req: next(responses)
        )

        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_chats(client, chat_type="oneOnOne", top=100)

        assert len(result) == 60
        first_url = str(route.calls[0].request.url)
        assert "oneOnOne" in first_url


# ---------------------------------------------------------------------------
# Activity aggregator tests
# ---------------------------------------------------------------------------


class TestTeamsActivity:
    @respx.mock
    async def test_aget_teams_activity(self):
        """Activity aggregator fetches teams, channels, messages, and chats."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        recent_ts = now.isoformat()
        old_ts = "2020-01-01T00:00:00Z"

        # Mock: 1 team, 2 channels, 1 recent channel message, 1 old
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(
                200, json={"value": [{"id": "t1", "displayName": "TestTeam"}]}
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "chat-1",
                            "chatType": "oneOnOne",
                            "topic": None,
                            "members": [{"displayName": "Alice"}],
                            "lastMessagePreview": {
                                "createdDateTime": recent_ts,
                                "body": {"content": "Hey!"},
                                "from": {"user": {"displayName": "Alice"}},
                            },
                        }
                    ]
                },
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "c1", "displayName": "General"},
                        {"id": "c2", "displayName": "Random"},
                    ]
                },
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m1",
                            "createdDateTime": recent_ts,
                            "from": {"user": {"displayName": "Bob"}, "application": None},
                            "body": {"contentType": "text", "content": "New update"},
                            "attachments": [],
                        }
                    ]
                },
            )
        )
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c2/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m2",
                            "createdDateTime": old_ts,
                            "from": {"user": {"displayName": "Charlie"}, "application": None},
                            "body": {"contentType": "text", "content": "Old message"},
                            "attachments": [],
                        }
                    ]
                },
            )
        )

        async with AsyncGraphClient("tok") as client:
            activity = await teams.aget_teams_activity(client, hours=24)

        # Should include the recent channel message and the recent chat, but not the old one
        assert len(activity) == 2
        sources = {a["source"] for a in activity}
        assert "channel" in sources
        assert "chat" in sources
        # Sorted by timestamp descending
        assert activity[0]["timestamp"] >= activity[1]["timestamp"]

    @respx.mock
    async def test_aget_teams_activity_empty(self):
        """No activity returns empty list."""
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": []})
        )

        async with AsyncGraphClient("tok") as client:
            activity = await teams.aget_teams_activity(client, hours=24)

        assert activity == []

    @respx.mock
    async def test_aget_teams_activity_raises_when_teams_unavailable(self):
        """Activity aggregator raises TeamsNotAvailableError when Teams is not licensed (403)."""
        respx.get(f"{GRAPH_BASE_URL}/me/joinedTeams").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )

        async with AsyncGraphClient("tok") as client:
            with pytest.raises(teams.TeamsNotAvailableError):
                await teams.aget_teams_activity(client, hours=24)


# ---------------------------------------------------------------------------
# Desktop JSON operations
# ---------------------------------------------------------------------------


def _query_property(url: str, param: str) -> str:
    """Pull the leading OData property name out of a $orderby/$filter clause.

    Reads it back off the wire rather than off the module constant, so the test
    would still notice if the two clauses were built from different sources.
    """
    value = parse_qs(urlparse(url).query)[param][0]
    return value.split(" ")[0]


class TestChatsPageSync:
    """Synchronous chats-page tests."""

    @respx.mock
    def test_orderby_uses_last_message_preview(self):
        """lastUpdatedDateTime is not sortable on /me/chats; the preview timestamp is."""
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_PAGE)
        )
        with GraphClient("tok") as client:
            data = teams.chats_page(client)

        assert data == SAMPLE_CHATS_PAGE
        url = str(route.calls[0].request.url)
        assert "$orderby=lastMessagePreview/createdDateTime%20desc" in url
        assert "$expand=lastMessagePreview" in url
        assert "$top=50" in url
        assert "lastUpdatedDateTime" not in url
        assert "+" not in url

    @respx.mock
    def test_top_is_honoured(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_PAGE)
        )
        with GraphClient("tok") as client:
            teams.chats_page(client, top=10)

        assert "$top=10" in str(route.calls[0].request.url)

    @respx.mock
    def test_cursor_is_fetched_verbatim(self):
        route = respx.get(SAMPLE_CHATS_PAGE_NEXT_LINK).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with GraphClient("tok") as client:
            teams.chats_page(client, cursor=SAMPLE_CHATS_PAGE_NEXT_LINK, top=10)

        assert route.call_count == 1
        assert str(route.calls[0].request.url) == SAMPLE_CHATS_PAGE_NEXT_LINK

    @respx.mock
    def test_403_is_not_translated(self):
        """The desktop ops leave TeamsNotAvailableError to the markdown tools."""
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                teams.chats_page(client)

        assert exc.value.status_code == 403


class TestChatsPageAsync:
    """Async chats-page tests."""

    @respx.mock
    async def test_orderby_uses_last_message_preview(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/chats").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHATS_PAGE)
        )
        async with AsyncGraphClient("tok") as client:
            data = await teams.achats_page(client)

        assert data == SAMPLE_CHATS_PAGE
        assert "$orderby=lastMessagePreview/createdDateTime%20desc" in str(
            route.calls[0].request.url
        )

    @respx.mock
    async def test_cursor_is_fetched_verbatim(self):
        route = respx.get(SAMPLE_CHATS_PAGE_NEXT_LINK).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        async with AsyncGraphClient("tok") as client:
            await teams.achats_page(client, cursor=SAMPLE_CHATS_PAGE_NEXT_LINK)

        assert str(route.calls[0].request.url) == SAMPLE_CHATS_PAGE_NEXT_LINK


class TestChatMembers:
    """Chat members tests (sync and async)."""

    @respx.mock
    def test_requests_members_without_top(self):
        # Graph's /chats/{id}/members rejects $top ("Query option 'Top' is
        # not allowed"), so the request must carry no query options at all.
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
        )
        with GraphClient("tok") as client:
            data = teams.chat_members(client, "chat-1on1-001")

        assert data == SAMPLE_CHAT_MEMBERS_RESPONSE
        assert route.calls[0].request.url.path.endswith("/chats/chat-1on1-001/members")
        assert not route.calls[0].request.url.query

    @respx.mock
    def test_follows_next_link_across_pages(self):
        next_link = f"{GRAPH_BASE_URL}/chats/chat-group-001/members?$skiptoken=abc"
        # Register the cursor route first: a respx URL pattern without query
        # params matches ANY query string, so the bare route would swallow both.
        respx.get(next_link).mock(
            return_value=httpx.Response(200, json={"value": [{"userId": "u2"}]})
        )
        respx.get(f"{GRAPH_BASE_URL}/chats/chat-group-001/members").mock(
            return_value=httpx.Response(
                200, json={"value": [{"userId": "u1"}], "@odata.nextLink": next_link}
            )
        )
        with GraphClient("tok") as client:
            data = teams.chat_members(client, "chat-group-001")

        assert [m["userId"] for m in data["value"]] == ["u1", "u2"]
        assert "@odata.nextLink" not in data

    @respx.mock
    def test_page_cap_stops_the_walk_and_keeps_next_link(self):
        """A runaway nextLink chain stops at the cap, and the link survives as a
        truncation signal instead of silently yielding a partial roster."""
        calls = {"n": 0}

        def _endless(request):
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "value": [{"userId": f"u{calls['n']}"}],
                    "@odata.nextLink": f"{GRAPH_BASE_URL}/chats/c/members?$skiptoken={calls['n']}",
                },
            )

        respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(side_effect=_endless)
        with GraphClient("tok") as client:
            data = teams.chat_members(client, "chat-group-001")

        assert calls["n"] == teams._MAX_MEMBER_PAGES
        assert len(data["value"]) == teams._MAX_MEMBER_PAGES
        assert data["@odata.nextLink"]

    @respx.mock
    async def test_achat_members_encodes_chat_id(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            data = await teams.achat_members(client, "19:a/b+c@thread.v2")

        assert data == SAMPLE_CHAT_MEMBERS_RESPONSE
        assert "/chats/19%3Aa%2Fb%2Bc%40thread.v2/members" in str(route.calls[0].request.url)
        # The async path is the one get_chat_members_json ships; $top here is
        # what broke the tool, so pin the empty query string, not a substring.
        assert not route.calls[0].request.url.query

    @respx.mock
    async def test_achat_members_follows_next_link(self):
        next_link = f"{GRAPH_BASE_URL}/chats/chat-group-001/members?$skiptoken=abc"
        # Register the cursor route first: a respx URL pattern without query
        # params matches ANY query string, so the bare route would swallow both.
        respx.get(next_link).mock(
            return_value=httpx.Response(200, json={"value": [{"userId": "u2"}]})
        )
        respx.get(f"{GRAPH_BASE_URL}/chats/chat-group-001/members").mock(
            return_value=httpx.Response(
                200, json={"value": [{"userId": "u1"}], "@odata.nextLink": next_link}
            )
        )
        async with AsyncGraphClient("tok") as client:
            data = await teams.achat_members(client, "chat-group-001")

        assert [m["userId"] for m in data["value"]] == ["u1", "u2"]


class TestChatMessagesPageSync:
    """Synchronous chat-messages-page tests."""

    @respx.mock
    def test_filter_and_orderby_share_one_property(self):
        """Graph silently ignores a $filter whose property differs from $orderby's."""
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        with GraphClient("tok") as client:
            teams.chat_messages_page(client, "chat-1on1-001", since="2026-01-05T00:00:00Z")

        url = str(route.calls[0].request.url)
        assert _query_property(url, "$filter") == _query_property(url, "$orderby")
        assert _query_property(url, "$orderby") == teams.CHAT_MESSAGE_SORT_PROP

    @respx.mock
    def test_since_uses_percent_20_not_plus(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        with GraphClient("tok") as client:
            teams.chat_messages_page(client, "chat-1on1-001", since="2026-01-05T00:00:00Z")

        url = str(route.calls[0].request.url)
        assert "$filter=lastModifiedDateTime%20gt%202026-01-05T00%3A00%3A00Z" in url
        assert "$orderby=lastModifiedDateTime%20desc" in url
        assert "+" not in url

    @respx.mock
    def test_empty_since_sends_no_filter(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        with GraphClient("tok") as client:
            data = teams.chat_messages_page(client, "chat-1on1-001")

        assert data == SAMPLE_CHAT_MESSAGES_PAGE
        url = str(route.calls[0].request.url)
        assert "$filter" not in url
        assert "$top=50" in url

    @respx.mock
    def test_cursor_is_fetched_verbatim(self):
        cursor = "https://graph.microsoft.com/v1.0/chats/c1/messages?$skiptoken=a%2Bb%2Fc"
        route = respx.get(cursor).mock(return_value=httpx.Response(200, json={"value": []}))
        with GraphClient("tok") as client:
            teams.chat_messages_page(
                client, "chat-1on1-001", since="2026-01-05T00:00:00Z", cursor=cursor
            )

        assert route.call_count == 1
        assert str(route.calls[0].request.url) == cursor


class TestChatMessagesPageAsync:
    """Async chat-messages-page tests."""

    @respx.mock
    async def test_filter_and_orderby_share_one_property(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.achat_messages_page(client, "chat-1on1-001", since="2026-01-05T00:00:00Z")

        url = str(route.calls[0].request.url)
        assert _query_property(url, "$filter") == _query_property(url, "$orderby")

    @respx.mock
    async def test_chat_id_is_url_encoded(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MESSAGES_PAGE)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.achat_messages_page(client, "19:a/b+c@thread.v2")

        assert "/chats/19%3Aa%2Fb%2Bc%40thread.v2/messages" in str(route.calls[0].request.url)

    @respx.mock
    async def test_cursor_is_fetched_verbatim(self):
        cursor = "https://graph.microsoft.com/v1.0/chats/c1/messages?$skiptoken=a%2Bb%2Fc"
        route = respx.get(cursor).mock(return_value=httpx.Response(200, json={"value": []}))
        async with AsyncGraphClient("tok") as client:
            await teams.achat_messages_page(client, "chat-1on1-001", cursor=cursor)

        assert str(route.calls[0].request.url) == cursor


# ---------------------------------------------------------------------------
# Token claims and mark-as-read tests
# ---------------------------------------------------------------------------


def _make_fake_jwt(claims: dict) -> str:
    """Build a fake JWT with given payload claims."""
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


class TestDecodeTokenClaims:
    def test_valid_token(self):
        token = _make_fake_jwt({"oid": "user-obj-id", "tid": "tenant-123", "sub": "x"})
        result = teams.decode_token_claims(token)
        assert result == {"oid": "user-obj-id", "tid": "tenant-123"}

    def test_missing_claims(self):
        token = _make_fake_jwt({"sub": "x"})
        result = teams.decode_token_claims(token)
        assert result == {"oid": "", "tid": ""}

    def test_invalid_token(self):
        result = teams.decode_token_claims("not-a-jwt")
        assert result == {"oid": "", "tid": ""}

    def test_malformed_payload(self):
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode()
        sig = base64.urlsafe_b64encode(b"s").rstrip(b"=").decode()
        token = f"{header}.{payload}.{sig}"
        result = teams.decode_token_claims(token)
        assert result == {"oid": "", "tid": ""}


class TestMarkChatRead:
    @respx.mock
    async def test_amark_chat_read_success(self):
        chat_id = "chat-1on1-001"
        route = respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/markChatReadForUser").mock(
            return_value=httpx.Response(204)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.amark_chat_read(client, chat_id, "user-oid-123", "tenant-tid-456")

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"user": {"id": "user-oid-123", "tenantId": "tenant-tid-456"}}

    @respx.mock
    async def test_amark_chat_read_403_raises_not_available(self):
        chat_id = "chat-1on1-001"
        respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/markChatReadForUser").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.amark_chat_read(client, chat_id, "uid", "tid")

    @respx.mock
    async def test_amark_chat_read_propagates_other_errors(self):
        chat_id = "chat-1on1-001"
        respx.post(f"{GRAPH_BASE_URL}/chats/{chat_id}/markChatReadForUser").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "NotFound", "message": "Chat not found"}}
            )
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await teams.amark_chat_read(client, chat_id, "uid", "tid")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _is_chat_unread helper tests
# ---------------------------------------------------------------------------


class TestIsChatUnread:
    @pytest.fixture(autouse=True)
    def _import_helper(self):
        from ms_graph_mcp import _is_chat_unread

        self._is_chat_unread = _is_chat_unread

    def test_no_preview_returns_false(self):
        chat = {"lastMessagePreview": None, "viewpoint": None}
        assert self._is_chat_unread(chat) is False

    def test_no_preview_key_returns_false(self):
        chat = {"viewpoint": None}
        assert self._is_chat_unread(chat) is False

    def test_null_viewpoint_with_messages_returns_true(self):
        chat = {
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T14:00:00Z",
                "body": {"content": "hi"},
                "from": {},
            },
            "viewpoint": None,
        }
        assert self._is_chat_unread(chat) is True

    def test_null_last_read_with_messages_returns_true(self):
        chat = {
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T14:00:00Z",
                "body": {"content": "hi"},
                "from": {},
            },
            "viewpoint": {"lastMessageReadDateTime": None},
        }
        assert self._is_chat_unread(chat) is True

    def test_equal_timestamps_returns_false(self):
        chat = {
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T14:00:00Z",
                "body": {"content": "hi"},
                "from": {},
            },
            "viewpoint": {"lastMessageReadDateTime": "2025-12-15T14:00:00Z"},
        }
        assert self._is_chat_unread(chat) is False

    def test_read_after_message_returns_false(self):
        chat = {
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T14:00:00Z",
                "body": {"content": "hi"},
                "from": {},
            },
            "viewpoint": {"lastMessageReadDateTime": "2025-12-15T15:00:00Z"},
        }
        assert self._is_chat_unread(chat) is False

    def test_unread_returns_true(self):
        chat = {
            "lastMessagePreview": {
                "createdDateTime": "2025-12-15T14:00:00Z",
                "body": {"content": "hi"},
                "from": {},
            },
            "viewpoint": {"lastMessageReadDateTime": "2025-12-15T13:00:00Z"},
        }
        assert self._is_chat_unread(chat) is True


# ---------------------------------------------------------------------------
# Sending files and inline images
# ---------------------------------------------------------------------------

CHAT_UPLOAD_URL = (
    f"{GRAPH_BASE_URL}/me/drive/root:/Microsoft%20Teams%20Chat%20Files/notes.txt:/content"
)
UPLOADED_ITEM_URL = f"{GRAPH_BASE_URL}/me/drive/items/teams-upload-001"
# Cleanup addresses the item through its own drive (parentReference.driveId).
UPLOADED_ITEM_DRIVE_URL = f"{GRAPH_BASE_URL}/drives/drive-001/items/teams-upload-001"
CHAT_MEMBERS_URL = f"{GRAPH_BASE_URL}/chats/chat-1on1-001/members"
INVITE_URL = f"{GRAPH_BASE_URL}/me/drive/items/teams-upload-001/invite"
CHAT_MESSAGES_URL = f"{GRAPH_BASE_URL}/chats/chat-1on1-001/messages"

NOTES = ResolvedAttachment(name="notes.txt", data=b"hello", content_type="text/plain")
PNG = ResolvedAttachment(name="pic.png", data=b"\x89PNG\r\n\x1a\n", content_type="image/png")


def _call_trail() -> list[tuple[str, str]]:
    """(method, path) for every request respx saw, in the order it saw them."""
    return [(c.request.method, c.request.url.path) for c in respx.calls]


def _mock_chat_upload() -> None:
    """The four requests a chat file send makes, all answered happily."""
    respx.put(url__startswith=CHAT_UPLOAD_URL).mock(
        return_value=httpx.Response(201, json=SAMPLE_TEAMS_UPLOAD_RESPONSE)
    )
    respx.get(url__startswith=UPLOADED_ITEM_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_TEAMS_UPLOADED_ITEM)
    )
    respx.get(CHAT_MEMBERS_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
    )
    respx.post(INVITE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_INVITE_RESPONSE))


class TestAttachmentFromDriveItem:
    """The Teams file card is keyed off the GUID buried in the driveItem eTag."""

    def test_guid_is_pulled_out_of_the_quoted_etag(self):
        entry = _attachment_from_drive_item(SAMPLE_TEAMS_UPLOADED_ITEM)
        assert entry == {
            "id": TEAMS_UPLOAD_GUID,
            "contentType": "reference",
            "contentUrl": TEAMS_WEBDAV_URL,
            "name": "notes.txt",
        }

    def test_webdav_url_wins_over_the_browser_url(self):
        """Teams follows contentUrl itself; the /personal/... webUrl is a page, not a file."""
        item = dict(SAMPLE_TEAMS_UPLOADED_ITEM)
        entry = _attachment_from_drive_item(item)
        assert entry["contentUrl"] == item["webDavUrl"] != item["webUrl"]

    def test_web_url_is_the_fallback(self):
        item = {k: v for k, v in SAMPLE_TEAMS_UPLOADED_ITEM.items() if k != "webDavUrl"}
        assert _attachment_from_drive_item(item)["contentUrl"] == item["webUrl"]

    def test_missing_etag_is_refused(self):
        item = {k: v for k, v in SAMPLE_TEAMS_UPLOADED_ITEM.items() if k != "eTag"}
        with pytest.raises(ValueError, match="no eTag GUID"):
            _attachment_from_drive_item(item)

    def test_etag_without_a_guid_is_refused(self):
        with pytest.raises(ValueError, match="no eTag GUID"):
            _attachment_from_drive_item({"name": "x.txt", "eTag": '"12345,1"'})


class TestMemberEmails:
    """Only real addresses are worth inviting, and each of them once."""

    def test_both_members_are_kept_in_order(self):
        assert _member_emails(SAMPLE_CHAT_MEMBERS_RESPONSE) == [
            "user@example.com",
            "alice@example.com",
        ]

    def test_blanks_junk_and_case_duplicates_are_dropped(self):
        members = {
            "value": [
                {"email": " Alice@Example.com "},
                {"email": "alice@example.com"},
                {"email": "   "},
                {"email": None},
                {"displayName": "No address"},
                "not-a-dict",
            ]
        }
        assert _member_emails(members) == ["Alice@Example.com"]

    def test_an_empty_roster_is_no_emails(self):
        assert _member_emails({}) == []

    def test_the_sender_is_left_out_by_user_id(self):
        """The signed-in user owns the file; inviting the owner can sink the whole invite."""
        assert _member_emails(SAMPLE_CHAT_MEMBERS_RESPONSE, "user-id-001") == ["alice@example.com"]

    def test_an_unknown_user_id_excludes_nobody(self):
        assert _member_emails(SAMPLE_CHAT_MEMBERS_RESPONSE, "someone-else") == [
            "user@example.com",
            "alice@example.com",
        ]


class TestHostedContents:
    """Inline images ride inside the message payload, so they are checked first."""

    def test_images_are_numbered_from_one(self):
        hosted = _hosted_contents([PNG, PNG])
        assert [h["@microsoft.graph.temporaryId"] for h in hosted] == ["1", "2"]
        assert hosted[0]["contentType"] == "image/png"
        assert base64.b64decode(hosted[0]["contentBytes"]) == PNG.data

    def test_a_non_image_is_refused(self):
        with pytest.raises(ValueError, match="not an image"):
            _hosted_contents([NOTES])

    def test_an_oversized_image_is_refused(self):
        big = ResolvedAttachment(
            name="huge.png",
            data=b"x" * (teams.MAX_HOSTED_IMAGE_BYTES + 1),
            content_type="image/png",
        )
        with pytest.raises(ValueError, match="the limit is 4,000,000"):
            _hosted_contents([big])

    def test_no_images_is_no_payload(self):
        assert _hosted_contents([]) == []


class TestPrepareTeamsBodyWithExtras:
    """A body carrying a file card or a picture is always HTML."""

    def test_plain_body_is_untouched_without_extras(self):
        assert _prepare_teams_body("hi", "text") == {"contentType": "text", "content": "hi"}

    def test_text_mode_becomes_html_and_stays_escaped(self):
        result = _prepare_teams_body("a < b\nc", "text", attachment_ids=["G1"])
        assert result == {
            "contentType": "html",
            "content": 'a &lt; b<br>c<attachment id="G1"></attachment>',
        }

    def test_auto_mode_keeps_detected_html(self):
        result = _prepare_teams_body("<b>hi</b>", "auto", attachment_ids=["G1"])
        assert result["content"] == '<b>hi</b><attachment id="G1"></attachment>'

    def test_html_mode_passes_the_markup_through(self):
        result = _prepare_teams_body("<p>hi</p>", "html", attachment_ids=["G1", "G2"])
        assert result["content"] == (
            '<p>hi</p><attachment id="G1"></attachment><attachment id="G2"></attachment>'
        )

    def test_images_follow_the_attachments_in_order(self):
        result = _prepare_teams_body("hi", "text", attachment_ids=["G1"], image_count=2)
        assert result["content"] == (
            'hi<attachment id="G1"></attachment>'
            '<img src="../hostedContents/1/$value">'
            '<img src="../hostedContents/2/$value">'
        )

    def test_empty_content_is_just_the_tags(self):
        result = _prepare_teams_body("", "auto", image_count=1)
        assert result == {
            "contentType": "html",
            "content": '<img src="../hostedContents/1/$value">',
        }


class TestSendChatMessagePayload:
    """The plain send is unchanged; the new keywords only add keys."""

    @respx.mock
    def test_no_extras_sends_the_same_payload_as_before(self):
        route = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with GraphClient("tok") as client:
            teams.send_chat_message(client, "chat-1on1-001", "hi", "text")

        assert json.loads(route.calls[0].request.content) == {
            "body": {"contentType": "text", "content": "hi"}
        }

    @respx.mock
    async def test_graph_shaped_extras_pass_straight_through(self):
        route = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        attachment = {"id": "G1", "contentType": "reference", "contentUrl": "u", "name": "n"}
        hosted = [{"@microsoft.graph.temporaryId": "1"}]
        async with AsyncGraphClient("tok") as client:
            await teams.asend_chat_message(
                client,
                "chat-1on1-001",
                "hi",
                "text",
                attachments=[attachment],
                hosted_contents=hosted,
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["attachments"] == [attachment]
        assert payload["hostedContents"] == hosted


class TestSendChatFile:
    """Uploading to OneDrive, sharing it, then posting the card that points at it."""

    @respx.mock
    async def test_upload_share_and_post_in_that_order(self):
        _mock_chat_upload()
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client, content="here", files=[NOTES], chat_id="chat-1on1-001"
            )

        assert _call_trail() == [
            ("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/notes.txt:/content"),
            ("GET", "/v1.0/me/drive/items/teams-upload-001"),
            ("GET", "/v1.0/chats/chat-1on1-001/members"),
            ("POST", "/v1.0/me/drive/items/teams-upload-001/invite"),
            ("POST", "/v1.0/chats/chat-1on1-001/messages"),
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
        assert payload["body"]["contentType"] == "html"
        assert f'<attachment id="{TEAMS_UPLOAD_GUID}"></attachment>' in payload["body"]["content"]
        assert "hostedContents" not in payload

    @respx.mock
    async def test_the_upload_renames_rather_than_overwriting(self):
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client, content="here", files=[NOTES], chat_id="chat-1on1-001"
            )

        assert "@microsoft.graph.conflictBehavior=rename" in str(respx.calls[0].request.url)

    @respx.mock
    async def test_every_chat_member_is_invited_to_read_the_file(self):
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client, content="here", files=[NOTES], chat_id="chat-1on1-001"
            )

        invite = json.loads(respx.calls[3].request.content)
        assert invite["recipients"] == [
            {"email": "user@example.com"},
            {"email": "alice@example.com"},
        ]
        assert invite["roles"] == ["read"]
        assert invite["sendInvitation"] is False

    @respx.mock
    async def test_a_failed_share_is_logged_and_the_message_still_goes_out(self, caplog):
        """People already in the chat can see the file; a share error must not lose the message."""
        _mock_chat_upload()
        respx.post(INVITE_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with caplog.at_level("WARNING"):
            async with AsyncGraphClient("tok") as client:
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert post.called
        assert "Could not share notes.txt" in caplog.text

    @respx.mock
    async def test_a_failed_member_lookup_skips_the_share_and_still_posts(self, caplog):
        """The file is already uploaded by then; losing the message would be worse."""
        _mock_chat_upload()
        respx.get(CHAT_MEMBERS_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        invite = respx.post(INVITE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_INVITE_RESPONSE)
        )
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with caplog.at_level("WARNING"):
            async with AsyncGraphClient("tok") as client:
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert not invite.called
        assert post.called
        assert "Could not share notes.txt" in caplog.text

    @respx.mock
    async def test_a_403_on_the_upload_is_a_scope_problem_and_nothing_is_posted(self):
        respx.put(url__startswith=CHAT_UPLOAD_URL).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(FilesScopeMissingError, match="Files.ReadWrite"):
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert not post.called

    @respx.mock
    async def test_a_non_403_upload_error_stays_a_graph_error(self):
        respx.put(url__startswith=CHAT_UPLOAD_URL).mock(
            return_value=httpx.Response(507, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert exc.value.status_code == 507

    @respx.mock
    async def test_a_403_on_the_message_itself_is_still_a_teams_problem(self):
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        removed = respx.delete(UPLOADED_ITEM_DRIVE_URL).mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert removed.call_count == 1

    @respx.mock
    async def test_a_failed_post_removes_the_uploaded_file(self):
        """Otherwise a retry re-uploads under a renamed copy; the drive fills with duplicates."""
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        )
        removed = respx.delete(UPLOADED_ITEM_DRIVE_URL).mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError, match="503"):
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert removed.call_count == 1
        assert _call_trail()[-1] == ("DELETE", "/v1.0/drives/drive-001/items/teams-upload-001")

    @respx.mock
    async def test_a_failed_second_upload_removes_the_first(self):
        _mock_chat_upload()
        respx.put(url__startswith=CHAT_UPLOAD_URL).mock(
            side_effect=[
                httpx.Response(201, json=SAMPLE_TEAMS_UPLOAD_RESPONSE),
                httpx.Response(403, json=GRAPH_ERROR_403),
            ]
        )
        removed = respx.delete(UPLOADED_ITEM_DRIVE_URL).mock(return_value=httpx.Response(204))
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(FilesScopeMissingError):
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES, NOTES], chat_id="chat-1on1-001"
                )

        assert removed.call_count == 1
        assert not post.called

    @respx.mock
    async def test_a_cleanup_failure_does_not_mask_the_real_error(self):
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        )
        respx.delete(UPLOADED_ITEM_DRIVE_URL).mock(return_value=httpx.Response(500))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError, match="503"):
                await teams.asend_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

    @respx.mock
    async def test_the_sender_is_not_invited_to_their_own_file(self):
        _mock_chat_upload()
        invite = respx.post(INVITE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_INVITE_RESPONSE)
        )
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client,
                content="here",
                files=[NOTES],
                chat_id="chat-1on1-001",
                exclude_user_id="user-id-001",
            )

        recipients = json.loads(invite.calls[0].request.content)["recipients"]
        assert recipients == [{"email": "alice@example.com"}]

    @respx.mock
    def test_sync_failed_post_removes_the_uploaded_file(self):
        _mock_chat_upload()
        respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        )
        removed = respx.delete(UPLOADED_ITEM_DRIVE_URL).mock(return_value=httpx.Response(204))
        with GraphClient("tok") as client:
            with pytest.raises(GraphError, match="503"):
                teams.send_message_with_files(
                    client, content="here", files=[NOTES], chat_id="chat-1on1-001"
                )

        assert removed.call_count == 1

    @respx.mock
    def test_sync_twin_walks_the_same_path(self):
        _mock_chat_upload()
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with GraphClient("tok") as client:
            teams.send_message_with_files(
                client, content="here", files=[NOTES], chat_id="chat-1on1-001"
            )

        assert [m for m, _ in _call_trail()] == ["PUT", "GET", "GET", "POST", "POST"]
        assert json.loads(post.calls[0].request.content)["attachments"][0]["name"] == "notes.txt"


class TestSendChannelFile:
    """A channel file lands in the channel's own Files folder, not the sender's drive."""

    def _mock_channel_upload(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/filesFolder").mock(
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

    @respx.mock
    async def test_a_failed_post_removes_the_file_from_the_channel_drive(self):
        """Channel files live in the team drive, so cleanup must address that drive."""
        self._mock_channel_upload()
        respx.post(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/messages").mock(
            return_value=httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        )
        removed = respx.delete(f"{GRAPH_BASE_URL}/drives/drive-001/items/teams-upload-001").mock(
            return_value=httpx.Response(204)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError, match="503"):
                await teams.asend_message_with_files(
                    client,
                    content="deck",
                    files=[NOTES],
                    team_id="team-001",
                    channel_id="channel-001",
                )

        assert removed.call_count == 1

    @respx.mock
    async def test_uploads_under_the_channel_drive_and_posts_to_the_channel(self):
        self._mock_channel_upload()
        post = respx.post(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client,
                content="deck",
                files=[NOTES],
                team_id="team-001",
                channel_id="channel-001",
            )

        assert _call_trail() == [
            ("GET", "/v1.0/teams/team-001/channels/channel-001/filesFolder"),
            ("PUT", "/v1.0/drives/drive-team-001/items/folder-channel-001:/notes.txt:/content"),
            ("GET", "/v1.0/drives/drive-team-001/items/teams-upload-001"),
            ("POST", "/v1.0/teams/team-001/channels/channel-001/messages"),
        ]
        payload = json.loads(post.calls[0].request.content)
        assert payload["attachments"][0]["contentUrl"] == TEAMS_WEBDAV_URL

    @respx.mock
    def test_sync_twin_uploads_under_the_channel_drive(self):
        self._mock_channel_upload()
        respx.post(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/messages").mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        with GraphClient("tok") as client:
            teams.send_message_with_files(
                client,
                content="deck",
                files=[NOTES],
                team_id="team-001",
                channel_id="channel-001",
            )

        assert [m for m, _ in _call_trail()] == ["GET", "PUT", "GET", "POST"]

    @respx.mock
    async def test_a_403_on_the_files_folder_means_no_teams(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/filesFolder").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(TeamsNotAvailableError):
                await teams.asend_message_with_files(
                    client,
                    content="deck",
                    files=[NOTES],
                    team_id="team-001",
                    channel_id="channel-001",
                )

    @respx.mock
    async def test_a_files_folder_without_a_drive_fails_loudly(self):
        respx.get(f"{GRAPH_BASE_URL}/teams/team-001/channels/channel-001/filesFolder").mock(
            return_value=httpx.Response(200, json={"id": "folder-1"})
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError, match="no drive"):
                await teams.asend_message_with_files(
                    client,
                    content="deck",
                    files=[NOTES],
                    team_id="team-001",
                    channel_id="channel-001",
                )


class TestSendInlineImages:
    """Pictures need no drive at all — the bytes travel with the message."""

    @respx.mock
    async def test_image_only_send_uploads_nothing(self):
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client, content="look", images=[PNG], chat_id="chat-1on1-001"
            )

        assert [m for m, _ in _call_trail()] == ["POST"]
        payload = json.loads(post.calls[0].request.content)
        assert payload["hostedContents"][0]["@microsoft.graph.temporaryId"] == "1"
        assert payload["hostedContents"][0]["contentType"] == "image/png"
        assert '<img src="../hostedContents/1/$value">' in payload["body"]["content"]
        assert "attachments" not in payload

    @respx.mock
    async def test_a_bad_image_is_rejected_before_any_upload(self):
        put = respx.put(url__startswith=CHAT_UPLOAD_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_TEAMS_UPLOAD_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="not an image"):
                await teams.asend_message_with_files(
                    client,
                    content="look",
                    files=[NOTES],
                    images=[NOTES],
                    chat_id="chat-1on1-001",
                )

        assert not put.called

    @respx.mock
    async def test_a_bad_target_costs_nothing(self):
        route = respx.route().mock(return_value=httpx.Response(200, json={}))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="chat_id"):
                await teams.asend_message_with_files(client, content="hi", files=[NOTES])

        assert not route.called

    @respx.mock
    async def test_mentions_survive_alongside_a_picture(self):
        post = respx.post(CHAT_MESSAGES_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CHAT_MESSAGE_SENT)
        )
        mentions = [{"id": 0, "mentionText": "Alice", "mentioned": {"user": {"id": "u1"}}}]
        async with AsyncGraphClient("tok") as client:
            await teams.asend_message_with_files(
                client,
                content="<at>Alice</at> look",
                content_type="html",
                mentions=mentions,
                images=[PNG],
                chat_id="chat-1on1-001",
            )

        assert json.loads(post.calls[0].request.content)["mentions"] == mentions
