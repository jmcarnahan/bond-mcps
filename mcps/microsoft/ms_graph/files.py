"""
File and drive operations using the Microsoft Graph API.

Covers both OneDrive (personal drive) and SharePoint (site document libraries)
via the unified Drive/DriveItem abstraction. All functions accept a GraphClient
or AsyncGraphClient and return parsed dicts.
"""

import asyncio
import base64
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from .document_extract import (
    MAX_DOCUMENT_DOWNLOAD_BYTES,
    extract_document_text,
    is_extractable_document,
)
from .graph_client import AsyncGraphClient, GraphClient, GraphError

logger = logging.getLogger(__name__)

MAX_TEXT_DOWNLOAD_BYTES = 2_000_000  # 2 MB
MAX_SIMPLE_UPLOAD_BYTES = 4_000_000  # 4 MB (Graph simple upload limit)

# 12 x 320 KiB. OneDrive requires fragments to be a multiple of 320 KiB and
# Outlook wants each PUT under 4 MB, so one constant serves both session kinds.
UPLOAD_CHUNK_BYTES = 3_932_160

_THUMBNAIL_SIZES = frozenset({"small", "medium", "large"})


def _path_segments(folder_path: str, filename: str) -> tuple[str, str]:
    """Percent-encode a folder path and file name for the ``root:/…:`` path form.

    Names come from callers (an LLM spec, a Teams file), and a ``#`` or ``?``
    in one would otherwise end the URL path early. Slashes stay in the folder
    path (they are separators there) and are encoded in the file name.
    """
    return quote(folder_path.strip("/"), safe="/"), quote(filename, safe="")


# Content-type mapping for upload (by file extension)
_UPLOAD_CONTENT_TYPES: dict[str, str] = {
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

_COPY_POLL_INTERVAL = 2  # seconds between copy status polls
_COPY_POLL_TIMEOUT = 30  # seconds before giving up on a copy operation

# MIME types considered text-readable
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/x-sh",
        "application/sql",
    }
)

# File extensions considered text-readable (fallback when MIME is missing)
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".csv",
        ".json",
        ".md",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".xml",
        ".yaml",
        ".yml",
        ".log",
        ".cfg",
        ".ini",
        ".sh",
        ".sql",
        ".java",
        ".c",
        ".cpp",
        ".css",
        ".svg",
        ".toml",
        ".tf",
        ".go",
        ".rs",
        ".rb",
        ".jsx",
        ".tsx",
        ".vue",
        ".scss",
        ".less",
        ".bat",
        ".ps1",
        ".r",
        ".m",
        ".h",
        ".hpp",
        ".swift",
        ".kt",
        ".gradle",
        ".properties",
        ".env",
        ".gitignore",
        ".dockerfile",
        ".makefile",
    }
)


# Matches SharePoint/OneDrive sharing URLs broadly.
_SHARING_URL_PATTERN = re.compile(
    r"^https?://"
    r"("
    r"[a-zA-Z0-9\-]+\.sharepoint\.com"
    r"|1drv\.ms"
    r"|onedrive\.live\.com"
    r")"
    r"/",
    re.IGNORECASE,
)


def is_sharing_url(value: str) -> bool:
    """Return True if the value looks like a SharePoint/OneDrive sharing URL."""
    return bool(_SHARING_URL_PATTERN.match(value.strip()))


def _encode_sharing_url(url: str) -> str:
    """Encode a sharing URL into the share token format for the Graph Shares API.

    Algorithm (from Microsoft docs):
    1. Base64-encode the URL
    2. Replace '/' with '_', '+' with '-', remove trailing '='
    3. Prefix with 'u!'
    """
    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    token = encoded.replace("/", "_").replace("+", "-").rstrip("=")
    return f"u!{token}"


def _drive_base(site_id: str | None = None, drive_id: str | None = None) -> str:
    """Return the Graph API base path for a drive.

    - drive_id provided: specific drive -> ``/drives/{drive_id}``
    - No drive_id, with site_id: SharePoint site drive -> ``/sites/{site_id}/drive``
    - Neither: user's OneDrive -> ``/me/drive``
    """
    if drive_id:
        return f"/drives/{drive_id}"
    if site_id:
        return f"/sites/{site_id}/drive"
    return "/me/drive"


def _is_text_file(item: dict[str, Any]) -> bool:
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
) -> list[dict[str, Any]]:
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
    drive_id: str = "",
) -> dict[str, Any]:
    """Get metadata for a single drive item by ID."""
    base = _drive_base(site_id or None, drive_id or None)
    return client.get(f"{base}/items/{item_id}")


def get_drive_item_content(
    client: GraphClient,
    item_id: str,
    site_id: str = "",
) -> tuple[dict[str, Any], str | None]:
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
) -> list[dict[str, Any]]:
    """Search within a single drive (legacy per-drive search endpoint)."""
    base = _drive_base(site_id or None)
    escaped = query.replace("'", "''")
    data = client.get(f"{base}/root/search(q='{escaped}')", params={"$top": top})
    return data.get("value", [])


def search_files_unified(
    client: GraphClient,
    query: str,
    top: int = 10,
) -> list[dict[str, Any]]:
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
) -> list[dict[str, Any]]:
    """Search for SharePoint sites, or list followed sites if no query."""
    if query:
        data = client.get("/sites", params={"$search": f'"{query}"', "$top": top})
    else:
        data = client.get("/me/followedSites", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Sharing link resolution — synchronous
# ---------------------------------------------------------------------------


def resolve_sharing_link(
    client: GraphClient,
    url: str,
) -> dict[str, Any]:
    """Resolve a sharing URL to its underlying driveItem metadata."""
    token = _encode_sharing_url(url.strip())
    return client.get(f"/shares/{token}/driveItem")


def resolve_sharing_link_content(
    client: GraphClient,
    url: str,
) -> tuple[dict[str, Any], str | None]:
    """Resolve a sharing URL and download text content if applicable.

    Returns (item_metadata, text_content_or_None).
    """
    token = _encode_sharing_url(url.strip())
    item = client.get(f"/shares/{token}/driveItem")

    if not _is_text_file(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_TEXT_DOWNLOAD_BYTES:
        return item, None

    raw = client.get_bytes(f"/shares/{token}/driveItem/content")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return item, text


def list_sharing_link_children(
    client: GraphClient,
    url: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """If the sharing URL points to a folder, list its children."""
    token = _encode_sharing_url(url.strip())
    data = client.get(f"/shares/{token}/root/children", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------


async def alist_drive_children(
    client: AsyncGraphClient,
    folder_path: str = "",
    site_id: str = "",
    top: int = 20,
) -> list[dict[str, Any]]:
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
    drive_id: str = "",
) -> dict[str, Any]:
    """Get metadata for a single drive item by ID (async)."""
    base = _drive_base(site_id or None, drive_id or None)
    return await client.get(f"{base}/items/{item_id}")


async def aget_drive_item_content(
    client: AsyncGraphClient,
    item_id: str,
    site_id: str = "",
) -> tuple[dict[str, Any], str | None]:
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
) -> list[dict[str, Any]]:
    """Search within a single drive (async)."""
    base = _drive_base(site_id or None)
    escaped = query.replace("'", "''")
    data = await client.get(f"{base}/root/search(q='{escaped}')", params={"$top": top})
    return data.get("value", [])


async def asearch_files_unified(
    client: AsyncGraphClient,
    query: str,
    top: int = 10,
) -> list[dict[str, Any]]:
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
) -> list[dict[str, Any]]:
    """Search for SharePoint sites, or list followed sites if no query (async)."""
    if query:
        data = await client.get("/sites", params={"$search": f'"{query}"', "$top": top})
    else:
        data = await client.get("/me/followedSites", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Sharing link resolution — asynchronous
# ---------------------------------------------------------------------------


async def aresolve_sharing_link(
    client: AsyncGraphClient,
    url: str,
) -> dict[str, Any]:
    """Resolve a sharing URL to its underlying driveItem metadata (async)."""
    token = _encode_sharing_url(url.strip())
    return await client.get(f"/shares/{token}/driveItem")


async def aresolve_sharing_link_content(
    client: AsyncGraphClient,
    url: str,
) -> tuple[dict[str, Any], str | None]:
    """Resolve a sharing URL and download text content if applicable (async).

    Returns (item_metadata, text_content_or_None).
    """
    token = _encode_sharing_url(url.strip())
    item = await client.get(f"/shares/{token}/driveItem")

    if not _is_text_file(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_TEXT_DOWNLOAD_BYTES:
        return item, None

    raw = await client.get_bytes(f"/shares/{token}/driveItem/content")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return item, text


async def alist_sharing_link_children(
    client: AsyncGraphClient,
    url: str,
    top: int = 20,
) -> list[dict[str, Any]]:
    """If the sharing URL points to a folder, list its children (async)."""
    token = _encode_sharing_url(url.strip())
    data = await client.get(f"/shares/{token}/root/children", params={"$top": top})
    return data.get("value", [])


# ---------------------------------------------------------------------------
# Document content extraction — synchronous
# ---------------------------------------------------------------------------


def get_drive_item_extracted_content(
    client: GraphClient,
    item_id: str,
    site_id: str = "",
    item: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Download an Office document and extract its text content.

    Pass a pre-fetched ``item`` dict to avoid a redundant metadata GET.
    Returns (item_metadata, extracted_text_or_None).
    """
    base = _drive_base(site_id or None)
    if item is None:
        item = client.get(f"{base}/items/{item_id}")

    if not is_extractable_document(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_DOCUMENT_DOWNLOAD_BYTES:
        return item, None

    raw = client.get_bytes(f"{base}/items/{item_id}/content")
    mime = item.get("file", {}).get("mimeType", "")
    name = item.get("name", "")
    text = extract_document_text(raw, mime, name)
    return item, text


def resolve_sharing_link_extracted_content(
    client: GraphClient,
    url: str,
    item: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Resolve a sharing URL and extract document text content.

    Pass a pre-fetched ``item`` dict to avoid a redundant metadata GET.
    Returns (item_metadata, extracted_text_or_None).
    """
    token = _encode_sharing_url(url.strip())
    if item is None:
        item = client.get(f"/shares/{token}/driveItem")

    if not is_extractable_document(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_DOCUMENT_DOWNLOAD_BYTES:
        return item, None

    raw = client.get_bytes(f"/shares/{token}/driveItem/content")
    mime = item.get("file", {}).get("mimeType", "")
    name = item.get("name", "")
    text = extract_document_text(raw, mime, name)
    return item, text


# ---------------------------------------------------------------------------
# Document content extraction — asynchronous
# ---------------------------------------------------------------------------


async def aget_drive_item_extracted_content(
    client: AsyncGraphClient,
    item_id: str,
    site_id: str = "",
    item: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Download an Office document and extract its text content (async).

    Pass a pre-fetched ``item`` dict to avoid a redundant metadata GET.
    Returns (item_metadata, extracted_text_or_None).
    """
    base = _drive_base(site_id or None)
    if item is None:
        item = await client.get(f"{base}/items/{item_id}")

    if not is_extractable_document(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_DOCUMENT_DOWNLOAD_BYTES:
        return item, None

    raw = await client.get_bytes(f"{base}/items/{item_id}/content")
    mime = item.get("file", {}).get("mimeType", "")
    name = item.get("name", "")
    text = extract_document_text(raw, mime, name)
    return item, text


async def aresolve_sharing_link_extracted_content(
    client: AsyncGraphClient,
    url: str,
    item: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Resolve a sharing URL and extract document text content (async).

    Pass a pre-fetched ``item`` dict to avoid a redundant metadata GET.
    Returns (item_metadata, extracted_text_or_None).
    """
    token = _encode_sharing_url(url.strip())
    if item is None:
        item = await client.get(f"/shares/{token}/driveItem")

    if not is_extractable_document(item):
        return item, None

    size = item.get("size", 0)
    if size > MAX_DOCUMENT_DOWNLOAD_BYTES:
        return item, None

    raw = await client.get_bytes(f"/shares/{token}/driveItem/content")
    mime = item.get("file", {}).get("mimeType", "")
    name = item.get("name", "")
    text = extract_document_text(raw, mime, name)
    return item, text


# ---------------------------------------------------------------------------
# Write / mutate operations — synchronous
# ---------------------------------------------------------------------------


def upload_file(
    client: GraphClient,
    folder_path: str,
    filename: str,
    content: str,
    site_id: str = "",
) -> dict[str, Any]:
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
    url = (
        f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    )
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
    source_drive_id: str = "",
) -> dict[str, Any]:
    """Server-side copy of a drive item to a new name (and optionally a new folder).

    Works for any file type including Word, Excel, and PDF. The Graph copy API
    is asynchronous — this function polls until the operation completes (up to
    _COPY_POLL_TIMEOUT seconds). Returns the completed operation status dict.
    """
    source = get_drive_item(client, item_id, site_id, drive_id=source_drive_id)
    drive_id = destination_drive_id or source.get("parentReference", {}).get("driveId", "")

    parent_ref: dict[str, Any] = {"driveId": drive_id}
    if destination_folder_id:
        parent_ref["id"] = destination_folder_id
    else:
        parent_ref["id"] = source.get("parentReference", {}).get("id", "")

    item_drive_id = source.get("parentReference", {}).get("driveId", "")
    if item_drive_id:
        copy_path = f"/drives/{item_drive_id}/items/{item_id}/copy"
    else:
        copy_path = f"{_drive_base(site_id or None)}/items/{item_id}/copy"
    location = client.post_with_location(
        copy_path,
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
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """Create or overwrite a text file at folder_path/filename (async)."""
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(encoded):,} bytes, which exceeds the 4 MB simple upload limit. "
            "Split the content or use the resumable upload API."
        )
    base = _drive_base(site_id or None)
    path = folder_path.strip("/")
    url = (
        f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    )
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
) -> dict[str, Any]:
    """Upload raw bytes to folder_path/filename (async). Used for binary files such as
    exported PDFs, PNGs, or PPTX files where the content is already encoded."""
    if len(data) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(data):,} bytes, which exceeds the 4 MB simple upload limit. "
            "Split the content or use the resumable upload API."
        )
    base = _drive_base(site_id or None)
    path = folder_path.strip("/")
    url = (
        f"{base}/root:/{path}/{filename}:/content" if path else f"{base}/root:/{filename}:/content"
    )
    return await client.put(url, content=data, content_type=content_type)


async def aupload_bytes_by_id(
    client: AsyncGraphClient,
    item_id: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
) -> dict[str, Any]:
    """Overwrite an existing file's content by item ID (async)."""
    if len(data) > MAX_SIMPLE_UPLOAD_BYTES:
        raise ValueError(
            f"Content is {len(data):,} bytes, which exceeds the 4 MB simple upload limit."
        )
    base = _drive_base(site_id or None)
    url = f"{base}/items/{item_id}/content"
    return await client.put(url, content=data, content_type=content_type)


async def acopy_drive_item(
    client: AsyncGraphClient,
    item_id: str,
    new_name: str,
    destination_folder_id: str = "",
    site_id: str = "",
    destination_drive_id: str = "",
    source_drive_id: str = "",
) -> dict[str, Any]:
    """Server-side copy of a drive item (async). Polls until completion."""
    source = await aget_drive_item(client, item_id, site_id, drive_id=source_drive_id)
    drive_id = destination_drive_id or source.get("parentReference", {}).get("driveId", "")

    parent_ref: dict[str, Any] = {"driveId": drive_id}
    if destination_folder_id:
        parent_ref["id"] = destination_folder_id
    else:
        parent_ref["id"] = source.get("parentReference", {}).get("id", "")

    item_drive_id = source.get("parentReference", {}).get("driveId", "")
    if item_drive_id:
        copy_path = f"/drives/{item_drive_id}/items/{item_id}/copy"
    else:
        copy_path = f"{_drive_base(site_id or None)}/items/{item_id}/copy"
    location = await client.post_with_location(
        copy_path,
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
) -> dict[str, Any]:
    """Rename a file or folder by item ID (async). Returns the updated driveItem."""
    base = _drive_base(site_id or None)
    return await client.patch(f"{base}/items/{item_id}", json_data={"name": new_name})


async def adelete_drive_item(
    client: AsyncGraphClient,
    item_id: str,
    site_id: str = "",
) -> None:
    """Delete a file or folder by item ID (async). Graph replies 204 No Content.

    The item is moved to the recycle bin, not permanently erased, so this is
    recoverable from the SharePoint/OneDrive UI.
    """
    base = _drive_base(site_id or None)
    await client.delete(f"{base}/items/{item_id}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_search_response(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten Microsoft Search API response into a list of driveItem dicts.

    Response shape: ``value[] -> hitsContainers[] -> hits[] -> { resource, summary }``
    Injects ``_searchSummary`` into each resource dict.
    """
    if not data:
        return []
    results: list[dict[str, Any]] = []
    for entry in data.get("value", []):
        for container in entry.get("hitsContainers", []):
            for hit in container.get("hits", []):
                resource = hit.get("resource", {})
                summary = hit.get("summary", "")
                if summary:
                    resource["_searchSummary"] = summary
                results.append(resource)
    return results


# ---------------------------------------------------------------------------
# Upload sessions (files above the 4 MB simple-upload limit)
# ---------------------------------------------------------------------------


def _upload_session_path(
    folder_path: str,
    filename: str,
    site_id: str,
    drive_id: str,
    parent_id: str,
) -> str:
    """Graph path that opens an upload session for a new/replaced file."""
    base = _drive_base(site_id or None, drive_id or None)
    path, name = _path_segments(folder_path, filename)
    if parent_id:
        return f"{base}/items/{parent_id}:/{name}:/createUploadSession"
    if path:
        return f"{base}/root:/{path}/{name}:/createUploadSession"
    return f"{base}/root:/{name}:/createUploadSession"


def _simple_upload_path(
    folder_path: str,
    filename: str,
    site_id: str,
    drive_id: str,
    parent_id: str,
    conflict_behavior: str,
) -> str:
    """Graph path for a simple content PUT.

    The conflict behavior travels as a query parameter because ``client.put``
    talks to a bearer client that takes no ``params``.
    """
    base = _drive_base(site_id or None, drive_id or None)
    path, name = _path_segments(folder_path, filename)
    if parent_id:
        url = f"{base}/items/{parent_id}:/{name}:/content"
    elif path:
        url = f"{base}/root:/{path}/{name}:/content"
    else:
        url = f"{base}/root:/{name}:/content"
    if conflict_behavior != "replace":
        url += f"?@microsoft.graph.conflictBehavior={conflict_behavior}"
    return url


def _upload_session_body(filename: str, conflict_behavior: str) -> dict[str, Any]:
    """Request body for createUploadSession."""
    return {
        "item": {
            "@microsoft.graph.conflictBehavior": conflict_behavior,
            "name": filename,
        }
    }


def _upload_url_from_session(session: dict[str, Any] | None) -> str:
    """Pull uploadUrl out of a createUploadSession response, or fail loudly."""
    upload_url = (session or {}).get("uploadUrl", "")
    if not upload_url:
        raise GraphError(500, "NoUploadUrl", "createUploadSession did not return an uploadUrl")
    return upload_url


def _final_item(body: dict[str, Any] | None) -> dict[str, Any]:
    """The last fragment carries the finished driveItem; anything else is a bug."""
    if body is None:
        raise GraphError(
            500,
            "NoDriveItem",
            "Upload session completed without returning the uploaded item",
        )
    return body


def _check_session_payload(data: bytes) -> None:
    """An upload session needs at least one fragment to send."""
    if not data:
        raise ValueError("Cannot upload empty content through an upload session.")


def upload_bytes_session(
    client: GraphClient,
    folder_path: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
    drive_id: str = "",
    parent_id: str = "",
    conflict_behavior: str = "replace",
) -> dict[str, Any]:
    """Upload bytes through a resumable upload session. Returns the driveItem.

    Fragments go out sequentially because each one must be acknowledged before
    the next range is valid. A failure mid-way cancels the session so the
    abandoned upload does not linger in the drive's quota.
    """
    _check_session_payload(data)
    session = client.post(
        _upload_session_path(folder_path, filename, site_id, drive_id, parent_id),
        json_data=_upload_session_body(filename, conflict_behavior),
    )
    upload_url = _upload_url_from_session(session)
    total = len(data)
    result = None
    try:
        start = 0
        while start < total:
            chunk = data[start : start + UPLOAD_CHUNK_BYTES]
            result = client.put_range(upload_url, chunk, start, total, content_type)
            start += len(chunk)
    except Exception:
        _cancel_session(client, upload_url)
        raise
    return _final_item(result.body if result else None)


async def aupload_bytes_session(
    client: AsyncGraphClient,
    folder_path: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
    drive_id: str = "",
    parent_id: str = "",
    conflict_behavior: str = "replace",
) -> dict[str, Any]:
    """Upload bytes through a resumable upload session (async). Returns the driveItem."""
    _check_session_payload(data)
    session = await client.post(
        _upload_session_path(folder_path, filename, site_id, drive_id, parent_id),
        json_data=_upload_session_body(filename, conflict_behavior),
    )
    upload_url = _upload_url_from_session(session)
    total = len(data)
    result = None
    try:
        start = 0
        while start < total:
            chunk = data[start : start + UPLOAD_CHUNK_BYTES]
            result = await client.put_range(upload_url, chunk, start, total, content_type)
            start += len(chunk)
    except Exception:
        await _acancel_session(client, upload_url)
        raise
    return _final_item(result.body if result else None)


def _cancel_session(client: GraphClient, upload_url: str) -> None:
    """Best-effort cancel — the original failure is what the caller must see."""
    try:
        client.delete_url(upload_url)
    except Exception:
        logger.debug("Could not cancel upload session at %s", upload_url, exc_info=True)


async def _acancel_session(client: AsyncGraphClient, upload_url: str) -> None:
    """Best-effort cancel (async)."""
    try:
        await client.delete_url(upload_url)
    except Exception:
        logger.debug("Could not cancel upload session at %s", upload_url, exc_info=True)


def upload_any(
    client: GraphClient,
    folder_path: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
    drive_id: str = "",
    parent_id: str = "",
    conflict_behavior: str = "replace",
) -> dict[str, Any]:
    """Upload bytes of any size, picking simple upload or an upload session."""
    if len(data) <= MAX_SIMPLE_UPLOAD_BYTES:
        url = _simple_upload_path(
            folder_path, filename, site_id, drive_id, parent_id, conflict_behavior
        )
        return client.put(url, content=data, content_type=content_type)
    return upload_bytes_session(
        client,
        folder_path,
        filename,
        data,
        content_type,
        site_id=site_id,
        drive_id=drive_id,
        parent_id=parent_id,
        conflict_behavior=conflict_behavior,
    )


async def aupload_any(
    client: AsyncGraphClient,
    folder_path: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    site_id: str = "",
    drive_id: str = "",
    parent_id: str = "",
    conflict_behavior: str = "replace",
) -> dict[str, Any]:
    """Upload bytes of any size, picking simple upload or an upload session (async)."""
    if len(data) <= MAX_SIMPLE_UPLOAD_BYTES:
        url = _simple_upload_path(
            folder_path, filename, site_id, drive_id, parent_id, conflict_behavior
        )
        return await client.put(url, content=data, content_type=content_type)
    return await aupload_bytes_session(
        client,
        folder_path,
        filename,
        data,
        content_type,
        site_id=site_id,
        drive_id=drive_id,
        parent_id=parent_id,
        conflict_behavior=conflict_behavior,
    )


# ---------------------------------------------------------------------------
# Sharing-link raw content and thumbnails
# ---------------------------------------------------------------------------


def _check_shared_item(item: dict[str, Any]) -> None:
    """Refuse folders and oversized files before downloading anything."""
    if "folder" in item:
        raise ValueError(
            f"'{item.get('name', 'item')}' is a folder, not a file; sharing links to "
            "folders cannot be downloaded."
        )
    size = item.get("size", 0)
    if isinstance(size, int) and size > MAX_DOCUMENT_DOWNLOAD_BYTES:
        raise ValueError(
            f"'{item.get('name', 'item')}' is {size:,} bytes, which exceeds the "
            f"{MAX_DOCUMENT_DOWNLOAD_BYTES:,} byte download limit."
        )


def _check_thumbnail_size(size: str) -> None:
    """Graph only serves the three named thumbnail sizes."""
    if size not in _THUMBNAIL_SIZES:
        raise ValueError(f"size must be 'small', 'medium', or 'large'; got {size!r}")


def resolve_sharing_link_bytes(client: GraphClient, url: str) -> tuple[dict[str, Any], bytes]:
    """Resolve a sharing URL and download the file's raw bytes.

    Returns (item_metadata, raw_bytes). Unlike resolve_sharing_link_content this
    does not care whether the file is text — attachments are frequently binary.
    """
    token = _encode_sharing_url(url.strip())
    item = client.get(f"/shares/{token}/driveItem")
    _check_shared_item(item)
    return item, client.get_bytes(f"/shares/{token}/driveItem/content")


async def aresolve_sharing_link_bytes(
    client: AsyncGraphClient, url: str
) -> tuple[dict[str, Any], bytes]:
    """Resolve a sharing URL and download the file's raw bytes (async)."""
    token = _encode_sharing_url(url.strip())
    item = await client.get(f"/shares/{token}/driveItem")
    _check_shared_item(item)
    return item, await client.get_bytes(f"/shares/{token}/driveItem/content")


def get_sharing_link_thumbnail(
    client: GraphClient, url: str, size: str = "medium"
) -> tuple[bytes, str] | None:
    """Fetch a thumbnail for a shared item. None when the item has no thumbnail."""
    _check_thumbnail_size(size)
    token = _encode_sharing_url(url.strip())
    try:
        return client.get_bytes_with_type(f"/shares/{token}/driveItem/thumbnails/0/{size}/content")
    except GraphError as e:
        if e.status_code == 404:
            return None
        raise


async def aget_sharing_link_thumbnail(
    client: AsyncGraphClient, url: str, size: str = "medium"
) -> tuple[bytes, str] | None:
    """Fetch a thumbnail for a shared item (async). None when there is none."""
    _check_thumbnail_size(size)
    token = _encode_sharing_url(url.strip())
    try:
        return await client.get_bytes_with_type(
            f"/shares/{token}/driveItem/thumbnails/0/{size}/content"
        )
    except GraphError as e:
        if e.status_code == 404:
            return None
        raise
