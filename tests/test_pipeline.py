from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from src.acquisition import AcquisitionResult
from src.artifacts import ArtifactManager
from src.config import Settings
from src.job_store import JobStore
from src.notion import NotionConfigurationError
from src.openrouter import BudgetExceededError
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


class FakeAcquirer:
    def __init__(self) -> None:
        self.calls = 0
        self.specs: list[InputSpec] = []

    def acquire(self, spec: InputSpec, artifacts: ArtifactManager) -> AcquisitionResult:
        self.calls += 1
        self.specs.append(spec)
        content = b"%PDF-1.4\nfixture\n%%EOF"
        return AcquisitionResult(
            metadata=PaperMetadata(title="Fixture", doi="10.1000/fixture"),
            source_pdf=artifacts.write_source_pdf(content),
            pdf_sha256=hashlib.sha256(content).hexdigest(),
        )


class FakeZotero:
    def __init__(self) -> None:
        self.upsert_calls = 0
        self.resolve_calls = 0

    def is_available(self) -> bool:
        return True

    def upsert_non_zotero(
        self, metadata: PaperMetadata, source_pdf: Path,
    ) -> ZoteroResolution:
        self.upsert_calls += 1
        synced = PaperMetadata(
            **{
                **asdict(metadata),
                "zotero_library_id": "0",
                "zotero_item_key": "PARENT01",
            }
        )
        return ZoteroResolution(synced, source_pdf, "PARENT01", "ATTACH01")

    def resolve(self, spec: InputSpec) -> ZoteroResolution:
        self.resolve_calls += 1
        raise AssertionError("local input must not use Zotero resolution")


class AttachmentSelectingZotero(FakeZotero):
    def __init__(self, source_pdf: Path) -> None:
        super().__init__()
        self.source_pdf = source_pdf
        self.selections: list[str | None] = []

    def resolve(self, spec: InputSpec) -> ZoteroResolution:
        self.resolve_calls += 1
        self.selections.append(spec.attachment_key)
        metadata = PaperMetadata(
            title="Zotero fixture", zotero_library_id="0",
            zotero_item_key=spec.source,
        )
        if spec.attachment_key is None:
            return ZoteroResolution(
                metadata, None, spec.source, None, JobState.NEEDS_INPUT,
                "Multiple PDF attachments found; select one",
            )
        return ZoteroResolution(
            metadata, self.source_pdf, spec.source, spec.attachment_key,
        )


class CollectionZotero(FakeZotero):
    def __init__(self, source_pdfs: dict[str, Path]) -> None:
        super().__init__()
        self.source_pdfs = source_pdfs
        self.collection_calls = 0

    def expand_collection(self, spec: InputSpec) -> list[InputSpec]:
        self.collection_calls += 1
        return [
            InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", research_interest=spec.research_interest),
            InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", research_interest=spec.research_interest),
            InputSpec(InputKind.ZOTERO_ITEM, "PARENT02", research_interest=spec.research_interest),
        ]

    def resolve(self, spec: InputSpec) -> ZoteroResolution:
        self.resolve_calls += 1
        metadata = PaperMetadata(
            title=spec.source, zotero_library_id="0", zotero_item_key=spec.source,
        )
        return ZoteroResolution(
            metadata, self.source_pdfs[spec.source], spec.source, "ATTACH01",
        )


class FakeConverter:
    def __init__(self) -> None:
        self.calls = 0

    def convert(self, pdf_path: Path, artifact_dir: Path) -> ArtifactBundle:
        self.calls += 1
        manager = ArtifactManager(artifact_dir.parent, artifact_dir.name)
        bundle = manager.create()
        manager.write_paper("# Fixture\n\nEnough text for the paper.")
        manager.write_document({"pages": [{"number": 1}], "paragraphs": []})
        manager.write_json(
            bundle.logs_dir / "quality_result.json",
            {"passed": True, "llm_allowed": True, "issues": []},
        )
        return bundle


class FakeSummarizer:
    def __init__(self) -> None:
        self.calls = 0
        self.budgets: list[float] = []

    def summarize_artifacts(
        self, bundle: ArtifactBundle, *, job_id: str, research_interest: str = "",
    ) -> PaperSummary:
        self.calls += 1
        summary = PaperSummary(
            background="背景です", question="課題です", novelty="新規性です",
            methods="手法です", datasets=["データ"], results="結果です",
            strengths="強みです", limitations="限界です", takeaways="要点です",
            relevance_score=4, score_rationale="理由です", keywords=["研究"],
        )
        ArtifactManager(bundle.root.parent, bundle.root.name).write_summary(asdict(summary))
        return summary


class FakeNotion:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def upsert(
        self, metadata: PaperMetadata, summary: PaperSummary, *,
        stored_page_id: str | None = None, topics: tuple[str, ...] = (),
        processing_status: str = "Completed", model_prompt_version: str = "",
    ) -> str:
        self.calls.append(stored_page_id)
        return "notion-page-1"


class FailingOnceNotion(FakeNotion):
    def upsert(self, *args: object, **kwargs: object) -> str:
        self.calls.append(kwargs.get("stored_page_id"))
        if len(self.calls) == 1:
            raise RuntimeError("temporary Notion failure")
        return "notion-page-after-resume"


class ConfigurationNotion(FakeNotion):
    def upsert(self, *args: object, **kwargs: object) -> str:
        raise NotionConfigurationError("NOTION_API_KEY is required")


class BudgetSummarizer(FakeSummarizer):
    def summarize_artifacts(self, *args: object, **kwargs: object) -> PaperSummary:
        raise BudgetExceededError("Required USD 0.60 exceeds remaining USD 0.50")


class BudgetThenSuccessSummarizer(FakeSummarizer):
    def __init__(self) -> None:
        super().__init__()
        self.settings = SimpleNamespace(paper_budget_usd=0.99)
        self.seen_budgets: list[float] = []

    def summarize_artifacts(self, *args: object, **kwargs: object) -> PaperSummary:
        self.seen_budgets.append(self.settings.paper_budget_usd)
        if len(self.seen_budgets) == 1:
            raise BudgetExceededError("increase the paper budget")
        return super().summarize_artifacts(*args, **kwargs)


def _service(tmp_path: Path) -> tuple[PipelineService, dict[str, object]]:
    settings = Settings(input_dir=tmp_path / "input", output_dir=tmp_path / "output")
    store = JobStore(settings.state_path)
    dependencies: dict[str, object] = {
        "acquirer": FakeAcquirer(),
        "zotero": FakeZotero(),
        "converter": FakeConverter(),
        "summarizer": FakeSummarizer(),
        "notion": FakeNotion(),
    }
    service = PipelineService(settings, store=store, **dependencies)
    dependencies["store"] = store
    return service, dependencies


def test_successful_local_pipeline_persists_every_checkpoint_and_notion_id(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    stages: list[str] = []
    service._on_event = lambda event: stages.append(event.stage or "")

    job = service.ingest(
        InputSpec(InputKind.LOCAL_PDF, str(source), research_interest="graphs"),
        max_cost_usd=0.25,
    )

    assert job.state is JobState.COMPLETED
    assert job.paper_id is not None
    store = dependencies["store"]
    assert isinstance(store, JobStore)
    assert list(store.list_checkpoints(job.id)) == [
        "acquiring", "zotero_sync", "converting", "quality_check",
        "extracting", "synthesizing", "notion_sync",
    ]
    assert store.get_notion_page_id(job.paper_id) == "notion-page-1"
    assert (job.artifact_dir / "summary.json").is_file()
    assert dependencies["acquirer"].calls == 1
    assert dependencies["zotero"].upsert_calls == 1
    assert dependencies["converter"].calls == 1
    assert dependencies["summarizer"].calls == 1
    assert dependencies["notion"].calls == [None]
    assert stages == [
        "queued", "acquiring", "zotero_sync", "converting", "quality_check",
        "extracting", "synthesizing", "notion_sync", "completed",
    ]


def test_mocked_remote_pipeline_uses_the_same_service_without_live_calls(
    tmp_path: Path,
) -> None:
    service, dependencies = _service(tmp_path)
    spec = InputSpec(InputKind.ARXIV, "2401.01234", research_interest="causality")

    job = service.ingest(spec)

    assert job.state is JobState.COMPLETED
    assert dependencies["acquirer"].specs == [spec]
    assert dependencies["zotero"].upsert_calls == 1
    assert dependencies["notion"].calls == [None]


def test_a_later_ingest_reuses_the_persisted_notion_page_id(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    spec = InputSpec(InputKind.LOCAL_PDF, str(source))

    first = service.ingest(spec)
    second = service.ingest(spec)

    assert first.paper_id == second.paper_id
    assert dependencies["notion"].calls == [None, "notion-page-1"]


def test_resume_reuses_durable_outputs_and_retries_only_incomplete_notion_sync(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    notion = FailingOnceNotion()
    service._notion = notion

    failed = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))
    resumed = service.resume(failed.id)

    assert failed.state is JobState.FAILED
    assert failed.error == "temporary Notion failure"
    assert resumed.state is JobState.COMPLETED
    assert dependencies["acquirer"].calls == 1
    assert dependencies["zotero"].upsert_calls == 1
    assert dependencies["converter"].calls == 1
    assert dependencies["summarizer"].calls == 1
    assert notion.calls == [None, None]
    assert dependencies["store"].get_notion_page_id(resumed.paper_id) == (
        "notion-page-after-resume"
    )


def test_resume_recomputes_a_corrupted_summary_but_reuses_earlier_stages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    notion = FailingOnceNotion()
    service._notion = notion
    failed = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))
    assert failed.artifact_dir is not None
    (failed.artifact_dir / "summary.json").write_text("not JSON", encoding="utf-8")

    resumed = service.resume(failed.id)

    assert resumed.state is JobState.COMPLETED
    assert dependencies["acquirer"].calls == 1
    assert dependencies["converter"].calls == 1
    assert dependencies["summarizer"].calls == 2


def test_resume_reacquires_when_the_source_pdf_no_longer_matches_its_checkpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    service._notion = FailingOnceNotion()
    failed = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))
    assert failed.artifact_dir is not None
    (failed.artifact_dir / "source.pdf").write_bytes(b"corrupted")

    resumed = service.resume(failed.id)

    assert resumed.state is JobState.COMPLETED
    assert dependencies["acquirer"].calls == 2
    assert dependencies["converter"].calls == 1
    assert dependencies["summarizer"].calls == 1


def test_missing_service_configuration_is_user_actionable_needs_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, _ = _service(tmp_path)
    service._notion = ConfigurationNotion()
    stages: list[str] = []
    service._on_event = lambda event: stages.append(event.stage or "")

    job = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))

    assert job.state is JobState.NEEDS_INPUT
    assert job.error == "NOTION_API_KEY is required"
    assert stages[-1] == "needs_input"


def test_budget_denial_has_a_distinct_resumable_state(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, _ = _service(tmp_path)
    service.summarizer = BudgetSummarizer()
    stages: list[str] = []
    service._on_event = lambda event: stages.append(event.stage or "")

    job = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))

    assert job.state is JobState.BUDGET_EXCEEDED
    assert job.error == "Required USD 0.60 exceeds remaining USD 0.50"
    assert stages[-1] == "budget_exceeded"


def test_resume_accepts_attachment_selection_without_any_early_llm_call(
    tmp_path: Path,
) -> None:
    source = tmp_path / "zotero.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    zotero = AttachmentSelectingZotero(source)
    service.zotero = zotero

    paused = service.ingest(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))
    resumed = service.resume(paused.id, attachment_key="ATTACH02")

    assert paused.state is JobState.NEEDS_INPUT
    assert paused.error == "Multiple PDF attachments found; select one"
    assert resumed.state is JobState.COMPLETED
    assert zotero.selections == [None, "ATTACH02"]
    assert dependencies["summarizer"].calls == 1


def test_resume_without_cost_override_reuses_the_persisted_job_budget(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, _ = _service(tmp_path)
    summarizer = BudgetThenSuccessSummarizer()
    service.summarizer = summarizer

    paused = service.ingest(
        InputSpec(InputKind.LOCAL_PDF, str(source)), max_cost_usd=0.20,
    )
    resumed = service.resume(paused.id)

    assert paused.state is JobState.BUDGET_EXCEEDED
    assert resumed.state is JobState.COMPLETED
    assert summarizer.seen_budgets == [0.20, 0.20]


def test_collection_batch_deduplicates_keys_and_only_unprocessed_skips_completed(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "zotero-1.pdf"
    second_source = tmp_path / "zotero-2.pdf"
    first_source.write_bytes(b"%PDF-1.4\nfixture one\n%%EOF")
    second_source.write_bytes(b"%PDF-1.4\nfixture two\n%%EOF")
    service, dependencies = _service(tmp_path)
    zotero = CollectionZotero({"PARENT01": first_source, "PARENT02": second_source})
    service.zotero = zotero
    collection = InputSpec(
        InputKind.ZOTERO_COLLECTION, "COLLECT1", research_interest="graphs",
    )

    first = service.ingest_collection(collection, max_cost_usd=0.30)
    second = service.ingest_collection(
        collection, max_cost_usd=0.30, only_unprocessed=True,
    )

    assert [job.state for job in first] == [JobState.COMPLETED, JobState.COMPLETED]
    assert second == []
    assert zotero.resolve_calls == 2
    assert dependencies["converter"].calls == 2
    assert dependencies["summarizer"].calls == 2
    assert dependencies["notion"].calls == [None, None]
    assert dependencies["store"].get_checkpoint(
        first[0].id, JobState.ZOTERO_SYNC,
    )["attachment_key"] == "ATTACH01"
