"""Shared FastMCP response-format middleware for Bond MCP servers."""

from bond_common.middleware import (
    DEPRECATED_ALIAS_TAG,
    DESKTOP_HEADER,
    DESKTOP_HEADER_VALUE,
    FormatNegotiation,
    HideDeprecatedAliases,
)
from bond_common.render import render_compact

__all__ = [
    "DEPRECATED_ALIAS_TAG",
    "DESKTOP_HEADER",
    "DESKTOP_HEADER_VALUE",
    "FormatNegotiation",
    "HideDeprecatedAliases",
    "render_compact",
]
