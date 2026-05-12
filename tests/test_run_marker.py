from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_marker import MarkerError, run_marker


def test_run_marker_invokes_cli_with_output_dir(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "marker"

    def fake_run(_args, **_kw):
        # Marker writes paper/paper.md under the output dir
        produced = out_dir / "paper"
        produced.mkdir(parents=True)
        (produced / "paper.md").write_text("# Paper\n")
        return MagicMock(returncode=0)

    mocker.patch("src.run_marker.subprocess.run", side_effect=fake_run)
    md_path = run_marker(src_pdf, out_dir)
    assert md_path == out_dir / "paper" / "paper.md"
    assert md_path.exists()


def test_run_marker_raises_when_no_markdown_produced(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_marker.subprocess.run",
                 return_value=MagicMock(returncode=0))
    with pytest.raises(MarkerError):
        run_marker(src_pdf, tmp_path / "marker")


def test_run_marker_raises_on_nonzero_exit(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_marker.subprocess.run",
                 return_value=MagicMock(returncode=1, stderr=b"boom"))
    with pytest.raises(MarkerError):
        run_marker(src_pdf, tmp_path / "marker")
