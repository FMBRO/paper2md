"""Single-PDF orchestration (spec §4.2 / §13.1)."""
from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from src.evaluate_quality import write_quality_report
from src.extract_figures import collect_marker_figures
from src.inspect_pdf import inspect_pdf, write_text_layer_report
from src.normalize_markdown import normalize_markdown
from src.run_marker import run_marker
from src.run_ocr import run_ocrmypdf


def convert_one(input_pdf: Path | str, settings: Settings) -> dict:
    input_pdf = Path(input_pdf)
    paper_name = input_pdf.stem
    paper_dir = settings.output_dir / paper_name
    logs_dir = paper_dir / "logs"
    paper_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # [1] inspection
    inspection = inspect_pdf(input_pdf)
    write_text_layer_report(input_pdf, logs_dir)

    # [2] OCR branch
    ocr_executed = False
    target_pdf = input_pdf
    needs_ocr = settings.force_ocr or (settings.enable_ocr and not inspection["has_text_layer"])
    if needs_ocr:
        ocr_pdf = paper_dir / "paper_ocr.pdf"
        run_ocrmypdf(
            input_pdf, ocr_pdf,
            lang=settings.language,
            deskew=settings.ocr_deskew,
            clean=settings.ocr_clean,
        )
        target_pdf = ocr_pdf
        ocr_executed = True

    # [3] conversion
    if settings.engine != "marker":
        raise NotImplementedError(f"Engine '{settings.engine}' not yet wired (see plan §future-work)")
    raw_md = run_marker(target_pdf, paper_dir / "marker")

    # [4] figure relocation
    collect_marker_figures(raw_md, paper_dir / "figures")

    # [5] markdown normalization
    final_md = paper_dir / "paper.md"
    normalize_markdown(raw_md, final_md)

    # [6] quality evaluation
    write_quality_report(
        markdown_path=final_md,
        source_pdf=input_pdf,
        logs_dir=logs_dir,
        engine=settings.engine,
        has_text_layer=inspection["has_text_layer"],
        ocr_used=ocr_executed,
        page_count=inspection["page_count"],
    )

    # [7] conversion report
    report = {
        "input_file": input_pdf.name,
        "has_text_layer": inspection["has_text_layer"],
        "ocr_executed": ocr_executed,
        "engine": settings.engine,
        "status": "success",
        "output_markdown": str(final_md),
    }
    (logs_dir / "conversion_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
