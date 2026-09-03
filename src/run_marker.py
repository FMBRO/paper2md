"""Wrapper around the `marker_single` CLI (spec §5.2.2)."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from src.document_normalizer import load_marker_document, materialize_marker_markdown
from src.subprocess_utils import OutputCallback, run_streaming_command


class MarkerError(RuntimeError):
    """Raised when Marker fails to produce a markdown file."""


_SHORT_STEM = "p"


def run_marker(
    input_pdf: Path | str,
    output_dir: Path | str,
    *,
    on_output: OutputCallback | None = None,
) -> Path:
    """Run ``marker_single`` and return the produced ``.md`` path.

    Marker writes ``{output_dir}/{stem}/{stem}.md``, duplicating the stem in
    the path. On Windows that easily exceeds MAX_PATH (260) for long paper
    filenames — even when ``output_dir`` is a short tempdir. To keep the
    duplicated tail small, copy the input to a one-letter name inside the
    tempdir and let Marker derive its stem from that.
    """
    input_pdf = Path(input_pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        short_input = tmpdir / f"{_SHORT_STEM}{input_pdf.suffix or '.pdf'}"
        shutil.copy2(input_pdf, short_input)

        returncode = run_streaming_command(
            [
                "marker_single", str(short_input), "--output_dir", str(tmpdir),
                "--output_format", "json",
            ],
            on_output,
        )
        if returncode != 0:
            raise MarkerError(
                f"marker_single failed (exit {returncode}) — see output above"
            )

        produced_dir = tmpdir / _SHORT_STEM
        marker_document = load_marker_document(produced_dir)
        if marker_document is not None:
            produced_md = produced_dir / f"{_SHORT_STEM}.md"
            produced_md.write_text(
                materialize_marker_markdown(marker_document), encoding="utf-8",
            )
        else:
            candidates = sorted(produced_dir.glob(f"{_SHORT_STEM}.md"))
            if not candidates:
                candidates = sorted(tmpdir.rglob("*.md"))
            if not candidates:
                raise MarkerError(
                    f"marker_single produced no supported renderer artifact under {tmpdir}"
                )
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
