"""Tests for attachment operations and the content-transport layer."""

import base64
import json
from urllib.parse import quote

import httpx
import pytest
import respx
from ms_graph import attachments, document_create, files, mail_policy
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError

from .conftest import (
    SAMPLE_ATTACHMENT_LOCATION,
    SAMPLE_ATTACHMENT_UPLOAD_SESSION,
    SAMPLE_ATTACHMENT_UPLOAD_URL,
    SAMPLE_ATTACHMENTS_NEXT_LINK,
    SAMPLE_ATTACHMENTS_PAGE_FINAL,
    SAMPLE_ATTACHMENTS_PAGE_NEXT,
    SAMPLE_ATTACHMENTS_RESPONSE,
    SAMPLE_AWKWARD_MESSAGE_ID,
    SAMPLE_CREATED_ATTACHMENT,
    SAMPLE_DRIVE_ITEM_FILE,
    SAMPLE_DRIVE_ITEM_FOLDER,
    SAMPLE_FILE_ATTACHMENT,
    SAMPLE_INLINE_ATTACHMENT,
    SAMPLE_ITEM_ATTACHMENT,
    SAMPLE_MESSAGE,
    SAMPLE_REFERENCE_ATTACHMENT,
    SAMPLE_SENDER_ONLY_EXTERNAL,
    SAMPLE_SENDER_ONLY_INTERNAL,
    SAMPLE_SHARED_TEXT_FILE,
    SAMPLE_UPLOADED_FILE,
)

MSG_ID = SAMPLE_MESSAGE["id"]
ATTACHMENTS_URL = f"{GRAPH_BASE_URL}/me/messages/{quote(MSG_ID, safe='')}/attachments"
FILE_ATTACHMENT_URL = f"{ATTACHMENTS_URL}/{quote(SAMPLE_FILE_ATTACHMENT['id'], safe='')}"
SHARING_URL = "https://contoso.sharepoint.com/:x:/s/team/EaB123-xyz"


def _docx_bytes() -> bytes:
    """A real docx so the text sink exercises the extractor, not a stub."""
    return document_create.markdown_to_docx("# Title\n\nHello")


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


class TestAttachmentSummary:
    """attachment_summary normalizes every @odata.type Graph can return."""

    def test_file_attachment(self):
        summary = attachments.attachment_summary(SAMPLE_FILE_ATTACHMENT)
        assert summary == {
            "id": "AAMkAttachFile001=",
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size": 1_258_291,
            "is_inline": False,
            "content_id": None,
            "kind": "file",
            "source_url": None,
        }

    def test_inline_attachment_keeps_content_id(self):
        summary = attachments.attachment_summary(SAMPLE_INLINE_ATTACHMENT)
        assert summary["is_inline"] is True
        assert summary["content_id"] == "logo@company"

    def test_item_attachment(self):
        summary = attachments.attachment_summary(SAMPLE_ITEM_ATTACHMENT)
        assert summary["kind"] == "item"
        assert summary["content_type"] == ""

    def test_reference_attachment_carries_source_url(self):
        summary = attachments.attachment_summary(SAMPLE_REFERENCE_ATTACHMENT)
        assert summary["kind"] == "reference"
        assert summary["source_url"] == "https://contoso.sharepoint.com/:w:/s/team/Q4Plan"

    def test_unknown_odata_type(self):
        summary = attachments.attachment_summary({"@odata.type": "#microsoft.graph.somethingNew"})
        assert summary["kind"] == "unknown"

    def test_missing_type_and_size(self):
        summary = attachments.attachment_summary({"id": "x"})
        assert summary["kind"] == "unknown"
        assert summary["size"] == 0

    def test_non_integer_size_becomes_zero(self):
        assert attachments.attachment_summary({"size": "1024"})["size"] == 0


class TestGuessContentType:
    """guess_content_type covers the extensions mimetypes forgets."""

    def test_markdown(self):
        assert attachments.guess_content_type("notes.md") == "text/markdown"

    def test_known_extension(self):
        assert attachments.guess_content_type("report.pdf") == "application/pdf"

    def test_unknown_extension_uses_fallback(self):
        assert attachments.guess_content_type("blob.zzz") == "application/octet-stream"
        assert attachments.guess_content_type("blob.zzz", "text/plain") == "text/plain"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestListAttachmentsAsync:
    """Listing selects metadata only and follows paging."""

    @respx.mock
    async def test_selects_metadata_and_never_content_bytes(self):
        route = respx.get(url__startswith=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            items = await attachments.alist_message_attachments(client, MSG_ID)

        assert len(items) == 3
        query = str(route.calls[0].request.url)
        assert "contentId" in query
        assert "contentBytes" not in query

    @respx.mock
    async def test_follows_next_link(self):
        # The nextLink shares the collection's path, so it is registered first:
        # respx hands a request to the first route that matches it.
        next_page = respx.get(SAMPLE_ATTACHMENTS_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_FINAL)
        )
        respx.get(url=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        async with AsyncGraphClient("tok") as client:
            items = await attachments.alist_message_attachments(client, MSG_ID)

        assert [item["id"] for item in items] == [
            SAMPLE_FILE_ATTACHMENT["id"],
            SAMPLE_REFERENCE_ATTACHMENT["id"],
        ]
        assert next_page.call_count == 1

    @respx.mock
    async def test_page_cap_bounds_the_requests(self, monkeypatch):
        """At the cap the walk stops without fetching a page it would throw away."""
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_PAGES", 2)
        # Every page points onward; only the cap ends the walk.
        next_page = respx.get(SAMPLE_ATTACHMENTS_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        respx.get(url=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        async with AsyncGraphClient("tok") as client:
            items = await attachments.alist_message_attachments(client, MSG_ID)

        assert next_page.call_count == 1
        assert len(items) == 2 * len(SAMPLE_ATTACHMENTS_PAGE_NEXT["value"])

    @respx.mock
    def test_sync_page_cap_bounds_the_requests(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_PAGES", 2)
        next_page = respx.get(SAMPLE_ATTACHMENTS_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        respx.get(url=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        with GraphClient("tok") as client:
            items = attachments.list_message_attachments(client, MSG_ID)

        assert next_page.call_count == 1
        assert len(items) == 2 * len(SAMPLE_ATTACHMENTS_PAGE_NEXT["value"])

    @respx.mock
    async def test_shared_mailbox_routes_through_users(self):
        route = respx.get(
            url__startswith=f"{GRAPH_BASE_URL}/users/shared@example.com/messages/"
        ).mock(return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_RESPONSE))
        async with AsyncGraphClient("tok") as client:
            await attachments.alist_message_attachments(
                client, MSG_ID, mailbox="shared@example.com"
            )

        assert route.called

    @respx.mock
    async def test_awkward_message_id_is_encoded(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await attachments.alist_message_attachments(client, SAMPLE_AWKWARD_MESSAGE_ID)

        assert "/me/messages/AAMkA%2FGI2%2BTG93AAA%3D/attachments" in str(
            route.calls[0].request.url
        )


class TestListAttachmentsSync:
    """Synchronous listing twin."""

    @respx.mock
    def test_returns_every_attachment(self):
        route = respx.get(url__startswith=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_RESPONSE)
        )
        with GraphClient("tok") as client:
            items = attachments.list_message_attachments(client, MSG_ID)

        assert len(items) == 3
        assert "contentBytes" not in str(route.calls[0].request.url)

    @respx.mock
    def test_follows_next_link(self):
        respx.get(SAMPLE_ATTACHMENTS_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_FINAL)
        )
        respx.get(url=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_PAGE_NEXT)
        )
        with GraphClient("tok") as client:
            assert len(attachments.list_message_attachments(client, MSG_ID)) == 2


class TestAttachmentContent:
    """Metadata, raw bytes, and expanded item attachments."""

    @respx.mock
    async def test_metadata_uses_select(self):
        route = respx.get(url__startswith=FILE_ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        async with AsyncGraphClient("tok") as client:
            data = await attachments.aget_attachment_metadata(
                client, MSG_ID, SAMPLE_FILE_ATTACHMENT["id"]
            )

        assert data["name"] == "report.pdf"
        assert "contentId" in str(route.calls[0].request.url)

    @respx.mock
    async def test_bytes_return_content_type(self):
        respx.get(f"{FILE_ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            data, content_type = await attachments.aget_attachment_bytes(
                client, MSG_ID, SAMPLE_FILE_ATTACHMENT["id"]
            )

        assert (data, content_type) == (b"%PDF-1.7", "application/pdf")

    @respx.mock
    def test_sync_bytes_return_content_type(self):
        respx.get(f"{FILE_ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
            )
        )
        with GraphClient("tok") as client:
            assert attachments.get_attachment_bytes(
                client, MSG_ID, SAMPLE_FILE_ATTACHMENT["id"]
            ) == (b"%PDF-1.7", "application/pdf")

    @respx.mock
    async def test_item_attachment_expands_the_inner_item(self):
        item_url = f"{ATTACHMENTS_URL}/{quote(SAMPLE_ITEM_ATTACHMENT['id'], safe='')}"
        route = respx.get(url__startswith=item_url).mock(
            return_value=httpx.Response(
                200, json={**SAMPLE_ITEM_ATTACHMENT, "item": {"subject": "Budget"}}
            )
        )
        async with AsyncGraphClient("tok") as client:
            data = await attachments.aget_item_attachment(
                client, MSG_ID, SAMPLE_ITEM_ATTACHMENT["id"]
            )

        assert data["item"]["subject"] == "Budget"
        assert "expand" in str(route.calls[0].request.url)
        assert "microsoft.graph.itemattachment" in str(route.calls[0].request.url)

    @respx.mock
    def test_sync_item_attachment(self):
        item_url = f"{ATTACHMENTS_URL}/{quote(SAMPLE_ITEM_ATTACHMENT['id'], safe='')}"
        respx.get(url__startswith=item_url).mock(
            return_value=httpx.Response(200, json=SAMPLE_ITEM_ATTACHMENT)
        )
        with GraphClient("tok") as client:
            assert (
                attachments.get_item_attachment(client, MSG_ID, SAMPLE_ITEM_ATTACHMENT["id"])["id"]
                == SAMPLE_ITEM_ATTACHMENT["id"]
            )

    @respx.mock
    def test_sync_metadata(self):
        respx.get(url__startswith=FILE_ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        with GraphClient("tok") as client:
            assert (
                attachments.get_attachment_metadata(client, MSG_ID, SAMPLE_FILE_ATTACHMENT["id"])[
                    "name"
                ]
                == "report.pdf"
            )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class TestAddFileAttachment:
    """The 3 MB branch is invisible to callers, so both halves are tested here."""

    @respx.mock
    async def test_small_attachment_posts_content_bytes(self):
        route = respx.post(ATTACHMENTS_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        async with AsyncGraphClient("tok") as client:
            attachment_id = await attachments.aadd_file_attachment(
                client, MSG_ID, "notes.txt", b"hello world", "text/plain"
            )

        assert attachment_id == SAMPLE_CREATED_ATTACHMENT["id"]
        payload = json.loads(route.calls[0].request.content)
        assert payload["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert payload["name"] == "notes.txt"
        assert payload["contentType"] == "text/plain"
        assert base64.b64decode(payload["contentBytes"]) == b"hello world"

    @respx.mock
    def test_sync_small_attachment_posts_content_bytes(self):
        route = respx.post(ATTACHMENTS_URL).mock(
            return_value=httpx.Response(201, json=SAMPLE_CREATED_ATTACHMENT)
        )
        with GraphClient("tok") as client:
            attachment_id = attachments.add_file_attachment(
                client, MSG_ID, "notes.txt", b"hello world", "text/plain"
            )

        assert attachment_id == SAMPLE_CREATED_ATTACHMENT["id"]
        assert json.loads(route.calls[0].request.content)["name"] == "notes.txt"

    @respx.mock
    async def test_response_without_id_raises(self):
        respx.post(ATTACHMENTS_URL).mock(return_value=httpx.Response(201, json={}))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await attachments.aadd_file_attachment(
                    client, MSG_ID, "notes.txt", b"hi", "text/plain"
                )

        assert exc.value.error_code == "NoAttachmentId"

    @respx.mock
    async def test_large_attachment_uses_an_upload_session(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_INLINE_ATTACHMENT_BYTES", 8)
        monkeypatch.setattr(files, "UPLOAD_CHUNK_BYTES", 4)
        session = respx.post(f"{ATTACHMENTS_URL}/createUploadSession").mock(
            return_value=httpx.Response(201, json=SAMPLE_ATTACHMENT_UPLOAD_SESSION)
        )
        put = respx.put(url__startswith=SAMPLE_ATTACHMENT_UPLOAD_URL.split("?")[0]).mock(
            side_effect=[
                httpx.Response(200, json={"nextExpectedRanges": ["4-9"]}),
                httpx.Response(200, json={"nextExpectedRanges": ["8-9"]}),
                httpx.Response(201, headers={"Location": SAMPLE_ATTACHMENT_LOCATION}),
            ]
        )
        async with AsyncGraphClient("tok") as client:
            attachment_id = await attachments.aadd_file_attachment(
                client, MSG_ID, "big.bin", b"0123456789", "application/octet-stream"
            )

        assert json.loads(session.calls[0].request.content) == {
            "AttachmentItem": {"attachmentType": "file", "name": "big.bin", "size": 10}
        }
        assert put.call_count == 3
        assert [call.request.headers["content-range"] for call in put.calls] == [
            "bytes 0-3/10",
            "bytes 4-7/10",
            "bytes 8-9/10",
        ]
        assert "authorization" not in put.calls[0].request.headers
        # The id is percent-encoded inside the Location URL.
        assert attachment_id == "AAMkAttachBig006="

    @respx.mock
    async def test_missing_location_raises(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_INLINE_ATTACHMENT_BYTES", 8)
        monkeypatch.setattr(files, "UPLOAD_CHUNK_BYTES", 16)
        respx.post(f"{ATTACHMENTS_URL}/createUploadSession").mock(
            return_value=httpx.Response(201, json=SAMPLE_ATTACHMENT_UPLOAD_SESSION)
        )
        respx.put(url__startswith=SAMPLE_ATTACHMENT_UPLOAD_URL.split("?")[0]).mock(
            return_value=httpx.Response(201)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await attachments.aadd_file_attachment(
                    client, MSG_ID, "big.bin", b"0123456789", "application/octet-stream"
                )

        assert exc.value.error_code == "NoAttachmentId"

    @respx.mock
    async def test_fragment_failure_cancels_the_session(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_INLINE_ATTACHMENT_BYTES", 8)
        monkeypatch.setattr(files, "UPLOAD_CHUNK_BYTES", 4)
        respx.post(f"{ATTACHMENTS_URL}/createUploadSession").mock(
            return_value=httpx.Response(201, json=SAMPLE_ATTACHMENT_UPLOAD_SESSION)
        )
        respx.put(url__startswith=SAMPLE_ATTACHMENT_UPLOAD_URL.split("?")[0]).mock(
            return_value=httpx.Response(500, json={"error": {"code": "ServiceError"}})
        )
        cancel = respx.delete(url__startswith=SAMPLE_ATTACHMENT_UPLOAD_URL.split("?")[0]).mock(
            return_value=httpx.Response(204)
        )

        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await attachments.aadd_file_attachment(
                    client, MSG_ID, "big.bin", b"0123456789", "application/octet-stream"
                )

        assert exc.value.status_code == 500
        assert cancel.call_count == 1

    @respx.mock
    async def test_over_the_hard_limit_makes_no_request(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 4)
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="exceeds"):
                await attachments.aadd_file_attachment(
                    client, MSG_ID, "big.bin", b"01234", "application/octet-stream"
                )

    def test_sync_over_the_hard_limit_message_names_the_limit(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 4)
        with GraphClient("tok") as client:
            with pytest.raises(ValueError) as exc:
                attachments.add_file_attachment(client, MSG_ID, "big.bin", b"01234", "text/plain")

        assert "big.bin" in str(exc.value)


# ---------------------------------------------------------------------------
# Source specs
# ---------------------------------------------------------------------------


class TestResolveSourceSpecs:
    """Every source-spec kind, and every way one can be wrong."""

    async def test_text_spec(self):
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"text": "hello", "name": "notes.txt"}
            )

        assert att == attachments.ResolvedAttachment("notes.txt", b"hello", "text/plain")

    async def test_text_spec_with_docx_name_builds_a_document(self):
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"text": "# Title\n\nHello", "name": "report.docx"}
            )

        assert att.data[:2] == b"PK"
        assert att.content_type.endswith("wordprocessingml.document")

    async def test_text_spec_with_xlsx_name_builds_a_workbook(self):
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"text": "a,b\n1,2", "name": "data.xlsx"}
            )

        assert att.data[:2] == b"PK"
        assert att.content_type.endswith("spreadsheetml.sheet")

    async def test_text_spec_without_name_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="requires a 'name'"):
                await attachments.aresolve_attachment_source(client, {"text": "hello"})

    async def test_base64_spec(self):
        encoded = base64.b64encode(b"binary").decode()
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"base64": encoded, "name": "blob.pdf"}
            )

        assert att.data == b"binary"
        assert att.content_type == "application/pdf"

    async def test_bad_base64_names_the_attachment(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="attachment 'blob.pdf': invalid base64"):
                await attachments.aresolve_attachment_source(
                    client, {"base64": "not base64!!", "name": "blob.pdf"}
                )

    @respx.mock
    async def test_drive_item_spec(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2")
        )
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"drive_item_id": "file-id-001"}
            )

        assert att == attachments.ResolvedAttachment("report.csv", b"a,b\n1,2", "text/csv")

    @respx.mock
    async def test_drive_item_spec_honours_drive_id_and_name(self):
        respx.get(f"{GRAPH_BASE_URL}/drives/drive-9/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/drives/drive-9/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"x")
        )
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client,
                {"drive_item_id": "file-id-001", "drive_id": "drive-9", "name": "renamed.csv"},
            )

        assert att.name == "renamed.csv"

    @respx.mock
    async def test_drive_item_folder_is_refused(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/folder-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FOLDER)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="folder"):
                await attachments.aresolve_attachment_source(
                    client, {"drive_item_id": "folder-id-001"}
                )

    @respx.mock
    async def test_sharing_url_spec(self):
        respx.get(url__regex=r"/shares/u!.*/driveItem$").mock(
            return_value=httpx.Response(200, json=SAMPLE_SHARED_TEXT_FILE)
        )
        respx.get(url__regex=r"/shares/u!.*/driveItem/content$").mock(
            return_value=httpx.Response(200, content=b"# notes")
        )
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(client, {"url": SHARING_URL})

        assert att == attachments.ResolvedAttachment("notes.md", b"# notes", "text/markdown")

    @respx.mock
    async def test_forwarded_attachment_spec(self):
        # $value shares the attachment's path prefix, so it is registered first.
        respx.get(f"{FILE_ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
            )
        )
        respx.get(url__startswith=FILE_ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"message_id": MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
            )

        assert att == attachments.ResolvedAttachment("report.pdf", b"%PDF-1.7", "application/pdf")

    @respx.mock
    async def test_forwarded_reference_attachment_is_refused(self):
        ref_url = f"{ATTACHMENTS_URL}/{quote(SAMPLE_REFERENCE_ATTACHMENT['id'], safe='')}"
        respx.get(url__startswith=ref_url).mock(
            return_value=httpx.Response(200, json=SAMPLE_REFERENCE_ATTACHMENT)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="is a link, not a file"):
                await attachments.aresolve_attachment_source(
                    client,
                    {
                        "message_id": MSG_ID,
                        "attachment_id": SAMPLE_REFERENCE_ATTACHMENT["id"],
                    },
                )

    @respx.mock
    async def test_forwarded_item_attachment_is_refused(self):
        item_url = f"{ATTACHMENTS_URL}/{quote(SAMPLE_ITEM_ATTACHMENT['id'], safe='')}"
        respx.get(url__startswith=item_url).mock(
            return_value=httpx.Response(200, json=SAMPLE_ITEM_ATTACHMENT)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="forward the original message"):
                await attachments.aresolve_attachment_source(
                    client,
                    {"message_id": MSG_ID, "attachment_id": SAMPLE_ITEM_ATTACHMENT["id"]},
                )

    async def test_message_id_without_attachment_id_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="attachment_id"):
                await attachments.aresolve_attachment_source(client, {"message_id": MSG_ID})

    async def test_no_source_key_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="exactly one of"):
                await attachments.aresolve_attachment_source(client, {"name": "notes.txt"})

    async def test_two_source_keys_are_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="exactly one of"):
                await attachments.aresolve_attachment_source(
                    client, {"text": "hi", "base64": "aGk=", "name": "notes.txt"}
                )

    async def test_non_dict_spec_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="JSON object"):
                await attachments.aresolve_attachment_source(client, "notes.txt")

    async def test_content_type_override_wins(self):
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client,
                {"text": "id,name", "name": "data.txt", "content_type": "text/csv"},
            )

        assert att.content_type == "text/csv"

    async def test_non_string_content_type_override_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="content_type"):
                await attachments.aresolve_attachment_source(
                    client, {"text": "id,name", "name": "data.txt", "content_type": 7}
                )

    async def test_over_the_hard_limit_is_refused(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 4)
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="exceeds"):
                await attachments.aresolve_attachment_source(
                    client, {"text": "far too long", "name": "notes.txt"}
                )

    def test_sync_text_spec(self):
        with GraphClient("tok") as client:
            att = attachments.resolve_attachment_source(
                client, {"text": "hello", "name": "notes.txt"}
            )

        assert att.data == b"hello"

    @respx.mock
    def test_sync_drive_item_spec(self):
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json=SAMPLE_DRIVE_ITEM_FILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"a,b")
        )
        with GraphClient("tok") as client:
            att = attachments.resolve_attachment_source(client, {"drive_item_id": "file-id-001"})

        assert att.name == "report.csv"

    @respx.mock
    def test_sync_forwarded_attachment_spec(self):
        respx.get(f"{FILE_ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"Content-Type": "application/pdf"}
            )
        )
        respx.get(url__startswith=FILE_ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        with GraphClient("tok") as client:
            att = attachments.resolve_attachment_source(
                client, {"message_id": MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
            )

        assert att.data == b"%PDF"


class TestForwardSpecMailPolicy:
    """Forwarding an attachment re-originates it, so the source is judged first."""

    def _routes(self):
        """Attachment routes first, so the sender-check route cannot shadow them."""
        attachment_route = respx.get(url__startswith=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        message_route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        return attachment_route, message_route

    @respx.mock
    async def test_external_source_message_is_refused(self, monkeypatch):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        attachment_route, message_route = self._routes()
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="outside the allowed domains"):
                await attachments.aresolve_attachment_source(
                    client, {"message_id": MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
                )

        assert message_route.called
        assert not attachment_route.called

    @respx.mock
    def test_sync_external_source_message_is_refused(self, monkeypatch):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        attachment_route, message_route = self._routes()
        with GraphClient("tok") as client:
            with pytest.raises(ValueError, match="outside the allowed domains"):
                attachments.resolve_attachment_source(
                    client, {"message_id": MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
                )

        assert message_route.called
        assert not attachment_route.called

    @respx.mock
    async def test_spec_mailbox_is_the_mailbox_that_is_checked(self, monkeypatch):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/users/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="outside the allowed domains"):
                await attachments.aresolve_attachment_source(
                    client,
                    {
                        "message_id": MSG_ID,
                        "attachment_id": SAMPLE_FILE_ATTACHMENT["id"],
                        "mailbox": "support@example.com",
                    },
                )

        assert route.calls[0].request.url.path.startswith(
            "/v1.0/users/support@example.com/messages/"
        )

    @respx.mock
    async def test_internal_source_message_resolves_normally(self, monkeypatch):
        monkeypatch.setenv(mail_policy.ENV_ALLOWED_SENDER_DOMAINS, "example.com")
        respx.get(f"{FILE_ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
            )
        )
        respx.get(url__startswith=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        async with AsyncGraphClient("tok") as client:
            att = await attachments.aresolve_attachment_source(
                client, {"message_id": MSG_ID, "attachment_id": SAMPLE_FILE_ATTACHMENT["id"]}
            )

        assert att == attachments.ResolvedAttachment("report.pdf", b"%PDF-1.7", "application/pdf")


class TestResolveSourceLists:
    """A list of specs resolves in order and errors point at the bad entry."""

    async def test_resolves_in_order(self):
        async with AsyncGraphClient("tok") as client:
            resolved = await attachments.aresolve_attachment_sources(
                client,
                [
                    {"text": "one", "name": "a.txt"},
                    {"base64": base64.b64encode(b"two").decode(), "name": "b.bin"},
                ],
            )

        assert [att.name for att in resolved] == ["a.txt", "b.bin"]
        assert [att.data for att in resolved] == [b"one", b"two"]

    async def test_non_list_is_refused(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="JSON array of objects"):
                await attachments.aresolve_attachment_sources(client, {"text": "hi"})

    async def test_error_names_the_index(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match=r"attachments\[1\]: attachment spec"):
                await attachments.aresolve_attachment_sources(
                    client, [{"text": "one", "name": "a.txt"}, {"name": "b.txt"}]
                )

    def test_sync_resolves_in_order(self):
        with GraphClient("tok") as client:
            resolved = attachments.resolve_attachment_sources(
                client, [{"text": "one", "name": "a.txt"}]
            )

        assert resolved[0].name == "a.txt"

    def test_sync_non_list_is_refused(self):
        with GraphClient("tok") as client:
            with pytest.raises(ValueError, match="JSON array of objects"):
                attachments.resolve_attachment_sources(client, "a.txt")


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class TestDeliverText:
    """Text mode extracts documents, decodes text, and refuses binaries."""

    async def test_extracts_a_docx(self):
        att = attachments.ResolvedAttachment(
            "report.docx",
            _docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["mode"] == "text"
        assert result["name"] == "report.docx"
        assert "Title" in result["text"]
        assert result["truncated"] is False

    async def test_decodes_a_csv(self):
        att = attachments.ResolvedAttachment("data.csv", b"a,b\n1,2", "text/csv")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] == "a,b\n1,2"
        assert result["size"] == 7
        assert result["truncated"] is False

    async def test_extension_alone_is_enough_for_text(self):
        att = attachments.ResolvedAttachment("script.py", b"print(1)", "application/octet-stream")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] == "print(1)"

    async def test_binary_reports_a_reason(self):
        att = attachments.ResolvedAttachment("logo.png", b"\x89PNG\r\n", "image/png")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] is None
        assert result["reason"] == "binary"
        assert result["truncated"] is False

    async def test_unextractable_document_reports_unsupported(self):
        att = attachments.ResolvedAttachment("broken.pdf", b"not really a pdf", "application/pdf")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] is None
        assert result["reason"] == "unsupported"

    async def test_long_text_is_truncated(self, monkeypatch):
        monkeypatch.setattr(files, "MAX_TEXT_DOWNLOAD_BYTES", 5)
        att = attachments.ResolvedAttachment("log.txt", b"0123456789", "text/plain")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] == "01234"
        assert result["truncated"] is True

    async def test_non_utf8_text_falls_back_to_latin1(self):
        att = attachments.ResolvedAttachment("note.txt", b"caf\xe9", "text/plain")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] == "café"

    def test_sync_decodes_a_csv(self):
        att = attachments.ResolvedAttachment("data.csv", b"a,b", "text/csv")
        with GraphClient("tok") as client:
            assert attachments.deliver_attachment(client, att, "text")["text"] == "a,b"


class TestDeliverBase64:
    """Base64 mode is capped so bytes never flood an LLM's context."""

    async def test_under_the_cap_returns_base64(self):
        att = attachments.ResolvedAttachment("logo.png", b"\x89PNG", "image/png")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "base64")

        assert base64.b64decode(result["base64"]) == b"\x89PNG"
        assert result["size"] == 4

    async def test_over_the_cap_returns_an_error(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_BASE64_RETURN_BYTES", 3)
        att = attachments.ResolvedAttachment("logo.png", b"\x89PNG", "image/png")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "base64")

        assert result["error"] == "too_large"
        assert result["limit"] == 3
        assert "base64" not in result

    def test_sync_returns_base64(self):
        att = attachments.ResolvedAttachment("a.bin", b"xy", "application/octet-stream")
        with GraphClient("tok") as client:
            assert (
                base64.b64decode(attachments.deliver_attachment(client, att, "base64")["base64"])
                == b"xy"
            )


class TestDeliverOneDrive:
    """OneDrive mode parks the bytes in a folder and hands back the link."""

    @respx.mock
    async def test_uploads_and_returns_item_id_and_web_url(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Attachments/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        att = attachments.ResolvedAttachment("report.pdf", b"%PDF", "application/pdf")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "onedrive")

        assert route.calls[0].request.headers["Content-Type"] == "application/pdf"
        assert result["item_id"] == SAMPLE_UPLOADED_FILE["id"]
        assert result["web_url"] == SAMPLE_UPLOADED_FILE["webUrl"]
        assert result["mode"] == "onedrive"

    @respx.mock
    async def test_folder_path_is_honoured(self):
        route = respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Inbox/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        att = attachments.ResolvedAttachment("report.pdf", b"%PDF", "application/pdf")
        async with AsyncGraphClient("tok") as client:
            await attachments.adeliver_attachment(client, att, "onedrive", folder_path="Inbox")

        assert route.called

    @respx.mock
    def test_sync_uploads(self):
        respx.put(f"{GRAPH_BASE_URL}/me/drive/root:/Attachments/report.pdf:/content").mock(
            return_value=httpx.Response(201, json=SAMPLE_UPLOADED_FILE)
        )
        att = attachments.ResolvedAttachment("report.pdf", b"%PDF", "application/pdf")
        with GraphClient("tok") as client:
            assert (
                attachments.deliver_attachment(client, att, "onedrive")["item_id"]
                == SAMPLE_UPLOADED_FILE["id"]
            )


class TestDeliverBadMode:
    """An unknown sink mode fails before anything is fetched or uploaded."""

    async def test_bad_mode_is_refused(self):
        att = attachments.ResolvedAttachment("a.txt", b"x", "text/plain")
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="mode must be one of"):
                await attachments.adeliver_attachment(client, att, "pdf")

    def test_sync_bad_mode_is_refused(self):
        att = attachments.ResolvedAttachment("a.txt", b"x", "text/plain")
        with GraphClient("tok") as client:
            with pytest.raises(ValueError, match="mode must be one of"):
                attachments.deliver_attachment(client, att, "")


class TestReviewFixes:
    """Findings from the Phase 1 senior review, pinned so they cannot regress."""

    def test_content_id_is_selected_through_a_type_cast(self):
        # A bare "contentId" is a Graph 400: the property lives on fileAttachment,
        # not on the base attachment type the collection is declared as.
        assert "microsoft.graph.fileAttachment/contentId" in attachments.ATTACHMENT_LIST_SELECT
        assert ",contentId" not in attachments.ATTACHMENT_LIST_SELECT

    @respx.mock
    async def test_listing_query_carries_the_type_cast(self):
        route = respx.get(url__startswith=ATTACHMENTS_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_ATTACHMENTS_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            await attachments.alist_message_attachments(client, MSG_ID)

        assert "microsoft.graph.fileAttachment%2FcontentId" in str(route.calls[0].request.url)

    def test_office_types_are_known_without_a_system_mime_table(self):
        assert attachments.guess_content_type("deck.pptx").endswith("presentationml.presentation")
        assert attachments.guess_content_type("report.docx").endswith("wordprocessingml.document")
        assert attachments.guess_content_type("photo.JPG") == "image/jpeg"

    async def test_oversized_document_is_not_parsed(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_DOCUMENT_DOWNLOAD_BYTES", 4)
        att = attachments.ResolvedAttachment("big.pdf", b"%PDF-1.7 ....", "application/pdf")
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["text"] is None
        assert result["reason"] == "too_large"

    async def test_extractor_truncation_sets_the_flag(self, monkeypatch):
        monkeypatch.setattr(
            attachments, "extract_document_text", lambda *a: "body\n\n... [Content truncated. x]"
        )
        att = attachments.ResolvedAttachment("r.docx", b"PK..", attachments._DOCX_MIME)
        async with AsyncGraphClient("tok") as client:
            result = await attachments.adeliver_attachment(client, att, "text")

        assert result["truncated"] is True

    @respx.mock
    async def test_oversized_drive_item_is_refused_before_download(self, monkeypatch):
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 4)
        respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001").mock(
            return_value=httpx.Response(200, json={**SAMPLE_DRIVE_ITEM_FILE, "size": 5})
        )
        content = respx.get(f"{GRAPH_BASE_URL}/me/drive/items/file-id-001/content").mock(
            return_value=httpx.Response(200, content=b"01234")
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError, match="exceeds"):
                await attachments.aresolve_attachment_source(
                    client, {"drive_item_id": "file-id-001"}
                )

        assert not content.called
