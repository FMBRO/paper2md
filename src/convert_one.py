"""Single-PDF orchestration (spec §4.2 / §13.1)."""
from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from src.evaluate_quality import write_quality_report
from src.extract_figures import collect_marker_figures
from src.inspect_pdf import inspect_pdf, write_text_layer_report
from src.normalize_markdown import normalize_markdown
from src.pipeline_events import EventCallback, PipelineEvent
from src.run_marker import run_marker
from src.run_ocr import run_ocrmypdf


def convert_one(
    input_pdf: Path | str,
    settings: Settings,
    on_event: EventCallback | None = None,
) -> dict:
    input_pdf = Path(input_pdf)
    paper_name = input_pdf.stem
    paper_dir = settings.output_dir / paper_name
    logs_dir = paper_dir / "logs"
    paper_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    pipeline_log = logs_dir / "pipeline.log"

    with pipeline_log.open("w", encoding="utf-8", buffering=1) as log_file:
        def emit(event: PipelineEvent) -> None:
            if on_event is not None:
                on_event(event)

        def log(message: str) -> None:
            log_file.write(f"{message}\n")
            emit(PipelineEvent(kind="log", pdf_name=input_pdf.name, message=message))
            if on_event is None:
                print(message, flush=True)

        def stage(name: str, message: str) -> None:
            emit(PipelineEvent(
                kind="stage_changed",
                pdf_name=input_pdf.name,
                stage=name,
                message=message,
            ))
            log(message)

        try:
            # [1] inspection
            stage("inspection", "Inspecting PDF")
            inspection = inspect_pdf(input_pdf)
            write_text_layer_report(input_pdf, logs_dir)

            # [2] OCR branch
            ocr_executed = False
            target_pdf = input_pdf
            needs_ocr = settings.force_ocr or (
                settings.enable_ocr and not inspection["has_text_layer"]
            )
            if needs_ocr:
                stage("ocr", "Running OCRmyPDF")
                ocr_pdf = paper_dir / "paper_ocr.pdf"
                run_ocrmypdf(
                    input_pdf,
                    ocr_pdf,
                    lang=settings.language,
                    deskew=settings.ocr_deskew,
                    clean=settings.ocr_clean,
                    mode="force" if settings.force_ocr else "skip_text",
                    on_output=log,
                )
                target_pdf = ocr_pdf
                ocr_executed = True
            else:
                log("OCR not required")

            # [3] conversion
            stage("conversion", "Converting with Marker")
            if settings.engine != "marker":
                raise NotImplementedError(
                    f"Engine '{settings.engine}' not yet wired (see plan §future-work)"
                )
            raw_md = run_marker(target_pdf, paper_dir / "marker", on_output=log)

            # [4-6] post-processing and quality evaluation
            stage("post_processing", "Relocating figures")
            collect_marker_figures(raw_md, paper_dir / "figures")

            log("Normalizing Markdown")
            final_md = paper_dir / "paper.md"
            normalize_markdown(raw_md, final_md)

            log("Evaluating conversion quality")
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
            (logs_dir / "conversion_report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            log(f"Completed: {final_md}")
            return report
        except Exception as error:
            log(f"Failed: {error}")
            raise
