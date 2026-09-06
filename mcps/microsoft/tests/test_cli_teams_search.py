"""CLI parity for Teams message search.

The CLI is its own caller boundary: it normalizes ``--since`` itself, maps
``--exact`` onto the tri-state the core takes, and formats the hits, the
skipped footnote and the truncation footnote as prose. These tests call
``cmd_teams_search`` directly with an argparse.Namespace and assert on both
stdout and the request trail — a search that silently dropped a hit, or one
that reached the network on a bad cutoff, would look identical otherwise.
"""

import argparse
import json
from unittest.mock import patch
from urllib.parse import quote

import httpx
import ms_graph_cli
import pytest
import respx
from ms_graph.graph_client import GRAPH_BASE_URL

from .conftest import (
    GRAPH_ERROR_404,
    SAMPLE_HYDRATED_CHANNEL_MESSAGE,
    SAMPLE_HYDRATED_CHAT_MESSAGE,
    SAMPLE_SEARCH_CHANNEL_HIT,
    SAMPLE_SEARCH_CHAT_HIT,
    SAMPLE_SEARCH_MESSAGES_EMPTY,
    SEARCH_CHANNEL_ID,
    SEARCH_CHAT_ID,
    SEARCH_TEAM_ID,
    search_response,
)

SEARCH_URL = f"{GRAPH_BASE_URL}/search/query"
NOT_SUPPORTED_400 = {
    "error": {"code": "BadRequest", "message": "This API is not supported for MSA accounts"}
}

CHAT_MESSAGE_ID = "1750000000001"
CHANNEL_MESSAGE_ID = "1750000000002"

# httpx decodes URL.path, so the trail carries the raw ids, not '%3A'/'%40'.
CHAT_HYDRATE_PATH = f"/v1.0/chats/{SEARCH_CHAT_ID}/messages/{CHAT_MESSAGE_ID}"
CHANNEL_HYDRATE_PATH = f"/v1.0/chats/{SEARCH_CHANNEL_ID}/messages/{CHANNEL_MESSAGE_ID}"

BOTH_BODIES = {
    CHAT_MESSAGE_ID: SAMPLE_HYDRATED_CHAT_MESSAGE,
    CHANNEL_MESSAGE_ID: SAMPLE_HYDRATED_CHANNEL_MESSAGE,
}


@pytest.fixture(autouse=True)
def _local_token():
    """The CLI reads its token from the local auth proxy; short-circuit it."""
    with patch("ms_graph_cli.get_local_token", return_value="tok"):
        yield


def _hit(msg_id, created="2026-03-02T10:00:00Z", chat_id=SEARCH_CHAT_ID):
    """A chat search hit for the given message id."""
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


def _msg(msg_id, content="The #budget2026 numbers are in", created="2026-03-02T10:00:00Z"):
    """A hydrated chat message for the given id."""
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": created,
        "from": {"user": {"displayName": "Alice Smith"}, "application": None},
        "body": {"contentType": "text", "content": content},
        "attachments": [],
    }


# The index strips '#', so 'budget20260' is returned for a '"budget2026"'
# search; only the client-side literal check can tell the two apart.
STEMMED_HIT = _hit("9001")
STEMMED_MESSAGE = _msg("9001", content="budget20260 is a different tag")


def _mock_search(*pages):
    """Serve POST /search/query from these response bodies, in order."""
    return respx.post(SEARCH_URL).mock(
        side_effect=[httpx.Response(200, json=page) for page in pages]
    )


def _mock_hydration(bodies):
    """Serve GET /chats/*/messages/<id> from a {message id: body} map.

    An id that is not in the map answers 404, which is how the "one deleted
    message is skipped" test is built.
    """

    def _handler(request):
        msg_id = str(request.url).rsplit("/", 1)[-1]
        body = bodies.get(msg_id)
        if body is None:
            return httpx.Response(404, json=GRAPH_ERROR_404)
        return httpx.Response(200, json=body)

    return respx.get(url__startswith=f"{GRAPH_BASE_URL}/chats/").mock(side_effect=_handler)


def _args(query, **overrides):
    base = dict(query=query, since="", conversation_id="", max_results=25, exact="auto")
    base.update(overrides)
    return argparse.Namespace(**base)


def _trail() -> list[tuple[str, str]]:
    """(method, path) for every request the command made, in order."""
    return [(call.request.method, call.request.url.path) for call in respx.calls]


def _query_strings() -> list[str]:
    """The queryString of every /search/query request, in order."""
    return [
        json.loads(call.request.content)["requests"][0]["query"]["queryString"]
        for call in respx.calls
        if call.request.url.path.endswith("/search/query")
    ]


class TestCliTeamsSearch:
    @respx.mock
    def test_prints_hits_with_conversation_and_link(self, capsys):
        route = _mock_search(search_response([SAMPLE_SEARCH_CHAT_HIT, SAMPLE_SEARCH_CHANNEL_HIT]))
        _mock_hydration(BOTH_BODIES)

        ms_graph_cli.cmd_teams_search(_args("#budget2026"))

        out = capsys.readouterr().out
        assert "Messages matching '#budget2026' (2):" in out
        assert "[1] " in out
        assert "[2] " in out
        assert "Alice Smith" in out
        assert "2026-03-02T10:00:00Z" in out
        assert f"[chat:{SEARCH_CHAT_ID}]" in out
        assert f"[channel:{SEARCH_TEAM_ID}/{SEARCH_CHANNEL_ID}]" in out
        assert "The #budget2026 numbers are in" in out
        assert "https://teams.microsoft.com/l/message/chat/1750000000001" in out

        assert json.loads(route.calls[0].request.content) == {
            "requests": [
                {
                    "entityTypes": ["chatMessage"],
                    "query": {"queryString": '"budget2026"'},
                    "from": 0,
                    "size": 25,
                }
            ]
        }
        assert _trail() == [
            ("POST", "/v1.0/search/query"),
            ("GET", CHAT_HYDRATE_PATH),
            ("GET", CHANNEL_HYDRATE_PATH),
        ]
        # The ids reach the wire percent-encoded even though the path decodes.
        assert quote(SEARCH_CHAT_ID, safe="") in str(respx.calls[1].request.url)

    @respx.mock
    def test_no_results(self, capsys):
        _mock_search(SAMPLE_SEARCH_MESSAGES_EMPTY)

        ms_graph_cli.cmd_teams_search(_args("#nothing"))

        assert capsys.readouterr().out == "No messages matching '#nothing'.\n"
        assert [m for m, _ in _trail()] == ["POST"]

    @respx.mock
    def test_invalid_since_exits_before_any_request(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_teams_search(_args("#x", since="yesterday"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Invalid since format: 'yesterday'. Use YYYY-MM-DD or ISO datetime." in err
        assert _trail() == []

    @respx.mock
    def test_blank_query_exits_before_any_request(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_teams_search(_args("   "))

        assert exc.value.code == 1
        assert "Error: provide a search query." in capsys.readouterr().err
        assert _trail() == []

    @respx.mock
    def test_exact_default_drops_stemmed_hit_and_says_so(self, capsys):
        _mock_search(search_response([STEMMED_HIT]))
        _mock_hydration({"9001": STEMMED_MESSAGE})

        ms_graph_cli.cmd_teams_search(_args("#budget2026"))

        out = capsys.readouterr().out
        assert "No messages matching '#budget2026'." in out
        assert (
            "The index matched 1 message(s) but none carried the hashtag literally; "
            "retry with --exact no to see them." in out
        )

    @respx.mock
    def test_exact_no_keeps_stemmed_hit(self, capsys):
        _mock_search(search_response([STEMMED_HIT]))
        _mock_hydration({"9001": STEMMED_MESSAGE})

        ms_graph_cli.cmd_teams_search(_args("#budget2026", exact="no"))

        out = capsys.readouterr().out
        assert "(1):" in out
        assert "budget20260 is a different tag" in out

    @respx.mock
    def test_conversation_id_hydrates_only_the_scoped_hit(self, capsys):
        _mock_search(search_response([SAMPLE_SEARCH_CHAT_HIT, SAMPLE_SEARCH_CHANNEL_HIT]))
        _mock_hydration(BOTH_BODIES)

        ms_graph_cli.cmd_teams_search(_args("budget2026", conversation_id=SEARCH_CHANNEL_ID))

        out = capsys.readouterr().out
        assert "(1):" in out
        assert "Bob Jones" in out
        gets = [path for method, path in _trail() if method == "GET"]
        assert gets == [CHANNEL_HYDRATE_PATH]
        assert SEARCH_CHANNEL_ID in gets[0]

    @respx.mock
    def test_consumer_account_400_exits_with_message(self, capsys):
        respx.post(SEARCH_URL).mock(return_value=httpx.Response(400, json=NOT_SUPPORTED_400))

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_teams_search(_args("#budget2026"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Error: Teams message search is only available on work or school accounts." in err
        assert [m for m, _ in _trail()] == ["POST"]

    @respx.mock
    def test_skipped_footer(self, capsys):
        _mock_search(search_response([SAMPLE_SEARCH_CHAT_HIT, _hit("1750000000009")]))
        _mock_hydration({CHAT_MESSAGE_ID: SAMPLE_HYDRATED_CHAT_MESSAGE})

        ms_graph_cli.cmd_teams_search(_args("#budget2026"))

        out = capsys.readouterr().out
        assert "(1):" in out
        assert "The #budget2026 numbers are in" in out
        assert (
            "1 matching message(s) could not be read "
            "(deleted, or no longer shared with you) and were skipped." in out
        )

    @respx.mock
    def test_max_results_truncation_footer(self, capsys):
        _mock_search(search_response([_hit("m1"), _hit("m2"), _hit("m3")]))
        _mock_hydration({mid: _msg(mid) for mid in ("m1", "m2", "m3")})

        ms_graph_cli.cmd_teams_search(_args("#budget2026", max_results=1))

        out = capsys.readouterr().out
        assert "(1):" in out
        assert (
            "More results may exist. Narrow the search with --since or "
            "--conversation-id, or raise --max-results." in out
        )

    @respx.mock
    def test_since_date_reaches_the_query_string(self, capsys):
        _mock_search(SAMPLE_SEARCH_MESSAGES_EMPTY)

        ms_graph_cli.cmd_teams_search(_args("#budget2026", since="2026-01-01"))

        assert _query_strings() == ['"budget2026" sent>=2026-01-01']
