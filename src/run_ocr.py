"""Wrapper around the `ocrmypdf` CLI (spec §5.3.2)."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Literal

import fitz

from src.subprocess_utils import OutputCallback, run_streaming_command


class OCRError(RuntimeError):
    """Raised when ocrmypdf exits non-zero."""


def _tesseract_directory() -> Path | None:
    executable = shutil.which("tesseract")
    if executable:
        return Path(executable).parent
    candidates: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    return None


def _ocr_environment() -> dict[str, str]:
    environment = os.environ.copy()
    tesseract_dir = _tesseract_directory()
    if tesseract_dir is None:
        return environment
    current_path = environment.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if str(tesseract_dir) not in path_entries:
        environment["PATH"] = os.pathsep.join([str(tesseract_dir), *path_entries])
    return environment


def run_ocrmypdf(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    lang: str = "eng",
    deskew: bool = True,
    clean: bool = True,
    mode: Literal["auto", "skip_text", "redo", "force"] = "skip_text",
    on_output: OutputCallback | None = None,
) -> Path:
    """Run ``ocrmypdf`` and return the output path on success.

    ``--clean`` requires the external ``unpaper`` binary, which is not bundled
    with the pip package and is awkward to install on Windows. When the user
    requests cleaning but ``unpaper`` is not on PATH we drop the flag and warn,
    instead of letting ``ocrmypdf`` abort with exit 3.
    """
    cmd: list[str] = [
        "ocrmypdf", "-l", lang, "--skip-big", "200", "--output-type", "pdf",
    ]
    if mode == "auto":
        pass
    elif mode == "skip_text":
        cmd.append("--skip-text")
    elif mode == "redo":
        cmd.append("--redo-ocr")
    elif mode == "force":
        cmd.append("--force-ocr")
    else:
        raise ValueError(f"Unsupported OCR mode: {mode}")
    # OCRmyPDF rejects --redo-ocr together with image-processing options such
    # as --deskew. Redo mode repairs the existing text layer without altering
    # page images, so leave deskewing to other OCR modes.
    if deskew and mode != "redo":
        cmd.append("--deskew")
    if clean:
        if shutil.which("unpaper"):
            cmd.append("--clean")
        else:
            warning = (
                "[paper2md] warning: 'unpaper' not found on PATH; "
                "running ocrmypdf without --clean (install unpaper to enable noise removal)."
            )
            if on_output is None:
                print(warning, file=sys.stderr)
            else:
                on_output(warning)
    cmd.extend([str(input_pdf), str(output_pdf)])

    returncode = run_streaming_command(cmd, on_output, env=_ocr_environment())
    output_path = Path(output_pdf)
    if returncode == 10:
        try:
            with fitz.open(output_path) as document:
                readable_output = document.page_count > 0
        except (OSError, RuntimeError, ValueError):
            readable_output = False
        if readable_output:
            warning = (
                "[paper2md] warning: ocrmypdf could not produce PDF/A metadata "
                "but produced a readable PDF; continuing with quality validation."
            )
            if on_output is None:
                print(warning, file=sys.stderr)
            else:
                on_output(warning)
            return output_path
    if returncode != 0:
        raise OCRError(
            f"ocrmypdf failed (exit {returncode}) — see output above"
        )
    return output_path
