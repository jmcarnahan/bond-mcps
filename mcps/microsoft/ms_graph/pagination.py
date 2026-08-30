"""Shared pagination utilities for Microsoft Graph API."""

import re
from typing import Any

from .graph_client import AsyncGraphClient

_MAX_PAGES = 20
_MAX_MESSAGES_PAGES = 40

_ISO_SECOND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _normalize_ts(ts: str) -> str:
    """Normalize an ISO timestamp to second precision for safe string comparison.

    Strips milliseconds and timezone suffixes so '2026-06-18T00:00:00.500Z'
    becomes '2026-06-18T00:00:00' — comparable against any other normalized ts.
    """
    m = _ISO_SECOND_RE.match(ts)
    return m.group(1) if m else ts


async def apaginate(
    client: AsyncGraphClient,
    path: str,
    params: dict[str, Any],
    max_results: int,
    page_size: int,
    max_pages: int = _MAX_PAGES,
) -> list[dict[str, Any]]:
    """Fetch up to max_results items by following @odata.nextLink pages."""
    params = {**params, "$top": min(page_size, max_results)}
    results: list[dict[str, Any]] = []

    data = await client.get(path, params=params)
    results.extend(data.get("value", []))

    pages = 1
    while len(results) < max_results and pages < max_pages:
        next_link = data.get("@odata.nextLink")
        if not next_link:
            break
        data = await client.get(next_link)
        results.extend(data.get("value", []))
        pages += 1

    return results[:max_results]


async def apaginate_until_date(
    client: AsyncGraphClient,
    path: str,
    params: dict[str, Any],
    since: str,
    page_size: int = 50,
    date_field: str = "createdDateTime",
) -> list[dict[str, Any]]:
    """Fetch pages until all items are older than `since` (ISO 8601 string).

    Returns all items with date_field >= since, ordered newest-first.
    """
    since_norm = _normalize_ts(since)
    params = {**params, "$top": page_size}
    results: list[dict[str, Any]] = []

    data = await client.get(path, params=params)
    page_items = data.get("value", [])
    results.extend(page_items)

    pages = 1
    while pages < _MAX_MESSAGES_PAGES:
        if _page_reached_cutoff(page_items, since_norm, date_field):
            break
        next_link = data.get("@odata.nextLink")
        if not next_link:
            break
        data = await client.get(next_link)
        page_items = data.get("value", [])
        results.extend(page_items)
        pages += 1

    return [r for r in results if _normalize_ts(r.get(date_field, "")) >= since_norm]


def _page_reached_cutoff(items: list[dict[str, Any]], since_norm: str, date_field: str) -> bool:
    """True if any item on this page is older than the cutoff date."""
    for item in items:
        if _normalize_ts(item.get(date_field, "")) < since_norm:
            return True
    return False
