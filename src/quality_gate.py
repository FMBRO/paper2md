"""Deterministic conversion quality gate used before LLM processing."""
from __future__ import annotations

import re
from typing import Any


_GARBLED = re.compile(r"\ufffd|[\u0000-\u0008\u000b\u000c\u000e-\u001f]")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
_FIGURE_CAPTION = re.compile(
    r"^\s*(?:fig(?:ure)?\.?)\s*(\d+)(?:\s*\([a-z]\))?\s*[.:]",
    re.IGNORECASE,
)
_MARKER_FIGURE_CAPTION = re.compile(
    r"^\s*fig\.\s*(\d+)(?:\s*\([a-z]\))?\s+(?!shows?\b)",
    re.IGNORECASE,
)


def _figure_caption_label(text: str) -> str | None:
    match = _FIGURE_CAPTION.match(text) or _MARKER_FIGURE_CAPTION.match(text)
    return match.group(1) if match is not None else None


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _valid_table(markdown: str) -> bool:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 3 or not _TABLE_SEPARATOR.match(lines[1]):
        return False
    return all(line.count("|") >= 2 for line in lines)


def _sections_are_ordered(sections: list[dict[str, Any]]) -> bool:
    previous = 0
    for section in sections:
        level = section.get("level")
        if not isinstance(level, int) or not 1 <= level <= 6 or level > previous + 1:
            return False
        previous = level
    return True


def _normalized_text(document: dict[str, Any]) -> list[tuple[int | None, str]]:
    fields = {
        "sections": ("title",),
        "paragraphs": ("text",),
        "tables": ("markdown",),
        "equations": ("text",),
        "figures": ("path", "alt_text", "caption"),
        "captions": ("text",),
    }
    values: list[tuple[int | None, str]] = []
    for collection, names in fields.items():
        for item in document.get(collection, []):
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            for name in names:
                value = item.get(name)
                if isinstance(value, str) and value.strip():
                    values.append((page if isinstance(page, int) else None, value))
    return values


def _figure_caption_mismatch(
    figures: list[dict[str, Any]], captions: list[dict[str, Any]],
) -> bool:
    labels_by_page: dict[int, set[str]] = {}
    caption_records = [
        *captions,
        *(
            {"page": figure.get("page"), "text": figure.get("caption")}
            for figure in figures if figure.get("caption")
        ),
    ]
    for caption in caption_records:
        text = str(caption.get("text", ""))
        label = _figure_caption_label(text)
        page = caption.get("page")
        if label is not None and isinstance(page, int):
            labels_by_page.setdefault(page, set()).add(label)

    if not labels_by_page:
        return any(
            bool(figure.get("path") or figure.get("alt_text") or figure.get("caption"))
            for figure in figures
        )
    if not figures:
        return False

    unique_labels = {
        label for labels in labels_by_page.values() for label in labels
    }
    return len(figures) < len(unique_labels)


def evaluate_document_quality(
    document: dict[str, Any],
    *,
    page_text: list[dict[str, Any]],
    minimum_characters_per_page: int = 20,
) -> dict[str, Any]:
    """Evaluate deterministic artifacts and return an LLM authorization result."""
    issues: list[dict[str, str]] = []
    pages = document.get("pages", [])
    expected_pages = {page.get("number") for page in pages if isinstance(page, dict) and isinstance(page.get("number"), int)}
    inspected_pages = {page.get("page") for page in page_text if isinstance(page.get("page"), int)}
    if expected_pages != inspected_pages:
        issues.append(_issue("page_coverage", "Structured document pages do not exactly match inspected PDF pages."))
    if any(not page.get("has_text") for page in page_text):
        issues.append(_issue("empty_page_text", "At least one PDF page has no extractable text."))
    if any(page.get("character_count", 0) < minimum_characters_per_page for page in page_text):
        issues.append(_issue("low_character_density", "At least one PDF page has too little text."))

    normalized_text = _normalized_text(document)
    populated_pages = {page for page, value in normalized_text if page in expected_pages and value.strip()}
    if expected_pages - populated_pages:
        issues.append(_issue("empty_document_page", "At least one structured document page has no normalized content."))
    text = "\n".join(value for _, value in normalized_text)
    if _GARBLED.search(text):
        issues.append(_issue("garbled_characters", "Replacement or control characters were found."))
    sections = [item for item in document.get("sections", []) if isinstance(item, dict)]
    if sections and not _sections_are_ordered(sections):
        issues.append(_issue("section_hierarchy", "Section heading levels are malformed or skip a level."))
    if any(not _valid_table(str(table.get("markdown", ""))) for table in document.get("tables", []) if isinstance(table, dict)):
        issues.append(_issue("malformed_table", "At least one table is empty or malformed."))
    if any(not str(equation.get("text", "")).strip() for equation in document.get("equations", []) if isinstance(equation, dict)):
        issues.append(_issue("empty_equation", "At least one preserved equation has no content."))
    figures = [item for item in document.get("figures", []) if isinstance(item, dict)]
    captions = [item for item in document.get("captions", []) if isinstance(item, dict)]
    if _figure_caption_mismatch(figures, captions):
        issues.append(_issue("figure_caption_mismatch", "Figures and captions do not match one-to-one."))

    passed = not issues
    return {
        "passed": passed,
        "llm_allowed": passed,
        "issues": issues,
        "metrics": {
            "document_page_count": len(expected_pages),
            "inspected_page_count": len(inspected_pages),
            "minimum_characters_per_page": minimum_characters_per_page,
        },
    }
