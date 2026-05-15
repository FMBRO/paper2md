from pathlib import Path

from src.config import Settings
from src.convert_one import convert_one


def _settings(input_dir: Path, output_dir: Path, **overrides) -> Settings:
    return Settings(input_dir=input_dir, output_dir=output_dir, **overrides)


def test_convert_one_text_pdf_skips_ocr(mocker, text_pdf: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    settings = _settings(text_pdf.parent, output_dir)

    def fake_marker(_pdf, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / "paper.md"
        md.write_text("# Title\n\nAbstract\n\nbody", encoding="utf-8")
        return md

    ocr_spy = mocker.patch("src.convert_one.run_ocrmypdf")
    mocker.patch("src.convert_one.run_marker", side_effect=fake_marker)

    report = convert_one(text_pdf, settings)

    ocr_spy.assert_not_called()
    assert report["status"] == "success"
    assert report["ocr_executed"] is False
    paper_dir = output_dir / text_pdf.stem
    assert (paper_dir / "paper.md").exists()
    assert (paper_dir / "logs" / "text_layer_check.json").exists()
    assert (paper_dir / "logs" / "conversion_report.json").exists()
    assert (paper_dir / "logs" / "quality_report.md").exists()
    # Normalization ran
    assert "## Abstract" in (paper_dir / "paper.md").read_text(encoding="utf-8")


def test_convert_one_image_pdf_runs_ocr(mocker, image_pdf: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    settings = _settings(image_pdf.parent, output_dir, enable_ocr=True)

    def fake_ocr(src, dst, **_):
        Path(dst).write_bytes(b"%PDF-1.4-FAKE-OCR")
        return Path(dst)

    def fake_marker(_pdf, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / "paper.md"
        md.write_text("# Title\n\n## Abstract\n\nbody", encoding="utf-8")
        return md

    ocr_spy = mocker.patch("src.convert_one.run_ocrmypdf", side_effect=fake_ocr)
    mocker.patch("src.convert_one.run_marker", side_effect=fake_marker)

    report = convert_one(image_pdf, settings)
    ocr_spy.assert_called_once()
    assert report["ocr_executed"] is True
    paper_dir = output_dir / image_pdf.stem
    assert (paper_dir / "paper_ocr.pdf").exists()


def test_convert_one_passes_ocr_options_to_run_ocrmypdf(mocker, image_pdf: Path, tmp_path: Path) -> None:
    """Settings.ocr_deskew / Settings.ocr_clean must reach run_ocrmypdf so the
    YAML config keys ocr.options.{deskew,clean} are actually honoured."""
    output_dir = tmp_path / "output"
    settings = _settings(
        image_pdf.parent, output_dir,
        enable_ocr=True, ocr_deskew=False, ocr_clean=False,
    )

    def fake_ocr(src, dst, **_):
        Path(dst).write_bytes(b"%PDF-1.4-FAKE-OCR")
        return Path(dst)

    def fake_marker(_pdf, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / "paper.md"
        md.write_text("# Title\n\n## Abstract\n\nbody", encoding="utf-8")
        return md

    ocr_spy = mocker.patch("src.convert_one.run_ocrmypdf", side_effect=fake_ocr)
    mocker.patch("src.convert_one.run_marker", side_effect=fake_marker)

    convert_one(image_pdf, settings)

    kwargs = ocr_spy.call_args.kwargs
    assert kwargs["deskew"] is False
    assert kwargs["clean"] is False
