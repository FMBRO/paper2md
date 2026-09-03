"""Normalize Marker or Markdown output into a deterministic document artifact."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CAPTION = re.compile(r"^(?:\*\*)?(?:Figure|Fig\.)\s*\d+[^\n]*(?:\*\*)?$", re.IGNORECASE)
_PAGE_MARKER = re.compile(r"^<!--\s*page\s*:\s*(\d+)\s*-->$", re.IGNORECASE)
_MARKER_PAGE_ID = re.compile(r"/page/([^/]+)/", re.IGNORECASE)


def _marker_contract(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("pages"), list):
        return True
    return (
        str(payload.get("block_type", "")).casefold() == "document"
        and isinstance(payload.get("children"), list)
    )


def load_marker_document(marker_dir: Path | str) -> dict[str, Any] | None:
    """Load an explicit Marker renderer artifact and its sibling metadata.

    Marker 1.10.2 writes the recursive renderer tree to ``<stem>.json`` and
    writes ``page_stats`` to ``<stem>_meta.json``.  Synthetic debug JSON is
    intentionally ignored unless it has the older top-level ``pages`` shape.
    """
    path = Path(marker_dir)
    candidates = [path] if path.is_file() else sorted(
        candidate
        for candidate in path.rglob("*.json")
        if not candidate.name.endswith("_meta.json")
    )
    candidates.sort(key=lambda candidate: (
        candidate.name.casefold() != "p.json",
        len(candidate.parts),
        candidate.as_posix(),
    ))
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not _marker_contract(payload):
            continue
        if "children" in payload:
            metadata_path = candidate.with_name(f"{candidate.stem}_meta.json")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                payload = dict(payload)
                payload["metadata"] = metadata
        return payload
    return None


def _position(page: int, block: dict[str, Any]) -> dict[str, Any]:
    position: dict[str, Any] = {"page": page}
    bbox = block.get("bbox") or block.get("polygon") or block.get("position")
    if isinstance(bbox, list):
        position["bbox"] = bbox
    polygon = block.get("polygon")
    if isinstance(polygon, list) and polygon != bbox:
        position["polygon"] = polygon
    block_id = block.get("id")
    if isinstance(block_id, str) and block_id:
        position["id"] = block_id
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


def _kind(block: dict[str, Any]) -> str:
    value = str(block.get("type") or block.get("block_type") or "text")
    value = value.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _html_soup(block: dict[str, Any]) -> BeautifulSoup:
    value = block.get("html")
    return BeautifulSoup(value if isinstance(value, str) else "", "html.parser")


def _block_text(block: dict[str, Any]) -> str:
    value = block.get("text") or block.get("markdown") or block.get("content") or ""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _html_soup(block).get_text(" ", strip=True)


def _heading_level(block: dict[str, Any]) -> int:
    heading = _html_soup(block).find(re.compile(r"^h[1-6]$"))
    html_level = heading.name[1:] if heading is not None else None
    try:
        level = int(block.get("level") or block.get("heading_level") or html_level or 1)
    except (TypeError, ValueError):
        return 1
    return min(6, max(1, level))


def _marker_page_id(block: dict[str, Any]) -> str | None:
    explicit = block.get("page_id")
    if explicit is not None:
        return str(explicit)
    block_id = block.get("id")
    if isinstance(block_id, str):
        match = _MARKER_PAGE_ID.search(block_id)
        if match:
            return match.group(1)
    return None


def _marker_pages(marker_document: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    raw_pages = marker_document.get("pages")
    if isinstance(raw_pages, list):
        indexed: list[tuple[int, dict[str, Any]]] = []
        for index, raw_page in enumerate(raw_pages, start=1):
            if not isinstance(raw_page, dict):
                continue
            number = (
                raw_page.get("page") or raw_page.get("page_number")
                or raw_page.get("number") or index
            )
            try:
                indexed.append((int(number), raw_page))
            except (TypeError, ValueError):
                indexed.append((index, raw_page))
        return sorted(indexed, key=lambda item: item[0])

    metadata = marker_document.get("metadata")
    page_stats = metadata.get("page_stats") if isinstance(metadata, dict) else None
    page_numbers: dict[str, int] = {}
    if isinstance(page_stats, list):
        for index, stat in enumerate(page_stats, start=1):
            if isinstance(stat, dict) and stat.get("page_id") is not None:
                page_numbers[str(stat["page_id"])] = index
    children = marker_document.get("children")
    indexed = []
    if isinstance(children, list):
        for index, child in enumerate(children, start=1):
            if not isinstance(child, dict) or _kind(child) != "page":
                continue
            page_id = _marker_page_id(child)
            indexed.append((page_numbers.get(str(page_id), index), child))
    return indexed


def _leaf_blocks(block: dict[str, Any]) -> Iterable[dict[str, Any]]:
    children = _blocks(block)
    if children:
        for child in children:
            yield from _leaf_blocks(child)
    elif _kind(block) != "page":
        yield block


def _figure_fields(block: dict[str, Any]) -> tuple[str, str]:
    soup = _html_soup(block)
    image = soup.find("img")
    path = block.get("path") or block.get("image_path")
    alt = block.get("alt_text") or block.get("alt")
    if image is not None:
        path = path or image.get("src")
        alt = alt or image.get("alt")
    return (
        str(path) if isinstance(path, str) else "",
        str(alt) if isinstance(alt, str) else "",
    )


def normalize_marker_document(marker_document: dict[str, Any]) -> dict[str, Any]:
    """Return the stable document schema from Marker JSON-like output.

    Marker output has changed shape between releases.  This deliberately uses
    the common page/block fields and treats unknown block types as paragraphs,
    retaining page and bounding-box information whenever it is supplied.
    """
    indexed_pages = _marker_pages(marker_document)
    document = _document("marker", (number for number, _ in indexed_pages))
    current_section: str | None = None
    ordinal = 0
    for page_number, raw_page in indexed_pages:
        for block in _leaf_blocks(raw_page):
            kind = _kind(block)
            text = _block_text(block)
            position = _position(page_number, block)
            common = {"page": page_number, "ordinal": ordinal, "source_position": position}
            ordinal += 1
            if kind in {"sectionheader", "heading", "header", "title"}:
                if not text:
                    continue
                current_section = f"section-{len(document['sections']) + 1:03d}"
                document["sections"].append({
                    "id": current_section,
                    "title": text,
                    "level": _heading_level(block),
                    **common,
                })
            elif kind in {"table", "tableblock", "tableofcontents", "form"}:
                document["tables"].append({
                    "markdown": str(block.get("markdown") or block.get("html") or text),
                    "section_id": current_section,
                    **common,
                })
            elif kind in {"equation", "math", "formula"}:
                document["equations"].append({
                    "text": text,
                    "display": bool(block.get("display", True)),
                    "section_id": current_section,
                    **common,
                })
            elif kind in {"figure", "image", "picture"}:
                path, alt_text = _figure_fields(block)
                document["figures"].append({
                    "path": path,
                    "alt_text": alt_text,
                    "caption": None,
                    "section_id": current_section,
                    **common,
                })
            elif kind in {"caption", "figurecaption", "figure_caption"}:
                caption = {
                    "text": text,
                    "section_id": current_section,
                    **common,
                }
                document["captions"].append(caption)
                for figure in reversed(document["figures"]):
                    if figure["page"] == page_number and figure["caption"] is None:
                        figure["caption"] = text
                        break
            elif text:
                document["paragraphs"].append({
                    "text": text,
                    "section_id": current_section,
                    **common,
                })
    return document


def materialize_marker_markdown(marker_document: dict[str, Any]) -> str:
    """Render the normalized Marker tree into compatible local Markdown."""
    document = normalize_marker_document(marker_document)
    entries: list[tuple[int, int, str]] = []
    for section in document["sections"]:
        entries.append((section["page"], section["ordinal"], (
            "#" * int(section["level"]) + " " + section["title"]
        )))
    for paragraph in document["paragraphs"]:
        entries.append((paragraph["page"], paragraph["ordinal"], paragraph["text"]))
    for table in document["tables"]:
        entries.append((table["page"], table["ordinal"], table["markdown"]))
    for equation in document["equations"]:
        entries.append((equation["page"], equation["ordinal"], (
            f"$$\n{equation['text']}\n$$" if equation["display"] else f"${equation['text']}$"
        )))
    for figure in document["figures"]:
        entries.append((figure["page"], figure["ordinal"], (
            f"![{figure['alt_text']}]({figure['path']})"
        )))
    for caption in document["captions"]:
        entries.append((caption["page"], caption["ordinal"], caption["text"]))
    by_page: dict[int, list[tuple[int, str]]] = {}
    for page, ordinal, text in entries:
        if text.strip():
            by_page.setdefault(page, []).append((ordinal, text.strip()))
    blocks: list[str] = []
    for page in (item["number"] for item in document["pages"]):
        blocks.append(f"<!-- page: {page} -->")
        blocks.extend(text for _, text in sorted(by_page.get(page, [])))
    return "\n\n".join(blocks) + "\n"


def normalize_markdown_document(markdown: str, *, page_count: int = 1) -> dict[str, Any]:
    """Derive the stable schema from Markdown when Marker JSON is unavailable."""
    page_count = max(1, page_count)
    document = _document("markdown_fallback", range(1, page_count + 1))
    current_section: str | None = None
    page = 1
    # str.splitlines() treats form-feed as a line boundary and discards it.
    # Keep it as a sentinel so Markdown exported with PDF page breaks retains
    # deterministic page/source positions.
    lines = markdown.replace("\f", "\n\f\n").split("\n")
    index = 0
    pending_figure: dict[str, Any] | None = None
    ordinal = 0
    while index < len(lines):
        raw_line = lines[index]
        if raw_line == "\f":
            page = min(page + 1, page_count)
            index += 1
            continue
        line = raw_line.strip()
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
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            })
            ordinal += 1
            index += 1
            continue
        image = _IMAGE.search(line)
        if image:
            pending_figure = {
                "path": image.group(2), "alt_text": image.group(1), "caption": None,
                "page": page, "section_id": current_section,
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            }
            document["figures"].append(pending_figure)
            ordinal += 1
            index += 1
            continue
        if _CAPTION.match(line):
            caption = {
                "text": line.strip("*"), "page": page, "section_id": current_section,
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            }
            document["captions"].append(caption)
            ordinal += 1
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
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            })
            ordinal += 1
            index = end + 1 if end < len(lines) else end
            continue
        if line.startswith("|") and index + 1 < len(lines) and lines[index + 1].strip().startswith("|"):
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith("|"):
                end += 1
            document["tables"].append({
                "markdown": "\n".join(lines[index:end]), "page": page, "section_id": current_section,
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            })
            ordinal += 1
            index = end
            continue
        if line:
            document["paragraphs"].append({
                "text": line, "page": page, "section_id": current_section,
                "ordinal": ordinal,
                "source_position": {"page": page, "line": index + 1},
            })
            ordinal += 1
        index += 1
    return document
