"""Shared utility for parsing JSON options strings in MCP tools."""

import json


def parse_options(options: str) -> tuple[dict, str | None]:
    """Parse a JSON options string into a dict.

    Args:
        options: A JSON string (or empty string for no options).

    Returns:
        A tuple of (parsed_dict, error_message).
        If parsing succeeds: (dict, None)
        If options is empty/blank: ({}, None)
        If parsing fails: ({}, "Parameter 'options' must be valid JSON. ...")
        If not a dict: ({}, "Parameter 'options' must be a JSON object {...}.")
    """
    if not options or not options.strip():
        return {}, None

    try:
        parsed = json.loads(options)
    except (json.JSONDecodeError, TypeError):
        return {}, 'Parameter \'options\' must be valid JSON, e.g. {"key": "value"}.'

    if not isinstance(parsed, dict):
        return {}, "Parameter 'options' must be a JSON object {...}, not an array or primitive."

    return parsed, None


def opt_int(value, default: int) -> int:
    """Coerce an options value to int, falling back to default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def opt_bool(value, default: bool) -> bool:
    """Coerce an options value to bool, handling string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return default


def opt_str(value) -> str | None:
    """Coerce an options value to a stripped non-empty string, else None.

    Guards against callers passing non-string JSON values (e.g. a number) into
    code paths that later percent-encode or format the value as a string.
    """
    if value is None:
        return None
    s = str(value).strip()
    return s or None
