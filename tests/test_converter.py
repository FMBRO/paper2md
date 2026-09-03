import json
from pathlib import Path

import fitz

from src.converter import Converter


def test_converter_writes_artifact_bundle_marker_document_and_quality_result(
    text_pdf: Path, tmp_path: Path,
) -> None:
    def fake_marker(_pdf: Path, output_dir: Path, *, on_output=None) -> Path:
        output_dir.mkdir(parents=True)
        images = output_dir / "images"
        images.mkdir()
        (images / "plot.png").write_bytes(b"PNG")
        markdown = output_dir / "paper.md"
        markdown.write_text("# Title\n\nAbstract\n\n![plot](images/plot.png)\n", encoding="utf-8")
        (output_dir / "document.json").write_text(json.dumps({"pages": [{
            "page": 1,
            "blocks": [
                {"type": "SectionHeader", "text": "Title", "level": 1},
                {"type": "Text", "text": "A sufficiently long clean paragraph for quality."},
                {"type": "Figure", "path": "figures/figure_001.png", "alt_text": "plot"},
                {"type": "Caption", "text": "Figure 1. Plot."},
            ],
        }]}), encoding="utf-8")
        return markdown

    bundle = Converter(marker_runner=fake_marker).convert(text_pdf, tmp_path / "artifacts" / "paper")

    assert bundle.root == tmp_path / "artifacts" / "paper"
    assert bundle.source_pdf.exists()
    assert bundle.paper_md.exists()
    assert (bundle.figures_dir / "figure_001.png").exists()
    assert json.loads(bundle.document_json.read_text(encoding="utf-8"))["source"] == "marker"
    quality = json.loads((bundle.logs_dir / "quality_result.json").read_text(encoding="utf-8"))
    assert quality["passed"] is True
    assert json.loads((bundle.logs_dir / "conversion_report.json").read_text(encoding="utf-8"))["quality_passed"] is True


def test_converter_uses_ocr_only_for_empty_pages_then_gates_post_ocr_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Text on page one")
    doc.new_page()
    doc.save(source)
    doc.close()
    calls: list[tuple[Path, Path]] = []

    def fake_ocr(src: Path, dst: Path, **_kwargs) -> Path:
        calls.append((src, dst))
        ocr_doc = fitz.open()
        ocr_doc.new_page().insert_text((72, 72), "OCR supplied enough text for page one.")
        ocr_doc.new_page().insert_text((72, 72), "OCR supplied enough text for page two.")
        ocr_doc.save(dst)
        ocr_doc.close()
        return dst

    def fake_marker(_pdf: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True)
        markdown = output_dir / "paper.md"
        markdown.write_text("# Paper\n", encoding="utf-8")
        (output_dir / "structured.json").write_text(json.dumps({"pages": [
            {"page": 1, "blocks": [{"type": "SectionHeader", "text": "Paper", "level": 1}]},
            {"page": 2, "blocks": [{"type": "Text", "text": "Recovered body text."}]},
        ]}), encoding="utf-8")
        return markdown

    bundle = Converter(marker_runner=fake_marker, ocr_runner=fake_ocr).convert(source, tmp_path / "artifact")

    assert len(calls) == 1
    assert calls[0][1] == bundle.root / "paper_ocr.pdf"
    quality = json.loads((bundle.logs_dir / "quality_result.json").read_text(encoding="utf-8"))
    assert quality["passed"] is True


def test_converter_recovers_sparse_nonempty_text_with_skip_text_ocr(tmp_path: Path) -> None:
    source = tmp_path / "sparse.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "tiny")
    doc.save(source)
    doc.close()
    ocr_kwargs: dict = {}

    def fake_ocr(_src: Path, dst: Path, **kwargs) -> Path:
        ocr_kwargs.update(kwargs)
        ocr_doc = fitz.open()
        ocr_doc.new_page().insert_text((72, 72), "Recovered text that is long enough for the quality gate.")
        ocr_doc.save(dst)
        ocr_doc.close()
        return dst

    def fake_marker(_pdf: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True)
        markdown = output_dir / "paper.md"
        markdown.write_text("# Paper\n", encoding="utf-8")
        (output_dir / "document.json").write_text(json.dumps({"pages": [{"page": 1, "blocks": [
            {"type": "SectionHeader", "text": "Paper", "level": 1},
        ]}]}), encoding="utf-8")
        return markdown

    Converter(marker_runner=fake_marker, ocr_runner=fake_ocr).convert(source, tmp_path / "artifact")

    assert ocr_kwargs["mode"] == "skip_text"


def test_converter_uses_force_ocr_mode_when_explicitly_requested(text_pdf: Path, tmp_path: Path) -> None:
    ocr_kwargs: dict = {}

    def fake_ocr(_src: Path, dst: Path, **kwargs) -> Path:
        ocr_kwargs.update(kwargs)
        ocr_doc = fitz.open()
        ocr_doc.new_page().insert_text((72, 72), "Forced OCR text that is long enough for the quality gate.")
        ocr_doc.save(dst)
        ocr_doc.close()
        return dst

    def fake_marker(_pdf: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True)
        markdown = output_dir / "paper.md"
        markdown.write_text("# Paper\n", encoding="utf-8")
        (output_dir / "document.json").write_text(json.dumps({"pages": [{"page": 1, "blocks": [
            {"type": "SectionHeader", "text": "Paper", "level": 1},
        ]}]}), encoding="utf-8")
        return markdown

    Converter(marker_runner=fake_marker, ocr_runner=fake_ocr, force_ocr=True).convert(text_pdf, tmp_path / "artifact")

    assert ocr_kwargs["mode"] == "force"


def test_converter_recovers_garbled_nonempty_source_text_with_automatic_ocr(
    mocker, text_pdf: Path, tmp_path: Path,
) -> None:
    source_pages = [{"page": 1, "character_count": 80, "has_text": True, "has_garbled_text": True}]
    recovered_pages = [{"page": 1, "character_count": 80, "has_text": True, "has_garbled_text": False}]
    inspect_pages = mocker.patch("src.converter.inspect_page_text", side_effect=[source_pages, recovered_pages])
    ocr_kwargs: dict = {}

    def fake_ocr(_src: Path, dst: Path, **kwargs) -> Path:
        ocr_kwargs.update(kwargs)
        ocr_doc = fitz.open()
        ocr_doc.new_page().insert_text((72, 72), "Recovered text that is long enough for the quality gate.")
        ocr_doc.save(dst)
        ocr_doc.close()
        return dst

    def fake_marker(_pdf: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True)
        markdown = output_dir / "paper.md"
        markdown.write_text("# Paper\n", encoding="utf-8")
        (output_dir / "document.json").write_text(json.dumps({"pages": [{"page": 1, "blocks": [
            {"type": "SectionHeader", "text": "Paper", "level": 1},
        ]}]}), encoding="utf-8")
        return markdown

    Converter(marker_runner=fake_marker, ocr_runner=fake_ocr).convert(text_pdf, tmp_path / "artifact")

    assert inspect_pages.call_count == 2
    assert ocr_kwargs["mode"] == "skip_text"
