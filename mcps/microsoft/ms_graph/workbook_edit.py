"""Excel workbook editing orchestration.

Coordinates applying edit operations to an ``.xlsx`` workbook stored in
OneDrive / SharePoint via the Microsoft Graph Workbook API. Operations are
expressed as a JSON array of operation dicts, mirroring the shape of
``document_edit`` for Word.

Every edit is applied server-side, in place — there is no download or
re-upload, so formulas recalculate and existing charts / pivot tables /
formatting / macros are preserved.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import workbook
from .graph_client import AsyncGraphClient, GraphError

# Required fields per op. Optional fields (sheet, count, values) are validated
# separately where they need type checks.
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "set_cell": ["cell", "value"],
    "set_range": ["range", "values"],
    "add_column": ["header"],
    "insert_rows": ["at"],
    "delete_rows": ["at"],
    "insert_columns": ["at"],
    "delete_columns": ["at"],
}


class EditError(Exception):
    """Raised when an edit operation fails."""

    def __init__(self, op_index: int, op_type: str, message: str):
        self.op_index = op_index
        self.op_type = op_type
        super().__init__(f"Edit #{op_index} ({op_type}): {message}")


def parse_workbook_edits(edits_json: str) -> list[dict[str, Any]]:
    """Parse and validate the edits JSON string.

    Raises ValueError with a human-readable message on any structural problem.
    """
    try:
        data = json.loads(edits_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    if not isinstance(data, list):
        raise ValueError("Edits must be a JSON array")

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Edit #{i}: must be an object")
        op = item.get("op")
        if not op:
            raise ValueError(f"Edit #{i}: missing 'op' field")
        if op not in _REQUIRED_FIELDS:
            raise ValueError(
                f"Edit #{i}: unknown op '{op}'. Valid: {', '.join(_REQUIRED_FIELDS.keys())}"
            )
        for field in _REQUIRED_FIELDS[op]:
            if field not in item:
                raise ValueError(f"Edit #{i} ({op}): missing '{field}'")

        # Type / shape checks for the non-string fields.
        if op == "set_range" and not _is_2d_list(item["values"]):
            raise ValueError(f"Edit #{i} (set_range): 'values' must be a 2-D array")
        if op == "add_column" and "values" in item and not isinstance(item["values"], list):
            raise ValueError(f"Edit #{i} (add_column): 'values' must be an array")
        if op in ("insert_rows", "delete_rows", "insert_columns", "delete_columns"):
            _validate_position(item, i, op)

    return data


def _is_2d_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(row, list) for row in value)


def _validate_position(item: dict[str, Any], index: int, op: str) -> None:
    at = item["at"]
    if not isinstance(at, int) or at < 1:
        raise ValueError(f"Edit #{index} ({op}): 'at' must be a positive integer (1-based)")
    count = item.get("count", 1)
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"Edit #{index} ({op}): 'count' must be a positive integer")


# ---------------------------------------------------------------------------
# A1-notation helpers
# ---------------------------------------------------------------------------

_ADDR_RE = re.compile(r"([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?$")


def _col_to_index(letters: str) -> int:
    """'A' -> 1, 'B' -> 2, 'Z' -> 26, 'AA' -> 27."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def _index_to_col(idx: int) -> str:
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA'."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _parse_used_range(used: dict[str, Any]) -> dict[str, Any]:
    """Parse a usedRange payload for add_column placement.

    Returns a dict with keys: empty, end_col, start_row — the last used column
    (so the new column goes at end_col + 1) and the top row (where the header
    goes). An empty sheet is reported as empty=True.
    """
    address = used.get("address", "")
    # Address may be "Sheet1!A1:C10" or "A1:C10"; strip the sheet prefix.
    if "!" in address:
        address = address.split("!", 1)[1]
    m = _ADDR_RE.match(address)
    if not m:
        return {"empty": True, "end_col": 1, "start_row": 1}

    start_col = _col_to_index(m.group(1))
    start_row = int(m.group(2))
    end_col = _col_to_index(m.group(3)) if m.group(3) else start_col
    end_row = int(m.group(4)) if m.group(4) else start_row

    # A brand-new sheet reports usedRange as a single cell whose only value is
    # empty. Treat that as empty so add_column starts at column A / row 1.
    values = used.get("values") or [[None]]
    is_empty = (
        start_col == end_col
        and start_row == end_row
        and (not values or all(cell in (None, "") for row in values for cell in row))
    )
    return {"empty": is_empty, "end_col": end_col, "start_row": start_row}


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def apply_workbook_edits(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply edit operations to a workbook in place via the Graph Workbook API.

    Opens a persistent session (falling back to session-less mode if that
    fails), resolves the default worksheet, dispatches each op in order, and
    always closes the session. Returns a summary dict with the applied op
    types, the default sheet name, and the worksheet names.

    Raises EditError (carrying the failing op index) if an operation fails.
    """
    if not operations:
        return {"operations": [], "default_sheet": "", "worksheets": []}

    session_id = None
    try:
        try:
            session_id = await workbook.acreate_session(client, base, item_id)
        except GraphError:
            session_id = None  # session-less fallback; each request auto-commits

        worksheets = await workbook.alist_worksheets(client, base, item_id, session_id)
        if not worksheets:
            raise EditError(0, operations[0]["op"], "Workbook has no worksheets")
        sheet_names = [ws.get("name", "") for ws in worksheets]
        default_sheet = sheet_names[0]

        summary: list[str] = []
        for i, op in enumerate(operations):
            op_type = op["op"]
            sheet = op.get("sheet") or default_sheet
            if op.get("sheet") and op["sheet"] not in sheet_names:
                raise EditError(
                    i, op_type, f"Worksheet '{op['sheet']}' not found. Available: {sheet_names}"
                )
            try:
                await _dispatch(client, base, item_id, sheet, op, session_id)
            except EditError:
                raise
            except GraphError as e:
                raise EditError(i, op_type, e.args[0]) from e
            except Exception as e:  # noqa: BLE001 - surface any op failure with its index
                raise EditError(i, op_type, str(e)) from e
            summary.append(op_type)

        return {
            "operations": summary,
            "default_sheet": default_sheet,
            "worksheets": sheet_names,
        }
    finally:
        if session_id:
            try:
                await workbook.aclose_session(client, base, item_id, session_id)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


async def _dispatch(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    op: dict[str, Any],
    session_id: str | None,
) -> None:
    op_type = op["op"]
    if op_type == "set_cell":
        await workbook.aset_range(
            client, base, item_id, sheet, op["cell"], [[op["value"]]], session_id
        )
    elif op_type == "set_range":
        await workbook.aset_range(
            client, base, item_id, sheet, op["range"], op["values"], session_id
        )
    elif op_type == "add_column":
        await _apply_add_column(client, base, item_id, sheet, op, session_id)
    elif op_type == "insert_rows":
        at, count = op["at"], op.get("count", 1)
        await workbook.ainsert_range(
            client, base, item_id, sheet, f"{at}:{at + count - 1}", "Down", session_id
        )
    elif op_type == "delete_rows":
        at, count = op["at"], op.get("count", 1)
        await workbook.adelete_range(
            client, base, item_id, sheet, f"{at}:{at + count - 1}", "Up", session_id
        )
    elif op_type == "insert_columns":
        start, count = op["at"], op.get("count", 1)
        addr = f"{_index_to_col(start)}:{_index_to_col(start + count - 1)}"
        await workbook.ainsert_range(client, base, item_id, sheet, addr, "Right", session_id)
    elif op_type == "delete_columns":
        start, count = op["at"], op.get("count", 1)
        addr = f"{_index_to_col(start)}:{_index_to_col(start + count - 1)}"
        await workbook.adelete_range(client, base, item_id, sheet, addr, "Left", session_id)


async def _apply_add_column(
    client: AsyncGraphClient,
    base: str,
    item_id: str,
    sheet: str,
    op: dict[str, Any],
    session_id: str | None,
) -> None:
    """Append a new column immediately to the right of the current used range.

    Placement: the new column is (used range's last column + 1). The header is
    written at the used range's *top* row, and values fill the cells directly
    beneath it. So for a used range B5:D10, the column lands at E, header at E5,
    values at E6, E7, ... — one value per row. On an empty sheet the column
    starts at A1.

    Note: values are placed contiguously below the header regardless of how
    many data rows the existing range has; the caller is responsible for
    passing one value per existing data row if row alignment matters.
    """
    used = await workbook.aget_used_range(client, base, item_id, sheet, session_id)
    parsed = _parse_used_range(used)

    if parsed["empty"]:
        col_index = 1
        header_row = 1
    else:
        col_index = parsed["end_col"] + 1
        header_row = parsed["start_row"]

    col = _index_to_col(col_index)
    header = op["header"]
    values: list[Any] = op.get("values", [])

    # Write header + values as one vertical range in a single PATCH.
    column_values: list[list[Any]] = [[header]] + [[v] for v in values]
    end_row = header_row + len(column_values) - 1
    address = f"{col}{header_row}:{col}{end_row}"
    await workbook.aset_range(client, base, item_id, sheet, address, column_values, session_id)
