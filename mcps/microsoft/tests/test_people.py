"""Tests for ms_graph.people — directory search."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from ms_graph import people
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph.people import (
    DIRECTORY_SELECT,
    MAX_DIRECTORY_TOP,
    DirectoryScopeMissingError,
    _search_clause,
    _search_path,
)

from .conftest import GRAPH_ERROR_403, SAMPLE_USERS_SEARCH_RESPONSE

USERS_PREFIX = f"{GRAPH_BASE_URL}/users?"
GRAPH_ERROR_429 = {"error": {"code": "TooManyRequests", "message": "throttled"}}


def _query_of(request) -> dict:
    """The decoded query string of a request respx captured."""
    return parse_qs(urlparse(str(request.url)).query)


class TestSearchClause:
    def test_a_plain_value_is_unchanged(self):
        assert _search_clause("smith") == "smith"

    def test_a_double_quote_is_escaped(self):
        assert _search_clause('a"b') == 'a\\"b'

    def test_a_backslash_is_escaped(self):
        assert _search_clause("a\\b") == "a\\\\b"

    def test_a_backslash_is_escaped_before_a_quote_is_added(self):
        """The backslash pass runs first, so the escape it adds is not re-escaped."""
        assert _search_clause('a\\"b') == 'a\\\\\\"b'

    def test_an_ampersand_is_dropped(self):
        assert _search_clause("a & b") == "a  b"

    def test_unicode_is_untouched(self):
        assert _search_clause("Zoë Müller") == "Zoë Müller"


class TestSearchPath:
    def test_the_search_clause_covers_name_and_mail(self):
        query = parse_qs(urlparse(_search_path("smi", 10)).query)

        assert query["$search"] == ['"displayName:smi" OR "mail:smi"']

    def test_the_fixed_query_options(self):
        query = parse_qs(urlparse(_search_path("smi", 10)).query)

        assert query["$select"] == [DIRECTORY_SELECT]
        assert query["$count"] == ["true"]
        assert query["$orderby"] == ["displayName"]

    def test_top_is_passed_through(self):
        assert parse_qs(urlparse(_search_path("smi", 10)).query)["$top"] == ["10"]

    def test_top_below_one_is_clamped_up(self):
        assert parse_qs(urlparse(_search_path("smi", 0)).query)["$top"] == ["1"]

    def test_top_above_the_max_is_clamped_down(self):
        query = parse_qs(urlparse(_search_path("smi", 500)).query)

        assert query["$top"] == [str(MAX_DIRECTORY_TOP)]

    def test_a_space_is_percent_encoded_never_a_plus(self):
        """Graph's OData parser reads + literally, so it must not appear."""
        path = _search_path("ann lee", 10)

        assert "+" not in path
        assert "%20" in path

    def test_the_path_is_the_users_collection(self):
        assert _search_path("smi", 10).startswith("/users?")


class TestSearchUsers:
    @respx.mock
    def test_returns_the_directory_rows(self):
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with GraphClient("tok") as client:
            result = people.search_users(client, "smi")

        assert result == SAMPLE_USERS_SEARCH_RESPONSE["value"]

    @respx.mock
    def test_sends_the_advanced_query_header_and_the_clause(self):
        route = respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with GraphClient("tok") as client:
            people.search_users(client, "smi")

        request = route.calls[0].request
        assert request.headers["ConsistencyLevel"] == "eventual"
        assert _query_of(request)["$search"] == ['"displayName:smi" OR "mail:smi"']

    @respx.mock
    def test_403_is_the_missing_directory_scope(self):
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                people.search_users(client, "smi")

    @respx.mock
    def test_nothing_searchable_makes_no_request(self):
        """An "&" alone escapes to an empty clause, which Graph would 400."""
        route = respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        with GraphClient("tok") as client:
            assert people.search_users(client, "&") == []

        assert not route.called


class TestAsearchUsers:
    @respx.mock
    async def test_returns_the_directory_rows(self):
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await people.asearch_users(client, "smi")

        assert result == SAMPLE_USERS_SEARCH_RESPONSE["value"]

    @respx.mock
    async def test_sends_the_advanced_query_header_and_the_clause(self):
        route = respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await people.asearch_users(client, "ann lee")

        request = route.calls[0].request
        assert request.headers["ConsistencyLevel"] == "eventual"
        assert _query_of(request)["$search"] == ['"displayName:ann lee" OR "mail:ann lee"']

    @respx.mock
    async def test_top_reaches_the_request(self):
        route = respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await people.asearch_users(client, "smi", top=500)

        assert _query_of(route.calls[0].request)["$top"] == [str(MAX_DIRECTORY_TOP)]

    @respx.mock
    async def test_403_is_the_missing_directory_scope(self):
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                await people.asearch_users(client, "smi")

    @respx.mock
    async def test_429_propagates_as_a_graph_error(self):
        """Throttling is transient; only a 403 means the scope is missing."""
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(429, json=GRAPH_ERROR_429)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await people.asearch_users(client, "smi")

        assert exc_info.value.status_code == 429

    @respx.mock
    async def test_nothing_searchable_makes_no_request(self):
        route = respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json=SAMPLE_USERS_SEARCH_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            assert await people.asearch_users(client, " & ") == []

        assert not route.called

    @respx.mock
    async def test_a_response_without_value_is_an_empty_list(self):
        respx.get(url__startswith=USERS_PREFIX).mock(
            return_value=httpx.Response(200, json={"@odata.count": 0})
        )
        async with AsyncGraphClient("tok") as client:
            result = await people.asearch_users(client, "smi")

        assert result == []
