"""FastMCP middleware selecting a response format per caller.

Bond MCP tools return one canonical dict. bond-desktop parses that dict and
asks for it by sending ``X-Bond-Client: desktop``; every other caller — the
models — gets the compact rendering from `render_compact` instead.

The compact path deliberately returns content ONLY, with no
``structured_content``: Claude Code forwards just ``structuredContent`` to the
model whenever both channels are present, so leaving it set would send the
verbose JSON and hide the compact text.

Declaring ``output_schema=None`` on a tool is therefore what opts it into
compact rendering. It is not merely spec hygiene (the MCP spec requires
structured results whenever a tool advertises an output schema, and clients
enforce it): in fastmcp 3.3.1 EVERY tool produces structured content — a
``-> str`` tool is auto-wrapped as ``{"result": ...}`` behind a generated
schema — so the presence of structured content cannot tell the two surfaces
apart. The advertised schema can.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import mcp.types as mt
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

from bond_common.render import render_compact

# Header a programmatic caller sends to opt into the canonical JSON contract.
DESKTOP_HEADER = "x-bond-client"
DESKTOP_HEADER_VALUE = "desktop"

# Tag marking a tool as a back-compat forwarder for a renamed tool: still
# callable, hidden from discovery by HideDeprecatedAliases.
DEPRECATED_ALIAS_TAG = "deprecated-alias"


class FormatNegotiation(Middleware):
    """Render dict-returning tool results compactly unless the caller opts out."""

    def __init__(self, overrides: dict[str, Callable[[dict], str]] | None = None):
        self.overrides = overrides

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)

        if result.structured_content is None:
            return result

        if not await _opts_into_compact(context):
            return result

        # Outside an HTTP request (in-process clients, tests) fastmcp returns
        # an empty mapping, which lands on the compact path.
        if get_http_headers().get(DESKTOP_HEADER) == DESKTOP_HEADER_VALUE:
            return result

        return ToolResult(
            content=render_compact(result.structured_content, context.message.name, self.overrides),
            meta=result.meta,
        )


class HideDeprecatedAliases(Middleware):
    """Drop deprecated-alias tools from discovery while keeping them callable."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [tool for tool in tools if DEPRECATED_ALIAS_TAG not in _tags(tool)]


def _tags(tool: Any) -> set[str]:
    return getattr(tool, "tags", None) or set()


async def _opts_into_compact(context: MiddlewareContext[mt.CallToolRequestParams]) -> bool:
    """Does the called tool declare `output_schema=None`?

    Anything we cannot resolve — no server context, a tool injected by another
    middleware — passes through: rendering compactly against an advertised
    schema would produce a result the client rejects.
    """
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        return False
    tool = await fastmcp_context.fastmcp.get_tool(context.message.name)
    return tool is not None and tool.output_schema is None
