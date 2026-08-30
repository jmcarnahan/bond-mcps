"""Tests for Teams operations (sync and async)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from ms_graph import teams
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph.teams import (
    TeamsNotAvailableError,
    extract_message_sender,
    extract_message_text,
)

from .conftest import (
    GRAPH_ERROR_403,
    SAMPLE_CHANNEL_MESSAGE,
    SAMPLE_CHANNEL_MESSAGE_BOT,
    SAMPLE_CHANNEL_MESSAGE_USER,
    SAMPLE_CHANNEL_MESSAGES_RESPONSE,
    SAMPLE_CHANNELS_RESPONSE,
    SAMPLE_CHAT_MEMBERS_RESPONSE,
    SAMPLE_CHAT_MESSAGE_SENT,
    SAMPLE_CHAT_MESSAGES_PAGE,
    SAMPLE_CHAT_MESSAGES_RESPONSE,
    SAMPLE_CHATS_PAGE,
    SAMPLE_CHATS_PAGE_NEXT_LINK,
    SAMPLE_CHATS_RESPONSE,
    SAMPLE_TEAMS_RESPONSE,
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
        respx.get(f"{GRAPH_BASE_URL}/teams/t1/channels/c1/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHANNEL_MESSAGES_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await teams.alist_channel_messages(client, "t1", "c1")

        assert len(result) == 2

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
            result = await teams.alist_chat_messages(client, "chat-1")

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
    def test_requests_one_page_of_fifty(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
        )
        with GraphClient("tok") as client:
            data = teams.chat_members(client, "chat-1on1-001")

        assert data == SAMPLE_CHAT_MEMBERS_RESPONSE
        assert str(route.calls[0].request.url).endswith("/chats/chat-1on1-001/members?$top=50")

    @respx.mock
    async def test_achat_members_encodes_chat_id(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(
            return_value=httpx.Response(200, json=SAMPLE_CHAT_MEMBERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            data = await teams.achat_members(client, "19:a/b+c@thread.v2")

        assert data == SAMPLE_CHAT_MEMBERS_RESPONSE
        assert "/chats/19%3Aa%2Fb%2Bc%40thread.v2/members" in str(route.calls[0].request.url)


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
