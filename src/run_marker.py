"""Wrapper around the `marker_single` CLI (spec §5.2.2)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class MarkerError(RuntimeError):
    """Raised when Marker fails to produce a markdown file."""


def run_marker(input_pdf: Path | str, output_dir: Path | str) -> Path:
    """Run ``marker_single`` and return the produced ``.md`` path.

    Marker writes ``{output_dir}/{stem}/{stem}.md``, which can blow past
    Windows MAX_PATH (260) when the PDF filename is long because the stem
    appears twice. To avoid that, run Marker into a short system tempdir
    and move the produced files into ``output_dir`` (flattening the extra
    ``{stem}/`` wrapper).
    """
    input_pdf = Path(input_pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        result = subprocess.run(
            ["marker_single", str(input_pdf), "--output_dir", str(tmpdir)],
        )
        if result.returncode != 0:
            raise MarkerError(
                f"marker_single failed (exit {result.returncode}) — see output above"
            )

        candidates = sorted(tmpdir.rglob("*.md"))
        if not candidates:
            raise MarkerError(f"marker_single produced no .md under {tmpdir}")
        produced_md = candidates[0]

        for item in produced_md.parent.iterdir():
            dst = output_dir / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(item), str(dst))

        return output_dir / produced_md.name
