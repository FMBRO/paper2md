"""Wrapper around the `ocrmypdf` CLI (spec §5.3.2)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


class OCRError(RuntimeError):
    """Raised when ocrmypdf exits non-zero."""


def run_ocrmypdf(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    lang: str = "eng",
    deskew: bool = True,
    clean: bool = True,
) -> Path:
    """Run ``ocrmypdf`` and return the output path on success.

    ``--clean`` requires the external ``unpaper`` binary, which is not bundled
    with the pip package and is awkward to install on Windows. When the user
    requests cleaning but ``unpaper`` is not on PATH we drop the flag and warn,
    instead of letting ``ocrmypdf`` abort with exit 3.
    """
    cmd: list[str] = ["ocrmypdf", "-l", lang]
    if deskew:
        cmd.append("--deskew")
    if clean:
        if shutil.which("unpaper"):
            cmd.append("--clean")
        else:
            print(
                "[paper2md] warning: 'unpaper' not found on PATH; "
                "running ocrmypdf without --clean (install unpaper to enable noise removal).",
                file=sys.stderr,
            )
    cmd.extend([str(input_pdf), str(output_pdf)])

    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise OCRError(
            f"ocrmypdf failed (exit {result.returncode}) — see output above"
        )
    return Path(output_pdf)
