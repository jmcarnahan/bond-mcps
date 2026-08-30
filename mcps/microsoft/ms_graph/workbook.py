"""Async wrappers over the Microsoft Graph Excel Workbook API.

Unlike Word (which has no granular editing endpoint, so ``document_edit`` must
download / modify with python-docx / re-upload), Excel workbooks expose a full
REST API that mutates cells, ranges, rows, and columns **server-side, in place**.
The file is never downloaded or re-uploaded — Excel's own engine applies each
change, so formulas recalculate and charts / pivot tables / formatting / macros
are preserved.

All endpoints are rooted at ``{base}/items/{item_id}/workbook`` where ``base``
comes from ``files._drive_base(site_id or None)`` (same drive/site routing as
every other file operation).

Ranges are addressed in A1 notation (e.g. ``"A1"``, ``"A1:C10"``, ``"3:3"`` for
a whole row, ``"B:B"`` for a whole column). Sheet names containing a single
quote must have it doubled, per OData literal escaping.
"""

from __future__ import annotations

from typing import Any

from .graph_client import AsyncGraphClient


def _workbook_base(base: str, item_id: str) -> str:
    return f"{base}/items/{item_id}/workbook"


def _session_headers(session_id: str | None) -> dict[str, str] | None:
    """Header dict carrying the workbook session id, or None if session-less."""
    if session_id:
        return {"workbook-session-id": session_id}
    return None


def _escape_sheet(name: str) -> str:
    """Escape a worksheet name for use in an OData string literal."""
    return name.replace("'", "''")


async def acreate_session(client: AsyncGraphClient, base: str, item_id: str) -> str | None:
    """Open a persistent workbook session. Returns the session id.

    A persistent session (``persistChanges: true``) keeps multiple sequential
    edits consistent and is faster than reopening the workbook per request.
    Returns ``None`` if the session cannot be created; callers then fall back to
    session-less mode where each request auto-commits.
    """
    result = await client.post(
        f"{_workbook_base(base, item_id)}/createSession",
        json_data={"persistChanges": True},
    )
    if result:
        return result.get("id")
    return None


async def aclose_session(
    client: AsyncGraphClient, base: str, item_id: str, session_id: str
) -> None:
    """Close a workbook session (best-effort; callers ignore failures)."""
    await client.post(
        f"{_workbook_base(base, item_id)}/closeSession",
        headers=_session_headers(session_id),
    )


async def alist_worksheets(
    client: AsyncGraphClient, base: str, item_id: str, session_id: str | None = None
) -> list[dict[str, Any]]:
    """List worksheets in the workbook (name, position, visibility, id)."""
    result = await client.get(
        f"{_workbook_base(base, item_id)}/worksheets",
        headers=_session_headers(session_id),
    )
    return result.get("value", [])


async def aget_used_range(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Get the used range of a worksheet (address, rowCount, columnCount, values)."""
    path = f"{_workbook_base(base, item_id)}/worksheets('{_escape_sheet(sheet)}')/usedRange"
    return await client.get(
        path,
        params={"$select": "address,rowCount,columnCount,values"},
        headers=_session_headers(session_id),
    )


async def aset_range(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    address: str,
    values: list[list[Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write a 2-D array of values into ``address`` on ``sheet``.

    Cell strings that start with ``=`` are written as formulas and recalculated
    by Excel server-side.
    """
    path = (
        f"{_workbook_base(base, item_id)}/worksheets('{_escape_sheet(sheet)}')"
        f"/range(address='{address}')"
    )
    return await client.patch(
        path,
        json_data={"values": values},
        headers=_session_headers(session_id),
    )


async def ainsert_range(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    address: str,
    shift: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Insert cells at ``address``, shifting existing cells ``"Down"`` or ``"Right"``."""
    path = (
        f"{_workbook_base(base, item_id)}/worksheets('{_escape_sheet(sheet)}')"
        f"/range(address='{address}')/insert"
    )
    return await client.post(
        path,
        json_data={"shift": shift},
        headers=_session_headers(session_id),
    )


async def adelete_range(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    address: str,
    shift: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Delete cells at ``address``, shifting remaining cells ``"Up"`` or ``"Left"``."""
    path = (
        f"{_workbook_base(base, item_id)}/worksheets('{_escape_sheet(sheet)}')"
        f"/range(address='{address}')/delete"
    )
    return await client.post(
        path,
        json_data={"shift": shift},
        headers=_session_headers(session_id),
    )
