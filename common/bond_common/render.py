"""Compact text rendering of canonical tool-response dicts.

Bond MCP tools return one canonical dict per tool. Programmatic callers
(bond-desktop) get that dict verbatim; models get the compact rendering
produced here, which is roughly 3-5x cheaper in tokens than serialized JSON
and stays greppable.

`render_compact` applies these rules in order — they are the contract every
tool docstring documents:

1. **Per-tool override.** If `overrides` has an entry for `tool`, that
   callable renders the payload and nothing below applies.
2. **Error envelopes.** A payload containing an ``error`` key renders as
   ``key: value`` lines with ``error: <code>`` first, so failures stay tiny
   and greppable. Remaining keys follow in their original order.
3. **Tables.** A payload whose only list-valued key holds dicts with
   all-scalar values (and whose other keys are scalars) renders as pipe-CSV:
   a header row of the union of row keys in first-seen order, then one row
   per item, then each remaining non-empty scalar key as a trailing
   ``key: value`` line (``next_cursor``, counts, policy notices). An empty
   list renders as ``<key>: (none)`` plus those same trailing lines.
4. **Records.** An all-scalar payload renders as ``key: value`` lines.
5. **Anything nested** falls back to compact JSON.

Scalars are ``str``/``int``/``float``/``bool``/``None``. ``None`` renders as
an empty string (empty CSV cell, nothing after the colon); booleans render
lowercase ``true``/``false`` so models can grep them consistently. Values
containing ``|`` or newlines are quoted by the ``csv`` module.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from typing import Any

# Marker for a table key whose list came back empty. A bare "key:" line reads
# as a missing value; "(none)" reads as a deliberate empty result.
EMPTY_TABLE = "(none)"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_text(value: Any) -> str:
    """Render one value for a CSV cell or a `key: value` line."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    # Non-scalars only reach here from the error-envelope rule, where a stray
    # list (e.g. mark_mail_read's `failed`) must not derail the rendering.
    return _compact_json(value)


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)


def _kv_lines(payload: dict, keys: list[str]) -> list[str]:
    return [f"{key}: {_scalar_text(payload[key])}" for key in keys]


def _table_key(payload: dict) -> str | None:
    """Return the single list-valued key if the payload renders as a table.

    Qualifies when exactly one key holds a list, every other key is scalar,
    and every row is a dict of scalars. Ragged rows are fine — the header is
    the union of their keys and missing cells render empty.
    """
    list_keys = [key for key, value in payload.items() if isinstance(value, list)]
    if len(list_keys) != 1:
        return None
    table_key = list_keys[0]
    if not all(_is_scalar(value) for key, value in payload.items() if key != table_key):
        return None
    for row in payload[table_key]:
        if not isinstance(row, dict) or not all(_is_scalar(cell) for cell in row.values()):
            return None
    return table_key


def _render_table(payload: dict, table_key: str) -> str:
    rows: list[dict] = payload[table_key]
    # Trailing context lines (next_cursor, counts, policy notices). Empty
    # strings and nulls are dropped — they carry no information for a model.
    trailing = [
        f"{key}: {_scalar_text(value)}"
        for key, value in payload.items()
        if key != table_key and value is not None and value != ""
    ]

    if not rows:
        return "\n".join([f"{table_key}: {EMPTY_TABLE}", *trailing])

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    buffer = io.StringIO()
    # lineterminator must be "\n": the csv default "\r\n" would leak carriage
    # returns into every response.
    writer = csv.writer(buffer, delimiter="|", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_scalar_text(row.get(column)) for column in columns])

    return "\n".join([buffer.getvalue().rstrip("\n"), *trailing])


def render_compact(
    payload: dict,
    tool: str = "",
    overrides: dict[str, Callable[[dict], str]] | None = None,
) -> str:
    """Render a canonical tool-response dict as compact text for a model.

    See the module docstring for the ordered rules. `tool` selects a per-tool
    renderer from `overrides`; both are optional so the function stays usable
    standalone in tests.
    """
    if overrides and tool in overrides:
        return overrides[tool](payload)

    if not payload:
        # An empty text block would render as a blank tool result; "{}" at
        # least says the tool answered with an empty payload.
        return "{}"

    if "error" in payload:
        ordered = ["error", *(key for key in payload if key != "error")]
        return "\n".join(_kv_lines(payload, ordered))

    table_key = _table_key(payload)
    if table_key is not None:
        return _render_table(payload, table_key)

    if all(_is_scalar(value) for value in payload.values()):
        return "\n".join(_kv_lines(payload, list(payload)))

    return _compact_json(payload)
