"""
Token and cloud ID resolution for MCP server.

Path 1: Bearer header / X-Atlassian-Cloud-Id header (Bond AI backend) -- preferred
Path 2: Local OAuth (Claude Code / standalone) -- if ATLASSIAN_CLIENT_ID set
Path 3: PermissionError
"""

import os


def get_atlassian_token() -> str:
    """
    Resolve an Atlassian OAuth access token.

    Resolution order:
    1. Authorization: Bearer header (from Bond AI backend)
    2. Local OAuth (when ATLASSIAN_CLIENT_ID env var is set)
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
        pass  # Outside HTTP request context

    # Path 2: Local OAuth
    if os.environ.get("ATLASSIAN_CLIENT_ID"):
        from atlassian.local_auth import get_local_token_and_cloud_id
        token, _ = get_local_token_and_cloud_id()
        return token

    # Path 3: No auth available
    raise PermissionError(
        "Authorization required. Either connect your Atlassian account "
        "in Bond AI Settings -> Connections, or set ATLASSIAN_CLIENT_ID "
        "and ATLASSIAN_CLIENT_SECRET for local auth."
    )


def get_cloud_id() -> str:
    """
    Resolve an Atlassian cloud ID.

    Resolution order:
    1. X-Atlassian-Cloud-Id header (from Bond AI backend)
    2. ATLASSIAN_CLOUD_ID environment variable
    3. Local OAuth discovery (accessible-resources API)
    4. Raise PermissionError

    Returns:
        The cloud ID string.

    Raises:
        PermissionError: If no cloud ID can be obtained.
    """
    # Path 1: Try header (Bond AI backend)
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers(include={"x-atlassian-cloud-id"})
        cloud_id = headers.get("x-atlassian-cloud-id")
        if cloud_id:
            return cloud_id
    except Exception:  # nosec B110
        pass

    # Path 2: Environment variable
    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID")
    if cloud_id:
        return cloud_id

    # Path 3: Local OAuth discovery
    if os.environ.get("ATLASSIAN_CLIENT_ID"):
        from atlassian.local_auth import get_local_token_and_cloud_id
        _, cloud_id = get_local_token_and_cloud_id()
        return cloud_id

    # Path 4: No cloud ID available
    raise PermissionError(
        "Atlassian Cloud ID required. Either configure it in Bond AI Settings "
        "-> Connections, or set ATLASSIAN_CLOUD_ID environment variable."
    )
