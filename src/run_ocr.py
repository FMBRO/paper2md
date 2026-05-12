"""Wrapper around the `ocrmypdf` CLI (spec §5.3.2)."""
from __future__ import annotations

import subprocess
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
    """Run ``ocrmypdf`` and return the output path on success."""
    cmd: list[str] = ["ocrmypdf", "-l", lang]
    if deskew:
        cmd.append("--deskew")
    if clean:
        cmd.append("--clean")
    cmd.extend([str(input_pdf), str(output_pdf)])

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise OCRError(
            f"ocrmypdf failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
        )
    return Path(output_pdf)
