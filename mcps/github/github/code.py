"""
Code and content operations using the GitHub REST API.

Includes file content, code search, and user info.
All functions accept a GitHubClient or AsyncGitHubClient and return parsed dicts.
"""

import base64
from typing import Any, Dict, List, Optional

from .github_client import GitHubClient, AsyncGitHubClient

# GitHub API caps per_page at 100
MAX_PER_PAGE = 100

# Max file size we'll attempt to decode as text (512 KB)
MAX_TEXT_DECODE_BYTES = 524_288

# MIME prefixes and types we consider text-readable
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = frozenset({
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/x-sh", "application/sql",
})

# File extensions considered text-readable (fallback)
_TEXT_EXTENSIONS = frozenset({
    ".txt", ".csv", ".json", ".md", ".py", ".js", ".ts", ".html", ".xml",
    ".yaml", ".yml", ".log", ".cfg", ".ini", ".sh", ".sql", ".java", ".c",
    ".cpp", ".css", ".svg", ".toml", ".tf", ".go", ".rs", ".rb", ".jsx",
    ".tsx", ".vue", ".scss", ".less", ".bat", ".ps1", ".r", ".h", ".hpp",
    ".swift", ".kt", ".gradle", ".properties", ".env", ".gitignore",
    ".dockerfile", ".makefile",
})


def _is_likely_text(name: str) -> bool:
    """Guess whether a file is text-readable based on its extension."""
    dot_idx = name.rfind(".")
    if dot_idx >= 0:
        ext = name[dot_idx:].lower()
        return ext in _TEXT_EXTENSIONS
    return False


def _decode_file_content(data: dict) -> dict:
    """Decode base64 file content if the file is likely text. Adds decoded_content key."""
    if not isinstance(data, dict):
        return data
    if data.get("encoding") != "base64" or not data.get("content"):
        return data

    size = data.get("size", 0)
    name = data.get("name", "")

    # Skip decode for files that are too large
    if size > MAX_TEXT_DECODE_BYTES:
        data["decoded_content"] = None
        data["_decode_note"] = f"File too large to decode ({size} bytes, limit {MAX_TEXT_DECODE_BYTES})"
        return data

    raw = base64.b64decode(data["content"])

    # Try UTF-8 decode; if it fails, the file is binary
    try:
        data["decoded_content"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not a text file — don't silently corrupt with replacement chars
        data["decoded_content"] = None
        data["_decode_note"] = "Binary file — cannot decode as text"

    return data


def _cap_per_page(per_page: int) -> int:
    """Cap per_page to GitHub API maximum of 100."""
    return min(max(per_page, 1), MAX_PER_PAGE)


# ---------------------------------------------------------------------------
# Synchronous
# ---------------------------------------------------------------------------

def get_file_content(
    client: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
) -> Dict[str, Any]:
    """Get a file's content from a repository. Returns decoded content for text files."""
    params = {}
    if ref:
        params["ref"] = ref
    data = client.get(f"/repos/{owner}/{repo}/contents/{path}", params=params or None)
    return _decode_file_content(data)


def create_or_update_file(
    client: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    sha: str = "",
    branch: str = "",
) -> Dict[str, Any]:
    """Create or update a file in a repository."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: Dict[str, Any] = {
        "message": message,
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha
    if branch:
        payload["branch"] = branch
    return client.put(f"/repos/{owner}/{repo}/contents/{path}", json_data=payload)


def search_code(
    client: GitHubClient,
    query: str,
    per_page: int = 10,
) -> List[Dict[str, Any]]:
    """Search code across GitHub repositories."""
    data = client.get(
        "/search/code",
        params={"q": query, "per_page": _cap_per_page(per_page)},
    )
    return data.get("items", [])


def get_authenticated_user(client: GitHubClient) -> Dict[str, Any]:
    """Get the authenticated user's profile."""
    return client.get("/user")


# ---------------------------------------------------------------------------
# Asynchronous
# ---------------------------------------------------------------------------

async def aget_file_content(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
) -> Dict[str, Any]:
    """Get a file's content from a repository (async). Returns decoded content for text files."""
    params = {}
    if ref:
        params["ref"] = ref
    data = await client.get(f"/repos/{owner}/{repo}/contents/{path}", params=params or None)
    return _decode_file_content(data)


async def acreate_or_update_file(
    client: AsyncGitHubClient,
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    sha: str = "",
    branch: str = "",
) -> Dict[str, Any]:
    """Create or update a file in a repository (async)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: Dict[str, Any] = {
        "message": message,
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha
    if branch:
        payload["branch"] = branch
    return await client.put(f"/repos/{owner}/{repo}/contents/{path}", json_data=payload)


async def asearch_code(
    client: AsyncGitHubClient,
    query: str,
    per_page: int = 10,
) -> List[Dict[str, Any]]:
    """Search code across GitHub repositories (async)."""
    data = await client.get(
        "/search/code",
        params={"q": query, "per_page": _cap_per_page(per_page)},
    )
    return data.get("items", [])


async def aget_authenticated_user(client: AsyncGitHubClient) -> Dict[str, Any]:
    """Get the authenticated user's profile (async)."""
    return await client.get("/user")
