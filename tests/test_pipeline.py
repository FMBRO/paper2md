from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from src.acquisition import (
    AcquisitionError,
    AcquisitionInputError,
    AcquisitionResult,
    DocumentAcquirer,
)
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
from src.zotero import ZoteroError, ZoteroInputError, ZoteroResolution


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


class ChangingAcquirer(FakeAcquirer):
    def acquire(self, spec: InputSpec, artifacts: ArtifactManager) -> AcquisitionResult:
        self.calls += 1
        self.specs.append(spec)
        content = (
            b"%PDF-1.4\nfirst version\n%%EOF"
            if self.calls == 1
            else b"%PDF-1.4\nchanged version\n%%EOF"
        )
        return AcquisitionResult(
            metadata=PaperMetadata(title="Fixture", doi="10.1000/fixture"),
            source_pdf=artifacts.write_source_pdf(content),
            pdf_sha256=hashlib.sha256(content).hexdigest(),
        )


class ErrorAcquirer:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def acquire(self, spec: InputSpec, artifacts: ArtifactManager) -> AcquisitionResult:
        raise self.error


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
    def __init__(self, source_pdfs: dict[str, Path], library_id: str = "0") -> None:
        super().__init__()
        self.source_pdfs = source_pdfs
        self.library_id = library_id
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
            title=spec.source, zotero_library_id=self.library_id,
            zotero_item_key=spec.source,
        )
        return ZoteroResolution(
            metadata, self.source_pdfs[spec.source], spec.source, "ATTACH01",
        )


class ErrorZotero(FakeZotero):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def resolve(self, spec: InputSpec) -> ZoteroResolution:
        raise self.error


class UnavailableZotero(ErrorZotero):
    def is_available(self) -> bool:
        return False


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
        self.interests: list[str] = []

    def summarize_artifacts(
        self, bundle: ArtifactBundle, *, job_id: str, research_interest: str = "",
    ) -> PaperSummary:
        self.calls += 1
        self.interests.append(research_interest)
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


def test_changed_reacquired_pdf_invalidates_all_dependent_outputs(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    service.acquirer = ChangingAcquirer()
    notion = FailingOnceNotion()
    service._notion = notion
    failed = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))
    assert failed.artifact_dir is not None
    (failed.artifact_dir / "source.pdf").write_bytes(b"corrupted")

    resumed = service.resume(failed.id)

    assert resumed.state is JobState.COMPLETED
    assert service.acquirer.calls == 2
    assert dependencies["zotero"].upsert_calls == 2
    assert dependencies["converter"].calls == 2
    assert dependencies["summarizer"].calls == 2
    assert notion.calls == [None, None]


def test_recomputed_conversion_invalidates_quality_summary_and_notion(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    notion = FailingOnceNotion()
    service._notion = notion
    failed = service.ingest(InputSpec(InputKind.LOCAL_PDF, str(source)))
    assert failed.artifact_dir is not None
    (failed.artifact_dir / "document.json").write_text(
        '{"pages": [], "changed": true}\n', encoding="utf-8",
    )

    resumed = service.resume(failed.id)

    assert resumed.state is JobState.COMPLETED
    assert dependencies["acquirer"].calls == 1
    assert dependencies["zotero"].upsert_calls == 1
    assert dependencies["converter"].calls == 2
    assert dependencies["summarizer"].calls == 2
    assert notion.calls == [None, None]


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


def test_user_correctable_source_and_attachment_errors_need_input(tmp_path: Path) -> None:
    service, dependencies = _service(tmp_path / "source")
    service.acquirer = ErrorAcquirer(AcquisitionInputError("source is not a PDF"))
    source_job = service.ingest(InputSpec(InputKind.PDF_URL, "https://example.test/bad.pdf"))

    zotero_service, zotero_dependencies = _service(tmp_path / "zotero")
    zotero_service.zotero = ErrorZotero(ZoteroInputError("invalid attachment selection"))
    attachment_job = zotero_service.ingest(
        InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", attachment_key="BADPDF01")
    )

    assert source_job.state is JobState.NEEDS_INPUT
    assert source_job.error == "source is not a PDF"
    assert attachment_job.state is JobState.NEEDS_INPUT
    assert attachment_job.error == "invalid attachment selection"
    assert dependencies["summarizer"].calls == 0
    assert zotero_dependencies["summarizer"].calls == 0


def test_transient_acquisition_and_zotero_errors_remain_failed(tmp_path: Path) -> None:
    service, _ = _service(tmp_path / "source")
    service.acquirer = ErrorAcquirer(AcquisitionError("network unavailable"))
    source_job = service.ingest(InputSpec(InputKind.PDF_URL, "https://example.test/paper.pdf"))

    zotero_service, _ = _service(tmp_path / "zotero")
    zotero_service.zotero = UnavailableZotero(ZoteroError("Zotero local API unavailable"))
    zotero_job = zotero_service.ingest(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))

    assert source_job.state is JobState.FAILED
    assert source_job.error == "network unavailable"
    assert zotero_job.state is JobState.FAILED
    assert zotero_job.error == "Zotero local API unavailable"


def test_missing_local_source_creates_a_resumable_needs_input_job(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.acquirer = DocumentAcquirer()

    job = service.ingest(
        InputSpec(InputKind.LOCAL_PDF, str(tmp_path / "missing.pdf"))
    )

    assert job.state is JobState.NEEDS_INPUT
    assert job.error is not None
    assert "does not exist" in job.error


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


def test_research_interest_override_after_late_failure_invalidates_summary_and_notion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    notion = FailingOnceNotion()
    service._notion = notion
    failed = service.ingest(
        InputSpec(InputKind.LOCAL_PDF, str(source), research_interest="old interest")
    )

    resumed = service.resume(failed.id, research_interest="new interest")

    assert resumed.state is JobState.COMPLETED
    assert dependencies["acquirer"].calls == 1
    assert dependencies["converter"].calls == 1
    assert dependencies["summarizer"].calls == 2
    assert dependencies["summarizer"].interests == ["old interest", "new interest"]
    assert notion.calls == [None, None]


def test_attachment_override_after_late_failure_invalidates_every_checkpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "zotero.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service, dependencies = _service(tmp_path)
    zotero = AttachmentSelectingZotero(source)
    notion = FailingOnceNotion()
    service.zotero = zotero
    service._notion = notion
    failed = service.ingest(
        InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", attachment_key="ATTACH01")
    )

    resumed = service.resume(failed.id, attachment_key="ATTACH02")

    assert resumed.state is JobState.COMPLETED
    assert zotero.selections == ["ATTACH01", "ATTACH02"]
    assert dependencies["converter"].calls == 2
    assert dependencies["summarizer"].calls == 2
    assert notion.calls == [None, None]


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


def test_only_unprocessed_uses_the_configured_zotero_library_identity(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "zotero-1.pdf"
    second_source = tmp_path / "zotero-2.pdf"
    first_source.write_bytes(b"%PDF-1.4\nfixture one\n%%EOF")
    second_source.write_bytes(b"%PDF-1.4\nfixture two\n%%EOF")
    service, _ = _service(tmp_path)
    service.settings.zotero.user_id = 42
    zotero = CollectionZotero(
        {"PARENT01": first_source, "PARENT02": second_source}, library_id="42",
    )
    service.zotero = zotero
    collection = InputSpec(InputKind.ZOTERO_COLLECTION, "COLLECT1")

    first = service.ingest_collection(collection, only_unprocessed=True)
    second = service.ingest_collection(collection, only_unprocessed=True)

    assert len(first) == 2
    assert second == []
    assert zotero.resolve_calls == 2
