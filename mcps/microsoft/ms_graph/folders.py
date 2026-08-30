# ABOUTME: Async mail folder operations (list, get, create, rename, move, delete) via Graph mailFolders API.
# ABOUTME: All functions accept an AsyncGraphClient; well-known names (inbox, drafts, ...) work anywhere a folder_id is accepted.
"""
Mail folder operations using the Microsoft Graph API.

Folders in Outlook map to Graph ``mailFolders``. All functions accept an
AsyncGraphClient and return parsed dicts. Well-known folder names (inbox,
sentitems, drafts, deleteditems, ...) work anywhere a folder_id is accepted.
"""

from typing import Any
from urllib.parse import quote

from .graph_client import AsyncGraphClient
from .pagination import apaginate

# Fields worth returning for a folder listing/detail. childFolderCount tells the
# caller whether a folder has nested folders worth drilling into.
_FOLDER_SELECT = "id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount"

# Upper bound on top-level folders to scan when resolving a display name. Set
# well above any realistic top-level folder count so resolution never silently
# misses a folder (which would look like "folder not found" for a real folder).
_RESOLVE_FOLDER_SCAN_LIMIT = 1000

# Graph's documented well-known folder names. These are accepted as literal
# folder IDs in any mailFolders path, so they never need display-name resolution.
_WELL_KNOWN_FOLDERS = frozenset(
    {
        "inbox",
        "drafts",
        "sentitems",
        "deleteditems",
        "junkemail",
        "outbox",
        "archive",
        "clutter",
        "conversationhistory",
        "recoverableitemsdeletions",
        "scheduled",
        "searchfolders",
        "syncissues",
        "msgfolderroot",
        "archivemsgfolderroot",
    }
)


class FolderNotFoundError(Exception):
    """Raised when a folder display name cannot be resolved to a folder ID."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Folder '{name}' not found.")


def _mail_base(mailbox: str | None) -> str:
    """Graph API path prefix: /users/{mailbox} for shared, /me for own."""
    if mailbox:
        return f"/users/{quote(mailbox, safe='@')}"
    return "/me"


def _folder_path(folder_id: str, mailbox: str | None = None) -> str:
    """Build the mailFolders path for a folder, percent-encoding the ID.

    Real folder IDs contain reserved characters (=, /, +); well-known names
    (inbox, drafts, ...) contain none, so encoding is safe for both.
    """
    return f"{_mail_base(mailbox)}/mailFolders/{quote(folder_id, safe='')}"


async def alist_folders(
    client: AsyncGraphClient,
    parent_id: str | None = None,
    top: int = 100,
    include_hidden: bool = False,
    mailbox: str | None = None,
) -> list[dict[str, Any]]:
    """List mail folders, paginating past Graph's default page size (async).

    With ``parent_id`` set, lists that folder's child folders; otherwise lists
    the top-level folders. ``include_hidden`` surfaces folders Outlook hides by
    default (e.g. Conversation History internals). ``mailbox`` targets a shared
    mailbox (``/users/{mailbox}``) instead of the signed-in user (``/me``).
    """
    base = _mail_base(mailbox)
    path = (
        f"{_folder_path(parent_id, mailbox)}/childFolders" if parent_id else f"{base}/mailFolders"
    )
    params: dict[str, Any] = {"$select": _FOLDER_SELECT}
    if include_hidden:
        params["includeHiddenFolders"] = "true"
    return await apaginate(client, path, params, top, page_size=100)


async def aresolve_folder_id(
    client: AsyncGraphClient,
    name_or_id: str,
    mailbox: str | None = None,
) -> str:
    """Resolve a folder display name to a real folder ID (async).

    Resolution order:
    1. Well-known names (inbox, sentitems, ...) pass through with no network call.
    2. Otherwise the value is matched case-insensitively against the top-level
       folder display names; the first match's ID is returned. (If two folders
       share a display name, Graph's listing order decides which one wins.)
    3. If no display name matches, the value is assumed to already be a real
       folder ID and returned unchanged, so callers can pass an ID directly.

    Matching display names *before* falling back to a literal ID is deliberate:
    a real folder whose name happens to look ID-like (long, containing '/', '+',
    or '=') must still resolve rather than be sent to Graph verbatim (which would
    400). A genuine ID simply won't match any display name and falls through.

    Nested (child) folders are not searched.
    """
    name = name_or_id.strip()
    if name.lower() in _WELL_KNOWN_FOLDERS:
        return name.lower()

    target = name.casefold()
    # Scan well past Graph's default page so mailboxes with many top-level
    # folders still resolve; otherwise a real folder past page 1 looks missing.
    for folder in await alist_folders(client, top=_RESOLVE_FOLDER_SCAN_LIMIT, mailbox=mailbox):
        if folder.get("displayName", "").casefold() == target:
            return folder["id"]

    # No display-name match: treat the value as a real folder ID if it plausibly
    # is one; otherwise it's an unknown name and the caller gets a clean error.
    if _looks_like_folder_id(name):
        return name
    raise FolderNotFoundError(name)


def _looks_like_folder_id(value: str) -> bool:
    """Heuristic: does an unresolved value plausibly look like a real folder ID?

    Only consulted *after* display-name resolution fails, to decide between
    "assume it's already an ID" and "raise not-found". Real Graph mailFolder IDs
    are long, base64-ish tokens (contain =, /, or +) with no spaces.
    """
    return len(value) >= 40 and " " not in value and any(c in value for c in "=/+")


async def aget_folder(client: AsyncGraphClient, folder_id: str) -> dict[str, Any]:
    """Get a single mail folder by ID or well-known name (async)."""
    return await client.get(_folder_path(folder_id), params={"$select": _FOLDER_SELECT})


async def acreate_folder(
    client: AsyncGraphClient,
    display_name: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Create a mail folder and return it (async).

    With ``parent_id`` set, creates a child folder under it; otherwise creates a
    top-level folder.
    """
    path = f"{_folder_path(parent_id)}/childFolders" if parent_id else "/me/mailFolders"
    return await client.post(path, json_data={"displayName": display_name})


async def arename_folder(
    client: AsyncGraphClient, folder_id: str, display_name: str
) -> dict[str, Any]:
    """Rename a mail folder and return the updated folder (async)."""
    return await client.patch(_folder_path(folder_id), json_data={"displayName": display_name})


async def amove_folder(
    client: AsyncGraphClient, folder_id: str, destination_id: str
) -> dict[str, Any]:
    """Move a folder under a new parent and return the updated folder (async).

    ``destination_id`` can be a real folder ID or a well-known name (inbox,
    sentitems, drafts, deleteditems, archive, ...).
    """
    return await client.post(
        f"{_folder_path(folder_id)}/move", json_data={"destinationId": destination_id}
    )


async def adelete_folder(client: AsyncGraphClient, folder_id: str) -> None:
    """Delete a mail folder by ID (async).

    Graph moves the folder (and its contents) to Deleted Items.
    """
    await client.delete(_folder_path(folder_id))
