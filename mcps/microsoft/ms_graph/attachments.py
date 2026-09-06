"""
Attachment operations shared by mail and Teams: Graph attachment ops, plus the
content-transport layer (source specs in, sink modes out) every send/receive
tool uses.

The server runs remotely, so no caller can hand it a local path. Every send
takes attachments as JSON *source specs* (text, base64, a drive item, a sharing
URL, or another message's attachment) and every receive returns content through
one *sink* with three modes (text, base64, onedrive). Both directions live here
so mail and Teams cannot drift apart.
"""

import base64
import logging
import mimetypes
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from . import document_create, files, mail_policy
from . import mail as mail_ops
from .document_extract import (
    MAX_DOCUMENT_DOWNLOAD_BYTES,
    extract_document_text,
    is_extractable_document,
)
from .graph_client import AsyncGraphClient, GraphClient, GraphError
from .mail import ATTACHMENT_LIST_SELECT, _safe_id

logger = logging.getLogger(__name__)

# Graph switches strategy at 3 MB: below it an attachment posts inline as
# base64 contentBytes, at or above it needs an upload session.
MAX_INLINE_ATTACHMENT_BYTES = 3_000_000
# Outlook's hard per-attachment ceiling.
MAX_ATTACHMENT_BYTES = 150_000_000
# LLM-facing base64 sink cap — bytes must not flood the model's context.
MAX_BASE64_RETURN_BYTES = 1_000_000
# Desktop JSON base64 cap: FastMCP mirrors a dict result into a text block as
# well, so the payload crosses the wire twice, and the desktop decodes it on
# its main isolate.
MAX_JSON_ATTACHMENT_BYTES = 10_000_000
# Per-message cap on listed summaries; callers report the true count alongside.
MAX_LISTED_ATTACHMENTS = 50
# A message with more attachment pages than this is pathological, not real.
MAX_ATTACHMENT_PAGES = 5

_VALID_SINK_MODES = frozenset({"text", "base64", "onedrive"})

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The final fragment of an attachment upload session answers 201 with an empty
# body; the new attachment id is only in the Location URL.
_ATTACHMENT_ID_RE = re.compile(r"Attachments\('([^']+)'\)")

_ODATA_KINDS = {
    "fileattachment": "file",
    "itemattachment": "item",
    "referenceattachment": "reference",
}

_SOURCE_KEYS = ("text", "base64", "drive_item_id", "url")
_SOURCE_ERROR = (
    "attachment spec must have exactly one of: text, base64, drive_item_id, url, "
    "message_id+attachment_id"
)

_ITEM_EXPAND = "microsoft.graph.itemattachment/item"

# The extractor appends this marker when it cuts a document's text off.
_EXTRACT_TRUNCATED_MARKER = "... [Content truncated."

# Types the server must know even when the container has no /etc/mime.types
# (slim images ship none, and Python's built-in table lacks the Office types).
_KNOWN_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": _DOCX_MIME,
    ".xlsx": _XLSX_MIME,
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".zip": "application/zip",
}


# ---------------------------------------------------------------------------
# Shapes and helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedAttachment:
    """One attachment resolved to bytes, ready to send anywhere."""

    name: str
    data: bytes
    content_type: str


def guess_content_type(name: str, fallback: str = "application/octet-stream") -> str:
    """Guess a MIME type from a filename, falling back when nothing matches."""
    lowered = name.lower()
    dot_idx = lowered.rfind(".")
    if dot_idx >= 0:
        known = _KNOWN_CONTENT_TYPES.get(lowered[dot_idx:])
        if known:
            return known
    guessed, _ = mimetypes.guess_type(name)
    return guessed or fallback


def attachment_summary(att: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Graph attachment into the flat shape every surface returns.

    ``@odata.type`` is the only signal for what kind of attachment this is; it
    is always present and cannot be ``$select``ed away.
    """
    odata_type = att.get("@odata.type") or ""
    suffix = str(odata_type).rsplit(".", 1)[-1].lower()
    kind = _ODATA_KINDS.get(suffix, "unknown")
    size = att.get("size", 0)
    if isinstance(size, bool) or not isinstance(size, int):
        size = 0
    return {
        "id": att.get("id", "") or "",
        "name": att.get("name", "") or "",
        "content_type": att.get("contentType", "") or "",
        "size": size,
        "is_inline": bool(att.get("isInline", False)),
        "content_id": att.get("contentId") or None,
        "kind": kind,
        "source_url": (att.get("sourceUrl") or None) if kind == "reference" else None,
    }


def _attachments_path(message_id: str, mailbox: str | None) -> str:
    """Graph path for a message's attachment collection."""
    return f"{mail_ops._base(mailbox)}/messages/{_safe_id(message_id)}/attachments"


def _attachment_path(message_id: str, attachment_id: str, mailbox: str | None) -> str:
    """Graph path for a single attachment."""
    return f"{_attachments_path(message_id, mailbox)}/{_safe_id(attachment_id)}"


def _list_select() -> dict[str, str]:
    """Query params for an attachment read — never fetch contentBytes by accident."""
    return {"$select": ATTACHMENT_LIST_SELECT}


def _inline_payload(name: str, data: bytes, content_type: str) -> dict[str, Any]:
    """Body for a small attachment posted straight onto the message."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "contentType": content_type,
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


def _session_payload(name: str, size: int) -> dict[str, Any]:
    """Body for createUploadSession on a message's attachment collection."""
    return {"AttachmentItem": {"attachmentType": "file", "name": name, "size": size}}


def _check_send_size(name: str, data: bytes) -> None:
    """Refuse anything Outlook would reject before a single byte goes out."""
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"attachment '{name}' is {len(data):,} bytes, which exceeds the "
            f"{MAX_ATTACHMENT_BYTES:,} byte (150 MB) attachment limit."
        )


def _upload_url(session: dict[str, Any] | None) -> str:
    """Pull uploadUrl out of a createUploadSession response, or fail loudly."""
    upload_url = (session or {}).get("uploadUrl", "")
    if not upload_url:
        raise GraphError(500, "NoUploadUrl", "createUploadSession did not return an uploadUrl")
    return upload_url


def _attachment_id_from_location(location: str) -> str:
    """Extract the attachment id from the final fragment's Location header."""
    match = _ATTACHMENT_ID_RE.search(location or "")
    if not match:
        raise GraphError(
            500,
            "NoAttachmentId",
            "Attachment upload session finished without an attachment id in Location",
        )
    # The id sits inside a URL path segment, so Graph percent-encodes the '='
    # and '/' that Exchange ids routinely contain.
    return unquote(match.group(1))


def _created_attachment_id(created: dict[str, Any] | None) -> str:
    """The POST response carries the new attachment's id."""
    attachment_id = (created or {}).get("id", "")
    if not attachment_id:
        raise GraphError(500, "NoAttachmentId", "Adding the attachment returned no id")
    return attachment_id


# ---------------------------------------------------------------------------
# Mail attachment reads — synchronous
# ---------------------------------------------------------------------------


def list_message_attachments(
    client: GraphClient, message_id: str, mailbox: str | None = None
) -> list[dict[str, Any]]:
    """List a message's attachments (metadata only, never contentBytes).

    Follows nextLink up to MAX_ATTACHMENT_PAGES pages; the cap bounds the
    number of requests, not just the number of pages kept.
    """
    data = client.get(_attachments_path(message_id, mailbox), params=_list_select())
    items: list[dict[str, Any]] = list(data.get("value", []))
    for _ in range(MAX_ATTACHMENT_PAGES - 1):
        next_link = data.get("@odata.nextLink", "")
        if not next_link:
            break
        data = client.get(next_link)
        items.extend(data.get("value", []))
    return items


def get_attachment_metadata(
    client: GraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> dict[str, Any]:
    """Fetch one attachment's metadata without its bytes."""
    return client.get(_attachment_path(message_id, attachment_id, mailbox), params=_list_select())


def get_attachment_bytes(
    client: GraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> tuple[bytes, str]:
    """Download one attachment's raw bytes and its content type."""
    path = f"{_attachment_path(message_id, attachment_id, mailbox)}/$value"
    return client.get_bytes_with_type(path)


def get_item_attachment(
    client: GraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> dict[str, Any]:
    """Fetch an attached message/event, expanded to its inner item."""
    return client.get(
        _attachment_path(message_id, attachment_id, mailbox),
        params={"$expand": _ITEM_EXPAND},
    )


# ---------------------------------------------------------------------------
# Mail attachment reads — asynchronous
# ---------------------------------------------------------------------------


async def alist_message_attachments(
    client: AsyncGraphClient, message_id: str, mailbox: str | None = None
) -> list[dict[str, Any]]:
    """List a message's attachments (async). See list_message_attachments."""
    data = await client.get(_attachments_path(message_id, mailbox), params=_list_select())
    items: list[dict[str, Any]] = list(data.get("value", []))
    for _ in range(MAX_ATTACHMENT_PAGES - 1):
        next_link = data.get("@odata.nextLink", "")
        if not next_link:
            break
        data = await client.get(next_link)
        items.extend(data.get("value", []))
    return items


async def aget_attachment_metadata(
    client: AsyncGraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> dict[str, Any]:
    """Fetch one attachment's metadata without its bytes (async)."""
    return await client.get(
        _attachment_path(message_id, attachment_id, mailbox), params=_list_select()
    )


async def aget_attachment_bytes(
    client: AsyncGraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> tuple[bytes, str]:
    """Download one attachment's raw bytes and its content type (async)."""
    path = f"{_attachment_path(message_id, attachment_id, mailbox)}/$value"
    return await client.get_bytes_with_type(path)


async def aget_item_attachment(
    client: AsyncGraphClient, message_id: str, attachment_id: str, mailbox: str | None = None
) -> dict[str, Any]:
    """Fetch an attached message/event, expanded to its inner item (async)."""
    return await client.get(
        _attachment_path(message_id, attachment_id, mailbox),
        params={"$expand": _ITEM_EXPAND},
    )


# ---------------------------------------------------------------------------
# Mail attachment writes
# ---------------------------------------------------------------------------


def add_file_attachment(
    client: GraphClient,
    message_id: str,
    name: str,
    data: bytes,
    content_type: str,
    mailbox: str | None = None,
) -> str:
    """Attach bytes to a draft message and return the new attachment id.

    The size branch lives here so no caller has to know that Graph changes
    protocol at 3 MB.
    """
    _check_send_size(name, data)
    path = _attachments_path(message_id, mailbox)
    if len(data) < MAX_INLINE_ATTACHMENT_BYTES:
        created = client.post(path, json_data=_inline_payload(name, data, content_type))
        return _created_attachment_id(created)

    session = client.post(
        f"{path}/createUploadSession", json_data=_session_payload(name, len(data))
    )
    upload_url = _upload_url(session)
    total = len(data)
    result = None
    try:
        start = 0
        while start < total:
            chunk = data[start : start + files.UPLOAD_CHUNK_BYTES]
            result = client.put_range(upload_url, chunk, start, total)
            start += len(chunk)
    except Exception:
        _cancel_upload(client, upload_url)
        raise
    return _attachment_id_from_location(result.location if result else "")


async def aadd_file_attachment(
    client: AsyncGraphClient,
    message_id: str,
    name: str,
    data: bytes,
    content_type: str,
    mailbox: str | None = None,
) -> str:
    """Attach bytes to a draft message and return the new attachment id (async)."""
    _check_send_size(name, data)
    path = _attachments_path(message_id, mailbox)
    if len(data) < MAX_INLINE_ATTACHMENT_BYTES:
        created = await client.post(path, json_data=_inline_payload(name, data, content_type))
        return _created_attachment_id(created)

    session = await client.post(
        f"{path}/createUploadSession", json_data=_session_payload(name, len(data))
    )
    upload_url = _upload_url(session)
    total = len(data)
    result = None
    try:
        start = 0
        while start < total:
            chunk = data[start : start + files.UPLOAD_CHUNK_BYTES]
            result = await client.put_range(upload_url, chunk, start, total)
            start += len(chunk)
    except Exception:
        await _acancel_upload(client, upload_url)
        raise
    return _attachment_id_from_location(result.location if result else "")


def _cancel_upload(client: GraphClient, upload_url: str) -> None:
    """Best-effort session cancel — the original failure is what matters."""
    try:
        client.delete_url(upload_url)
    except Exception:
        logger.debug("Could not cancel attachment upload session", exc_info=True)


async def _acancel_upload(client: AsyncGraphClient, upload_url: str) -> None:
    """Best-effort session cancel (async)."""
    try:
        await client.delete_url(upload_url)
    except Exception:
        logger.debug("Could not cancel attachment upload session", exc_info=True)


# ---------------------------------------------------------------------------
# Source specs -> bytes
# ---------------------------------------------------------------------------


def _source_kind(spec: Any) -> str:
    """Identify the single source key in a spec, or explain what is wrong."""
    if not isinstance(spec, dict):
        raise ValueError("attachment spec must be a JSON object")
    present = [key for key in _SOURCE_KEYS if key in spec]
    if "message_id" in spec or "attachment_id" in spec:
        present.append("message")
    if len(present) != 1:
        raise ValueError(_SOURCE_ERROR)
    return present[0]


def _required_name(spec: dict[str, Any], kind: str) -> str:
    """text/base64 carry no name of their own, so the spec must supply one."""
    name = spec.get("name") or ""
    if not name:
        raise ValueError(f"attachment spec with '{kind}' requires a 'name'")
    return name


def _text_source(spec: dict[str, Any]) -> tuple[str, bytes, str]:
    """Turn a text spec into bytes, converting to docx/xlsx when the name says so."""
    name = _required_name(spec, "text")
    text = spec.get("text")
    if not isinstance(text, str):
        raise ValueError(f"attachment '{name}': 'text' must be a string")
    lowered = name.lower()
    if lowered.endswith(".docx"):
        return name, document_create.markdown_to_docx(text), _DOCX_MIME
    if lowered.endswith(".xlsx"):
        return name, document_create.csv_to_xlsx(text), _XLSX_MIME
    return name, text.encode("utf-8"), guess_content_type(name, "text/plain")


def _base64_source(spec: dict[str, Any]) -> tuple[str, bytes, str]:
    """Decode a base64 spec, refusing anything that is not valid base64."""
    name = _required_name(spec, "base64")
    value = spec.get("base64")
    if not isinstance(value, str):
        raise ValueError(f"attachment '{name}': invalid base64")
    try:
        data = base64.b64decode(value, validate=True)
    except Exception as e:
        raise ValueError(f"attachment '{name}': invalid base64") from e
    return name, data, guess_content_type(name)


def _drive_item_name_and_type(spec: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    """Name and MIME for a drive-item source; the spec may override the name."""
    if "folder" in item:
        raise ValueError(
            f"'{item.get('name', 'item')}' is a folder, not a file; attach a file instead."
        )
    name = spec.get("name") or item.get("name") or "attachment"
    mime = item.get("file", {}).get("mimeType", "") or guess_content_type(name)
    # Refuse from the metadata, before pulling a file that would only be
    # rejected after the download.
    size = item.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"attachment '{name}' is {size:,} bytes, which exceeds the "
            f"{MAX_ATTACHMENT_BYTES:,} byte (150 MB) attachment limit."
        )
    return name, mime


def _message_source_ids(spec: dict[str, Any]) -> tuple[str, str]:
    """Both halves of a forwarded-attachment reference are required."""
    message_id = spec.get("message_id") or ""
    attachment_id = spec.get("attachment_id") or ""
    if not message_id or not attachment_id:
        raise ValueError("attachment spec needs both 'message_id' and 'attachment_id'")
    return message_id, attachment_id


def _check_forwarded_kind(summary: dict[str, Any]) -> None:
    """Only file attachments can be re-sent; the other kinds carry no bytes."""
    kind = summary["kind"]
    name = summary["name"] or summary["id"]
    if kind == "reference":
        raise ValueError(f"attachment '{name}' is a link, not a file; send the link instead.")
    if kind == "item":
        raise ValueError(
            f"attachment '{name}' is an attached item; forward the original message instead."
        )


def _finalize(
    spec: dict[str, Any], name: str, data: bytes, content_type: str
) -> ResolvedAttachment:
    """Apply the spec's content_type override and enforce the send ceiling.

    The override is honoured only when it is a non-empty string; anything else
    (a number, an object) would otherwise be sent to Graph as the MIME type.
    """
    override = spec.get("content_type")
    if override is not None and not isinstance(override, str):
        raise ValueError(f"attachment '{name}': 'content_type' must be a string")
    resolved_type = override or content_type or "application/octet-stream"
    _check_send_size(name, data)
    return ResolvedAttachment(name=name, data=data, content_type=resolved_type)


async def aresolve_attachment_source(
    client: AsyncGraphClient, spec: dict[str, Any]
) -> ResolvedAttachment:
    """Resolve one attachment source spec into bytes (async)."""
    kind = _source_kind(spec)
    if kind == "text":
        name, data, content_type = _text_source(spec)
    elif kind == "base64":
        name, data, content_type = _base64_source(spec)
    elif kind == "drive_item_id":
        item_id = spec.get("drive_item_id") or ""
        site_id = spec.get("site_id", "")
        drive_id = spec.get("drive_id", "")
        item = await files.aget_drive_item(client, item_id, site_id=site_id, drive_id=drive_id)
        name, content_type = _drive_item_name_and_type(spec, item)
        base = files._drive_base(site_id or None, drive_id or None)
        data = await client.get_bytes(f"{base}/items/{_safe_id(item_id)}/content")
    elif kind == "url":
        item, data = await files.aresolve_sharing_link_bytes(client, spec.get("url") or "")
        name, content_type = _drive_item_name_and_type(spec, item)
    else:
        message_id, attachment_id = _message_source_ids(spec)
        mailbox = spec.get("mailbox") or None
        # Forwarding an attachment re-originates it as internal mail, so the
        # source message is judged before a byte of it is read.
        if not await mail_policy.acheck_message(client, message_id, mailbox):
            raise ValueError(mail_policy.EXTERNAL_SENDER_TEXT)
        summary = attachment_summary(
            await aget_attachment_metadata(client, message_id, attachment_id, mailbox)
        )
        _check_forwarded_kind(summary)
        data, header_type = await aget_attachment_bytes(client, message_id, attachment_id, mailbox)
        name = spec.get("name") or summary["name"] or "attachment"
        content_type = summary["content_type"] or header_type
    return _finalize(spec, name, data, content_type)


def resolve_attachment_source(client: GraphClient, spec: dict[str, Any]) -> ResolvedAttachment:
    """Resolve one attachment source spec into bytes."""
    kind = _source_kind(spec)
    if kind == "text":
        name, data, content_type = _text_source(spec)
    elif kind == "base64":
        name, data, content_type = _base64_source(spec)
    elif kind == "drive_item_id":
        item_id = spec.get("drive_item_id") or ""
        site_id = spec.get("site_id", "")
        drive_id = spec.get("drive_id", "")
        item = files.get_drive_item(client, item_id, site_id=site_id, drive_id=drive_id)
        name, content_type = _drive_item_name_and_type(spec, item)
        base = files._drive_base(site_id or None, drive_id or None)
        data = client.get_bytes(f"{base}/items/{_safe_id(item_id)}/content")
    elif kind == "url":
        item, data = files.resolve_sharing_link_bytes(client, spec.get("url") or "")
        name, content_type = _drive_item_name_and_type(spec, item)
    else:
        message_id, attachment_id = _message_source_ids(spec)
        mailbox = spec.get("mailbox") or None
        # See aresolve_attachment_source: the source message is judged first.
        if not mail_policy.check_message(client, message_id, mailbox):
            raise ValueError(mail_policy.EXTERNAL_SENDER_TEXT)
        summary = attachment_summary(
            get_attachment_metadata(client, message_id, attachment_id, mailbox)
        )
        _check_forwarded_kind(summary)
        data, header_type = get_attachment_bytes(client, message_id, attachment_id, mailbox)
        name = spec.get("name") or summary["name"] or "attachment"
        content_type = summary["content_type"] or header_type
    return _finalize(spec, name, data, content_type)


def _check_specs(specs: Any) -> list[Any]:
    """The whole list is validated before any of it is fetched."""
    if not isinstance(specs, list):
        raise ValueError("attachments must be a JSON array of objects")
    return specs


async def aresolve_attachment_sources(
    client: AsyncGraphClient, specs: Any
) -> list[ResolvedAttachment]:
    """Resolve a list of source specs in order (async); errors name the index."""
    resolved: list[ResolvedAttachment] = []
    for index, spec in enumerate(_check_specs(specs)):
        try:
            resolved.append(await aresolve_attachment_source(client, spec))
        except ValueError as e:
            raise ValueError(f"attachments[{index}]: {e}") from e
    return resolved


def resolve_attachment_sources(client: GraphClient, specs: Any) -> list[ResolvedAttachment]:
    """Resolve a list of source specs in order; errors name the index."""
    resolved: list[ResolvedAttachment] = []
    for index, spec in enumerate(_check_specs(specs)):
        try:
            resolved.append(resolve_attachment_source(client, spec))
        except ValueError as e:
            raise ValueError(f"attachments[{index}]: {e}") from e
    return resolved


# ---------------------------------------------------------------------------
# Bytes -> sink
# ---------------------------------------------------------------------------


def _is_text_like(att: ResolvedAttachment) -> bool:
    """Text by MIME type, or by extension when the MIME is unhelpful."""
    content_type = att.content_type or ""
    if content_type.startswith("text/") or content_type in files._TEXT_MIME_TYPES:
        return True
    dot_idx = att.name.rfind(".")
    return dot_idx >= 0 and att.name[dot_idx:].lower() in files._TEXT_EXTENSIONS


def _text_sink(att: ResolvedAttachment) -> dict[str, Any]:
    """Text mode: extract documents, decode text files, refuse binaries."""
    if is_extractable_document({"file": {"mimeType": att.content_type}, "name": att.name}):
        # The extractors parse in memory; the same ceiling inspect_file uses
        # keeps a 150 MB PDF from being opened here.
        if len(att.data) > MAX_DOCUMENT_DOWNLOAD_BYTES:
            return {"text": None, "truncated": False, "reason": "too_large"}
        text = extract_document_text(att.data, att.content_type, att.name)
        if text is None:
            return {"text": None, "truncated": False, "reason": "unsupported"}
        return {"text": text, "truncated": _EXTRACT_TRUNCATED_MARKER in text}
    if _is_text_like(att):
        raw = att.data
        truncated = len(raw) > files.MAX_TEXT_DOWNLOAD_BYTES
        if truncated:
            raw = raw[: files.MAX_TEXT_DOWNLOAD_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return {"text": text, "truncated": truncated}
    return {"text": None, "truncated": False, "reason": "binary"}


def _base64_sink(att: ResolvedAttachment) -> dict[str, Any]:
    """Base64 mode: hand back bytes, or say why they were withheld."""
    if len(att.data) > MAX_BASE64_RETURN_BYTES:
        return {"error": "too_large", "limit": MAX_BASE64_RETURN_BYTES}
    return {"base64": base64.b64encode(att.data).decode("ascii")}


def _sink_base(att: ResolvedAttachment, mode: str) -> dict[str, Any]:
    """The keys every sink mode returns."""
    if mode not in _VALID_SINK_MODES:
        raise ValueError(f"mode must be one of: text, base64, onedrive; got {mode!r}")
    return {
        "mode": mode,
        "name": att.name,
        "content_type": att.content_type,
        "size": len(att.data),
    }


def _onedrive_result(base: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Uploaded-to-OneDrive result: where the caller can now find the file."""
    return {**base, "item_id": item.get("id", ""), "web_url": item.get("webUrl", "")}


async def adeliver_attachment(
    client: AsyncGraphClient,
    att: ResolvedAttachment,
    mode: str,
    folder_path: str = "Attachments",
    site_id: str = "",
) -> dict[str, Any]:
    """Deliver resolved bytes through one sink mode (async)."""
    base = _sink_base(att, mode)
    if mode == "text":
        return {**base, **_text_sink(att)}
    if mode == "base64":
        return {**base, **_base64_sink(att)}
    item = await files.aupload_any(
        client, folder_path, att.name, att.data, att.content_type, site_id=site_id
    )
    return _onedrive_result(base, item)


def deliver_attachment(
    client: GraphClient,
    att: ResolvedAttachment,
    mode: str,
    folder_path: str = "Attachments",
    site_id: str = "",
) -> dict[str, Any]:
    """Deliver resolved bytes through one sink mode."""
    base = _sink_base(att, mode)
    if mode == "text":
        return {**base, **_text_sink(att)}
    if mode == "base64":
        return {**base, **_base64_sink(att)}
    item = files.upload_any(
        client, folder_path, att.name, att.data, att.content_type, site_id=site_id
    )
    return _onedrive_result(base, item)
