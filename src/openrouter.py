"""Privacy-preserving, resumable OpenRouter paper summarization."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError

from src.artifacts import ArtifactManager
from src.config import OPENROUTER_CHAT_COMPLETIONS_ENDPOINT, OpenRouterSettings
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


class InputLimitExceededError(RuntimeError):
    """Raised before sending content that cannot fit a configured input ceiling."""


class UnresolvedUsageError(RuntimeError):
    """Raised when a successful HTTP response has no trustworthy billable cost."""


class RequestInFlightError(RuntimeError):
    """Raised when another caller holds a request lease beyond the wait bound."""


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    section: str = Field(min_length=1)
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
    evidence: list[EvidenceResponse] = Field(min_length=1)

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
    evidence: list[EvidenceResponse] = Field(min_length=1)

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


class _AuditUsageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: Decimal | None = Field(default=None, ge=0)


class _BillableResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    generation_id: str | None = Field(default=None, alias="id")
    model: str | None = None
    provider: str | None = None
    usage: _AuditUsageResponse | None = None


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


@dataclass(frozen=True, slots=True)
class PricingSnapshot(ModelPricing):
    model: str
    version: str

    def as_record(self) -> dict[str, str]:
        return {
            "model": self.model,
            "input_per_million": str(self.input_per_million),
            "output_per_million": str(self.output_per_million),
            "version": self.version,
        }


class ModelCatalog(Protocol):
    def pricing_for(self, model: str) -> PricingSnapshot: ...


class OpenRouterModelCatalog:
    """Load current per-token prices from OpenRouter's official model catalog."""

    endpoint = "https://openrouter.ai/api/v1/models"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def pricing_for(self, model: str) -> PricingSnapshot:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY is required")
        try:
            response = self.client.get(
                self.endpoint,
                params={
                    "q": model,
                    "supported_parameters": "structured_outputs",
                    "zdr": "true",
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError as error:
            raise PricingUnavailableError(f"Pricing unavailable for {model}") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise PricingUnavailableError(f"Pricing unavailable for {model}")
        try:
            payload = response.json()
            matches = [item for item in payload["data"] if item.get("id") == model]
            if len(matches) != 1:
                raise ValueError("model missing or ambiguous")
            item = matches[0]
            supported = set(item.get("supported_parameters") or [])
            if not {"structured_outputs", "response_format"} & supported:
                raise ValueError("structured output unavailable")
            prompt = Decimal(str(item["pricing"]["prompt"])) * Decimal(1_000_000)
            completion = Decimal(str(item["pricing"]["completion"])) * Decimal(1_000_000)
            if prompt < 0 or completion < 0:
                raise ValueError("negative price")
            version = (
                response.headers.get("etag")
                or response.headers.get("last-modified")
                or f"created:{item['created']}"
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise PricingUnavailableError(f"Pricing unavailable for {model}") from error
        return PricingSnapshot(
            model=model,
            input_per_million=prompt,
            output_per_million=completion,
            version=version,
        )


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


def estimate_serialized_tokens(payload: dict[str, Any]) -> int:
    """Conservative tokenizer-independent upper bound for a JSON request."""
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return len(serialized.encode("utf-8"))


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
    pricing: PricingSnapshot,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    max_output_tokens: int,
) -> dict[str, Any]:
    """Build the enforced OpenRouter JSON-schema request body."""
    if not settings.require_parameters or not settings.zdr or settings.data_collection:
        raise PrivacyRequirementsError(
            "Structured output, ZDR, and disabled provider data collection are mandatory"
        )
    if pricing.model != model:
        raise PricingUnavailableError(f"Pricing snapshot does not match {model}")
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
            "max_price": {
                "prompt": float(pricing.input_per_million),
                "completion": float(pricing.output_per_million),
            },
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
        catalog: ModelCatalog | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or OpenRouterSettings()
        self.client = client or httpx.Client(timeout=self.settings.request_timeout_seconds)
        self.catalog = catalog or OpenRouterModelCatalog(self.client)

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _cache_key(self, payload: dict[str, Any]) -> str:
        logical_request = {
            key: payload[key]
            for key in (
                "model", "messages", "max_tokens", "temperature", "stream",
                "response_format",
            )
        }
        return self._request_hash(logical_request)

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
        pricing: PricingSnapshot,
        max_input_tokens: int,
        stage: str,
        validate: Callable[[ResponseModel], None] | None = None,
        reservation_id: str | None = None,
    ) -> ResponseModel:
        owner_token = str(uuid.uuid4())
        deadline = time.monotonic() + 30.0
        attempts = self.settings.max_validation_retries + 1
        while True:
            cached = self._cached(cache_key, response_model, validate)
            if cached is not None:
                if reservation_id is not None:
                    self.store.release_llm_budget(reservation_id)
                return cached
            if self.store.has_unresolved_llm_call(cache_key):
                raise UnresolvedUsageError(
                    "OpenRouter cost remains unresolved for this request"
                )
            if self.store.llm_attempt_count(cache_key) >= attempts:
                raise StructuredOutputError(
                    f"Structured output retries already exhausted after {attempts} attempts"
                )
            if self.store.claim_llm_request(
                cache_key,
                owner_token,
                now=time.time(),
                lease_seconds=(
                    self.settings.request_timeout_seconds * attempts + 30.0
                ),
            ):
                break
            if time.monotonic() >= deadline:
                raise RequestInFlightError("Timed out waiting for an identical LLM request")
            time.sleep(0.01)
        active_reservation = reservation_id or str(uuid.uuid4())
        preserve_reservation = False
        try:
            authorized_per_attempt = self._call_cost(
                pricing, max_input_tokens, int(payload["max_tokens"]),
            )
            first_attempt = self.store.llm_attempt_count(cache_key)
            required = float(authorized_per_attempt * (attempts - first_attempt))
            if reservation_id is None:
                reserved = self.store.reserve_llm_budget(
                    job_id,
                    active_reservation,
                    amount_usd=required,
                    budget_usd=self.settings.paper_budget_usd,
                )
                if not reserved:
                    raise BudgetExceededError("OpenRouter request would exceed paper budget")
            else:
                available = self.store.llm_budget_reservation(active_reservation)
                if available is None or available + 1e-12 < required:
                    raise BudgetExceededError("Reserved synthesis budget is insufficient")
            return self._claimed_structured_call(
                payload=payload,
                response_model=response_model,
                job_id=job_id,
                cache_key=cache_key,
                pricing=pricing,
                max_input_tokens=max_input_tokens,
                stage=stage,
                validate=validate,
                reservation_id=active_reservation,
                authorized_per_attempt=float(authorized_per_attempt),
            )
        except UnresolvedUsageError:
            preserve_reservation = True
            raise
        finally:
            if not preserve_reservation:
                self.store.release_llm_budget(active_reservation)
            self.store.release_llm_request(cache_key, owner_token)

    def _claimed_structured_call(
        self,
        *,
        payload: dict[str, Any],
        response_model: type[ResponseModel],
        job_id: str,
        cache_key: str,
        pricing: PricingSnapshot,
        max_input_tokens: int,
        stage: str,
        validate: Callable[[ResponseModel], None] | None = None,
        reservation_id: str,
        authorized_per_attempt: float,
    ) -> ResponseModel:
        cached = self._cached(cache_key, response_model, validate)
        if cached is not None:
            return cached
        if self.store.has_unresolved_llm_call(cache_key):
            raise UnresolvedUsageError("OpenRouter cost remains unresolved for this request")
        self._validate_endpoint()
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
            self._ensure_input_limit(attempt_payload, max_input_tokens, stage)
            request_hash = self._request_hash(attempt_payload)
            try:
                response = self.client.post(
                    self.settings.endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=attempt_payload,
                    timeout=self.settings.request_timeout_seconds,
                )
            except httpx.HTTPError as error:
                raise OpenRouterAPIError("OpenRouter request failed") from error
            if response.status_code < 200 or response.status_code >= 300:
                raise OpenRouterAPIError(f"OpenRouter returned HTTP {response.status_code}")
            try:
                raw_response = response.json()
            except ValueError as error:
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=str(attempt_payload["model"]),
                    provider=None,
                    generation_id=None,
                    response={"body_sha256": hashlib.sha256(response.content).hexdigest()},
                    usage={},
                    cost_usd=0.0,
                    cost_resolved=False,
                    validated=False,
                    pricing=pricing.as_record(),
                )
                self.store.retain_unresolved_llm_budget(
                    reservation_id, authorized_per_attempt,
                )
                raise UnresolvedUsageError(
                    "OpenRouter returned 2xx without parseable cost metadata"
                ) from error
            try:
                audit = _BillableResponse.model_validate(raw_response)
            except ValidationError as error:
                audit = _BillableResponse()
                last_error = error
            resolved = (
                audit.generation_id is not None
                and audit.model is not None
                and audit.provider is not None
                and audit.usage is not None
                and audit.usage.prompt_tokens is not None
                and audit.usage.completion_tokens is not None
                and audit.usage.total_tokens is not None
                and audit.usage.cost is not None
            )
            if not resolved:
                usage = audit.usage.model_dump(mode="json") if audit.usage else {}
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=audit.model or str(attempt_payload["model"]),
                    provider=audit.provider,
                    generation_id=audit.generation_id,
                    response=raw_response,
                    usage=usage,
                    cost_usd=0.0,
                    cost_resolved=False,
                    validated=False,
                    pricing=pricing.as_record(),
                )
                self.store.retain_unresolved_llm_budget(
                    reservation_id, authorized_per_attempt,
                )
                raise UnresolvedUsageError(
                    "OpenRouter returned 2xx but usage cost is unavailable"
                ) from last_error
            usage = audit.usage.model_dump(mode="json")
            cost_usd = float(audit.usage.cost)
            if audit.model != attempt_payload["model"]:
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=audit.model,
                    provider=audit.provider,
                    generation_id=audit.generation_id,
                    response=raw_response,
                    usage=usage,
                    cost_usd=cost_usd,
                    validated=False,
                    pricing=pricing.as_record(),
                )
                self.store.consume_llm_budget(reservation_id, authorized_per_attempt)
                raise OpenRouterAPIError("OpenRouter returned an unexpected model")
            try:
                envelope = _CompletionResponse.model_validate(raw_response)
            except ValidationError as error:
                self.store.record_llm_call(
                    job_id,
                    request_hash=request_hash,
                    cache_key=cache_key,
                    model=audit.model,
                    provider=audit.provider,
                    generation_id=audit.generation_id,
                    response=raw_response,
                    usage=usage,
                    cost_usd=cost_usd,
                    validated=False,
                    pricing=pricing.as_record(),
                )
                self.store.consume_llm_budget(reservation_id, authorized_per_attempt)
                last_error = error
                continue
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
                    generation_id=audit.generation_id,
                    response={"content": content},
                    usage=usage,
                    cost_usd=cost_usd,
                    validated=False,
                    pricing=pricing.as_record(),
                )
                self.store.consume_llm_budget(reservation_id, authorized_per_attempt)
                last_error = error
                continue
            self.store.record_llm_call(
                job_id,
                request_hash=request_hash,
                cache_key=cache_key,
                model=envelope.model,
                provider=envelope.provider,
                generation_id=audit.generation_id,
                response=parsed.model_dump(mode="json"),
                usage=usage,
                cost_usd=cost_usd,
                validated=True,
                pricing=pricing.as_record(),
            )
            self.store.consume_llm_budget(reservation_id, authorized_per_attempt)
            return parsed
        raise StructuredOutputError(
            f"Structured output remained invalid after {attempts} attempts"
        ) from last_error

    def _map_payload(
        self, chunk: DocumentChunk, research_interest: str, pricing: PricingSnapshot,
    ) -> dict[str, Any]:
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
            pricing=pricing,
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
        self,
        extractions: list[ChunkExtractionResponse],
        research_interest: str,
        pricing: PricingSnapshot,
    ) -> dict[str, Any]:
        return build_structured_payload(
            settings=self.settings,
            model=self.settings.synthesis_model,
            pricing=pricing,
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

    def _reduction_payload(
        self,
        extractions: list[ChunkExtractionResponse],
        research_interest: str,
        pricing: PricingSnapshot,
    ) -> dict[str, Any]:
        return build_structured_payload(
            settings=self.settings,
            model=self.settings.extraction_model,
            pricing=pricing,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "検証済み抽出結果を、根拠を失わず重複を除いて日本語で圧縮してください。"
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
            response_model=ChunkExtractionResponse,
            max_output_tokens=self.settings.extraction_max_output_tokens,
        )

    @staticmethod
    def _ensure_input_limit(
        payload: dict[str, Any], max_input_tokens: int, stage: str,
    ) -> None:
        estimated = estimate_serialized_tokens(payload)
        if estimated > max_input_tokens:
            raise InputLimitExceededError(
                f"{stage} request requires at most {estimated} conservative tokens, "
                f"above configured ceiling {max_input_tokens}"
            )

    def _reduction_groups(
        self,
        extractions: list[ChunkExtractionResponse],
        research_interest: str,
        pricing: PricingSnapshot,
    ) -> list[tuple[list[ChunkExtractionResponse], dict[str, Any]]]:
        groups: list[tuple[list[ChunkExtractionResponse], dict[str, Any]]] = []
        pending: list[ChunkExtractionResponse] = []
        pending_payload: dict[str, Any] | None = None
        for extraction in extractions:
            candidate = [*pending, extraction]
            payload = self._reduction_payload(candidate, research_interest, pricing)
            if estimate_serialized_tokens(payload) <= self.settings.extraction_max_input_tokens:
                pending = candidate
                pending_payload = payload
                continue
            if not pending or pending_payload is None:
                raise InputLimitExceededError(
                    "one validated extraction cannot fit the reduction input ceiling"
                )
            groups.append((pending, pending_payload))
            pending = [extraction]
            pending_payload = self._reduction_payload(pending, research_interest, pricing)
            self._ensure_input_limit(
                pending_payload, self.settings.extraction_max_input_tokens, "reduction",
            )
        if pending and pending_payload is not None:
            groups.append((pending, pending_payload))
        if len(groups) >= len(extractions):
            raise InputLimitExceededError(
                "reduction ceiling cannot combine two extraction results"
            )
        return groups

    @staticmethod
    def _call_cost(pricing: PricingSnapshot, input_tokens: int, output_tokens: int) -> Decimal:
        return estimate_worst_case_cost(
            pricing,
            max_input_tokens=input_tokens,
            max_output_tokens=output_tokens,
        )

    def _validate_endpoint(self) -> None:
        if self.settings.endpoint != OPENROUTER_CHAT_COMPLETIONS_ENDPOINT:
            raise ValueError(
                f"openrouter endpoint must be exactly {OPENROUTER_CHAT_COMPLETIONS_ENDPOINT}"
            )

    @staticmethod
    def _evidence_validator(
        chunks: list[DocumentChunk],
    ) -> Callable[[BaseModel], None]:
        def normalize(value: str) -> str:
            return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

        spans: dict[tuple[int, str], list[str]] = {}
        for chunk in chunks:
            for evidence in chunk.evidence:
                spans.setdefault((evidence.page, chunk.section), []).append(
                    normalize(evidence.source_text)
                )

        def validate(response: BaseModel) -> None:
            evidence_items = getattr(response, "evidence", [])
            for anchor in evidence_items:
                matching_spans = spans.get((anchor.page, anchor.section))
                if not matching_spans:
                    raise ValueError("evidence page and section do not identify a source span")
                quote = normalize(anchor.quote)
                if not quote:
                    raise ValueError("evidence quote is empty after normalization")
                if not any(quote in source for source in matching_spans):
                    raise ValueError("evidence quote is absent from its source span")

        return validate

    def summarize(
        self,
        document: dict[str, Any],
        *,
        job_id: str,
        artifacts: ArtifactManager,
        research_interest: str = "",
    ) -> PaperSummary:
        self._validate_endpoint()
        chunks = build_document_chunks(document, max_chars=self.settings.chunk_max_chars)
        if not chunks:
            raise ValueError("document contains no summarizable content")
        extraction_pricing = self.catalog.pricing_for(self.settings.extraction_model)
        synthesis_pricing = self.catalog.pricing_for(self.settings.synthesis_model)
        map_payloads = [
            self._map_payload(chunk, research_interest, extraction_pricing)
            for chunk in chunks
        ]
        for payload in map_payloads:
            self._ensure_input_limit(
                payload, self.settings.extraction_max_input_tokens, "extraction",
            )
        map_cache_keys = [self._cache_key(payload) for payload in map_payloads]
        cached_maps = [
            self._cached(key, ChunkExtractionResponse, self._evidence_validator([chunk]))
            for key, chunk in zip(map_cache_keys, chunks, strict=True)
        ]
        synthesis_reservation_id = str(uuid.uuid4())
        synthesis_headroom = float(
            self._call_cost(
                synthesis_pricing,
                self.settings.synthesis_max_input_tokens,
                self.settings.synthesis_max_output_tokens,
            ) * (self.settings.max_validation_retries + 1)
        )
        if not self.store.reserve_llm_budget(
            job_id,
            synthesis_reservation_id,
            amount_usd=synthesis_headroom,
            budget_usd=self.settings.paper_budget_usd,
        ):
            raise BudgetExceededError("Final synthesis headroom is unavailable")
        try:
            extractions: list[ChunkExtractionResponse] = []
            for payload, cache_key, cached, chunk in zip(
                map_payloads, map_cache_keys, cached_maps, chunks, strict=True,
            ):
                result = cached or self._structured_call(
                    payload=payload,
                    response_model=ChunkExtractionResponse,
                    job_id=job_id,
                    cache_key=cache_key,
                    pricing=extraction_pricing,
                    max_input_tokens=self.settings.extraction_max_input_tokens,
                    stage="extraction",
                    validate=self._evidence_validator([chunk]),
                )
                extractions.append(ChunkExtractionResponse.model_validate(result))

            synthesis_payload = self._synthesis_payload(
                extractions, research_interest, synthesis_pricing,
            )
        except BaseException:
            self.store.release_llm_budget(synthesis_reservation_id)
            raise
        try:
            for level in range(self.settings.max_reduction_levels + 1):
                if estimate_serialized_tokens(synthesis_payload) <= self.settings.synthesis_max_input_tokens:
                    break
                if level == self.settings.max_reduction_levels:
                    raise InputLimitExceededError(
                        f"synthesis request still exceeds its ceiling after {level} reduction levels"
                    )
                groups = self._reduction_groups(
                    extractions, research_interest, extraction_pricing,
                )
                reduced: list[ChunkExtractionResponse] = []
                for _, payload in groups:
                    cache_key = self._cache_key(payload)
                    response = self._structured_call(
                        payload=payload,
                        response_model=ChunkExtractionResponse,
                        job_id=job_id,
                        cache_key=cache_key,
                        pricing=extraction_pricing,
                        max_input_tokens=self.settings.extraction_max_input_tokens,
                        stage="reduction",
                        validate=self._evidence_validator(chunks),
                    )
                    reduced.append(ChunkExtractionResponse.model_validate(response))
                extractions = reduced
                synthesis_payload = self._synthesis_payload(
                    extractions, research_interest, synthesis_pricing,
                )
            synthesis_cache_key = self._cache_key(synthesis_payload)
            evidence_validator = self._evidence_validator(chunks)
            cached_summary = self._cached(
                synthesis_cache_key, PaperSummaryResponse, evidence_validator,
            )
            if cached_summary is None:
                response = self._structured_call(
                    payload=synthesis_payload,
                    response_model=PaperSummaryResponse,
                    job_id=job_id,
                    cache_key=synthesis_cache_key,
                    pricing=synthesis_pricing,
                    max_input_tokens=self.settings.synthesis_max_input_tokens,
                    stage="synthesis",
                    validate=evidence_validator,
                    reservation_id=synthesis_reservation_id,
                )
            else:
                response = cached_summary
        finally:
            self.store.release_llm_budget(synthesis_reservation_id)
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
    source_text: str
    source_position: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    text: str
    kind: str
    section: str
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
            section=str(section_titles.get(section_id) or "Unsectioned"),
            start_page=min(pages),
            end_page=max(pages),
            evidence=tuple(ChunkEvidence(
                kind=kind,
                page=int(block.get("page") or block.get("source_position", {}).get("page") or 1),
                section_id=block.get("section_id"),
                source_text=str(block[key]).strip(),
                source_position=dict(block.get("source_position") or {"page": pages[index]}),
            ) for index, (kind, block, key) in enumerate(pending)),
        ))
        pending.clear()

    for item in blocks:
        kind, block, content_key = item
        text = str(block[content_key]).strip()
        if kind in {"table", "equation", "caption"} and len(text) > max_chars:
            raise InputLimitExceededError(
                f"indivisible {kind} block exceeds the configured chunk ceiling"
            )
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
