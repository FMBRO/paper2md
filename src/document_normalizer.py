"""Normalize Marker or Markdown output into a deterministic document artifact."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CAPTION = re.compile(r"^(?:\*\*)?(?:Figure|Fig\.)\s*\d+[^\n]*(?:\*\*)?$", re.IGNORECASE)
_PAGE_MARKER = re.compile(r"^<!--\s*page\s*:\s*(\d+)\s*-->$", re.IGNORECASE)


def _position(page: int, block: dict[str, Any]) -> dict[str, Any]:
    position: dict[str, Any] = {"page": page}
    bbox = block.get("bbox") or block.get("polygon") or block.get("position")
    if isinstance(bbox, list):
        position["bbox"] = bbox
    return position


def _document(source: str, page_numbers: Iterable[int]) -> dict[str, Any]:
    pages = [
        {"number": number, "source_position": {"page": number}}
        for number in sorted(set(page_numbers))
    ]
    return {
        "schema_version": 1,
        "source": source,
        "pages": pages,
        "sections": [],
        "paragraphs": [],
        "tables": [],
        "equations": [],
        "figures": [],
        "captions": [],
    }


def _blocks(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = page.get("blocks") or page.get("children") or page.get("elements") or []
    return [block for block in blocks if isinstance(block, dict)]


def _block_text(block: dict[str, Any]) -> str:
    value = block.get("text") or block.get("markdown") or block.get("content") or ""
    return value.strip() if isinstance(value, str) else ""


def _heading_level(block: dict[str, Any]) -> int:
    try:
        level = int(block.get("level") or block.get("heading_level") or 1)
    except (TypeError, ValueError):
        return 1
    return min(6, max(1, level))


def normalize_marker_document(marker_document: dict[str, Any]) -> dict[str, Any]:
    """Return the stable document schema from Marker JSON-like output.

    Marker output has changed shape between releases.  This deliberately uses
    the common page/block fields and treats unknown block types as paragraphs,
    retaining page and bounding-box information whenever it is supplied.
    """
    raw_pages = marker_document.get("pages") if isinstance(marker_document, dict) else None
    if not isinstance(raw_pages, list):
        raw_pages = []
    indexed_pages: list[tuple[int, dict[str, Any]]] = []
    for index, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, dict):
            continue
        number = raw_page.get("page") or raw_page.get("page_number") or raw_page.get("number") or index
        try:
            indexed_pages.append((int(number), raw_page))
        except (TypeError, ValueError):
            indexed_pages.append((index, raw_page))
    document = _document("marker", (number for number, _ in indexed_pages))
    current_section: str | None = None
    for page_number, raw_page in sorted(indexed_pages, key=lambda item: item[0]):
        for block in _blocks(raw_page):
            kind = str(block.get("type") or block.get("block_type") or "text").lower()
            text = _block_text(block)
            position = _position(page_number, block)
            if kind in {"sectionheader", "section_header", "heading", "header", "title"}:
                if not text:
                    continue
                current_section = f"section-{len(document['sections']) + 1:03d}"
                document["sections"].append({
                    "id": current_section,
                    "title": text,
                    "level": _heading_level(block),
                    "page": page_number,
                    "source_position": position,
                })
            elif kind in {"table", "tableblock"}:
                document["tables"].append({
                    "markdown": str(block.get("markdown") or text),
                    "page": page_number,
                    "section_id": current_section,
                    "source_position": position,
                })
            elif kind in {"equation", "math", "formula"}:
                document["equations"].append({
                    "text": text,
                    "display": bool(block.get("display", True)),
                    "page": page_number,
                    "section_id": current_section,
                    "source_position": position,
                })
            elif kind in {"figure", "image", "picture"}:
                document["figures"].append({
                    "path": str(block.get("path") or block.get("image_path") or ""),
                    "alt_text": str(block.get("alt_text") or block.get("alt") or ""),
                    "caption": None,
                    "page": page_number,
                    "section_id": current_section,
                    "source_position": position,
                })
            elif kind in {"caption", "figurecaption", "figure_caption"}:
                caption = {
                    "text": text,
                    "page": page_number,
                    "section_id": current_section,
                    "source_position": position,
                }
                document["captions"].append(caption)
                for figure in reversed(document["figures"]):
                    if figure["page"] == page_number and figure["caption"] is None:
                        figure["caption"] = text
                        break
            elif text:
                document["paragraphs"].append({
                    "text": text,
                    "page": page_number,
                    "section_id": current_section,
                    "source_position": position,
                })
    return document


def normalize_markdown_document(markdown: str, *, page_count: int = 1) -> dict[str, Any]:
    """Derive the stable schema from Markdown when Marker JSON is unavailable."""
    page_count = max(1, page_count)
    document = _document("markdown_fallback", range(1, page_count + 1))
    current_section: str | None = None
    page = 1
    lines = markdown.splitlines()
    index = 0
    pending_figure: dict[str, Any] | None = None
    while index < len(lines):
        line = lines[index].strip()
        if line == "\f":
            page = min(page + 1, page_count)
            index += 1
            continue
        page_marker = _PAGE_MARKER.match(line)
        if page_marker:
            page = min(max(1, int(page_marker.group(1))), page_count)
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            current_section = f"section-{len(document['sections']) + 1:03d}"
            document["sections"].append({
                "id": current_section,
                "title": heading.group(2),
                "level": len(heading.group(1)),
                "page": page,
                "source_position": {"page": page, "line": index + 1},
            })
            index += 1
            continue
        image = _IMAGE.search(line)
        if image:
            pending_figure = {
                "path": image.group(2), "alt_text": image.group(1), "caption": None,
                "page": page, "section_id": current_section,
                "source_position": {"page": page, "line": index + 1},
            }
            document["figures"].append(pending_figure)
            index += 1
            continue
        if _CAPTION.match(line):
            caption = {
                "text": line.strip("*"), "page": page, "section_id": current_section,
                "source_position": {"page": page, "line": index + 1},
            }
            document["captions"].append(caption)
            if pending_figure is not None and pending_figure["page"] == page:
                pending_figure["caption"] = caption["text"]
            index += 1
            continue
        if line == "$$":
            end = index + 1
            while end < len(lines) and lines[end].strip() != "$$":
                end += 1
            equation = "\n".join(lines[index + 1:end]).strip()
            document["equations"].append({
                "text": equation, "display": True, "page": page, "section_id": current_section,
                "source_position": {"page": page, "line": index + 1},
            })
            index = end + 1 if end < len(lines) else end
            continue
        if line.startswith("|") and index + 1 < len(lines) and lines[index + 1].strip().startswith("|"):
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith("|"):
                end += 1
            document["tables"].append({
                "markdown": "\n".join(lines[index:end]), "page": page, "section_id": current_section,
                "source_position": {"page": page, "line": index + 1},
            })
            index = end
            continue
        if line:
            document["paragraphs"].append({
                "text": line, "page": page, "section_id": current_section,
                "source_position": {"page": page, "line": index + 1},
            })
        index += 1
    return document
