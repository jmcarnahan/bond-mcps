"""Track Changes (revision markup) for Word documents via Open XML.

Manipulates the underlying lxml elements of python-docx Document objects
to produce w:ins / w:del markup that Word recognizes as tracked changes.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree  # nosec B410 — parsing trusted OPC package internals, not untrusted input

if TYPE_CHECKING:
    from docx import Document

_revision_id_counter: int = 0


def reset_revision_counter() -> None:
    global _revision_id_counter
    _revision_id_counter = 0


def _next_rid() -> str:
    global _revision_id_counter
    _revision_id_counter += 1
    return str(_revision_id_counter)


def enable_track_changes(doc: Document) -> None:
    """Add <w:trackChanges/> to document settings."""
    settings = doc.settings.element
    if settings.find(qn("w:trackChanges")) is None:
        tc = OxmlElement("w:trackChanges")
        settings.append(tc)


def get_paragraph_text(para_element: etree._Element) -> str:
    """Assemble visible text from a paragraph element.

    Reads text from normal runs and accepted insertions (<w:ins>).
    Skips text inside deletions (<w:del>).
    """
    parts: list[str] = []
    for child in para_element:
        tag = etree.QName(child.tag).localname
        if tag == "r":
            parts.append(_run_text(child))
        elif tag == "ins":
            for run in child.findall(qn("w:r")):
                parts.append(_run_text(run))
        # skip w:del, w:bookmarkStart, w:bookmarkEnd, etc.
    return "".join(parts)


def _run_text(run_element: etree._Element) -> str:
    """Extract text from a single <w:r> element."""
    parts: list[str] = []
    for t_elem in run_element.findall(qn("w:t")):
        parts.append(t_elem.text or "")
    return "".join(parts)


def _get_visible_runs(para_element: etree._Element) -> list[etree._Element]:
    """Return run elements that contribute visible text (direct runs + runs inside w:ins)."""
    runs: list[etree._Element] = []
    for child in para_element:
        tag = etree.QName(child.tag).localname
        if tag == "r":
            runs.append(child)
        elif tag == "ins":
            for run in child.findall(qn("w:r")):
                runs.append(run)
    return runs


def find_runs_for_range(para_element: etree._Element, start: int, end: int) -> list[dict[str, Any]]:
    """Map a character range [start, end) to affected run elements.

    Returns list of dicts with:
      - element: the <w:r> element
      - text_before: text in this run before the match
      - text_match: the matched portion
      - text_after: text in this run after the match
      - parent: the immediate parent of this run (paragraph or w:ins element)
    """
    runs = _get_visible_runs(para_element)
    result: list[dict[str, Any]] = []
    offset = 0

    for run in runs:
        run_text = _run_text(run)
        if not run_text:
            continue
        run_start = offset
        run_end = offset + len(run_text)
        offset = run_end

        if run_end <= start or run_start >= end:
            continue

        local_start = max(0, start - run_start)
        local_end = min(len(run_text), end - run_start)

        result.append(
            {
                "element": run,
                "text_before": run_text[:local_start],
                "text_match": run_text[local_start:local_end],
                "text_after": run_text[local_end:],
                "parent": run.getparent(),
            }
        )

    return result


def replace_with_revision(
    para_element: etree._Element,
    target: str,
    replacement: str,
    author: str,
    date: str,
) -> bool:
    """Replace target text with tracked change markup (w:del + w:ins).

    Returns True if replacement was made, False if target not found.
    """
    full_text = get_paragraph_text(para_element)
    idx = full_text.find(target)
    if idx == -1:
        return False

    affected = find_runs_for_range(para_element, idx, idx + len(target))
    if not affected:
        return False

    first_run = affected[0]["element"]
    first_parent = affected[0]["parent"]

    # Determine insertion point in the parent
    insert_pos = list(first_parent).index(first_run)

    # Build elements to insert
    new_elements: list[etree._Element] = []

    # Before-run (text before match in first affected run)
    if affected[0]["text_before"]:
        before_run = _make_run(affected[0]["text_before"], first_run)
        new_elements.append(before_run)

    # <w:del> element
    del_id = _next_rid()
    del_elem = _make_del(affected, author, date, del_id)
    new_elements.append(del_elem)

    # <w:ins> element
    if replacement:
        ins_id = _next_rid()
        ins_elem = _make_ins(replacement, first_run, author, date, ins_id)
        new_elements.append(ins_elem)

    # After-run (text after match in last affected run)
    if affected[-1]["text_after"]:
        after_run = _make_run(affected[-1]["text_after"], affected[-1]["element"])
        new_elements.append(after_run)

    # Remove original affected runs from their parents; track parents that may become empty
    parents_to_check: set[etree._Element] = set()
    for entry in affected:
        parent = entry["parent"]
        parent.remove(entry["element"])
        if parent is not first_parent:
            parents_to_check.add(parent)

    # Remove empty w:ins containers left behind
    for parent in parents_to_check:
        if len(parent) == 0 and etree.QName(parent.tag).localname == "ins":
            grandparent = parent.getparent()
            if grandparent is not None:
                grandparent.remove(parent)

    # Insert new elements at the position
    for i, elem in enumerate(new_elements):
        first_parent.insert(insert_pos + i, elem)

    return True


def insert_with_revision(
    para_element: etree._Element,
    text: str,
    author: str,
    date: str,
    position: str = "end",
) -> None:
    """Insert text wrapped in <w:ins> at start or end of a paragraph."""
    ins_id = _next_rid()
    ins_elem = OxmlElement("w:ins")
    ins_elem.set(qn("w:id"), ins_id)
    ins_elem.set(qn("w:author"), author)
    ins_elem.set(qn("w:date"), date)

    run = OxmlElement("w:r")
    t_elem = OxmlElement("w:t")
    t_elem.set(qn("xml:space"), "preserve")
    t_elem.text = text
    run.append(t_elem)
    ins_elem.append(run)

    if position == "start":
        para_element.insert(0, ins_elem)
    else:
        para_element.append(ins_elem)


def delete_paragraph_with_revision(
    para_element: etree._Element,
    author: str,
    date: str,
) -> None:
    """Mark all visible runs in a paragraph as deleted."""
    runs = _get_visible_runs(para_element)
    if not runs:
        return

    del_id = _next_rid()
    del_elem = OxmlElement("w:del")
    del_elem.set(qn("w:id"), del_id)
    del_elem.set(qn("w:author"), author)
    del_elem.set(qn("w:date"), date)

    # Collect runs into the del element
    first_run = runs[0]
    first_parent = first_run.getparent()
    insert_pos = list(first_parent).index(first_run)

    for run in runs:
        parent = run.getparent()
        parent.remove(run)
        # Convert w:t to w:delText
        for t_elem in run.findall(qn("w:t")):
            del_text = OxmlElement("w:delText")
            del_text.set(qn("xml:space"), "preserve")
            del_text.text = t_elem.text or ""
            run.replace(t_elem, del_text)
        del_elem.append(run)

    first_parent.insert(insert_pos, del_elem)


def _make_run(text: str, template_run: etree._Element) -> etree._Element:
    """Create a new <w:r> with text, copying formatting from template_run."""
    run = OxmlElement("w:r")
    rpr = template_run.find(qn("w:rPr"))
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t_elem = OxmlElement("w:t")
    t_elem.set(qn("xml:space"), "preserve")
    t_elem.text = text
    run.append(t_elem)
    return run


def _make_del(
    affected: list[dict[str, Any]],
    author: str,
    date: str,
    del_id: str,
) -> etree._Element:
    """Build a <w:del> element from affected runs."""
    del_elem = OxmlElement("w:del")
    del_elem.set(qn("w:id"), del_id)
    del_elem.set(qn("w:author"), author)
    del_elem.set(qn("w:date"), date)

    for entry in affected:
        run = OxmlElement("w:r")
        rpr = entry["element"].find(qn("w:rPr"))
        if rpr is not None:
            run.append(copy.deepcopy(rpr))
        del_text = OxmlElement("w:delText")
        del_text.set(qn("xml:space"), "preserve")
        del_text.text = entry["text_match"]
        run.append(del_text)
        del_elem.append(run)

    return del_elem


def _make_ins(
    text: str,
    template_run: etree._Element,
    author: str,
    date: str,
    ins_id: str,
) -> etree._Element:
    """Build a <w:ins> element with replacement text."""
    ins_elem = OxmlElement("w:ins")
    ins_elem.set(qn("w:id"), ins_id)
    ins_elem.set(qn("w:author"), author)
    ins_elem.set(qn("w:date"), date)

    run = OxmlElement("w:r")
    rpr = template_run.find(qn("w:rPr"))
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t_elem = OxmlElement("w:t")
    t_elem.set(qn("xml:space"), "preserve")
    t_elem.text = text
    run.append(t_elem)
    ins_elem.append(run)

    return ins_elem
