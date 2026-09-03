"""On-disk, per-paper artifacts with durable text and JSON writes."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from src.research_models import ArtifactBundle


class ArtifactManager:
    def __init__(self, output_dir: Path | str, paper_id: str) -> None:
        self.root = Path(output_dir) / paper_id
        self.bundle = ArtifactBundle(
            root=self.root,
            source_pdf=self.root / "source.pdf",
            metadata_json=self.root / "metadata.json",
            document_json=self.root / "document.json",
            paper_md=self.root / "paper.md",
            figures_dir=self.root / "figures",
            summary_json=self.root / "summary.json",
            manifest_json=self.root / "manifest.json",
            logs_dir=self.root / "logs",
        )

    def create(self) -> ArtifactBundle:
        self.root.mkdir(parents=True, exist_ok=True)
        self.bundle.figures_dir.mkdir(exist_ok=True)
        self.bundle.logs_dir.mkdir(exist_ok=True)
        return self.bundle

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def write_json(self, path: Path, payload: Any) -> None:
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def write_text(self, path: Path, content: str) -> None:
        self._atomic_write(path, content)

    def write_metadata(self, payload: Any) -> None:
        self.write_json(self.bundle.metadata_json, payload)

    def write_document(self, payload: Any) -> None:
        self.write_json(self.bundle.document_json, payload)

    def write_summary(self, payload: Any) -> None:
        self.write_json(self.bundle.summary_json, payload)

    def write_manifest(self, payload: Any) -> None:
        self.write_json(self.bundle.manifest_json, payload)

    def write_paper(self, markdown: str) -> None:
        self.write_text(self.bundle.paper_md, markdown)

    def copy_source(self, source: Path | str) -> Path:
        source = Path(source)
        self.create()
        handle, temporary_name = tempfile.mkstemp(prefix=".source.", suffix=".tmp", dir=self.root)
        os.close(handle)
        try:
            shutil.copyfile(source, temporary_name)
            os.replace(temporary_name, self.bundle.source_pdf)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return self.bundle.source_pdf

    def write_source_pdf(self, content: bytes) -> Path:
        """Durably store already-validated downloaded PDF content."""
        self.create()
        handle, temporary_name = tempfile.mkstemp(prefix=".source.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(handle, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.bundle.source_pdf)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return self.bundle.source_pdf
