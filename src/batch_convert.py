"""Batch entry point (spec §4.1, §7)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.config import Settings, load_settings
from src.convert_one import convert_one


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="paper2md - batch convert academic PDFs to Markdown")
    p.add_argument("--input_dir", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--engine", choices=["marker", "mineru", "docling", "nougat"], default="marker")
    p.add_argument("--enable_ocr", action="store_true")
    p.add_argument("--force_ocr", action="store_true")
    p.add_argument("--ocr_lang", default="eng")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--skip_existing", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--save_logs", action="store_true")
    return p


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {
        "input_dir": Path(args.input_dir),
        "output_dir": Path(args.output_dir),
        "engine": args.engine,
        "language": args.ocr_lang,
        "enable_ocr": args.enable_ocr or None,  # only override if set
        "force_ocr": args.force_ocr or None,
        "workers": args.workers,
        "skip_existing": args.skip_existing or None,
        "overwrite": args.overwrite or None,
        "save_logs": args.save_logs or None,
    }
    cleaned = {k: v for k, v in overrides.items() if v is not None}
    config_path = Path(args.config)
    if config_path.exists():
        return load_settings(config_path, overrides=cleaned)
    # No YAML — build Settings directly from CLI defaults.
    return Settings(**cleaned)


def run_batch(settings: Settings) -> dict:
    pdfs = sorted(Path(settings.input_dir).glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in {settings.input_dir}")

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"total": len(pdfs), "success": 0, "failed": 0, "failed_files": []}

    for pdf in pdfs:
        paper_md = settings.output_dir / pdf.stem / "paper.md"
        if settings.skip_existing and paper_md.exists():
            summary["success"] += 1
            continue
        try:
            convert_one(pdf, settings)
            summary["success"] += 1
        except Exception as err:
            summary["failed"] += 1
            summary["failed_files"].append(pdf.name)
            if not settings.continue_on_error:
                _write_summary(settings.output_dir, summary)
                raise
            print(f"[paper2md] {pdf.name} failed: {err}", file=sys.stderr)

    _write_summary(settings.output_dir, summary)
    return summary


def _write_summary(output_dir: Path, summary: dict) -> None:
    (output_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = _settings_from_args(args)
    summary = run_batch(settings)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
