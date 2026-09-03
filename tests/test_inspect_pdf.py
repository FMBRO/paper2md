import json
from pathlib import Path

import fitz

from src.inspect_pdf import (
    has_text_layer,
    inspect_page_text,
    inspect_pdf,
    render_pages,
    write_text_layer_report,
)


def test_has_text_layer_true_for_text_pdf(text_pdf: Path) -> None:
    assert has_text_layer(text_pdf) is True


def test_has_text_layer_false_for_image_pdf(image_pdf: Path) -> None:
    assert has_text_layer(image_pdf) is False


def test_has_text_layer_threshold(text_pdf: Path) -> None:
    # If we demand 100000 chars, even the text fixture fails the threshold.
    assert has_text_layer(text_pdf, min_chars=100_000) is False


def test_inspect_pdf_returns_report(text_pdf: Path) -> None:
    report = inspect_pdf(text_pdf)
    assert report["input_pdf"] == str(text_pdf)
    assert report["has_text_layer"] is True
    assert report["page_count"] == 1
    assert report["checked_pages"] == 1
    assert report["extracted_char_count"] > 100
    assert report["pages"] == inspect_page_text(text_pdf)


def test_write_text_layer_report(text_pdf: Path, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    out = write_text_layer_report(text_pdf, logs_dir)
    assert out == logs_dir / "text_layer_check.json"
    payload = json.loads(out.read_text())
    assert payload["has_text_layer"] is True


def test_render_pages_writes_one_png_per_page(text_pdf: Path, tmp_path: Path) -> None:
    pages_dir = tmp_path / "pages"
    paths = render_pages(text_pdf, pages_dir)
    assert len(paths) == 1
    assert paths[0] == pages_dir / "page_001.png"
    assert paths[0].exists()
    assert paths[0].stat().st_size > 0


def test_inspect_page_text_reports_every_page_in_mixed_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "mixed.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Text on first page")
    doc.new_page()
    doc.save(pdf)
    doc.close()

    pages = inspect_page_text(pdf)

    assert pages == [
        {"page": 1, "character_count": 18, "has_text": True},
        {"page": 2, "character_count": 0, "has_text": False},
    ]
