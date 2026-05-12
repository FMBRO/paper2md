"""Shared pytest fixtures: generate small synthetic PDFs for the test suite."""
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageDraw


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    """Tiny PDF that has a real text layer."""
    pdf_path = tmp_path / "text_paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    body = ("This is a synthetic test paper used by the paper2md test suite. "
            "It contains enough characters to satisfy the text-layer heuristic. ") * 4
    page.insert_text((72, 72), body, fontsize=10)
    doc.save(pdf_path)
    doc.close()
    return pdf_path


@pytest.fixture
def image_pdf(tmp_path: Path) -> Path:
    """Tiny PDF with NO text layer — a single rasterized page."""
    pdf_path = tmp_path / "image_paper.pdf"
    img = Image.new("RGB", (612, 792), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 60), "rasterized page (no text layer)", fill="black")
    img.save(pdf_path, "PDF", resolution=72)
    return pdf_path
