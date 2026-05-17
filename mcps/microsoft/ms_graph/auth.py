"""
Token resolution for MCP server.

Path 1: Bearer header (Bond AI backend) -- always preferred
Path 2: Local MSAL auth (Claude Code / standalone) -- if MS_CLIENT_ID set
Path 3: PermissionError
"""

import os


def get_graph_token() -> str:
    """
    Resolve a Microsoft Graph OAuth token.

    Resolution order:
    1. Authorization: Bearer header (from Bond AI backend)
    2. Local MSAL auth (when MS_CLIENT_ID env var is set)
    3. Raise PermissionError

    Returns:
        The raw access token string.

    Raises:
        PermissionError: If no valid token can be obtained.
    """
    # Path 1: Try Bearer header (works when running behind Bond AI)
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers(include={"authorization"})
        auth = headers.get("authorization")
        if auth and auth.startswith("Bearer "):
            return auth[7:]
    except Exception:  # nosec B110
        pass  # Outside HTTP request context (e.g., stdio transport)

    # Path 2: Local MSAL auth
    if os.environ.get("MS_CLIENT_ID"):
        from ms_graph.local_auth import get_local_token
        return get_local_token()

    # Path 3: No auth available
    raise PermissionError(
        "Authorization required. Either connect your Microsoft account "
        "in Bond AI Settings -> Connections, or set MS_CLIENT_ID for local auth."
    )


def get_powerbi_token() -> str:
    """
    Resolve a Power BI API token.

    Power BI uses a separate resource (analysis.windows.net) from Graph, so a
    distinct token is required. When running behind Bond AI, the `powerbi`
    connection entry in bond_mcp_config uses PBI-scoped OAuth — Bond passes that
    token as the standard Authorization: Bearer header, same as any other connection.

    Resolution order:
    1. Authorization: Bearer header (Bond AI passes PBI-scoped token for powerbi connection)
    2. Local MSAL auth with Power BI resource scope
    3. Raise PermissionError
    """
    # Path 1: Standard Bearer header (works when running behind Bond AI with
    # the powerbi connection active — Bond acquires a PBI-scoped token and
    # forwards it here exactly like it does for any OAuth2 MCP connection)
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers(include={"authorization"})
        auth = headers.get("authorization")
        if auth and auth.startswith("Bearer "):
            return auth[7:]
    except Exception:  # nosec B110
        pass  # Outside HTTP request context (e.g., local CLI)

    # Path 2: Local MSAL auth with Power BI resource scope
    if os.environ.get("MS_CLIENT_ID"):
        from ms_graph.local_auth import get_local_powerbi_token
        return get_local_powerbi_token()

    # Path 3: No auth available
    raise PermissionError(
        "Power BI authorization required. Either connect your Microsoft account "
        "in Bond AI Settings -> Connections, or set MS_CLIENT_ID for local auth."
    )
