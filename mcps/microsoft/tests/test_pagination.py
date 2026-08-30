"""Tests for the shared pagination utility."""

import httpx
import respx
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient
from ms_graph.pagination import _MAX_PAGES, apaginate, apaginate_until_date


@respx.mock
async def test_single_page_no_next_link():
    """Stops after one request when no @odata.nextLink present."""
    respx.get(f"{GRAPH_BASE_URL}/me/items").mock(
        return_value=httpx.Response(200, json={"value": [{"id": i} for i in range(10)]})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate(client, "/me/items", {}, max_results=50, page_size=50)

    assert len(results) == 10
    assert respx.calls.call_count == 1


@respx.mock
async def test_follows_next_link():
    """Follows @odata.nextLink to fetch multiple pages."""
    page1 = {
        "value": [{"id": i} for i in range(50)],
        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/items?$skip=50",
    }
    page2 = {"value": [{"id": i} for i in range(50, 80)]}

    responses = iter(
        [
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    respx.get(f"{GRAPH_BASE_URL}/me/items").mock(side_effect=lambda req: next(responses))

    async with AsyncGraphClient("tok") as client:
        results = await apaginate(client, "/me/items", {}, max_results=100, page_size=50)

    assert len(results) == 80


@respx.mock
async def test_stops_at_max_results():
    """Truncates results to max_results even if more pages available."""
    page1 = {
        "value": [{"id": i} for i in range(50)],
        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/items?$skip=50",
    }
    page2 = {
        "value": [{"id": i} for i in range(50, 100)],
        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/items?$skip=100",
    }

    responses = iter(
        [
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    respx.get(f"{GRAPH_BASE_URL}/me/items").mock(side_effect=lambda req: next(responses))

    async with AsyncGraphClient("tok") as client:
        results = await apaginate(client, "/me/items", {}, max_results=75, page_size=50)

    assert len(results) == 75


@respx.mock
async def test_empty_first_page():
    """Returns empty list when first page has no results."""
    respx.get(f"{GRAPH_BASE_URL}/me/items").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate(client, "/me/items", {}, max_results=50, page_size=50)

    assert results == []


@respx.mock
async def test_preserves_params():
    """Passes caller params including $top on the first request."""
    route = respx.get(f"{GRAPH_BASE_URL}/me/items").mock(
        return_value=httpx.Response(200, json={"value": [{"id": 1}]})
    )
    async with AsyncGraphClient("tok") as client:
        await apaginate(client, "/me/items", {"$filter": "x eq 'y'"}, max_results=30, page_size=50)

    url = str(route.calls[0].request.url)
    assert "%24filter" in url or "$filter" in url
    assert "%24top=30" in url or "$top=30" in url


@respx.mock
async def test_stops_at_max_pages_safety_cap():
    """Exits after _MAX_PAGES even if nextLink keeps coming."""
    call_count = 0

    def _make_page(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "value": [{"id": call_count}],
                "@odata.nextLink": f"{GRAPH_BASE_URL}/me/items?$skip={call_count}",
            },
        )

    respx.get(f"{GRAPH_BASE_URL}/me/items").mock(side_effect=_make_page)

    async with AsyncGraphClient("tok") as client:
        results = await apaginate(client, "/me/items", {}, max_results=9999, page_size=50)

    assert call_count == _MAX_PAGES
    assert len(results) == _MAX_PAGES


# ---------------------------------------------------------------------------
# apaginate_until_date tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_date_pagination_single_page_all_within():
    """All items are newer than since — returns everything."""
    items = [
        {"id": 1, "createdDateTime": "2026-06-20T10:00:00Z"},
        {"id": 2, "createdDateTime": "2026-06-19T10:00:00Z"},
    ]
    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": items})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-18T00:00:00Z"
        )

    assert len(results) == 2


@respx.mock
async def test_date_pagination_filters_old_messages():
    """Items older than since are excluded from results."""
    items = [
        {"id": 1, "createdDateTime": "2026-06-20T10:00:00Z"},
        {"id": 2, "createdDateTime": "2026-06-15T10:00:00Z"},
    ]
    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": items})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-18T00:00:00Z"
        )

    assert len(results) == 1
    assert results[0]["id"] == 1


@respx.mock
async def test_date_pagination_follows_pages_until_cutoff():
    """Pages through results until an item older than since is found."""
    page1 = {
        "value": [
            {"id": 1, "createdDateTime": "2026-06-20T10:00:00Z"},
            {"id": 2, "createdDateTime": "2026-06-19T10:00:00Z"},
        ],
        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/messages?$skip=2",
    }
    page2 = {
        "value": [
            {"id": 3, "createdDateTime": "2026-06-18T10:00:00Z"},
            {"id": 4, "createdDateTime": "2026-06-10T10:00:00Z"},
        ],
    }

    responses = iter(
        [
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(side_effect=lambda req: next(responses))

    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-17T00:00:00Z"
        )

    assert len(results) == 3
    assert [r["id"] for r in results] == [1, 2, 3]


@respx.mock
async def test_date_pagination_stops_when_page_has_old_items():
    """Does not fetch more pages once an item older than since is found."""
    page1 = {
        "value": [
            {"id": 1, "createdDateTime": "2026-06-20T10:00:00Z"},
            {"id": 2, "createdDateTime": "2026-06-10T10:00:00Z"},
        ],
        "@odata.nextLink": f"{GRAPH_BASE_URL}/me/messages?$skip=2",
    }

    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(return_value=httpx.Response(200, json=page1))

    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-15T00:00:00Z"
        )

    assert len(results) == 1
    assert respx.calls.call_count == 1


@respx.mock
async def test_date_pagination_empty_response():
    """Returns empty list when no messages exist."""
    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-01T00:00:00Z"
        )

    assert results == []


@respx.mock
async def test_date_pagination_handles_millisecond_timestamps():
    """Correctly includes messages with millisecond timestamps at the boundary."""
    items = [
        {"id": 1, "createdDateTime": "2026-06-18T00:00:00.500Z"},
        {"id": 2, "createdDateTime": "2026-06-18T00:00:00.000Z"},
        {"id": 3, "createdDateTime": "2026-06-17T23:59:59.999Z"},
    ]
    respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": items})
    )
    async with AsyncGraphClient("tok") as client:
        results = await apaginate_until_date(
            client, "/me/messages", {}, since="2026-06-18T00:00:00Z"
        )

    assert len(results) == 2
    assert [r["id"] for r in results] == [1, 2]
