from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_marker import MarkerError, run_marker


def test_run_marker_invokes_cli_with_tempdir_and_flattens(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "marker"

    def fake_run(args, **_kw):
        # Wrapper copies the input to a short name inside tempdir and passes
        # that short path to marker_single. Marker derives the stem from the
        # input filename, so it writes {tmpdir}/{short_stem}/{short_stem}.md
        # plus a sibling images/ directory.
        input_path = Path(args[1])
        tmpdir = Path(args[args.index("--output_dir") + 1])
        short_stem = input_path.stem
        produced = tmpdir / short_stem
        produced.mkdir(parents=True)
        (produced / f"{short_stem}.md").write_text("# Paper\n")
        images = produced / "images"
        images.mkdir()
        (images / "fig.png").write_bytes(b"PNG")
        return MagicMock(returncode=0)

    mocker.patch("src.run_marker.subprocess.run", side_effect=fake_run)
    md_path = run_marker(src_pdf, out_dir)
    # The {stem}/ wrapper is flattened — the .md and images/ land directly
    # under out_dir. The .md filename matches the short stem the wrapper used.
    assert md_path.parent == out_dir
    assert md_path.suffix == ".md"
    assert md_path.exists()
    assert (out_dir / "images" / "fig.png").exists()


def test_run_marker_renames_input_to_short_name_to_avoid_max_path(mocker, tmp_path: Path) -> None:
    """Marker writes ``{outdir}/{stem}/{stem}.md`` — duplicating the stem.
    On Windows, a 112-char stem makes the path exceed MAX_PATH (260) even when
    outdir is a short tempdir. The wrapper must copy the input to a short name
    before invoking marker_single so the duplicated stem stays small."""
    long_stem = "11. Skin Parameter Map Retrieval from a Dedicated Multispectral Imaging System Applied to DermatologyCosmetology"
    src_pdf = tmp_path / f"{long_stem}.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "marker"

    captured: dict[str, str] = {}

    def fake_run(args, **_kw):
        input_path = Path(args[1])
        captured["input_arg"] = str(input_path)
        captured["input_stem"] = input_path.stem
        tmpdir = Path(args[args.index("--output_dir") + 1])
        produced = tmpdir / input_path.stem
        produced.mkdir(parents=True)
        (produced / f"{input_path.stem}.md").write_text("# Paper\n")
        return MagicMock(returncode=0)

    mocker.patch("src.run_marker.subprocess.run", side_effect=fake_run)
    md_path = run_marker(src_pdf, out_dir)

    # marker_single must NOT have been called with the original long stem.
    assert long_stem not in captured["input_stem"], \
        f"Marker invoked with the long original stem: {captured['input_stem']!r}"
    # The short stem must keep the duplicated-stem tail well under MAX_PATH.
    assert len(captured["input_stem"]) <= 16
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
