"""Structured conversion service built around the existing Marker wrapper."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.artifacts import ArtifactManager
from src.document_normalizer import normalize_markdown_document, normalize_marker_document
from src.evaluate_quality import write_quality_report
from src.extract_figures import collect_marker_figures
from src.inspect_pdf import inspect_pdf, inspect_page_text
from src.normalize_markdown import normalize_markdown
from src.quality_gate import evaluate_document_quality
from src.research_models import ArtifactBundle
from src.run_marker import run_marker
from src.run_ocr import run_ocrmypdf


def _load_marker_document(marker_dir: Path) -> dict[str, Any] | None:
    """Find a Marker JSON document without treating unrelated JSON as one."""
    for candidate in sorted(marker_dir.rglob("*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
            return payload
    return None


class Converter:
    """Convert one PDF into the durable ArtifactBundle consumed by the pipeline."""

    def __init__(
        self,
        *,
        marker_runner: Callable[..., Path] = run_marker,
        ocr_runner: Callable[..., Path] = run_ocrmypdf,
        enable_ocr: bool = True,
        force_ocr: bool = False,
        language: str = "eng",
        ocr_deskew: bool = True,
        ocr_clean: bool = True,
    ) -> None:
        self.marker_runner = marker_runner
        self.ocr_runner = ocr_runner
        self.enable_ocr = enable_ocr
        self.force_ocr = force_ocr
        self.language = language
        self.ocr_deskew = ocr_deskew
        self.ocr_clean = ocr_clean

    def convert(self, pdf_path: Path | str, artifact_dir: Path | str) -> ArtifactBundle:
        pdf_path = Path(pdf_path)
        artifact_dir = Path(artifact_dir)
        manager = ArtifactManager(artifact_dir.parent, artifact_dir.name)
        bundle = manager.create()
        source_pdf = manager.copy_source(pdf_path)
        source_page_text = inspect_page_text(source_pdf)
        source_inspection = inspect_pdf(source_pdf)
        manager.write_json(bundle.logs_dir / "source_page_text_coverage.json", source_page_text)

        target_pdf = source_pdf
        ocr_used = self.force_ocr or (
            self.enable_ocr and any(not page["has_text"] for page in source_page_text)
        )
        if ocr_used:
            target_pdf = bundle.root / "paper_ocr.pdf"
            self.ocr_runner(
                source_pdf, target_pdf, lang=self.language, deskew=self.ocr_deskew,
                clean=self.ocr_clean,
            )

        page_text = inspect_page_text(target_pdf)
        inspection = inspect_pdf(target_pdf)
        manager.write_json(bundle.logs_dir / "page_text_coverage.json", page_text)
        manager.write_json(bundle.logs_dir / "text_layer_check.json", inspection)

        marker_dir = bundle.root / "marker"
        raw_markdown = self.marker_runner(target_pdf, marker_dir)
        collect_marker_figures(raw_markdown, bundle.figures_dir)
        normalize_markdown(raw_markdown, bundle.paper_md)

        marker_document = _load_marker_document(marker_dir)
        if marker_document is None:
            document = normalize_markdown_document(
                bundle.paper_md.read_text(encoding="utf-8"),
                page_count=inspection["page_count"],
            )
        else:
            document = normalize_marker_document(marker_document)
        manager.write_document(document)
        quality = evaluate_document_quality(document, page_text=page_text)
        manager.write_json(bundle.logs_dir / "quality_result.json", quality)
        write_quality_report(
            markdown_path=bundle.paper_md,
            source_pdf=source_pdf,
            logs_dir=bundle.logs_dir,
            engine="marker",
            has_text_layer=inspection["has_text_layer"],
            ocr_used=ocr_used,
            page_count=inspection["page_count"],
        )
        report = {
            "input_file": pdf_path.name,
            "has_text_layer": inspection["has_text_layer"],
            "source_has_text_layer": source_inspection["has_text_layer"],
            "ocr_executed": ocr_used,
            "engine": "marker",
            "status": "success",
            "output_markdown": str(bundle.paper_md),
            "document_source": document["source"],
            "quality_passed": quality["passed"],
            "llm_allowed": quality["llm_allowed"],
        }
        manager.write_json(bundle.logs_dir / "conversion_report.json", report)
        manager.write_text(bundle.logs_dir / "converter.log", "Conversion completed\n")
        return bundle
