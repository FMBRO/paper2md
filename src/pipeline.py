"""Resumable orchestration shared by the research CLI and GUI."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import httpx

from src.acquisition import AcquisitionInputError, AcquisitionResult, DocumentAcquirer
from src.artifacts import ArtifactManager
from src.config import Settings
from src.converter import Converter
from src.job_store import JobStore
from src.notion import (
    NotionConfigurationError,
    NotionSchemaError,
    NotionSummaryUpserter,
)
from src.openrouter import (
    BudgetExceededError,
    InputLimitExceededError,
    OpenRouterConfigurationError,
    OpenRouterSummarizer,
    PricingUnavailableError,
    PrivacyRequirementsError,
    UnresolvedUsageError,
)
from src.pipeline_events import PipelineEvent
from src.research_models import (
    ArtifactBundle,
    EvidenceAnchor,
    InputKind,
    InputSpec,
    JobRecord,
    JobState,
    PaperMetadata,
    PaperSummary,
)
from src.zotero import ZoteroClient, ZoteroInputError, ZoteroResolution


class NeedsInputError(RuntimeError):
    """A known user action is required before the job can continue."""


def _metadata_payload(metadata: PaperMetadata) -> dict[str, Any]:
    return asdict(metadata)


def _metadata_from(payload: dict[str, Any]) -> PaperMetadata:
    return PaperMetadata(**payload)


def _summary_from(payload: dict[str, Any]) -> PaperSummary:
    value = dict(payload)
    value["evidence"] = [EvidenceAnchor(**item) for item in value.get("evidence", [])]
    return PaperSummary(**value)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineService:
    """Run every paper stage with durable, validated resume checkpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: JobStore | None = None,
        acquirer: Any | None = None,
        zotero: Any | None = None,
        converter: Any | None = None,
        summarizer: Any | None = None,
        notion: Any | None = None,
        on_event: Callable[[PipelineEvent], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or JobStore(settings.state_path)
        self.acquirer = acquirer or DocumentAcquirer()
        self.zotero = zotero or ZoteroClient(settings.zotero)
        self.converter = converter or Converter(
            enable_ocr=settings.enable_ocr,
            force_ocr=settings.force_ocr,
            language=settings.language,
            ocr_deskew=settings.ocr_deskew,
            ocr_clean=settings.ocr_clean,
        )
        self.summarizer = summarizer or OpenRouterSummarizer(
            self.store, settings=settings.openrouter,
        )
        self._notion = notion
        self._on_event = on_event

    @property
    def notion(self) -> Any:
        if self._notion is None:
            self._notion = NotionSummaryUpserter(
                settings=self.settings.notion, client=httpx.Client(),
            )
        return self._notion

    def ingest(self, spec: InputSpec, max_cost_usd: float = 0.50) -> JobRecord:
        if max_cost_usd < 0:
            raise ValueError("max_cost_usd must not be negative")
        artifact_dir = self._artifact_manager(spec).bundle.root
        job = self.store.create_job(
            spec, artifact_dir=artifact_dir, max_cost_usd=max_cost_usd,
        )
        self._emit(job.id, JobState.QUEUED)
        return self._run(job.id, max_cost_usd=max_cost_usd)

    def ingest_collection(
        self,
        spec: InputSpec,
        *,
        max_cost_usd: float = 0.50,
        only_unprocessed: bool = False,
    ) -> list[JobRecord]:
        if spec.kind is not InputKind.ZOTERO_COLLECTION:
            raise ValueError("ingest_collection requires a Zotero collection input")
        unique_specs: list[InputSpec] = []
        seen: set[tuple[InputKind, str]] = set()
        for item_spec in self.zotero.expand_collection(spec):
            key = (item_spec.kind, item_spec.source)
            if key in seen:
                continue
            seen.add(key)
            if only_unprocessed:
                identity = f"zotero:{self.settings.zotero.user_id}:{item_spec.source}"
                if self.store.has_completed_job(identity):
                    continue
            unique_specs.append(item_spec)
        return [self.ingest(item, max_cost_usd) for item in unique_specs]

    def resume(
        self,
        job_id: str,
        max_cost_usd: float | None = None,
        *,
        attachment_key: str | None = None,
        research_interest: str | None = None,
    ) -> JobRecord:
        job = self.store.get_job(job_id)
        if attachment_key is not None or research_interest is not None:
            next_attachment = (
                attachment_key
                if attachment_key is not None
                else job.input_spec.attachment_key
            )
            next_interest = (
                research_interest
                if research_interest is not None
                else job.input_spec.research_interest
            )
            invalidate_from = None
            if next_attachment != job.input_spec.attachment_key:
                invalidate_from = JobState.ACQUIRING
            elif next_interest != job.input_spec.research_interest:
                invalidate_from = JobState.EXTRACTING
            job = self.store.update_input_spec(
                job_id,
                InputSpec(
                    job.input_spec.kind,
                    job.input_spec.source,
                    attachment_key=next_attachment,
                    research_interest=next_interest,
                ),
                invalidate_from=invalidate_from,
            )
        if max_cost_usd is not None:
            job = self.store.update_job_budget(job_id, max_cost_usd)
        if job.state is JobState.COMPLETED:
            job = self.store.reopen_completed_job(job_id)
        elif job.state in {
            JobState.NEEDS_INPUT, JobState.BUDGET_EXCEEDED, JobState.FAILED,
        }:
            job = self.store.transition(job_id, JobState.QUEUED)
        return self._run(
            job_id,
            max_cost_usd=job.max_cost_usd,
        )

    def status(self, job_id: str) -> JobRecord:
        return self.store.get_job(job_id)

    def _artifact_manager(self, spec: InputSpec) -> ArtifactManager:
        if spec.kind is InputKind.LOCAL_PDF and Path(spec.source).is_file():
            digest = hashlib.sha256(Path(spec.source).read_bytes()).hexdigest()
        else:
            canonical = f"{spec.kind.value}:{spec.source}".encode("utf-8")
            digest = hashlib.sha256(canonical).hexdigest()
        return ArtifactManager(self.settings.output_dir / "papers", digest[:24])

    def _emit(self, job_id: str, stage: JobState, message: str = "") -> None:
        if self._on_event is not None:
            self._on_event(PipelineEvent(
                kind="stage_changed", pdf_name=job_id, stage=stage.value,
                message=message,
            ))

    def _enter(self, job_id: str, stage: JobState) -> None:
        job = self.store.get_job(job_id)
        if job.state is stage:
            return
        order = [
            JobState.QUEUED, JobState.ACQUIRING, JobState.ZOTERO_SYNC,
            JobState.CONVERTING, JobState.QUALITY_CHECK, JobState.EXTRACTING,
            JobState.SYNTHESIZING, JobState.NOTION_SYNC, JobState.COMPLETED,
        ]
        while order.index(job.state) < order.index(stage):
            next_state = order[order.index(job.state) + 1]
            job = self.store.transition(job_id, next_state)
            self._emit(job_id, next_state)

    def _checkpoint(self, job_id: str, stage: JobState, payload: Any) -> None:
        self.store.save_checkpoint(job_id, stage, payload)

    def _stop(self, job_id: str, state: JobState, error: Exception) -> JobRecord:
        job = self.store.transition(job_id, state, str(error))
        self._emit(job_id, state, str(error))
        return job

    def _run(self, job_id: str, *, max_cost_usd: float) -> JobRecord:
        try:
            job = self.store.get_job(job_id)
            manager = ArtifactManager(
                job.artifact_dir.parent, job.artifact_dir.name,
            ) if job.artifact_dir else self._artifact_manager(job.input_spec)
            bundle = manager.create()

            self._enter(job_id, JobState.ACQUIRING)
            acquisition_checkpoint = self.store.get_checkpoint(
                job_id, JobState.ACQUIRING,
            )
            acquisition = self._restored_acquisition(acquisition_checkpoint)
            acquired_zotero: ZoteroResolution | None = None
            if acquisition is not None and job.input_spec.kind is InputKind.ZOTERO_ITEM:
                acquired_zotero = self._zotero_from_acquisition_checkpoint(
                    acquisition_checkpoint, acquisition,
                )
            if acquisition is None:
                acquisition, acquired_zotero = self._acquire(job, manager)
                metadata = acquisition.metadata
                source_pdf = acquisition.source_pdf
                if acquisition.state is JobState.NEEDS_INPUT or source_pdf is None:
                    raise NeedsInputError("A readable PDF is required to continue")
                previous_sha256 = (
                    acquisition_checkpoint.get("pdf_sha256")
                    if isinstance(acquisition_checkpoint, dict)
                    else None
                )
                if (
                    previous_sha256 is not None
                    and previous_sha256 != acquisition.pdf_sha256
                ):
                    self.store.invalidate_checkpoints(
                        job_id, JobState.ZOTERO_SYNC,
                    )
                manager.write_metadata(_metadata_payload(metadata))
                paper_id = self.store.upsert_paper(
                    metadata, acquisition.pdf_sha256, bundle.root,
                )
                self.store.link_job_paper(job_id, paper_id, bundle.root)
                acquisition_payload = {
                    "metadata": _metadata_payload(metadata),
                    "pdf_sha256": acquisition.pdf_sha256,
                    "source_pdf": str(source_pdf),
                }
                if acquired_zotero is not None:
                    acquisition_payload.update({
                        "parent_key": acquired_zotero.parent_key,
                        "attachment_key": acquired_zotero.attachment_key,
                    })
                self._checkpoint(
                    job_id, JobState.ACQUIRING, acquisition_payload,
                )
            metadata = acquisition.metadata
            source_pdf = acquisition.source_pdf
            if source_pdf is None:
                raise NeedsInputError("A readable PDF is required to continue")
            current_job = self.store.get_job(job_id)
            paper_id = current_job.paper_id
            if paper_id is None:
                paper_id = self.store.upsert_paper(
                    metadata, acquisition.pdf_sha256, bundle.root,
                )
                self.store.link_job_paper(job_id, paper_id, bundle.root)

            self._enter(job_id, JobState.ZOTERO_SYNC)
            zotero_checkpoint = self.store.get_checkpoint(
                job_id, JobState.ZOTERO_SYNC,
            )
            resolution = self._restored_zotero(zotero_checkpoint)
            if resolution is None:
                self.store.invalidate_checkpoints(job_id, JobState.CONVERTING)
                if job.input_spec.kind is InputKind.ZOTERO_ITEM:
                    resolution = acquired_zotero
                    if resolution is None:
                        resolution = ZoteroResolution(
                            metadata, source_pdf,
                            metadata.zotero_item_key or job.input_spec.source,
                            job.input_spec.attachment_key,
                        )
                else:
                    resolution = self.zotero.upsert_non_zotero(metadata, source_pdf)
                if resolution.state is JobState.NEEDS_INPUT or resolution.source_pdf is None:
                    raise NeedsInputError(resolution.diagnostic or "Select a PDF attachment")
                metadata = resolution.metadata
                source_pdf = resolution.source_pdf
                manager.write_metadata(_metadata_payload(metadata))
                paper_id = self.store.upsert_paper(
                    metadata, acquisition.pdf_sha256, bundle.root,
                )
                self.store.link_job_paper(job_id, paper_id, bundle.root)
                self._checkpoint(job_id, JobState.ZOTERO_SYNC, {
                    "metadata": _metadata_payload(metadata),
                    "source_pdf": str(source_pdf),
                    "parent_key": resolution.parent_key,
                    "attachment_key": resolution.attachment_key,
                })
            metadata = resolution.metadata
            source_pdf = resolution.source_pdf
            if source_pdf is None:
                raise NeedsInputError("A readable PDF is required to continue")
            paper_id = self.store.get_job(job_id).paper_id
            if paper_id is None:
                raise RuntimeError("The job has no canonical paper after Zotero sync")

            self._enter(job_id, JobState.CONVERTING)
            converting_checkpoint = self.store.get_checkpoint(
                job_id, JobState.CONVERTING,
            )
            if not self._valid_conversion_checkpoint(
                converting_checkpoint, bundle, acquisition.pdf_sha256,
            ):
                self.store.invalidate_checkpoints(job_id, JobState.QUALITY_CHECK)
                bundle = self.converter.convert(source_pdf, bundle.root)
                self._checkpoint(
                    job_id, JobState.CONVERTING,
                    self._conversion_checkpoint(bundle, acquisition.pdf_sha256),
                )

            self._enter(job_id, JobState.QUALITY_CHECK)
            quality = self.store.get_checkpoint(job_id, JobState.QUALITY_CHECK)
            if not isinstance(quality, dict) or not quality.get("llm_allowed"):
                self.store.invalidate_checkpoints(job_id, JobState.EXTRACTING)
                quality = json.loads(
                    (bundle.logs_dir / "quality_result.json").read_text(encoding="utf-8")
                )
            if not quality.get("llm_allowed"):
                issues = quality.get("issues") or []
                detail = "; ".join(
                    str(item.get("message", item)) if isinstance(item, dict) else str(item)
                    for item in issues
                )
                raise NeedsInputError(f"Document quality check failed: {detail}")
            if self.store.get_checkpoint(job_id, JobState.QUALITY_CHECK) is None:
                self._checkpoint(job_id, JobState.QUALITY_CHECK, quality)

            self._enter(job_id, JobState.EXTRACTING)
            extraction_checkpoint = self.store.get_checkpoint(
                job_id, JobState.EXTRACTING,
            )
            summary = self._restored_summary(extraction_checkpoint, bundle)
            summary_was_restored = summary is not None
            if summary is None:
                self.store.invalidate_checkpoints(job_id, JobState.SYNTHESIZING)
                previous_budget = getattr(
                    getattr(self.summarizer, "settings", None), "paper_budget_usd", None,
                )
                if previous_budget is not None:
                    self.summarizer.settings.paper_budget_usd = max_cost_usd
                try:
                    summary = self.summarizer.summarize_artifacts(
                        bundle, job_id=job_id,
                        research_interest=(
                            job.input_spec.research_interest
                            if job.input_spec.research_interest is not None
                            else self.settings.research_interest
                        ),
                    )
                finally:
                    if previous_budget is not None:
                        self.summarizer.settings.paper_budget_usd = previous_budget
                self._checkpoint(
                    job_id, JobState.EXTRACTING, {"summary": asdict(summary)},
                )

            self._enter(job_id, JobState.SYNTHESIZING)
            if (
                not summary_was_restored
                or self.store.get_checkpoint(job_id, JobState.SYNTHESIZING) is None
            ):
                self._checkpoint(
                    job_id, JobState.SYNTHESIZING, {"summary": asdict(summary)},
                )

            self._enter(job_id, JobState.NOTION_SYNC)
            notion_checkpoint = self.store.get_checkpoint(job_id, JobState.NOTION_SYNC)
            if not isinstance(notion_checkpoint, dict) or not notion_checkpoint.get("page_id"):
                stored_page_id = self.store.get_notion_page_id(paper_id)
                page_id = self.notion.upsert(
                    metadata, summary, stored_page_id=stored_page_id,
                    model_prompt_version=(
                        f"{self.settings.openrouter.extraction_model} / "
                        f"{self.settings.openrouter.synthesis_model}"
                    ),
                )
                self.store.complete_notion_sync(
                    job_id, paper_id, page_id, {"page_id": page_id},
                )
            self._enter(job_id, JobState.COMPLETED)
            return self.store.get_job(job_id)
        except BudgetExceededError as error:
            return self._stop(job_id, JobState.BUDGET_EXCEEDED, error)
        except (
            NeedsInputError,
            AcquisitionInputError,
            NotionConfigurationError,
            NotionSchemaError,
            OpenRouterConfigurationError,
            PricingUnavailableError,
            PrivacyRequirementsError,
            InputLimitExceededError,
            UnresolvedUsageError,
            ZoteroInputError,
        ) as error:
            return self._stop(job_id, JobState.NEEDS_INPUT, error)
        except Exception as error:
            return self._stop(job_id, JobState.FAILED, error)

    def _acquire(
        self, job: JobRecord, manager: ArtifactManager,
    ) -> tuple[AcquisitionResult, ZoteroResolution | None]:
        if job.input_spec.kind is not InputKind.ZOTERO_ITEM:
            return self.acquirer.acquire(job.input_spec, manager), None
        resolution = self.zotero.resolve(job.input_spec)
        if resolution.state is JobState.NEEDS_INPUT or resolution.source_pdf is None:
            raise NeedsInputError(resolution.diagnostic or "Select a PDF attachment")
        content = resolution.source_pdf.read_bytes()
        source_pdf = manager.write_source_pdf(content)
        return (
            AcquisitionResult(
                resolution.metadata, source_pdf, hashlib.sha256(content).hexdigest(),
            ),
            ZoteroResolution(
                resolution.metadata, source_pdf, resolution.parent_key,
                resolution.attachment_key,
            ),
        )

    @staticmethod
    def _restored_acquisition(payload: Any) -> AcquisitionResult | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
            return None
        source_pdf = Path(str(payload.get("source_pdf", "")))
        if not source_pdf.is_file():
            return None
        try:
            expected_sha256 = payload.get("pdf_sha256")
            if (
                not isinstance(expected_sha256, str)
                or hashlib.sha256(source_pdf.read_bytes()).hexdigest() != expected_sha256
            ):
                return None
            return AcquisitionResult(
                _metadata_from(payload["metadata"]), source_pdf,
                expected_sha256,
            )
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _restored_zotero(payload: Any) -> ZoteroResolution | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
            return None
        source_pdf = Path(str(payload.get("source_pdf", "")))
        parent_key = payload.get("parent_key")
        if not source_pdf.is_file() or not isinstance(parent_key, str) or not parent_key:
            return None
        try:
            return ZoteroResolution(
                _metadata_from(payload["metadata"]), source_pdf, parent_key,
                payload.get("attachment_key"),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _zotero_from_acquisition_checkpoint(
        payload: Any, acquisition: AcquisitionResult,
    ) -> ZoteroResolution | None:
        if not isinstance(payload, dict):
            return None
        parent_key = payload.get("parent_key")
        if not isinstance(parent_key, str) or not parent_key or acquisition.source_pdf is None:
            return None
        return ZoteroResolution(
            acquisition.metadata, acquisition.source_pdf, parent_key,
            payload.get("attachment_key"),
        )

    @staticmethod
    def _conversion_checkpoint(
        bundle: ArtifactBundle, source_sha256: str | None,
    ) -> dict[str, Any]:
        quality_path = bundle.logs_dir / "quality_result.json"
        return {
            "version": 1,
            "artifact_dir": str(bundle.root),
            "source_sha256": source_sha256,
            "document_sha256": _file_sha256(bundle.document_json),
            "paper_sha256": _file_sha256(bundle.paper_md),
            "quality_sha256": _file_sha256(quality_path),
        }

    @staticmethod
    def _valid_conversion_checkpoint(
        payload: Any, bundle: ArtifactBundle, source_sha256: str | None,
    ) -> bool:
        quality_path = bundle.logs_dir / "quality_result.json"
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or Path(str(payload.get("artifact_dir", ""))) != bundle.root
            or payload.get("source_sha256") != source_sha256
        ):
            return False
        try:
            return (
                payload.get("document_sha256") == _file_sha256(bundle.document_json)
                and payload.get("paper_sha256") == _file_sha256(bundle.paper_md)
                and payload.get("quality_sha256") == _file_sha256(quality_path)
            )
        except OSError:
            return False

    @staticmethod
    def _restored_summary(payload: Any, bundle: ArtifactBundle) -> PaperSummary | None:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("summary"), dict)
            or not bundle.summary_json.is_file()
        ):
            return None
        try:
            artifact_payload = json.loads(bundle.summary_json.read_text(encoding="utf-8"))
            if artifact_payload != payload["summary"]:
                return None
            return _summary_from(artifact_payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


__all__ = ["NeedsInputError", "PipelineService"]
