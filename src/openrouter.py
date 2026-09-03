"""Privacy-preserving, resumable OpenRouter paper summarization."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import os
import re
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError

from src.artifacts import ArtifactManager
from src.config import OpenRouterSettings
from src.job_store import JobStore
from src.research_models import ArtifactBundle, EvidenceAnchor, PaperSummary


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class PrivacyRequirementsError(RuntimeError):
    """Raised when a request would weaken the required routing policy."""


class PricingUnavailableError(RuntimeError):
    """Raised when a model has no trusted price available."""


class BudgetExceededError(RuntimeError):
    """Raised before a request whose maximum cost is not authorized."""


class StructuredOutputError(RuntimeError):
    """Raised when bounded structured-output repair is exhausted."""


class OpenRouterAPIError(RuntimeError):
    """Raised for a sanitized OpenRouter transport or protocol failure."""


class OpenRouterConfigurationError(RuntimeError):
    """Raised when required environment configuration is absent."""


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    quote: str = Field(min_length=1)


_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


class PaperSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    background: str = Field(min_length=1)
    question: str = Field(min_length=1)
    novelty: str = Field(min_length=1)
    methods: str = Field(min_length=1)
    datasets: list[str]
    results: str = Field(min_length=1)
    strengths: str = Field(min_length=1)
    limitations: str = Field(min_length=1)
    takeaways: str = Field(min_length=1)
    relevance_score: int = Field(ge=1, le=5)
    score_rationale: str = Field(min_length=1)
    keywords: list[str]
    evidence: list[EvidenceResponse]

    @field_validator(
        "background", "question", "novelty", "methods", "results", "strengths",
        "limitations", "takeaways", "score_rationale",
    )
    @classmethod
    def require_japanese_narrative(cls, value: str) -> str:
        if not _JAPANESE.search(value):
            raise ValueError("narrative fields must be written in Japanese")
        return value


class ChunkExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    datasets: list[str]
    metrics: list[str]
    keywords: list[str]
    evidence: list[EvidenceResponse]

    @field_validator("summary")
    @classmethod
    def require_japanese_summary(cls, value: str) -> str:
        if not _JAPANESE.search(value):
            raise ValueError("chunk summary must be written in Japanese")
        return value


class _UsageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost: Decimal = Field(ge=0)


class _MessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    content: str


class _ChoiceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: _MessageResponse


class _CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    provider: str = Field(min_length=1)
    choices: list[_ChoiceResponse] = Field(min_length=1)
    usage: _UsageResponse


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: Decimal
    output_per_million: Decimal


# OpenRouter model-card list prices checked 2026-09-03. Unknown/overridden
# models intentionally have no fallback price and are rejected before a call.
CURRENT_MODEL_PRICING = {
    "google/gemini-3.8-flash": ModelPricing(Decimal("0.75"), Decimal("3.75")),
    "openai/gpt-5.6-sol": ModelPricing(Decimal("2.00"), Decimal("10.00")),
}


def estimate_worst_case_cost(
    pricing: ModelPricing, *, max_input_tokens: int, max_output_tokens: int,
) -> Decimal:
    if max_input_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token limits must not be negative")
    million = Decimal(1_000_000)
    return (
        Decimal(max_input_tokens) * pricing.input_per_million
        + Decimal(max_output_tokens) * pricing.output_per_million
    ) / million


@dataclass(slots=True)
class BudgetGuard:
    pricing: dict[str, ModelPricing]
    budget_usd: Decimal
    spent_usd: Decimal = Decimal("0")

    def authorize(
        self, model: str, *, max_input_tokens: int, max_output_tokens: int, calls: int = 1,
    ) -> Decimal:
        price = self.pricing.get(model)
        if price is None:
            raise PricingUnavailableError(f"Pricing unavailable for {model}")
        required = estimate_worst_case_cost(
            price,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
        ) * calls
        remaining = self.budget_usd - self.spent_usd
        if required > remaining:
            raise BudgetExceededError(
                f"Worst-case cost ${required} exceeds remaining budget ${remaining}"
            )
        return required

    def authorize_amount(self, required: Decimal) -> None:
        remaining = self.budget_usd - self.spent_usd
        if required > remaining:
            raise BudgetExceededError(
                f"Worst-case cost ${required} exceeds remaining budget ${remaining}"
            )


def build_structured_payload(
    *,
    settings: OpenRouterSettings,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    max_output_tokens: int,
) -> dict[str, Any]:
    """Build the enforced OpenRouter JSON-schema request body."""
    if not settings.require_parameters or not settings.zdr or settings.data_collection:
        raise PrivacyRequirementsError(
            "Structured output, ZDR, and disabled provider data collection are mandatory"
        )
    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "strict": True,
                "schema": response_model.model_json_schema(),
            },
        },
        "provider": {
            "require_parameters": True,
            "zdr": True,
            "data_collection": "deny",
        },
    }


class OpenRouterSummarizer:
    """Run validated map/reduce summarization with durable paid-call caching."""

    def __init__(
        self,
        store: JobStore,
        *,
        settings: OpenRouterSettings | None = None,
        client: httpx.Client | None = None,
        pricing: dict[str, ModelPricing] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or OpenRouterSettings()
        self.client = client or httpx.Client(timeout=60.0)
        self.pricing = dict(CURRENT_MODEL_PRICING if pricing is None else pricing)

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return self._request_hash(payload)

    def _cached(
        self,
        cache_key: str,
        response_model: type[ResponseModel],
        validate: Callable[[ResponseModel], None] | None = None,
    ) -> ResponseModel | None:
        cached = self.store.get_cached_llm_result(cache_key)
        if cached is None:
            return None
        try:
            parsed = response_model.model_validate(cached["response"])
            if validate is not None:
                validate(parsed)
            return parsed
        except (ValidationError, ValueError):
            return None

    def _structured_call(
        self,
        *,
        payload: dict[str, Any],
        response_model: type[ResponseModel],
        job_id: str,
        cache_key: str,
        validate: Callable[[ResponseModel], None] | None = None,
    ) -> ResponseModel:
        cached = self._cached(cache_key, response_model, validate)
        if cached is not None:
            return cached
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY is required")
        last_error: Exception | None = None
        attempts = self.settings.max_validation_retries + 1
        first_attempt = self.store.llm_attempt_count(cache_key)
        if first_attempt >= attempts:
            raise StructuredOutputError(
                f"Structured output retries already exhausted after {attempts} attempts"
            )
        for attempt in range(first_attempt, attempts):
            attempt_payload = dict(payload)
            if attempt:
                attempt_payload["messages"] = [
                    *payload["messages"],
                    {
                        "role": "system",
                        "content": (
                            f"検証失敗後の再生成 {attempt}/{self.settings.max_validation_retries}。"
                            "指定されたJSON Schemaだけに従ってください。"
                        ),
                    },
                ]
            request_hash = self._request_hash(attempt_payload)
            try:
                response = self.client.post(
                    self.settings.endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=attempt_payload,
                )
            except httpx.HTTPError as error:
                raise OpenRouterAPIError("OpenRouter request failed") from error
            if response.status_code < 200 or response.status_code >= 300:
                raise OpenRouterAPIError(f"OpenRouter returned HTTP {response.status_code}")
            try:
                envelope = _CompletionResponse.model_validate(response.json())
            except (ValueError, ValidationError) as error:
                last_error = error
                continue
            usage = envelope.usage.model_dump(mode="json")
            if envelope.model != attempt_payload["model"]:
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=envelope.model,
                    provider=envelope.provider,
                    response={"content": envelope.choices[0].message.content},
                    usage=usage,
                    cost_usd=float(envelope.usage.cost),
                    validated=False,
                )
                raise OpenRouterAPIError("OpenRouter returned an unexpected model")
            content = envelope.choices[0].message.content
            try:
                candidate = json.loads(content)
                parsed = response_model.model_validate(candidate)
                if validate is not None:
                    validate(parsed)
            except (json.JSONDecodeError, ValidationError, ValueError) as error:
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=envelope.model,
                    provider=envelope.provider,
                    response={"content": content},
                    usage=usage,
                    cost_usd=float(envelope.usage.cost),
                    validated=False,
                )
                last_error = error
                continue
            self.store.record_llm_call(
                job_id,
                request_hash=request_hash,
                cache_key=cache_key,
                model=envelope.model,
                provider=envelope.provider,
                response=parsed.model_dump(mode="json"),
                usage=usage,
                cost_usd=float(envelope.usage.cost),
                validated=True,
            )
            return parsed
        raise StructuredOutputError(
            f"Structured output remained invalid after {attempts} attempts"
        ) from last_error

    def _map_payload(self, chunk: DocumentChunk, research_interest: str) -> dict[str, Any]:
        chunk_data = {
            "section": chunk.section,
            "start_page": chunk.start_page,
            "end_page": chunk.end_page,
            "kind": chunk.kind,
            "text": chunk.text,
            "evidence_spans": [
                {
                    "page": item.page,
                    "section_id": item.section_id,
                    "kind": item.kind,
                    "source_position": item.source_position,
                }
                for item in chunk.evidence
            ],
        }
        return build_structured_payload(
            settings=self.settings,
            model=self.settings.extraction_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "論文の断片から事実だけを日本語で抽出してください。根拠は入力のページと節に限定してください。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"research_interest": research_interest, "chunk": chunk_data},
                        ensure_ascii=False,
                    ),
                },
            ],
            response_model=ChunkExtractionResponse,
            max_output_tokens=self.settings.extraction_max_output_tokens,
        )

    def _synthesis_payload(
        self, extractions: list[ChunkExtractionResponse], research_interest: str,
    ) -> dict[str, Any]:
        return build_structured_payload(
            settings=self.settings,
            model=self.settings.synthesis_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "検証済みの抽出結果だけを使い、全項目を日本語で統合してください。"
                        "推測を避け、relevance_scoreは1から5で評価してください。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "research_interest": research_interest,
                            "validated_extractions": [
                                item.model_dump(mode="json") for item in extractions
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            response_model=PaperSummaryResponse,
            max_output_tokens=self.settings.synthesis_max_output_tokens,
        )

    def _call_cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        pricing = self.pricing.get(model)
        if pricing is None:
            raise PricingUnavailableError(f"Pricing unavailable for {model}")
        return estimate_worst_case_cost(
            pricing,
            max_input_tokens=input_tokens,
            max_output_tokens=output_tokens,
        )

    @staticmethod
    def _evidence_validator(
        chunks: list[DocumentChunk],
    ) -> Callable[[BaseModel], None]:
        pages = {
            evidence.page for chunk in chunks for evidence in chunk.evidence
        }
        sections = {chunk.section for chunk in chunks if chunk.section}

        def validate(response: BaseModel) -> None:
            evidence_items = getattr(response, "evidence", [])
            for anchor in evidence_items:
                if anchor.page is not None and anchor.page not in pages:
                    raise ValueError("evidence page is outside the source document")
                if anchor.section is not None and anchor.section not in sections:
                    raise ValueError("evidence section is outside the source document")

        return validate

    def summarize(
        self,
        document: dict[str, Any],
        *,
        job_id: str,
        artifacts: ArtifactManager,
        research_interest: str = "",
    ) -> PaperSummary:
        chunks = build_document_chunks(document, max_chars=self.settings.chunk_max_chars)
        if not chunks:
            raise ValueError("document contains no summarizable content")
        map_payloads = [self._map_payload(chunk, research_interest) for chunk in chunks]
        map_cache_keys = [self._cache_key(payload) for payload in map_payloads]
        cached_maps = [
            self._cached(key, ChunkExtractionResponse, self._evidence_validator([chunk]))
            for key, chunk in zip(map_cache_keys, chunks, strict=True)
        ]
        uncached_count = sum(item is None for item in cached_maps)
        guard = BudgetGuard(
            pricing=self.pricing,
            budget_usd=Decimal(str(self.settings.paper_budget_usd)),
            spent_usd=Decimal(str(self.store.total_cost(job_id))),
        )
        attempts = self.settings.max_validation_retries + 1
        if uncached_count:
            planned_cost = (
                self._call_cost(
                    self.settings.extraction_model,
                    self.settings.extraction_max_input_tokens,
                    self.settings.extraction_max_output_tokens,
                ) * uncached_count * attempts
                + self._call_cost(
                    self.settings.synthesis_model,
                    self.settings.synthesis_max_input_tokens,
                    self.settings.synthesis_max_output_tokens,
                ) * attempts
            )
            guard.authorize_amount(planned_cost)

        extractions: list[ChunkExtractionResponse] = []
        for payload, cache_key, cached, chunk in zip(
            map_payloads, map_cache_keys, cached_maps, chunks, strict=True,
        ):
            result = cached or self._structured_call(
                payload=payload,
                response_model=ChunkExtractionResponse,
                job_id=job_id,
                cache_key=cache_key,
                validate=self._evidence_validator([chunk]),
            )
            extractions.append(ChunkExtractionResponse.model_validate(result))

        synthesis_payload = self._synthesis_payload(extractions, research_interest)
        synthesis_cache_key = self._cache_key(synthesis_payload)
        evidence_validator = self._evidence_validator(chunks)
        cached_summary = self._cached(
            synthesis_cache_key, PaperSummaryResponse, evidence_validator,
        )
        if cached_summary is None:
            if not uncached_count:
                guard.authorize(
                    self.settings.synthesis_model,
                    max_input_tokens=self.settings.synthesis_max_input_tokens,
                    max_output_tokens=self.settings.synthesis_max_output_tokens,
                    calls=attempts,
                )
            response = self._structured_call(
                payload=synthesis_payload,
                response_model=PaperSummaryResponse,
                job_id=job_id,
                cache_key=synthesis_cache_key,
                validate=evidence_validator,
            )
        else:
            response = cached_summary
        validated = PaperSummaryResponse.model_validate(response)
        artifacts.create()
        artifacts.write_summary(validated.model_dump(mode="json"))
        return PaperSummary(
            background=validated.background,
            question=validated.question,
            novelty=validated.novelty,
            methods=validated.methods,
            datasets=validated.datasets,
            results=validated.results,
            strengths=validated.strengths,
            limitations=validated.limitations,
            takeaways=validated.takeaways,
            relevance_score=validated.relevance_score,
            score_rationale=validated.score_rationale,
            keywords=validated.keywords,
            evidence=[EvidenceAnchor(**item.model_dump()) for item in validated.evidence],
        )

    def summarize_artifacts(
        self,
        bundle: ArtifactBundle,
        *,
        job_id: str,
        research_interest: str = "",
    ) -> PaperSummary:
        """Summarize Task 4's durable document artifact into the same bundle."""
        document = json.loads(bundle.document_json.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("document.json must contain a JSON object")
        artifacts = ArtifactManager(bundle.root.parent, bundle.root.name)
        return self.summarize(
            document,
            job_id=job_id,
            artifacts=artifacts,
            research_interest=research_interest,
        )


@dataclass(frozen=True, slots=True)
class ChunkEvidence:
    kind: str
    page: int
    section_id: str | None
    source_position: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    text: str
    kind: str
    section: str | None
    start_page: int
    end_page: int
    evidence: tuple[ChunkEvidence, ...]


def build_document_chunks(document: dict[str, Any], *, max_chars: int) -> list[DocumentChunk]:
    """Build section-local chunks while treating structured blocks as atoms."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    section_titles = {
        section.get("id"): section.get("title")
        for section in document.get("sections", [])
        if isinstance(section, dict)
    }
    blocks: list[tuple[str, dict[str, Any], str]] = []
    for collection, content_key, kind in (
        ("paragraphs", "text", "paragraph"),
        ("tables", "markdown", "table"),
        ("equations", "text", "equation"),
        ("captions", "text", "caption"),
    ):
        for index, block in enumerate(document.get(collection, [])):
            if isinstance(block, dict) and str(block.get(content_key, "")).strip():
                blocks.append((kind, block | {"_index": index}, content_key))
    blocks.sort(key=lambda item: (
        int(item[1].get("page") or item[1].get("source_position", {}).get("page") or 1),
        item[1].get("source_position", {}).get("line", 10**9),
        item[1].get("source_position", {}).get("bbox", [10**9, 0])[0],
        {"paragraph": 0, "table": 1, "equation": 2, "caption": 3}[item[0]],
        item[1]["_index"],
    ))

    chunks: list[DocumentChunk] = []
    pending: list[tuple[str, dict[str, Any], str]] = []

    def flush() -> None:
        if not pending:
            return
        pages = [int(block.get("page") or block.get("source_position", {}).get("page") or 1)
                 for _, block, _ in pending]
        section_id = pending[0][1].get("section_id")
        chunks.append(DocumentChunk(
            text="\n\n".join(str(block[key]).strip() for _, block, key in pending),
            kind=pending[0][0] if len(pending) == 1 else "mixed",
            section=section_titles.get(section_id),
            start_page=min(pages),
            end_page=max(pages),
            evidence=tuple(ChunkEvidence(
                kind=kind,
                page=int(block.get("page") or block.get("source_position", {}).get("page") or 1),
                section_id=block.get("section_id"),
                source_position=dict(block.get("source_position") or {"page": pages[index]}),
            ) for index, (kind, block, _) in enumerate(pending)),
        ))
        pending.clear()

    for item in blocks:
        kind, block, content_key = item
        text = str(block[content_key]).strip()
        section_id = block.get("section_id")
        pending_section = pending[0][1].get("section_id") if pending else section_id
        prospective_length = sum(len(str(existing[key]).strip()) for _, existing, key in pending)
        prospective_length += (2 * len(pending)) + len(text)
        if pending and (section_id != pending_section or prospective_length > max_chars or kind != "paragraph"):
            flush()
        if kind != "paragraph":
            pending.append(item)
            flush()
        else:
            pending.append(item)
    flush()
    return chunks
