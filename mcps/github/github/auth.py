"""
Token resolution for MCP server.

Path 1: Bearer header (Bond AI backend) -- always preferred
Path 2: Local OAuth (Claude Code / standalone) -- if GITHUB_CLIENT_ID set
Path 3: PermissionError
"""

import os


def get_github_token() -> str:
    """
    Resolve a GitHub OAuth access token.

    Resolution order:
    1. Authorization: Bearer header (from Bond AI backend)
    2. Local OAuth auth (when GITHUB_CLIENT_ID env var is set)
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

    # Path 2: Local OAuth
    if os.environ.get("GITHUB_CLIENT_ID"):
        from github.local_auth import get_local_token
        return get_local_token()

    # Path 3: No auth available
    raise PermissionError(
        "Authorization required. Either connect your GitHub account "
        "in Bond AI Settings -> Connections, or set GITHUB_CLIENT_ID for local auth."
    )
