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
