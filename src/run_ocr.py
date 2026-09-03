"""Wrapper around the `ocrmypdf` CLI (spec §5.3.2)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Literal

from src.subprocess_utils import OutputCallback, run_streaming_command


class OCRError(RuntimeError):
    """Raised when ocrmypdf exits non-zero."""


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
    cmd: list[str] = ["ocrmypdf", "-l", lang]
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
    if deskew:
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

    returncode = run_streaming_command(cmd, on_output)
    if returncode != 0:
        raise OCRError(
            f"ocrmypdf failed (exit {returncode}) — see output above"
        )
    return Path(output_pdf)
