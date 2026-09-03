"""PDF inspection: text-layer detection, page count, page rasterization."""
from __future__ import annotations

import json
import re
from pathlib import Path

import fitz


_GARBLED_TEXT = re.compile(r"\ufffd|[\u0000-\u0008\u000b\u000c\u000e-\u001f]")


def inspect_page_text(pdf_path: Path | str) -> list[dict]:
    """Return text coverage for every page, including mixed-text PDFs."""
    doc = fitz.open(str(pdf_path))
    try:
        result: list[dict] = []
        for index, page in enumerate(doc):
            text = page.get_text().strip()
            result.append({
                "page": index + 1,
                "character_count": len(text),
                "has_text": bool(text),
                "has_garbled_text": bool(_GARBLED_TEXT.search(text)),
            })
        return result
    finally:
        doc.close()


def has_text_layer(pdf_path: Path | str, min_chars: int = 100, max_pages: int = 3) -> bool:
    """Return True when the first ``max_pages`` pages contain at least ``min_chars`` of text."""
    doc = fitz.open(str(pdf_path))
    try:
        text = ""
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text += page.get_text()
        return len(text.strip()) >= min_chars
    finally:
        doc.close()


def inspect_pdf(pdf_path: Path | str, min_chars: int = 100, max_pages: int = 3) -> dict:
    """Build the §5.1.3 inspection record for a PDF."""
    pdf_path = Path(pdf_path)
    doc = fitz.open(str(pdf_path))
    try:
        pages = []
        for index, page in enumerate(doc):
            page_text = page.get_text().strip()
            pages.append({
                "page": index + 1,
                "character_count": len(page_text),
                "has_text": bool(page_text),
                "has_garbled_text": bool(_GARBLED_TEXT.search(page_text)),
            })
        text = ""
        checked = 0
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text += page.get_text()
            checked = i + 1
        char_count = len(text.strip())
        return {
            "input_pdf": str(pdf_path),
            "has_text_layer": char_count >= min_chars,
            "page_count": doc.page_count,
            "checked_pages": checked,
            "extracted_char_count": char_count,
            "pages": pages,
        }
    finally:
        doc.close()


def write_text_layer_report(pdf_path: Path | str, logs_dir: Path | str) -> Path:
    """Write the inspection record to ``{logs_dir}/text_layer_check.json``."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "text_layer_check.json"
    out_path.write_text(json.dumps(inspect_pdf(pdf_path), indent=2), encoding="utf-8")
    return out_path


def render_pages(pdf_path: Path | str, output_dir: Path | str, zoom: float = 2.0) -> list[Path]:
    """Rasterize every page to ``output_dir/page_{NNN}.png`` and return the paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    matrix = fitz.Matrix(zoom, zoom)
    paths: list[Path] = []
    try:
        for i, page in enumerate(doc):
            out = output_dir / f"page_{i + 1:03d}.png"
            page.get_pixmap(matrix=matrix).save(out)
            paths.append(out)
        return paths
    finally:
        doc.close()
