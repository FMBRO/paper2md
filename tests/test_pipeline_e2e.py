"""Deterministic acceptance flows for the complete research pipeline.

Every network, paid-model, and converter boundary is an in-process fake.  The
real orchestration, artifact manager, checkpoint store, and resume logic run.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from src.acquisition import AcquisitionResult
from src.artifacts import ArtifactManager
from src.config import Settings
from src.job_store import JobStore
from src.pipeline import PipelineService
from src.research_models import (
    ArtifactBundle,
    InputKind,
    InputSpec,
    JobState,
    PaperMetadata,
    PaperSummary,
)
from src.zotero import ZoteroResolution


PDF_BYTES = b"%PDF-1.4\nacceptance fixture\n%%EOF"


class DeterministicArxivAcquirer:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, spec: InputSpec, artifacts: ArtifactManager) -> AcquisitionResult:
        self.calls += 1
        assert spec == InputSpec(InputKind.ARXIV, "2401.01234")
        return AcquisitionResult(
            PaperMetadata(
                title="Deterministic arXiv paper",
                arxiv_id="2401.01234",
                source_url="https://arxiv.org/abs/2401.01234",
            ),
            artifacts.write_source_pdf(PDF_BYTES),
            hashlib.sha256(PDF_BYTES).hexdigest(),
        )


class DeterministicZotero:
    def __init__(self, attachment_pdf: Path) -> None:
        self.attachment_pdf = attachment_pdf
        self.resolve_calls = 0
        self.upsert_calls = 0

    def resolve(self, spec: InputSpec) -> ZoteroResolution:
        self.resolve_calls += 1
        metadata = PaperMetadata(
            title="Existing Zotero paper",
            zotero_library_id="0",
            zotero_item_key=spec.source,
        )
        if spec.attachment_key is None:
            return ZoteroResolution(
                metadata,
                None,
                spec.source,
                None,
                JobState.NEEDS_INPUT,
                "Multiple PDF attachments found; select one",
            )
        assert spec.attachment_key == "ATTACH-B"
        return ZoteroResolution(
            metadata, self.attachment_pdf, spec.source, spec.attachment_key,
        )

    def upsert_non_zotero(
        self, metadata: PaperMetadata, source_pdf: Path,
    ) -> ZoteroResolution:
        self.upsert_calls += 1
        synced = PaperMetadata(
            **{
                **asdict(metadata),
                "zotero_library_id": "0",
                "zotero_item_key": "ARXIV-PARENT",
            }
        )
        return ZoteroResolution(synced, source_pdf, "ARXIV-PARENT", "ARXIV-PDF")


class DeterministicConverter:
    def __init__(self) -> None:
        self.calls = 0

    def convert(self, pdf_path: Path, artifact_dir: Path) -> ArtifactBundle:
        self.calls += 1
        assert pdf_path.read_bytes() == PDF_BYTES
        artifacts = ArtifactManager(artifact_dir.parent, artifact_dir.name)
        bundle = artifacts.create()
        artifacts.write_document({
            "pages": [{"number": 1}],
            "sections": [{"title": "Introduction", "page": 1}],
            "paragraphs": [{"text": "Deterministic evidence", "page": 1}],
        })
        artifacts.write_paper("# Deterministic paper\n\nDeterministic evidence.\n")
        (bundle.figures_dir / "figure_001.png").write_bytes(b"figure fixture")
        artifacts.write_json(bundle.logs_dir / "quality_result.json", {
            "passed": True, "llm_allowed": True, "issues": [],
        })
        return bundle


class DeterministicSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    def summarize_artifacts(
        self, bundle: ArtifactBundle, *, job_id: str, research_interest: str = "",
    ) -> PaperSummary:
        self.calls += 1
        summary = PaperSummary(
            background="背景です。", question="問いです。", novelty="新規性です。",
            methods="手法です。", datasets=["データ"], results="結果です。",
            strengths="強みです。", limitations="限界です。", takeaways="要点です。",
            relevance_score=4, score_rationale="関連します。", keywords=["検証"],
        )
        ArtifactManager(bundle.root.parent, bundle.root.name).write_summary(
            asdict(summary)
        )
        return summary


class RecordingNotion:
    def __init__(self) -> None:
        self.upserts = 0

    def upsert(self, *_args, **_kwargs) -> str:
        self.upserts += 1
        return "notion-page-acceptance"


def _service(tmp_path: Path, *, acquirer, zotero):
    settings = Settings(tmp_path / "input", tmp_path / "output")
    converter = DeterministicConverter()
    summarizer = DeterministicSummarizer()
    notion = RecordingNotion()
    service = PipelineService(
        settings,
        store=JobStore(settings.state_path),
        acquirer=acquirer,
        zotero=zotero,
        converter=converter,
        summarizer=summarizer,
        notion=notion,
    )
    return service, converter, summarizer, notion


def _assert_one_complete_artifact_set(output_dir: Path) -> Path:
    papers_dir = output_dir / "papers"
    staging_dir = papers_dir / ".staging"
    if staging_dir.exists():
        assert not list(staging_dir.iterdir())
    roots = [
        path for path in papers_dir.iterdir()
        if path.is_dir() and path.name != ".staging"
    ]
    assert len(roots) == 1
    root = roots[0]
    assert {
        "source.pdf", "metadata.json", "document.json", "paper.md",
        "figures", "summary.json", "manifest.json", "logs",
    } <= {path.name for path in root.iterdir()}
    return root


def test_arxiv_style_flow_produces_one_artifact_set_and_one_notion_upsert(
    tmp_path: Path,
) -> None:
    attachment_pdf = tmp_path / "unused.pdf"
    attachment_pdf.write_bytes(PDF_BYTES)
    acquirer = DeterministicArxivAcquirer()
    zotero = DeterministicZotero(attachment_pdf)
    service, converter, summarizer, notion = _service(
        tmp_path, acquirer=acquirer, zotero=zotero,
    )

    job = service.ingest(InputSpec(InputKind.ARXIV, "2401.01234"))

    assert job.state is JobState.COMPLETED
    assert job.artifact_dir == _assert_one_complete_artifact_set(
        service.settings.output_dir
    )
    assert (acquirer.calls, zotero.upsert_calls, converter.calls) == (1, 1, 1)
    assert summarizer.calls == 1
    assert notion.upserts == 1
    manifest = json.loads(job.artifact_dir.joinpath("manifest.json").read_text(
        encoding="utf-8"
    ))
    assert manifest["canonical_identity"] == "arxiv:2401.01234"
    assert manifest["source_sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert manifest["job"] == {
        "id": job.id,
        "stage": "completed",
        "status": "completed",
        "total_cost_usd": 0.0,
        "max_cost_usd": 0.5,
    }
    assert manifest["external_ids"] == {
        "doi": None,
        "arxiv_id": "2401.01234",
        "zotero_library_id": "0",
        "zotero_item_key": "ARXIV-PARENT",
        "zotero_attachment_key": "ARXIV-PDF",
        "notion_page_id": "notion-page-acceptance",
    }
    assert manifest["artifact_sha256"]["source.pdf"] == hashlib.sha256(
        PDF_BYTES
    ).hexdigest()
    assert set(manifest["artifact_sha256"]) == {
        "source.pdf", "metadata.json", "document.json", "paper.md",
        "summary.json", "figures/figure_001.png", "logs/quality_result.json",
    }


def test_existing_zotero_multiple_attachments_cost_nothing_then_resume_is_idempotent(
    tmp_path: Path,
) -> None:
    attachment_pdf = tmp_path / "zotero-attachment.pdf"
    attachment_pdf.write_bytes(PDF_BYTES)
    zotero = DeterministicZotero(attachment_pdf)
    service, converter, summarizer, notion = _service(
        tmp_path,
        acquirer=DeterministicArxivAcquirer(),
        zotero=zotero,
    )

    waiting = service.ingest(InputSpec(InputKind.ZOTERO_ITEM, "PARENT-1"))

    assert waiting.state is JobState.NEEDS_INPUT
    assert waiting.total_cost_usd == 0.0
    assert summarizer.calls == 0
    assert notion.upserts == 0

    completed = service.resume(waiting.id, attachment_key="ATTACH-B")
    resumed_again = service.resume(completed.id)

    assert completed.state is JobState.COMPLETED
    assert resumed_again.state is JobState.COMPLETED
    assert completed.artifact_dir == _assert_one_complete_artifact_set(
        service.settings.output_dir
    )
    assert zotero.upsert_calls == 0  # Existing Zotero items remain read-only.
    assert converter.calls == 1
    assert summarizer.calls == 1
    assert notion.upserts == 1
    manifest = json.loads(completed.artifact_dir.joinpath("manifest.json").read_text(
        encoding="utf-8"
    ))
    assert manifest["canonical_identity"] == "zotero:0:PARENT-1"
    assert manifest["external_ids"]["zotero_item_key"] == "PARENT-1"
    assert manifest["external_ids"]["notion_page_id"] == "notion-page-acceptance"
