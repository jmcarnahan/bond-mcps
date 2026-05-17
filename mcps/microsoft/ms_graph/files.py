"""
File and drive operations using the Microsoft Graph API.

Covers both OneDrive (personal drive) and SharePoint (site document libraries)
via the unified Drive/DriveItem abstraction. All functions accept a GraphClient
or AsyncGraphClient and return parsed dicts.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from .graph_client import GraphClient, AsyncGraphClient, GraphError

MAX_TEXT_DOWNLOAD_BYTES = 524_288   # 512 KB
MAX_SIMPLE_UPLOAD_BYTES = 4_000_000  # 4 MB (Graph simple upload limit)

# Content-type mapping for upload (by file extension)
_UPLOAD_CONTENT_TYPES: Dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

_COPY_POLL_INTERVAL = 2   # seconds between copy status polls
_COPY_POLL_TIMEOUT = 30   # seconds before giving up on a copy operation

# MIME types considered text-readable
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = frozenset({
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
    "application/sql",
})

# File extensions considered text-readable (fallback when MIME is missing)
_TEXT_EXTENSIONS = frozenset({
    ".txt", ".csv", ".json", ".md", ".py", ".js", ".ts", ".html", ".xml",
    ".yaml", ".yml", ".log", ".cfg", ".ini", ".sh", ".sql", ".java", ".c",
    ".cpp", ".css", ".svg", ".toml", ".tf", ".go", ".rs", ".rb", ".jsx",
    ".tsx", ".vue", ".scss", ".less", ".bat", ".ps1", ".r", ".m", ".h",
    ".hpp", ".swift", ".kt", ".gradle", ".properties", ".env", ".gitignore",
    ".dockerfile", ".makefile",
})


def _drive_base(site_id: Optional[str] = None) -> str:
    """Return the Graph API base path for a drive.

    - No site_id (or empty string): user's OneDrive -> ``/me/drive``
    - With site_id: SharePoint site drive -> ``/sites/{site_id}/drive``
    """
    if site_id:
        return f"/sites/{site_id}/drive"
    return "/me/drive"


def _is_text_file(item: Dict[str, Any]) -> bool:
    """Determine if a driveItem is likely a text file based on MIME type or extension."""
    file_info = item.get("file", {})
    mime = file_info.get("mimeType", "")

    if mime:
        if any(mime.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES):
            return True
        if mime in _TEXT_MIME_TYPES:
            return True

    # Fallback to extension
    name = item.get("name", "")
    dot_idx = name.rfind(".")
    if dot_idx >= 0:
        ext = name[dot_idx:].lower()
        if ext in _TEXT_EXTENSIONS:
            return True

    return False


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------

def list_drive_children(
    client: GraphClient,
    folder_path: str = "",
    site_id: str = "",
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List files and folders at a given path in a drive."""
    base = _drive_base(site_id or None)
    if folder_path and folder_path != "/":
        path = folder_path.strip("/")
        url = f"{base}/root:/{path}:/children"
    else:
        url = f"{base}/root/children"
    data = client.get(url, params={"$top": top})
    return data.get("value", [])


def get_drive_item(
    client: GraphClient,
    item_id: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Get metadata for a single drive item by ID."""
    base = _drive_base(site_id or None)
    return client.get(f"{base}/items/{item_id}")


def get_drive_item_content(
    client: GraphClient,
    item_id: str,
    site_id: str = "",
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Get a drive item's metadata and, if it's a text file, its content.

    Returns ``(item_metadata, text_content_or_None)``.
    """
    item = get_drive_item(client, item_id, site_id)

    if not _is_text_file(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_TEXT_DOWNLOAD_BYTES:
        return item, None

    base = _drive_base(site_id or None)
    raw = client.get_bytes(f"{base}/items/{item_id}/content")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return item, text


def search_drive(
    client: GraphClient,
    query: str,
    site_id: str = "",
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search within a single drive (legacy per-drive search endpoint)."""
    base = _drive_base(site_id or None)
    escaped = query.replace("'", "''")
    data = client.get(f"{base}/root/search(q='{escaped}')", params={"$top": top})
    return data.get("value", [])


def search_files_unified(
    client: GraphClient,
    query: str,
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Cross-drive file search. Uses Microsoft Search API for org accounts,
    falls back to per-drive search for consumer accounts."""
    try:
        payload = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": top,
                }
            ]
        }
        data = client.post("/search/query", json_data=payload)
        return _parse_search_response(data)
    except GraphError as e:
        if e.status_code == 400 and "not supported" in str(e).lower():
            # Consumer account — fall back to per-drive search
            return search_drive(client, query, top=top)
        raise


def list_sites(
    client: GraphClient,
    query: str = "",
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search for SharePoint sites, or list followed sites if no query."""
    if query:
        data = client.get("/sites", params={"$search": f'"{query}"', "$top": top})
    else:
        data = client.get("/me/followedSites", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def alist_drive_children(
    client: AsyncGraphClient,
    folder_path: str = "",
    site_id: str = "",
    top: int = 20,
) -> List[Dict[str, Any]]:
    """List files and folders at a given path in a drive (async)."""
    base = _drive_base(site_id or None)
    if folder_path and folder_path != "/":
        path = folder_path.strip("/")
        url = f"{base}/root:/{path}:/children"
    else:
        url = f"{base}/root/children"
    data = await client.get(url, params={"$top": top})
    return data.get("value", [])


async def aget_drive_item(
    client: AsyncGraphClient,
    item_id: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Get metadata for a single drive item by ID (async)."""
    base = _drive_base(site_id or None)
    return await client.get(f"{base}/items/{item_id}")


async def aget_drive_item_content(
    client: AsyncGraphClient,
    item_id: str,
    site_id: str = "",
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Get a drive item's metadata and, if it's a text file, its content (async).

    Returns ``(item_metadata, text_content_or_None)``.
    """
    item = await aget_drive_item(client, item_id, site_id)

    if not _is_text_file(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_TEXT_DOWNLOAD_BYTES:
        return item, None

    base = _drive_base(site_id or None)
    raw = await client.get_bytes(f"{base}/items/{item_id}/content")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return item, text


async def asearch_drive(
    client: AsyncGraphClient,
    query: str,
    site_id: str = "",
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search within a single drive (async)."""
    base = _drive_base(site_id or None)
    escaped = query.replace("'", "''")
    data = await client.get(f"{base}/root/search(q='{escaped}')", params={"$top": top})
    return data.get("value", [])


async def asearch_files_unified(
    client: AsyncGraphClient,
    query: str,
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Cross-drive file search. Uses Microsoft Search API for org accounts,
    falls back to per-drive search for consumer accounts (async)."""
    try:
        payload = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": top,
                }
            ]
        }
        data = await client.post("/search/query", json_data=payload)
        return _parse_search_response(data)
    except GraphError as e:
        if e.status_code == 400 and "not supported" in str(e).lower():
            return await asearch_drive(client, query, top=top)
        raise


async def alist_sites(
    client: AsyncGraphClient,
    query: str = "",
    top: int = 10,
) -> List[Dict[str, Any]]:
    """Search for SharePoint sites, or list followed sites if no query (async)."""
    if query:
        data = await client.get("/sites", params={"$search": f'"{query}"', "$top": top})
    else:
        data = await client.get("/me/followedSites", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Write / mutate operations — synchronous
# ---------------------------------------------------------------------------

def upload_file(
    client: GraphClient,
    folder_path: str,
    filename: str,
    content: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Create or overwrite a text file at folder_path/filename.

    Uses the simple upload endpoint (max 4 MB). Returns the created/updated
    driveItem. Supported text formats: .txt, .md, .html, .csv, .json, .xml,
    .yaml — other extensions default to application/octet-stream.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(encoded):,} bytes, which exceeds the 4 MB simple upload limit. "
            "Split the content or use the resumable upload API."
        )
    base = _drive_base(site_id or None)
    path = folder_path.strip("/")
    url = f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    ext = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""
    content_type = _UPLOAD_CONTENT_TYPES.get(ext, "application/octet-stream")
    return client.put(url, content=encoded, content_type=content_type)


def copy_drive_item(
    client: GraphClient,
    item_id: str,
    new_name: str,
    destination_folder_id: str = "",
    site_id: str = "",
    destination_drive_id: str = "",
) -> Dict[str, Any]:
    """Server-side copy of a drive item to a new name (and optionally a new folder).

    Works for any file type including Word, Excel, and PDF. The Graph copy API
    is asynchronous — this function polls until the operation completes (up to
    _COPY_POLL_TIMEOUT seconds). Returns the completed operation status dict.
    """
    source = get_drive_item(client, item_id, site_id)
    drive_id = destination_drive_id or source.get("parentReference", {}).get("driveId", "")

    parent_ref: Dict[str, Any] = {"driveId": drive_id}
    if destination_folder_id:
        parent_ref["id"] = destination_folder_id
    else:
        parent_ref["id"] = source.get("parentReference", {}).get("id", "")

    base = _drive_base(site_id or None)
    location = client.post_with_location(
        f"{base}/items/{item_id}/copy",
        json_data={"name": new_name, "parentReference": parent_ref},
    )

    deadline = time.monotonic() + _COPY_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_COPY_POLL_INTERVAL)
        status = client.get_operation_status(location)
        if status.get("status") == "completed":
            return status
        if status.get("status") == "failed":
            err = status.get("error", {})
            raise GraphError(500, err.get("code", "CopyFailed"), err.get("message", "Copy failed"))
    raise GraphError(504, "CopyTimeout", f"Copy did not complete within {_COPY_POLL_TIMEOUT}s")


def rename_drive_item(
    client: GraphClient,
    item_id: str,
    new_name: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Rename a file or folder by item ID. Returns the updated driveItem."""
    base = _drive_base(site_id or None)
    return client.patch(f"{base}/items/{item_id}", json_data={"name": new_name})


# ---------------------------------------------------------------------------
# Write / mutate operations — asynchronous
# ---------------------------------------------------------------------------

async def aupload_file(
    client: AsyncGraphClient,
    folder_path: str,
    filename: str,
    content: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Create or overwrite a text file at folder_path/filename (async)."""
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(encoded):,} bytes, which exceeds the 4 MB simple upload limit. "
            "Split the content or use the resumable upload API."
        )
    base = _drive_base(site_id or None)
    path = folder_path.strip("/")
    url = f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    ext = f".{filename.rsplit('.', 1)[-1].lower()}" if "." in filename else ""
    content_type = _UPLOAD_CONTENT_TYPES.get(ext, "application/octet-stream")
    return await client.put(url, content=encoded, content_type=content_type)


async def aupload_bytes(
    client: AsyncGraphClient,
    folder_path: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
) -> Dict[str, Any]:
    """Upload raw bytes to folder_path/filename (async). Used for binary files such as
    exported PDFs, PNGs, or PPTX files where the content is already encoded."""
    if len(data) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(data):,} bytes, which exceeds the 4 MB simple upload limit. "
            "Split the content or use the resumable upload API."
        )
    base = _drive_base(site_id or None)
    path = folder_path.strip("/")
    url = f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    return await client.put(url, content=data, content_type=content_type)


async def acopy_drive_item(
    client: AsyncGraphClient,
    item_id: str,
    new_name: str,
    destination_folder_id: str = "",
    site_id: str = "",
    destination_drive_id: str = "",
) -> Dict[str, Any]:
    """Server-side copy of a drive item (async). Polls until completion."""
    source = await aget_drive_item(client, item_id, site_id)
    drive_id = destination_drive_id or source.get("parentReference", {}).get("driveId", "")

    parent_ref: Dict[str, Any] = {"driveId": drive_id}
    if destination_folder_id:
        parent_ref["id"] = destination_folder_id
    else:
        parent_ref["id"] = source.get("parentReference", {}).get("id", "")

    base = _drive_base(site_id or None)
    location = await client.post_with_location(
        f"{base}/items/{item_id}/copy",
        json_data={"name": new_name, "parentReference": parent_ref},
    )

    deadline = time.monotonic() + _COPY_POLL_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(_COPY_POLL_INTERVAL)
        status = await client.get_operation_status(location)
        if status.get("status") == "completed":
            return status
        if status.get("status") == "failed":
            err = status.get("error", {})
            raise GraphError(500, err.get("code", "CopyFailed"), err.get("message", "Copy failed"))
    raise GraphError(504, "CopyTimeout", f"Copy did not complete within {_COPY_POLL_TIMEOUT}s")


async def arename_drive_item(
    client: AsyncGraphClient,
    item_id: str,
    new_name: str,
    site_id: str = "",
) -> Dict[str, Any]:
    """Rename a file or folder by item ID (async). Returns the updated driveItem."""
    base = _drive_base(site_id or None)
    return await client.patch(f"{base}/items/{item_id}", json_data={"name": new_name})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_search_response(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten Microsoft Search API response into a list of driveItem dicts.

    Response shape: ``value[] -> hitsContainers[] -> hits[] -> { resource, summary }``
    Injects ``_searchSummary`` into each resource dict.
    """
    if not data:
        return []
    results: List[Dict[str, Any]] = []
    for entry in data.get("value", []):
        for container in entry.get("hitsContainers", []):
            for hit in container.get("hits", []):
                resource = hit.get("resource", {})
                summary = hit.get("summary", "")
                if summary:
                    resource["_searchSummary"] = summary
                results.append(resource)
    return results
