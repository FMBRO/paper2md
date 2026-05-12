"""Wrapper around the `marker_single` CLI (spec §5.2.2)."""
from __future__ import annotations

import subprocess
from pathlib import Path


class MarkerError(RuntimeError):
    """Raised when Marker fails to produce a markdown file."""


def run_marker(input_pdf: Path | str, output_dir: Path | str) -> Path:
    """Run ``marker_single`` and return the produced ``.md`` path.

    Marker writes ``{output_dir}/{stem}/{stem}.md``; we search for any ``*.md``
    under ``output_dir`` to be resilient to layout changes.
    """
    input_pdf = Path(input_pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["marker_single", str(input_pdf), "--output_dir", str(output_dir)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise MarkerError(
            f"marker_single failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
        )

    candidates = sorted(output_dir.rglob("*.md"))
    if not candidates:
        raise MarkerError(f"marker_single produced no .md under {output_dir}")
    return candidates[0]
