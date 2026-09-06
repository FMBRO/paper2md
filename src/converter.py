"""Structured conversion service built around the existing Marker wrapper."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fitz

from src.artifacts import ArtifactManager
from src.document_normalizer import (
    load_marker_document,
    normalize_markdown_document,
    normalize_marker_document,
)
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
    return load_marker_document(marker_dir)


def _recover_empty_equations_from_pdf(
    document: dict[str, Any], pdf_path: Path,
) -> None:
    """Fill empty positioned equations from the PDF text/OCR layer when possible."""
    empty = [
        equation for equation in document.get("equations", [])
        if isinstance(equation, dict) and not str(equation.get("text", "")).strip()
    ]
    if not empty:
        return
    with fitz.open(pdf_path) as pdf:
        for equation in empty:
            position = equation.get("source_position")
            bbox = position.get("bbox") if isinstance(position, dict) else None
            page_number = equation.get("page")
            if (
                not isinstance(page_number, int)
                or not 1 <= page_number <= pdf.page_count
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or not all(isinstance(value, (int, float)) for value in bbox)
            ):
                continue
            text = pdf[page_number - 1].get_text("text", clip=fitz.Rect(bbox)).strip()
            if text:
                equation["text"] = text


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
        minimum_page_characters: int = 20,
    ) -> None:
        self.marker_runner = marker_runner
        self.ocr_runner = ocr_runner
        self.enable_ocr = enable_ocr
        self.force_ocr = force_ocr
        self.language = language
        self.ocr_deskew = ocr_deskew
        self.ocr_clean = ocr_clean
        self.minimum_page_characters = minimum_page_characters

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
        sparse_or_garbled = any(
            page["has_text"] and (
                page["character_count"] < self.minimum_page_characters
                or page["has_garbled_text"]
            )
            for page in source_page_text
        )
        textless = any(not page["has_text"] for page in source_page_text)
        nonempty = any(page["has_text"] for page in source_page_text)
        ocr_used = self.force_ocr or self.enable_ocr and (
            sparse_or_garbled or textless
        )
        if ocr_used:
            target_pdf = bundle.root / "paper_ocr.pdf"
            mode = (
                "force" if self.force_ocr
                else "redo" if sparse_or_garbled
                else "skip_text" if textless and nonempty
                else "auto"
            )
            self.ocr_runner(
                source_pdf, target_pdf, lang=self.language, deskew=self.ocr_deskew,
                clean=self.ocr_clean, mode=mode,
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
        _recover_empty_equations_from_pdf(document, target_pdf)
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
