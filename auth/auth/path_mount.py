"""Path-prefix mounting for deployed environments with path-based routing.

When BOND_MCPS_ROOT_PATH is set (e.g., "/ms-graph"), wraps a FastMCP
instance in a Starlette Mount so the service handles requests at the
prefixed path (e.g., /ms-graph/mcp, /ms-graph/healthz). When not set,
returns the app unchanged (local dev behavior).

Also rewrites well-known discovery paths so RFC 9728/8414 clients find
them regardless of whether they use the mounted or root-level path.
"""

from __future__ import annotations

import os


def mount_app(mcp, *, transport: str = "streamable-http"):
    """Return an ASGI app, optionally mounted under BOND_MCPS_ROOT_PATH."""
    root_path = (os.environ.get("BOND_MCPS_ROOT_PATH") or "").strip().rstrip("/")
    inner_app = mcp.http_app(transport=transport)

    if not root_path:
        return inner_app

    from starlette.applications import Starlette
    from starlette.routing import Mount

    audience = os.environ.get("BOND_MCPS_JWT_AUDIENCE", root_path.strip("/"))

    root_well_known = f"/.well-known/oauth-protected-resource/{audience}/mcp"
    mounted_well_known = f"{root_path}/.well-known/oauth-protected-resource/mcp"
    internal_well_known = f"{root_path}/.well-known/oauth-protected-resource/{audience}/mcp"

    lifespan = getattr(inner_app, "lifespan", None)
    outer_starlette = Starlette(
        routes=[Mount(root_path, app=inner_app)],
        lifespan=lifespan,
    )

    async def _rewriting_app(scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == root_well_known or path == mounted_well_known:
                scope = dict(scope)
                scope["path"] = internal_well_known
        await outer_starlette(scope, receive, send)

    return _rewriting_app
