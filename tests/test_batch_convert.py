import json
from pathlib import Path

import pytest

from src.batch_convert import build_arg_parser, run_batch
from src.config import Settings


def _seed_pdfs(input_dir: Path, names: list[str]) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (input_dir / name).write_bytes(b"%PDF-1.4")


def test_run_batch_writes_summary_after_all_pdfs(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["a.pdf", "b.pdf"])
    settings = Settings(input_dir=input_dir, output_dir=output_dir)

    mocker.patch(
        "src.batch_convert.convert_one",
        side_effect=lambda pdf, _s: {"input_file": pdf.name, "status": "success"},
    )

    summary = run_batch(settings)
    assert summary == {"total": 2, "success": 2, "failed": 0, "failed_files": []}
    assert (output_dir / "batch_summary.json").exists()
    payload = json.loads((output_dir / "batch_summary.json").read_text(encoding="utf-8"))
    assert payload["total"] == 2


def test_run_batch_continues_on_failure(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["good.pdf", "bad.pdf", "good2.pdf"])
    settings = Settings(input_dir=input_dir, output_dir=output_dir, continue_on_error=True)

    def maybe_fail(pdf, _s):
        if pdf.name == "bad.pdf":
            raise RuntimeError("boom")
        return {"input_file": pdf.name, "status": "success"}

    mocker.patch("src.batch_convert.convert_one", side_effect=maybe_fail)

    summary = run_batch(settings)
    assert summary["success"] == 2
    assert summary["failed"] == 1
    assert summary["failed_files"] == ["bad.pdf"]


def test_run_batch_skip_existing(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["a.pdf"])
    existing = output_dir / "a" / "paper.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# already done", encoding="utf-8")

    settings = Settings(input_dir=input_dir, output_dir=output_dir, skip_existing=True)
    spy = mocker.patch("src.batch_convert.convert_one")

    summary = run_batch(settings)
    spy.assert_not_called()
    assert summary == {"total": 1, "success": 1, "failed": 0, "failed_files": []}


def test_arg_parser_minimal() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--input_dir", "in", "--output_dir", "out"])
    assert args.input_dir == "in"
    assert args.output_dir == "out"
    assert args.engine == "marker"


def test_arg_parser_supports_all_flags() -> None:
    parser = build_arg_parser()
    args = parser.parse_args([
        "--input_dir", "in", "--output_dir", "out",
        "--engine", "docling", "--enable_ocr", "--force_ocr",
        "--ocr_lang", "eng", "--workers", "2",
        "--skip_existing", "--overwrite", "--save_logs",
    ])
    assert args.engine == "docling"
    assert args.enable_ocr is True
    assert args.force_ocr is True
    assert args.workers == 2
    assert args.skip_existing is True
    assert args.overwrite is True
