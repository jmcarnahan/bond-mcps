"""Comment manipulation for Word documents via Open XML.

Creates/manages word/comments.xml part and inserts comment range markers
into the document body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree  # nosec B410 — parsing trusted OPC package internals, not untrusted input

from . import document_revisions

if TYPE_CHECKING:
    from docx import Document

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_COMMENTS_URI = PackURI("/word/comments.xml")
_COMMENTS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)


def get_or_create_comments_part(doc: Document) -> etree._Element:
    """Return the <w:comments> root element, creating the part if needed."""
    doc_part = doc.part

    # Check if comments part already exists via relationship
    for rel in doc_part.rels.values():
        if rel.reltype == RT.COMMENTS:
            return etree.fromstring(rel.target_part.blob)  # nosec B320

    # Create new comments XML
    comments_root = _make_empty_comments()
    blob = etree.tostring(comments_root, xml_declaration=True, encoding="UTF-8")

    comments_part = Part(
        _COMMENTS_URI,
        _COMMENTS_CONTENT_TYPE,
        blob,
        doc_part.package,
    )
    doc_part.relate_to(comments_part, RT.COMMENTS)

    return comments_root


def _save_comments_part(doc: Document, comments_root: etree._Element) -> None:
    """Persist updated comments XML back to the document package."""
    doc_part = doc.part
    for rel in doc_part.rels.values():
        if rel.reltype == RT.COMMENTS:
            rel.target_part._blob = etree.tostring(
                comments_root, xml_declaration=True, encoding="UTF-8"
            )
            return

    # Part didn't exist yet — create it
    blob = etree.tostring(comments_root, xml_declaration=True, encoding="UTF-8")
    comments_part = Part(
        _COMMENTS_URI,
        _COMMENTS_CONTENT_TYPE,
        blob,
        doc_part.package,
    )
    doc_part.relate_to(comments_part, RT.COMMENTS)


def _make_empty_comments() -> etree._Element:
    """Create an empty <w:comments> root element with proper namespaces."""
    nsmap = {
        "w": _W_NS,
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    return etree.Element(qn("w:comments"), nsmap=nsmap)


def _get_next_comment_id(comments_root: etree._Element) -> int:
    """Find the highest existing comment ID and return next available."""
    max_id = -1
    for comment in comments_root.findall(qn("w:comment")):
        cid = comment.get(qn("w:id"))
        if cid is not None:
            try:
                max_id = max(max_id, int(cid))
            except ValueError:
                pass
    return max_id + 1


def add_comment(
    doc: Document,
    para_element: etree._Element,
    target_text: str,
    comment_text: str,
    author: str = "Bond AI",
    date: str = "",
) -> bool:
    """Add a comment annotation to target_text within a paragraph.

    Returns True if the comment was added, False if target_text not found.
    """
    full_text = document_revisions.get_paragraph_text(para_element)
    idx = full_text.find(target_text)
    if idx == -1:
        return False

    affected = document_revisions.find_runs_for_range(para_element, idx, idx + len(target_text))
    if not affected:
        return False

    # Get or create comments part
    comments_root = get_or_create_comments_part(doc)
    comment_id = _get_next_comment_id(comments_root)

    # Add the comment entry to comments.xml
    comment_elem = OxmlElement("w:comment")
    comment_elem.set(qn("w:id"), str(comment_id))
    comment_elem.set(qn("w:author"), author)
    if date:
        comment_elem.set(qn("w:date"), date)
    # Comment body: a paragraph with the comment text
    comment_para = OxmlElement("w:p")
    comment_run = OxmlElement("w:r")
    comment_t = OxmlElement("w:t")
    comment_t.set(qn("xml:space"), "preserve")
    comment_t.text = comment_text
    comment_run.append(comment_t)
    comment_para.append(comment_run)
    comment_elem.append(comment_para)
    comments_root.append(comment_elem)

    # Insert commentRangeStart before the first affected run
    range_start = OxmlElement("w:commentRangeStart")
    range_start.set(qn("w:id"), str(comment_id))

    first_run = affected[0]["element"]
    first_parent = affected[0]["parent"]
    first_pos = list(first_parent).index(first_run)
    first_parent.insert(first_pos, range_start)

    # Insert commentRangeEnd after the last affected run
    range_end = OxmlElement("w:commentRangeEnd")
    range_end.set(qn("w:id"), str(comment_id))

    last_run = affected[-1]["element"]
    last_parent = affected[-1]["parent"]
    last_pos = list(last_parent).index(last_run)
    last_parent.insert(last_pos + 1, range_end)

    # Insert commentReference run after the range end
    ref_run = OxmlElement("w:r")
    ref_rpr = OxmlElement("w:rPr")
    ref_style = OxmlElement("w:rStyle")
    ref_style.set(qn("w:val"), "CommentReference")
    ref_rpr.append(ref_style)
    ref_run.append(ref_rpr)
    comment_ref = OxmlElement("w:commentReference")
    comment_ref.set(qn("w:id"), str(comment_id))
    ref_run.append(comment_ref)

    # Insert reference run right after range_end
    range_end_pos = list(last_parent).index(range_end)
    last_parent.insert(range_end_pos + 1, ref_run)

    # Save comments back to the package
    _save_comments_part(doc, comments_root)

    return True
