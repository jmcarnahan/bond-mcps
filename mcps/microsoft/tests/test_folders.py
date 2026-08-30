# ABOUTME: Unit tests for ms_graph/folders.py — covers list, get, create, rename, move, delete, and URL-encoding.
# ABOUTME: All HTTP calls are mocked via respx; no real Graph API calls are made.
"""Tests for mail folder operations (async) against /me/mailFolders."""

import json
from urllib.parse import quote

import httpx
import pytest
import respx
from ms_graph import folders
from ms_graph.folders import FolderNotFoundError
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphError

from .conftest import (
    GRAPH_ERROR_403,
    GRAPH_ERROR_404,
    SAMPLE_MAIL_FOLDER,
    SAMPLE_MAIL_FOLDERS_RESPONSE,
)

_FOLDERS_URL = f"{GRAPH_BASE_URL}/me/mailFolders"


class TestListFolders:
    """Listing top-level and child folders."""

    @respx.mock
    async def test_list_top_level_returns_folders(self):
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await folders.alist_folders(client)

        assert route.called
        assert len(result) == 2
        assert result[0]["displayName"] == "Projects"

    @respx.mock
    async def test_list_requests_folder_metadata_fields(self):
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await folders.alist_folders(client)

        select = route.calls[0].request.url.params["$select"]
        assert "displayName" in select
        assert "totalItemCount" in select
        assert "childFolderCount" in select

    @respx.mock
    async def test_list_child_folders_uses_parent_path(self):
        parent_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.get(f"{_FOLDERS_URL}/{parent_id}/childFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await folders.alist_folders(client, parent_id=parent_id)

        assert route.called
        assert len(result) == 2

    @respx.mock
    async def test_list_include_hidden_adds_query_param(self):
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await folders.alist_folders(client, include_hidden=True)

        assert route.calls[0].request.url.params["includeHiddenFolders"] == "true"

    @respx.mock
    async def test_list_empty_returns_empty_list(self):
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        async with AsyncGraphClient("tok") as client:
            result = await folders.alist_folders(client)

        assert result == []

    @respx.mock
    async def test_list_propagates_graph_error(self):
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.alist_folders(client)

        assert exc_info.value.status_code == 403


class TestGetFolder:
    """Fetching a single folder's details."""

    @respx.mock
    async def test_get_returns_folder(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.get(f"{_FOLDERS_URL}/{folder_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDER)
        )
        async with AsyncGraphClient("tok") as client:
            folder = await folders.aget_folder(client, folder_id)

        assert route.called
        assert folder["id"] == folder_id
        assert folder["displayName"] == "Projects"

    @respx.mock
    async def test_get_well_known_name(self):
        route = respx.get(f"{_FOLDERS_URL}/inbox").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "displayName": "Inbox"})
        )
        async with AsyncGraphClient("tok") as client:
            folder = await folders.aget_folder(client, "inbox")

        assert route.called
        assert folder["displayName"] == "Inbox"

    @respx.mock
    async def test_get_propagates_graph_error(self):
        respx.get(f"{_FOLDERS_URL}/bad-id").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.aget_folder(client, "bad-id")

        assert exc_info.value.status_code == 404


class TestCreateFolder:
    """Creating top-level and child folders."""

    @respx.mock
    async def test_create_top_level_posts_display_name(self):
        route = respx.post(_FOLDERS_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_MAIL_FOLDER)
        )
        async with AsyncGraphClient("tok") as client:
            created = await folders.acreate_folder(client, "Projects")

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"displayName": "Projects"}
        assert created["id"] == SAMPLE_MAIL_FOLDER["id"]

    @respx.mock
    async def test_create_child_folder_uses_parent_path(self):
        parent_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.post(f"{_FOLDERS_URL}/{parent_id}/childFolders").mock(
            return_value=httpx.Response(201, json=SAMPLE_MAIL_FOLDER)
        )
        async with AsyncGraphClient("tok") as client:
            await folders.acreate_folder(client, "Sub", parent_id=parent_id)

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"displayName": "Sub"}

    @respx.mock
    async def test_create_propagates_graph_error(self):
        respx.post(_FOLDERS_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.acreate_folder(client, "x")

        assert exc_info.value.status_code == 403


class TestRenameFolder:
    """Renaming a folder via PATCH."""

    @respx.mock
    async def test_rename_patches_display_name_and_returns_folder(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.patch(f"{_FOLDERS_URL}/{folder_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "displayName": "Renamed"})
        )
        async with AsyncGraphClient("tok") as client:
            updated = await folders.arename_folder(client, folder_id, "Renamed")

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"displayName": "Renamed"}
        assert updated["displayName"] == "Renamed"

    @respx.mock
    async def test_rename_propagates_graph_error(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.patch(f"{_FOLDERS_URL}/{folder_id}").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.arename_folder(client, folder_id, "x")

        assert exc_info.value.status_code == 404


class TestDeleteFolder:
    """Deleting a folder."""

    @respx.mock
    async def test_delete_calls_endpoint_and_returns_none(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.delete(f"{_FOLDERS_URL}/{folder_id}").mock(return_value=httpx.Response(204))
        async with AsyncGraphClient("tok") as client:
            result = await folders.adelete_folder(client, folder_id)

        assert route.called
        assert result is None

    @respx.mock
    async def test_delete_propagates_graph_error(self):
        respx.delete(f"{_FOLDERS_URL}/bad-id").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.adelete_folder(client, "bad-id")

        assert exc_info.value.status_code == 404


class TestMoveFolder:
    """Moving a folder under a new parent."""

    @respx.mock
    async def test_move_posts_destination_and_returns_folder(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        dest_id = "AQMkAGfolder-002"
        route = respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "parentFolderId": dest_id})
        )
        async with AsyncGraphClient("tok") as client:
            moved = await folders.amove_folder(client, folder_id, dest_id)

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"destinationId": dest_id}
        assert moved["parentFolderId"] == dest_id

    @respx.mock
    async def test_move_accepts_well_known_destination(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        route = respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDER)
        )
        async with AsyncGraphClient("tok") as client:
            await folders.amove_folder(client, folder_id, "archive")

        payload = json.loads(route.calls[0].request.content)
        assert payload == {"destinationId": "archive"}

    @respx.mock
    async def test_move_propagates_graph_error(self):
        folder_id = SAMPLE_MAIL_FOLDER["id"]
        respx.post(f"{_FOLDERS_URL}/{folder_id}/move").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await folders.amove_folder(client, folder_id, "inbox")

        assert exc_info.value.status_code == 404


class TestFolderIdEncoding:
    """Real folder IDs contain reserved chars and must be percent-encoded."""

    @respx.mock
    async def test_get_url_encodes_special_characters(self):
        folder_id = "AQ/folder+001="
        encoded_id = quote(folder_id, safe="")
        respx.get(f"{_FOLDERS_URL}/{encoded_id}").mock(
            return_value=httpx.Response(200, json={**SAMPLE_MAIL_FOLDER, "id": folder_id})
        )
        async with AsyncGraphClient("tok") as client:
            folder = await folders.aget_folder(client, folder_id)

        assert folder["id"] == folder_id

    @respx.mock
    async def test_child_folder_path_url_encodes_parent_id(self):
        """Listing/creating under a parent encodes reserved chars in the parent ID too."""
        parent_id = "AQ/parent+001="
        encoded_id = quote(parent_id, safe="")
        route = respx.get(f"{_FOLDERS_URL}/{encoded_id}/childFolders").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            result = await folders.alist_folders(client, parent_id=parent_id)

        assert route.called
        assert len(result) == 2


class TestResolveFolderId:
    """Resolving a folder display name to a real folder ID (issue #54)."""

    @respx.mock
    async def test_well_known_name_passes_through_without_http(self):
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "inbox")

        assert resolved == "inbox"
        assert not route.called

    @respx.mock
    async def test_well_known_name_is_case_insensitive(self):
        route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "Inbox")

        assert resolved == "inbox"
        assert not route.called

    @respx.mock
    async def test_real_id_passes_through_when_no_name_matches(self):
        """A genuine folder ID (matching no display name) is returned unchanged (#54)."""
        respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        real_id = "AAMkAGI2TG93AAA=" + "x" * 40  # long, contains '=', matches no name
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, real_id)

        assert resolved == real_id

    @respx.mock
    async def test_id_like_display_name_resolves_not_sent_verbatim(self):
        """A real folder whose NAME looks like an ID resolves via the list, not 400 (#54).

        This is the regression guard for the exact power-user names in the issue
        (e.g. 'everything_except_ELT/2024') — long, space-free, containing '/'/'+'/'='.
        Resolving must be tried BEFORE falling back to treating the value as an ID.
        """
        name = "everything_except_ELT_and_partner_updates/2024=="
        folder = {**SAMPLE_MAIL_FOLDER, "id": "AQMkAG-eee", "displayName": name}
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(200, json={"value": [folder]}))
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, name)

        assert resolved == "AQMkAG-eee"

    @respx.mock
    async def test_long_display_name_with_spaces_still_resolves(self):
        """A long display name with spaces and reserved chars resolves to its ID (#54)."""
        name = "Q3/Q4 Budget & Review == Archived Threads (all teams)"
        folder = {**SAMPLE_MAIL_FOLDER, "id": "AQMkAG-q3q4", "displayName": name}
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(200, json={"value": [folder]}))
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, name)

        assert resolved == "AQMkAG-q3q4"

    @respx.mock
    async def test_display_name_resolves_to_id(self):
        respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "Projects")

        assert resolved == SAMPLE_MAIL_FOLDER["id"]

    @respx.mock
    async def test_resolves_folder_beyond_first_page(self):
        """A mailbox with >100 top-level folders still resolves a folder on page 2 (#54).

        Guards against silently capping resolution at Graph's default page size,
        which would make a real folder look 'not found'.
        """
        page1 = {
            "value": [{"id": f"id-{i}", "displayName": f"Folder{i}"} for i in range(100)],
            "@odata.nextLink": f"{_FOLDERS_URL}?$skip=100",
        }
        page2 = {"value": [{"id": "id-149", "displayName": "Folder149"}]}
        responses = iter([httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
        respx.get(_FOLDERS_URL).mock(side_effect=lambda req: next(responses))
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "Folder149")

        assert resolved == "id-149"

    @respx.mock
    async def test_display_name_match_is_case_insensitive(self):
        respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "pROJECTS")

        assert resolved == SAMPLE_MAIL_FOLDER["id"]

    @respx.mock
    async def test_display_name_with_apostrophe_matches(self):
        """A name with a single quote resolves fine — no $filter escaping needed."""
        folder = {**SAMPLE_MAIL_FOLDER, "id": "AQMkAG-bob", "displayName": "Bob's Stuff"}
        respx.get(_FOLDERS_URL).mock(return_value=httpx.Response(200, json={"value": [folder]}))
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "bob's stuff")

        assert resolved == "AQMkAG-bob"

    @respx.mock
    async def test_unknown_name_raises_folder_not_found(self):
        respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(FolderNotFoundError) as exc_info:
                await folders.aresolve_folder_id(client, "ghost")

        assert exc_info.value.name == "ghost"
        assert str(exc_info.value) == "Folder 'ghost' not found."

    @respx.mock
    async def test_resolves_against_shared_mailbox_path(self):
        """A shared mailbox resolves against /users/{mailbox}, not /me."""
        mailbox = "shared@example.com"
        shared_url = f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders"
        me_route = respx.get(_FOLDERS_URL).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        shared_route = respx.get(shared_url).mock(
            return_value=httpx.Response(200, json=SAMPLE_MAIL_FOLDERS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            resolved = await folders.aresolve_folder_id(client, "Projects", mailbox=mailbox)

        assert resolved == SAMPLE_MAIL_FOLDER["id"]
        assert shared_route.called
        assert not me_route.called
