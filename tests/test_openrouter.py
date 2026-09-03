from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest


def test_section_chunks_keep_structured_blocks_atomic_and_retain_evidence() -> None:
    from src.openrouter import build_document_chunks

    table = "| metric | value |\n|---|---|\n| accuracy | 99.1% |"
    document = {
        "sections": [
            {"id": "intro", "title": "Introduction", "page": 1},
            {"id": "results", "title": "Results", "page": 2},
        ],
        "paragraphs": [
            {"text": "Background paragraph.", "page": 1, "section_id": "intro"},
            {"text": "Results paragraph.", "page": 2, "section_id": "results"},
        ],
        "tables": [{"markdown": table, "page": 2, "section_id": "results"}],
        "equations": [{"text": "E = mc^2", "page": 2, "section_id": "results"}],
        "captions": [{"text": "Figure 1. Overview", "page": 2, "section_id": "results"}],
    }

    chunks = build_document_chunks(document, max_chars=80)

    assert [chunk.section for chunk in chunks] == ["Introduction", "Results", "Results", "Results", "Results"]
    assert chunks[0].text == "Background paragraph."
    assert chunks[2].text == table
    assert chunks[2].kind == "table"
    assert chunks[2].start_page == chunks[2].end_page == 2
    assert chunks[2].evidence[0].section_id == "results"
    assert chunks[3].text == "E = mc^2"
    assert chunks[4].text == "Figure 1. Overview"


def test_chunk_builder_rejects_an_oversized_indivisible_table() -> None:
    from src.openrouter import InputLimitExceededError, build_document_chunks

    document = {
        "sections": [{"id": "results", "title": "Results", "page": 2}],
        "paragraphs": [],
        "tables": [{
            "markdown": "| metric | value |\n|---|---|\n| accuracy | 99.1% |",
            "page": 2,
            "section_id": "results",
        }],
        "equations": [],
        "captions": [],
    }

    with pytest.raises(InputLimitExceededError, match="table"):
        build_document_chunks(document, max_chars=20)


def test_section_chunks_combine_small_paragraphs_without_crossing_sections() -> None:
    from src.openrouter import build_document_chunks

    document = {
        "sections": [
            {"id": "a", "title": "A", "page": 1},
            {"id": "b", "title": "B", "page": 1},
        ],
        "paragraphs": [
            {"text": "one", "page": 1, "section_id": "a"},
            {"text": "two", "page": 1, "section_id": "a"},
            {"text": "three", "page": 1, "section_id": "b"},
        ],
        "tables": [],
        "equations": [],
        "captions": [],
    }

    chunks = build_document_chunks(document, max_chars=20)

    assert [(chunk.section, chunk.text) for chunk in chunks] == [
        ("A", "one\n\ntwo"),
        ("B", "three"),
    ]


def test_structured_payload_requires_schema_zdr_and_no_data_collection() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import PaperSummaryResponse, PricingSnapshot, build_structured_payload

    payload = build_structured_payload(
        settings=OpenRouterSettings(),
        model="google/gemini-3.8-flash",
        pricing=PricingSnapshot(
            model="google/gemini-3.8-flash",
            input_per_million=Decimal("0.75"),
            output_per_million=Decimal("3.75"),
            version="catalog-v1",
        ),
        messages=[{"role": "user", "content": "summarize"}],
        response_model=PaperSummaryResponse,
        max_output_tokens=1000,
    )

    assert payload["model"] == "google/gemini-3.8-flash"
    assert "models" not in payload
    assert payload["provider"] == {
        "require_parameters": True,
        "zdr": True,
        "data_collection": "deny",
        "max_price": {"prompt": 0.75, "completion": 3.75},
    }
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {"require_parameters": False},
        {"zdr": False},
        {"data_collection": True},
    ],
)
def test_structured_payload_fails_closed_when_privacy_is_weakened(settings_kwargs: dict[str, bool]) -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import PaperSummaryResponse, PricingSnapshot, PrivacyRequirementsError, build_structured_payload

    with pytest.raises(PrivacyRequirementsError):
        build_structured_payload(
            settings=OpenRouterSettings(**settings_kwargs),
            model="example/model",
            pricing=PricingSnapshot(
                model="example/model",
                input_per_million=Decimal("1"),
                output_per_million=Decimal("2"),
                version="test",
            ),
            messages=[{"role": "user", "content": "summarize"}],
            response_model=PaperSummaryResponse,
            max_output_tokens=100,
        )


def test_final_summary_schema_rejects_out_of_range_score_and_english_narrative() -> None:
    from pydantic import ValidationError

    from src.openrouter import PaperSummaryResponse

    payload = {
        "background": "研究背景です。",
        "question": "研究課題です。",
        "novelty": "新規性があります。",
        "methods": "手法を説明します。",
        "datasets": ["Dataset A"],
        "results": "結果は良好です。",
        "strengths": "強みがあります。",
        "limitations": "限界があります。",
        "takeaways": "重要な示唆です。",
        "relevance_score": 6,
        "score_rationale": "関連性の根拠です。",
        "keywords": ["機械学習"],
        "evidence": [{"page": 1, "section": "Introduction", "quote": "根拠となる記述。"}],
    }
    with pytest.raises(ValidationError):
        PaperSummaryResponse.model_validate(payload)


def test_final_summary_schema_requires_evidence() -> None:
    from pydantic import ValidationError

    from src.openrouter import PaperSummaryResponse

    payload = _summary()
    payload["evidence"] = []

    with pytest.raises(ValidationError, match="evidence"):
        PaperSummaryResponse.model_validate(payload)

    payload["relevance_score"] = 4
    payload["background"] = "English only"
    with pytest.raises(ValidationError):
        PaperSummaryResponse.model_validate(payload)


def test_worst_case_cost_uses_both_configured_token_limits() -> None:
    from src.openrouter import ModelPricing, estimate_worst_case_cost

    price = ModelPricing(input_per_million=Decimal("2"), output_per_million=Decimal("10"))

    assert estimate_worst_case_cost(price, max_input_tokens=20_000, max_output_tokens=3_000) == Decimal("0.070000")


def test_budget_guard_rejects_missing_pricing_and_exhausted_budget() -> None:
    from src.openrouter import BudgetExceededError, BudgetGuard, PricingUnavailableError

    guard = BudgetGuard(pricing={}, budget_usd=Decimal("0.50"), spent_usd=Decimal("0.49"))

    with pytest.raises(PricingUnavailableError, match="missing/model"):
        guard.authorize("missing/model", max_input_tokens=100, max_output_tokens=100)

    from src.openrouter import ModelPricing

    guarded = BudgetGuard(
        pricing={"priced/model": ModelPricing(Decimal("2"), Decimal("10"))},
        budget_usd=Decimal("0.50"),
        spent_usd=Decimal("0.49"),
    )
    with pytest.raises(BudgetExceededError, match="remaining"):
        guarded.authorize("priced/model", max_input_tokens=10_000, max_output_tokens=1_000)


def _document() -> dict[str, object]:
    return {
        "sections": [{"id": "intro", "title": "Introduction", "page": 1}],
        "paragraphs": [{
            "text": "This paper evaluates a compact method.",
            "page": 1,
            "section_id": "intro",
            "source_position": {"page": 1, "line": 3},
        }],
        "tables": [],
        "equations": [],
        "captions": [],
    }


def _extraction() -> dict[str, object]:
    return {
        "summary": "小型手法を評価している。",
        "datasets": ["Dataset A"],
        "metrics": ["accuracy 99.1%"],
        "keywords": ["評価"],
        "evidence": [{"page": 1, "section": "Introduction", "quote": "evaluates a compact method"}],
    }


def _summary() -> dict[str, object]:
    return {
        "background": "既存手法には計算量の課題がある。",
        "question": "小型化しても精度を維持できるかを問う。",
        "novelty": "新しい圧縮法を提案する。",
        "methods": "比較実験により提案法を評価する。",
        "datasets": ["Dataset A"],
        "results": "精度99.1%を達成した。",
        "strengths": "計算効率と精度を両立する。",
        "limitations": "単一データセットの評価に限られる。",
        "takeaways": "小型モデルでも高精度を実現できる。",
        "relevance_score": 4,
        "score_rationale": "研究関心との関連が高い。",
        "keywords": ["圧縮", "評価"],
        "evidence": [{"page": 1, "section": "Introduction", "quote": "evaluates a compact method"}],
    }


def _completion(content: object, *, model: str, provider: str, cost: float) -> dict[str, object]:
    return {
        "id": "generation-id",
        "model": model,
        "provider": provider,
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost": cost,
        },
    }


def _settings(**overrides: object):
    from src.config import OpenRouterSettings

    defaults = {
        "extraction_max_input_tokens": 4_000,
        "extraction_max_output_tokens": 200,
        "synthesis_max_input_tokens": 5_000,
        "synthesis_max_output_tokens": 500,
        "chunk_max_chars": 1000,
        "max_validation_retries": 1,
        "max_reduction_levels": 4,
    }
    return OpenRouterSettings(**(defaults | overrides))


class _FakeCatalog:
    def __init__(self, prices: dict[str, tuple[str, str]] | None = None) -> None:
        self.prices = prices or {
            "google/gemini-3.8-flash": ("0.75", "3.75"),
            "openai/gpt-5.6-sol": ("2.00", "10.00"),
        }
        self.requests: list[str] = []

    def pricing_for(self, model: str):
        from src.openrouter import PricingSnapshot, PricingUnavailableError

        self.requests.append(model)
        try:
            input_price, output_price = self.prices[model]
        except KeyError as error:
            raise PricingUnavailableError(f"Pricing unavailable for {model}") from error
        return PricingSnapshot(
            model=model,
            input_per_million=Decimal(input_price),
            output_per_million=Decimal(output_price),
            version="catalog-v1",
        )


def _pricing() -> _FakeCatalog:
    return _FakeCatalog()


def test_summarizer_persists_usage_writes_summary_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "top-secret-key")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        content = _extraction() if len(requests) == 1 else _summary()
        return httpx.Response(
            200,
            json=_completion(content, model=payload["model"], provider="provider-a", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    artifacts = ArtifactManager(tmp_path / "output", "paper")
    artifacts.create()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    summarizer = OpenRouterSummarizer(store, settings=_settings(), client=client, catalog=_pricing())

    result = summarizer.summarize(
        _document(), job_id=job.id, artifacts=artifacts, research_interest="軽量モデル",
    )
    resumed = summarizer.summarize(
        _document(), job_id=job.id, artifacts=artifacts, research_interest="軽量モデル",
    )

    assert result.background == "既存手法には計算量の課題がある。"
    assert resumed == result
    assert len(requests) == 2
    assert all(request.url == "https://openrouter.ai/api/v1/chat/completions" for request in requests)
    assert all(request.headers["authorization"] == "Bearer top-secret-key" for request in requests)
    assert [json.loads(request.content)["model"] for request in requests] == [
        "google/gemini-3.8-flash", "openai/gpt-5.6-sol",
    ]
    assert store.total_cost(job.id) == pytest.approx(0.02)
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT provider, usage_json, validated, pricing_json FROM llm_calls ORDER BY id"
        ).fetchall()
    assert rows[0][0] == "provider-a"
    assert json.loads(rows[0][1])["total_tokens"] == 120
    assert [row[2] for row in rows] == [1, 1]
    assert json.loads(rows[0][3]) == {
        "model": "google/gemini-3.8-flash",
        "input_per_million": "0.75",
        "output_per_million": "3.75",
        "version": "catalog-v1",
    }
    assert b"top-secret-key" not in store.path.read_bytes()
    saved = json.loads(artifacts.bundle.summary_json.read_text(encoding="utf-8"))
    assert saved["relevance_score"] == 4
    assert saved["evidence"][0]["page"] == 1


def test_summarizer_allows_configured_role_model_ids_without_cross_model_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        content = _extraction() if len(requests) == 1 else _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    catalog = _FakeCatalog({"custom/extract": ("1", "2"), "custom/synth": ("3", "4")})
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(extraction_model="custom/extract", synthesis_model="custom/synth"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=catalog,
    )

    summarizer.summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert [payload["model"] for payload in requests] == ["custom/extract", "custom/synth"]
    assert all("models" not in payload for payload in requests)
    assert catalog.requests == ["custom/extract", "custom/synth"]


def test_summarizer_repairs_malformed_output_without_forwarding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            response = _completion("not-json", model=payload["model"], provider="p", cost=0.01)
            response["choices"][0]["message"]["content"] = "not-json"  # type: ignore[index]
        elif len(payloads) == 2:
            response = _completion(_extraction(), model=payload["model"], provider="p", cost=0.01)
        else:
            response = _completion(_summary(), model=payload["model"], provider="p", cost=0.01)
        return httpx.Response(200, json=response)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    artifacts = ArtifactManager(tmp_path / "out", "paper")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = OpenRouterSummarizer(
        store, settings=_settings(), client=client, catalog=_pricing(),
    ).summarize(_document(), job_id=job.id, artifacts=artifacts)

    assert result.relevance_score == 4
    assert len(payloads) == 3
    assert "not-json" not in json.dumps(payloads[2], ensure_ascii=False)
    assert store.total_cost(job.id) == pytest.approx(0.03)


def test_billable_malformed_envelope_is_recorded_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            malformed = _completion(
                _extraction(), model=payload["model"], provider="provider-a", cost=0.01,
            )
            malformed.pop("choices")
            return httpx.Response(200, json=malformed)
        content = _extraction() if calls == 2 else _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="provider-a", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert result.relevance_score == 4
    assert store.total_cost(job.id) == pytest.approx(0.03)
    with sqlite3.connect(store.path) as connection:
        first = connection.execute(
            "SELECT generation_id, provider, validated, cost_resolved FROM llm_calls ORDER BY id LIMIT 1"
        ).fetchone()
    assert first == ("generation-id", "provider-a", 0, 1)


def test_missing_cost_is_persisted_as_unresolved_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, UnresolvedUsageError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        response = _completion(_extraction(), model=payload["model"], provider="p", cost=0.01)
        response["usage"].pop("cost")  # type: ignore[union-attr]
        return httpx.Response(200, json=response)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    with pytest.raises(UnresolvedUsageError, match="cost"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert calls == 1
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT generation_id, validated, cost_resolved FROM llm_calls"
        ).fetchone()
    assert row == ("generation-id", 0, 0)


def test_price_change_does_not_bypass_unresolved_logical_request_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, UnresolvedUsageError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def unresolved(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        response = _completion(_extraction(), model=payload["model"], provider="p", cost=0.01)
        response["usage"].pop("cost")  # type: ignore[union-attr]
        return httpx.Response(200, json=response)

    catalog = _pricing()
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    artifacts = ArtifactManager(tmp_path / "out", "paper")
    first = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(unresolved)),
        catalog=catalog,
    )
    with pytest.raises(UnresolvedUsageError):
        first.summarize(_document(), job_id=job.id, artifacts=artifacts)

    catalog.prices["google/gemini-3.8-flash"] = ("1.25", "5.00")
    resumed = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no resend"))),
        catalog=catalog,
    )
    with pytest.raises(UnresolvedUsageError):
        resumed.summarize(_document(), job_id=job.id, artifacts=artifacts)

    assert calls == 1


def test_summarizer_stops_after_bounded_malformed_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, StructuredOutputError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        response = _completion("bad", model=payload["model"], provider="p", cost=0.01)
        response["choices"][0]["message"]["content"] = "bad"  # type: ignore[index]
        return httpx.Response(200, json=response)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    with pytest.raises(StructuredOutputError, match="2 attempts"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )
    assert calls == 2
    assert store.total_cost(job.id) == pytest.approx(0.02)


def test_summarizer_budget_failure_makes_no_http_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import BudgetExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("budget rejection must happen before HTTP")

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    settings = _settings(paper_budget_usd=0.001)
    summarizer = OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(unexpected)),
        catalog=_pricing(),
    )

    with pytest.raises(BudgetExceededError):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )


def test_budget_aggregates_cost_across_jobs_for_the_same_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import BudgetExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec, PaperMetadata

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    store = JobStore(tmp_path / "state.sqlite3")
    paper_id = store.upsert_paper(PaperMetadata(doi="10.1/shared"), "a" * 64)
    first = store.create_job(InputSpec(InputKind.DOI, "10.1/shared"), paper_id=paper_id)
    second = store.create_job(InputSpec(InputKind.DOI, "10.1/shared"), paper_id=paper_id)
    store.record_llm_call(
        first.id,
        request_hash="prior-charge",
        cache_key="different-request",
        model="prior/model",
        provider="p",
        response={"ok": True},
        usage={"cost": 0.49},
        cost_usd=0.49,
        validated=True,
    )
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(paper_budget_usd=0.50),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))),
        catalog=_pricing(),
    )

    with pytest.raises(BudgetExceededError):
        summarizer.summarize(
            _document(), job_id=second.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert store.paper_total_cost(second.id) == pytest.approx(0.49)


def test_concurrent_jobs_atomically_reserve_one_shared_paper_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import BudgetExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec, PaperMetadata

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    barrier = threading.Barrier(2)
    calls: list[str] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        with lock:
            calls.append(payload["model"])
        time.sleep(0.10)
        content = _extraction() if payload["model"] == "google/gemini-3.8-flash" else _summary()
        cost = 0.001 if payload["model"] == "google/gemini-3.8-flash" else 0.01
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=cost),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    paper_id = store.upsert_paper(PaperMetadata(doi="10.1/concurrent"), "b" * 64)
    jobs = [
        store.create_job(InputSpec(InputKind.DOI, "10.1/concurrent"), paper_id=paper_id)
        for _ in range(2)
    ]
    settings = _settings(paper_budget_usd=0.025, max_validation_retries=0)
    summarizer = OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    def run(index: int):
        barrier.wait()
        return summarizer.summarize(
            _document(),
            job_id=jobs[index].id,
            artifacts=ArtifactManager(tmp_path / "out", f"paper-{index}"),
            research_interest=f"interest-{index}",
        )

    outcomes: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, index) for index in range(2)]
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as error:  # asserted by exact type below
                outcomes.append(error)

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, BudgetExceededError) for item in outcomes) == 1
    assert store.paper_total_cost(jobs[0].id) == pytest.approx(0.011)
    assert calls.count("google/gemini-3.8-flash") == 1
    assert calls.count("openai/gpt-5.6-sol") == 1


def test_serialized_map_request_must_fit_configured_input_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import InputLimitExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(extraction_max_input_tokens=100),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))),
        catalog=_pricing(),
    )

    with pytest.raises(InputLimitExceededError, match="extraction"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )


def test_many_map_results_are_hierarchically_reduced_before_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, estimate_serialized_tokens
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        content = _summary() if payload["model"] == "openai/gpt-5.6-sol" else _extraction()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.001),
        )

    document = _document()
    document["paragraphs"] = [
        {
            "text": "This paper evaluates a compact method.",
            "page": 1,
            "section_id": "intro",
            "source_position": {"page": 1, "line": index + 1},
        }
        for index in range(20)
    ]
    settings = _settings(
        chunk_max_chars=50,
        extraction_max_input_tokens=6_000,
        synthesis_max_input_tokens=4_000,
        max_validation_retries=0,
    )
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        document, job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    extraction_requests = [p for p in payloads if p["model"] == "google/gemini-3.8-flash"]
    synthesis_requests = [p for p in payloads if p["model"] == "openai/gpt-5.6-sol"]
    assert result.relevance_score == 4
    assert len(extraction_requests) > 20
    assert len(synthesis_requests) == 1
    assert estimate_serialized_tokens(synthesis_requests[0]) <= settings.synthesis_max_input_tokens


def test_reductions_cannot_consume_reserved_final_synthesis_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import BudgetExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        synthesis = payload["model"] == "openai/gpt-5.6-sol"
        return httpx.Response(
            200,
            json=_completion(
                _summary() if synthesis else _extraction(),
                model=payload["model"],
                provider="p",
                cost=0.012 if synthesis else 0.005,
            ),
        )

    document = _document()
    document["paragraphs"] = [
        {
            "text": "This paper evaluates a compact method.",
            "page": 1,
            "section_id": "intro",
            "source_position": {"page": 1, "line": index + 1},
        }
        for index in range(20)
    ]
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(
            paper_budget_usd=0.12,
            chunk_max_chars=50,
            extraction_max_input_tokens=6_000,
            synthesis_max_input_tokens=4_000,
            max_validation_retries=0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    with pytest.raises(BudgetExceededError):
        summarizer.summarize(
            document, job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert calls.count("google/gemini-3.8-flash") == 21
    assert "openai/gpt-5.6-sol" not in calls
    assert store.paper_total_cost(job.id) <= 0.12


def test_summarizer_fails_closed_when_configured_model_pricing_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, PricingUnavailableError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(extraction_model="unknown/model"),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))),
        catalog=_pricing(),
    )

    with pytest.raises(PricingUnavailableError, match="unknown/model"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )


def test_summarizer_reports_api_failure_without_response_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterAPIError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(503, text="upstream included secret-value")
    )
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store, settings=_settings(), client=httpx.Client(transport=transport), catalog=_pricing(),
    )

    with pytest.raises(OpenRouterAPIError) as error:
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )
    assert "503" in str(error.value)
    assert "secret-value" not in str(error.value)
    assert store.total_cost(job.id) == 0


def test_summarizer_revalidates_mutated_endpoint_before_catalog_or_http(tmp_path: Path) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    settings = _settings()
    settings.endpoint = "https://attacker.invalid/collect"
    catalog = _FakeCatalog()
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))),
        catalog=catalog,
    )

    with pytest.raises(ValueError, match="endpoint"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert catalog.requests == []


def test_summarizer_repairs_summary_with_evidence_outside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = _extraction() if calls == 1 else _summary()
        if calls == 2:
            content = dict(content)
            content["evidence"] = [{"page": 99, "section": "Invented", "quote": "存在しない根拠"}]
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert calls == 3
    assert result.evidence[0].page == 1


def test_summarizer_repairs_fabricated_evidence_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = _extraction() if calls == 1 else _summary()
        if calls == 2:
            content = dict(content)
            content["evidence"] = [{
                "page": 1, "section": "Introduction", "quote": "fabricated evidence",
            }]
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert calls == 3
    assert result.evidence[0].quote == "evaluates a compact method"


def test_summarizer_repairs_whitespace_only_evidence_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = _extraction() if calls == 1 else _summary()
        if calls == 2:
            content = dict(content)
            content["evidence"] = [{
                "page": 1, "section": "Introduction", "quote": " \t\n ",
            }]
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert calls == 3
    assert result.evidence[0].quote == "evaluates a compact method"


def test_summarizer_rejects_mismatched_real_page_and_section_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    document = {
        "sections": [
            {"id": "intro", "title": "Introduction", "page": 1},
            {"id": "results", "title": "Results", "page": 2},
        ],
        "paragraphs": [
            {"text": "Introduction source quote.", "page": 1, "section_id": "intro"},
            {"text": "Results source quote.", "page": 2, "section_id": "results"},
        ],
        "tables": [], "equations": [], "captions": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if payload["model"] == "google/gemini-3.8-flash":
            chunk = json.loads(payload["messages"][1]["content"])["chunk"]
            content = {
                "summary": "検証済みの抽出結果。",
                "datasets": [], "metrics": [], "keywords": [],
                "evidence": [{
                    "page": chunk["start_page"],
                    "section": chunk["section"],
                    "quote": chunk["text"],
                }],
            }
        else:
            content = _summary()
            if calls == 3:
                content = dict(content)
                content["evidence"] = [{
                    "page": 1, "section": "Results", "quote": "Results source quote.",
                }]
            else:
                content["evidence"] = [{
                    "page": 1, "section": "Introduction", "quote": "Introduction source quote.",
                }]
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        document, job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert calls == 4
    assert result.evidence[0].section == "Introduction"


def test_resume_does_not_repeat_exhausted_malformed_paid_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, StructuredOutputError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def malformed(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        response = _completion("bad", model=payload["model"], provider="p", cost=0.01)
        response["choices"][0]["message"]["content"] = "bad"  # type: ignore[index]
        return httpx.Response(200, json=response)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    artifacts = ArtifactManager(tmp_path / "out", "paper")
    first = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(malformed)),
        catalog=_pricing(),
    )
    with pytest.raises(StructuredOutputError):
        first.summarize(_document(), job_id=job.id, artifacts=artifacts)

    second = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no repeated HTTP"))),
        catalog=_pricing(),
    )
    with pytest.raises(StructuredOutputError, match="already exhausted"):
        second.summarize(_document(), job_id=job.id, artifacts=artifacts)

    assert calls == 2
    assert store.total_cost(job.id) == pytest.approx(0.02)


def test_summarizer_reads_task_four_artifact_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = _extraction() if calls == 1 else _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    artifacts = ArtifactManager(tmp_path / "out", "paper")
    artifacts.write_document(_document())
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    result = summarizer.summarize_artifacts(artifacts.bundle, job_id=job.id)

    assert result.relevance_score == 4
    assert artifacts.bundle.summary_json.exists()


def test_concurrent_identical_requests_share_one_paid_dispatch_per_cache_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        with lock:
            calls.append(payload["model"])
        time.sleep(0.10)
        content = _extraction() if payload["model"] == "google/gemini-3.8-flash" else _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )
    artifacts = ArtifactManager(tmp_path / "out", "paper")

    def run():
        barrier.wait()
        return summarizer.summarize(_document(), job_id=job.id, artifacts=artifacts)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))

    assert [result.relevance_score for result in results] == [4, 4]
    assert calls == ["google/gemini-3.8-flash", "openai/gpt-5.6-sol"]
    assert store.total_cost(job.id) == pytest.approx(0.02)


def test_request_claim_outlives_former_fixed_lease_during_bounded_retry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    fake_now = [1_000.0]
    monkeypatch.setattr("src.openrouter.time.time", lambda: fake_now[0])
    first_started = threading.Event()
    release_first = threading.Event()
    duplicate_dispatch = threading.Event()
    lock = threading.Lock()
    extraction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal extraction_calls
        payload = json.loads(request.content)
        if payload["model"] == "google/gemini-3.8-flash":
            with lock:
                extraction_calls += 1
                call_number = extraction_calls
            if call_number == 1:
                first_started.set()
                assert release_first.wait(2)
            else:
                duplicate_dispatch.set()
            content = _extraction()
        else:
            content = _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    second_claim_attempted = threading.Event()
    claim_calls = 0
    original_claim = store.claim_llm_request

    def tracked_claim(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls >= 2:
            second_claim_attempted.set()
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim_llm_request", tracked_claim)
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(request_timeout_seconds=200, max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    def run(name: str):
        return summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", name),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, "first")
        assert first_started.wait(1)
        fake_now[0] += 301
        second = executor.submit(run, "second")
        assert second_claim_attempted.wait(1)
        duplicate_before_completion = duplicate_dispatch.wait(0.25)
        release_first.set()
        assert first.result().relevance_score == 4
        assert second.result().relevance_score == 4

    assert not duplicate_before_completion
    assert extraction_calls == 1


def test_request_claim_heartbeat_prevents_takeover_after_original_lease_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    fake_now = [1_000.0]
    monkeypatch.setattr("src.openrouter.time.time", lambda: fake_now[0])
    first_started = threading.Event()
    release_first = threading.Event()
    duplicate_dispatch = threading.Event()
    lock = threading.Lock()
    extraction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal extraction_calls
        payload = json.loads(request.content)
        if payload["model"] == "google/gemini-3.8-flash":
            with lock:
                extraction_calls += 1
                call_number = extraction_calls
            if call_number == 1:
                first_started.set()
                assert release_first.wait(2)
            else:
                duplicate_dispatch.set()
            content = _extraction()
        else:
            content = _summary()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.01),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(request_timeout_seconds=0.01, max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )
    summarizer._claim_heartbeat_interval_seconds = 0.01

    def run(name: str):
        return summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", name),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, "first")
        assert first_started.wait(1)
        fake_now[0] += 20
        time.sleep(0.05)
        fake_now[0] += 20
        time.sleep(0.05)
        second = executor.submit(run, "second")
        duplicate_before_completion = duplicate_dispatch.wait(0.25)
        release_first.set()
        assert first.result().relevance_score == 4
        assert second.result().relevance_score == 4

    assert not duplicate_before_completion
    assert extraction_calls == 1


def test_failed_budget_settlement_rolls_back_llm_charge_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json=_completion(
                _extraction(), model=payload["model"], provider="p", cost=0.001,
            ),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_small_reservation_settle
               BEFORE DELETE ON llm_budget_reservations
               WHEN OLD.amount_usd < 0.01
               BEGIN SELECT RAISE(ABORT, 'simulated settlement crash'); END"""
        )
        connection.execute(
            """CREATE TRIGGER fail_small_reservation_update
               BEFORE UPDATE OF amount_usd ON llm_budget_reservations
               WHEN OLD.amount_usd < 0.01
               BEGIN SELECT RAISE(ABORT, 'simulated settlement crash'); END"""
        )
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated settlement crash"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    with sqlite3.connect(store.path) as connection:
        recorded_attempts = connection.execute(
            "SELECT COUNT(*) FROM llm_calls"
        ).fetchone()[0]
    assert recorded_attempts == 0
    assert store.total_cost(job.id) == 0
