import os
from pathlib import Path
import fitz
import pytest

from src.run_ocr import OCRError, run_ocrmypdf


def test_run_ocrmypdf_builds_expected_command(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)
    mocker.patch("src.run_ocr.shutil.which", return_value="/usr/bin/unpaper")

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert args[0] == "ocrmypdf"
    assert "-l" in args and args[args.index("-l") + 1] == "eng"
    assert args[args.index("--output-type") + 1] == "pdf"
    assert "--deskew" in args
    assert "--clean" in args
    assert args[-2:] == [str(src_pdf), str(dst_pdf)]


def test_run_ocrmypdf_skips_pages_that_exceed_the_safe_raster_size(
    mocker, tmp_path: Path,
) -> None:
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(tmp_path / "in.pdf", tmp_path / "out.pdf", clean=False)

    args = fake_run.call_args.args[0]
    assert args[args.index("--skip-big") + 1] == "200"


def test_run_ocrmypdf_uses_skip_text_mode_for_mixed_page_recovery(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(src_pdf, dst_pdf, clean=False)

    args = fake_run.call_args.args[0]
    assert "--skip-text" in args
    assert "--force-ocr" not in args


def test_run_ocrmypdf_uses_force_mode_for_explicit_preprocessing(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(src_pdf, dst_pdf, clean=False, mode="force")

    args = fake_run.call_args.args[0]
    assert "--force-ocr" in args
    assert "--skip-text" not in args


def test_run_ocrmypdf_uses_redo_mode_for_sparse_or_garbled_text(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(src_pdf, dst_pdf, clean=False, mode="redo")

    args = fake_run.call_args.args[0]
    assert "--redo-ocr" in args
    assert "--skip-text" not in args
    assert "--force-ocr" not in args


def test_run_ocrmypdf_omits_deskew_in_redo_mode(mocker, tmp_path: Path) -> None:
    """OCRmyPDF rejects --redo-ocr combined with --deskew."""
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(
        tmp_path / "in.pdf",
        tmp_path / "out.pdf",
        clean=False,
        deskew=True,
        mode="redo",
    )

    args = fake_run.call_args.args[0]
    assert "--redo-ocr" in args
    assert "--deskew" not in args


def test_run_ocrmypdf_auto_mode_leaves_text_policy_to_ocrmypdf(mocker, tmp_path: Path) -> None:
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(tmp_path / "in.pdf", tmp_path / "out.pdf", clean=False, mode="auto")

    args = fake_run.call_args.args[0]
    assert "--redo-ocr" not in args
    assert "--skip-text" not in args
    assert "--force-ocr" not in args


def test_run_ocrmypdf_omits_flags_when_disabled(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=False, clean=False)

    args = fake_run.call_args.args[0]
    assert "--deskew" not in args
    assert "--clean" not in args


def test_run_ocrmypdf_raises_on_failure(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_ocr.run_streaming_command", return_value=2)

    with pytest.raises(OCRError):
        run_ocrmypdf(src_pdf, tmp_path / "out.pdf")


def test_run_ocrmypdf_accepts_exit_10_when_the_output_is_a_readable_pdf(
    mocker, tmp_path: Path,
) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"

    def fake_run(*_args, **_kwargs):
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Recovered OCR text")
        document.save(dst_pdf)
        document.close()
        return 10

    mocker.patch("src.run_ocr.run_streaming_command", side_effect=fake_run)

    assert run_ocrmypdf(src_pdf, dst_pdf, clean=False) == dst_pdf


def test_run_ocrmypdf_drops_clean_when_unpaper_missing(mocker, tmp_path: Path) -> None:
    """--clean requires the external `unpaper` binary; if it's missing on PATH
    (typical on Windows) we must drop the flag rather than letting ocrmypdf
    fail with exit 3."""
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)
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
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)
    mocker.patch("src.run_ocr.shutil.which", return_value="/usr/bin/unpaper")

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert "--clean" in args


def test_run_ocrmypdf_forwards_output_and_warning(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    messages: list[str] = []

    def fake_run(_args, on_output, *, env=None):
        on_output("ocr progress")
        return 0

    mocker.patch("src.run_ocr.shutil.which", return_value=None)
    mocker.patch("src.run_ocr.run_streaming_command", side_effect=fake_run)
    run_ocrmypdf(
        src_pdf,
        tmp_path / "out.pdf",
        clean=True,
        on_output=messages.append,
    )
    assert "unpaper" in messages[0]
    assert messages[1] == "ocr progress"


def test_run_ocrmypdf_adds_standard_windows_tesseract_directory_to_child_path(
    mocker, tmp_path: Path,
) -> None:
    """A standard install must work even before the parent shell reloads PATH."""
    install_dir = tmp_path / "Tesseract-OCR"
    install_dir.mkdir()
    (install_dir / "tesseract.exe").write_bytes(b"fixture")
    original_path = os.environ.get("PATH", "")
    mocker.patch.dict(
        os.environ,
        {"ProgramFiles": str(tmp_path), "ProgramFiles(x86)": str(tmp_path), "PATH": original_path},
    )
    mocker.patch("src.run_ocr.shutil.which", return_value=None)
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(tmp_path / "in.pdf", tmp_path / "out.pdf", clean=False)

    child_environment = fake_run.call_args.kwargs["env"]
    assert child_environment["PATH"].split(os.pathsep)[0] == str(install_dir)
    assert os.environ.get("PATH", "") == original_path


def test_run_ocrmypdf_finds_per_user_windows_tesseract_install(
    mocker, tmp_path: Path,
) -> None:
    install_dir = tmp_path / "Programs" / "Tesseract-OCR"
    install_dir.mkdir(parents=True)
    (install_dir / "tesseract.exe").write_bytes(b"fixture")
    mocker.patch.dict(
        os.environ,
        {
            "LOCALAPPDATA": str(tmp_path),
            "ProgramFiles": str(tmp_path / "missing"),
            "ProgramFiles(x86)": str(tmp_path / "missing-x86"),
            "PATH": "",
        },
    )
    mocker.patch("src.run_ocr.shutil.which", return_value=None)
    fake_run = mocker.patch("src.run_ocr.run_streaming_command", return_value=0)

    run_ocrmypdf(tmp_path / "in.pdf", tmp_path / "out.pdf", clean=False)

    assert fake_run.call_args.kwargs["env"]["PATH"] == str(install_dir)
