from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_ocr import OCRError, run_ocrmypdf


def test_run_ocrmypdf_builds_expected_command(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))
    mocker.patch("src.run_ocr.shutil.which", return_value="/usr/bin/unpaper")

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert args[0] == "ocrmypdf"
    assert "-l" in args and args[args.index("-l") + 1] == "eng"
    assert "--deskew" in args
    assert "--clean" in args
    assert args[-2:] == [str(src_pdf), str(dst_pdf)]


def test_run_ocrmypdf_omits_flags_when_disabled(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=False, clean=False)

    args = fake_run.call_args.args[0]
    assert "--deskew" not in args
    assert "--clean" not in args


def test_run_ocrmypdf_raises_on_failure(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_ocr.subprocess.run",
                 return_value=MagicMock(returncode=2, stderr=b"boom"))

    with pytest.raises(OCRError):
        run_ocrmypdf(src_pdf, tmp_path / "out.pdf")


def test_run_ocrmypdf_drops_clean_when_unpaper_missing(mocker, tmp_path: Path) -> None:
    """--clean requires the external `unpaper` binary; if it's missing on PATH
    (typical on Windows) we must drop the flag rather than letting ocrmypdf
    fail with exit 3."""
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))
    mocker.patch("src.run_ocr.shutil.which", return_value=None)

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert "--clean" not in args
    # --deskew does not depend on unpaper and must still be applied.
    assert "--deskew" in args


def test_run_ocrmypdf_keeps_clean_when_unpaper_present(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))
    mocker.patch("src.run_ocr.shutil.which", return_value="/usr/bin/unpaper")

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert "--clean" in args
