"""Shared, dependency-free types for the research pipeline."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class InputKind(StrEnum):
    LOCAL_PDF = "local_pdf"
    PDF_URL = "pdf_url"
    DOI = "doi"
    ARXIV = "arxiv"
    ZOTERO_ITEM = "zotero_item"
    ZOTERO_COLLECTION = "zotero_collection"


class JobState(StrEnum):
    QUEUED = "queued"
    ACQUIRING = "acquiring"
    ZOTERO_SYNC = "zotero_sync"
    CONVERTING = "converting"
    QUALITY_CHECK = "quality_check"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    NOTION_SYNC = "notion_sync"
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"


@dataclass(slots=True)
class InputSpec:
    kind: InputKind
    source: str
    attachment_key: str | None = None
    research_interest: str | None = None

    def __post_init__(self) -> None:
        self.source = self.source.strip()
        if not self.source:
            raise ValueError("Input source must not be empty")


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value or None


def _normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^arxiv:\s*", "", value)
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value)
    value = re.sub(r"\.pdf$", "", value)
    return re.sub(r"v\d+$", "", value) or None


@dataclass(slots=True)
class PaperMetadata:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    published_date: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    source_url: str | None = None
    zotero_library_id: str | None = None
    zotero_item_key: str | None = None

    def __post_init__(self) -> None:
        self.doi = _normalize_doi(self.doi)
        self.arxiv_id = _normalize_arxiv(self.arxiv_id)
        if self.zotero_library_id is not None:
            self.zotero_library_id = str(self.zotero_library_id).strip()
        if self.zotero_item_key is not None:
            self.zotero_item_key = self.zotero_item_key.strip()

    def canonical_identity(self, pdf_sha256: str | None = None) -> str | None:
        if self.doi:
            return f"doi:{self.doi}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.zotero_library_id and self.zotero_item_key:
            return f"zotero:{self.zotero_library_id}:{self.zotero_item_key}"
        if pdf_sha256:
            return f"sha256:{pdf_sha256.strip().lower()}"
        return None


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    root: Path
    source_pdf: Path
    metadata_json: Path
    document_json: Path
    paper_md: Path
    figures_dir: Path
    summary_json: Path
    manifest_json: Path
    logs_dir: Path


@dataclass(slots=True)
class EvidenceAnchor:
    page: int | None = None
    section: str | None = None
    quote: str | None = None


@dataclass(slots=True)
class PaperSummary:
    background: str = ""
    question: str = ""
    novelty: str = ""
    methods: str = ""
    datasets: list[str] = field(default_factory=list)
    results: str = ""
    strengths: str = ""
    limitations: str = ""
    takeaways: str = ""
    relevance_score: int | None = None
    score_rationale: str = ""
    keywords: list[str] = field(default_factory=list)
    evidence: list[EvidenceAnchor] = field(default_factory=list)


@dataclass(slots=True)
class JobRecord:
    id: str
    paper_id: int | None
    state: JobState
    input_spec: InputSpec
    artifact_dir: Path | None = None
    error: str | None = None
    total_cost_usd: float = 0.0
