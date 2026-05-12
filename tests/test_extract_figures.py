from pathlib import Path

from src.extract_figures import collect_marker_figures


def test_collect_marker_figures_renames_and_rewrites(tmp_path: Path) -> None:
    marker_dir = tmp_path / "marker"
    marker_dir.mkdir()
    (marker_dir / "fig_a.png").write_bytes(b"PNGA")
    (marker_dir / "fig_b.jpg").write_bytes(b"JPGB")
    md = marker_dir / "paper.md"
    md.write_text("See ![alt1](fig_a.png) and ![alt2](fig_b.jpg).\n")

    figures_dir = tmp_path / "figures"
    new_md = collect_marker_figures(md, figures_dir)

    assert (figures_dir / "figure_001.png").exists()
    assert (figures_dir / "figure_002.jpg").exists()
    rewritten = new_md.read_text()
    assert "figures/figure_001.png" in rewritten
    assert "figures/figure_002.jpg" in rewritten
    assert "fig_a.png" not in rewritten
    assert "fig_b.jpg" not in rewritten


def test_collect_marker_figures_handles_no_images(tmp_path: Path) -> None:
    marker_dir = tmp_path / "marker"
    marker_dir.mkdir()
    md = marker_dir / "paper.md"
    md.write_text("No figures here.\n")
    figures_dir = tmp_path / "figures"

    new_md = collect_marker_figures(md, figures_dir)
    assert new_md.read_text() == "No figures here.\n"
    assert not figures_dir.exists() or not any(figures_dir.iterdir())
