from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_marker import MarkerError, run_marker


def test_run_marker_invokes_cli_with_tempdir_and_flattens(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "marker"

    def fake_run(args, **_kw):
        # marker_single is invoked with a tempdir as --output_dir. Marker writes
        # {tmpdir}/paper/paper.md plus a sibling images/ directory.
        tmpdir = Path(args[args.index("--output_dir") + 1])
        produced = tmpdir / "paper"
        produced.mkdir(parents=True)
        (produced / "paper.md").write_text("# Paper\n")
        images = produced / "images"
        images.mkdir()
        (images / "fig.png").write_bytes(b"PNG")
        return MagicMock(returncode=0)

    mocker.patch("src.run_marker.subprocess.run", side_effect=fake_run)
    md_path = run_marker(src_pdf, out_dir)
    # The {stem}/ wrapper is flattened — .md and images/ land directly under out_dir.
    assert md_path == out_dir / "paper.md"
    assert md_path.exists()
    assert (out_dir / "images" / "fig.png").exists()


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
