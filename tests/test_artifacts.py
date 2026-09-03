import json
from pathlib import Path


def test_artifact_manager_creates_standard_layout_and_writes_json_atomically(tmp_path: Path) -> None:
    from src.artifacts import ArtifactManager

    manager = ArtifactManager(tmp_path, "paper-123")
    bundle = manager.create()
    manager.write_metadata({"title": "A paper"})
    manager.write_paper("# A paper\n")

    assert bundle.source_pdf == tmp_path / "paper-123" / "source.pdf"
    assert bundle.figures_dir.is_dir()
    assert bundle.logs_dir.is_dir()
    assert json.loads(bundle.metadata_json.read_text(encoding="utf-8")) == {"title": "A paper"}
    assert bundle.paper_md.read_text(encoding="utf-8") == "# A paper\n"
    assert not list(bundle.root.glob("*.tmp"))
