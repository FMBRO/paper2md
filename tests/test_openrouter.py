from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3

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

    chunks = build_document_chunks(document, max_chars=30)

    assert [chunk.section for chunk in chunks] == ["Introduction", "Results", "Results", "Results", "Results"]
    assert chunks[0].text == "Background paragraph."
    assert chunks[2].text == table
    assert chunks[2].kind == "table"
    assert chunks[2].start_page == chunks[2].end_page == 2
    assert chunks[2].evidence[0].section_id == "results"
    assert chunks[3].text == "E = mc^2"
    assert chunks[4].text == "Figure 1. Overview"


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
    from src.openrouter import PaperSummaryResponse, build_structured_payload

    payload = build_structured_payload(
        settings=OpenRouterSettings(),
        model="google/gemini-3.8-flash",
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
    from src.openrouter import PaperSummaryResponse, PrivacyRequirementsError, build_structured_payload

    with pytest.raises(PrivacyRequirementsError):
        build_structured_payload(
            settings=OpenRouterSettings(**settings_kwargs),
            model="example/model",
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
        "extraction_max_input_tokens": 1000,
        "extraction_max_output_tokens": 200,
        "synthesis_max_input_tokens": 1000,
        "synthesis_max_output_tokens": 500,
        "chunk_max_chars": 1000,
        "max_validation_retries": 1,
    }
    return OpenRouterSettings(**(defaults | overrides))


def _pricing():
    from src.openrouter import ModelPricing

    return {
        "google/gemini-3.8-flash": ModelPricing(Decimal("0.75"), Decimal("3.75")),
        "openai/gpt-5.6-sol": ModelPricing(Decimal("2.00"), Decimal("10.00")),
    }


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
    summarizer = OpenRouterSummarizer(store, settings=_settings(), client=client, pricing=_pricing())

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
            "SELECT provider, usage_json, validated FROM llm_calls ORDER BY id"
        ).fetchall()
    assert rows[0][0] == "provider-a"
    assert json.loads(rows[0][1])["total_tokens"] == 120
    assert [row[2] for row in rows] == [1, 1]
    assert b"top-secret-key" not in store.path.read_bytes()
    saved = json.loads(artifacts.bundle.summary_json.read_text(encoding="utf-8"))
    assert saved["relevance_score"] == 4
    assert saved["evidence"][0]["page"] == 1


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
        store, settings=_settings(), client=client, pricing=_pricing(),
    ).summarize(_document(), job_id=job.id, artifacts=artifacts)

    assert result.relevance_score == 4
    assert len(payloads) == 3
    assert "not-json" not in json.dumps(payloads[2], ensure_ascii=False)
    assert store.total_cost(job.id) == pytest.approx(0.03)


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
        pricing=_pricing(),
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
        pricing=_pricing(),
    )

    with pytest.raises(BudgetExceededError):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )


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
        pricing=_pricing(),
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
        store, settings=_settings(), client=httpx.Client(transport=transport), pricing=_pricing(),
    )

    with pytest.raises(OpenRouterAPIError) as error:
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )
    assert "503" in str(error.value)
    assert "secret-value" not in str(error.value)
    assert store.total_cost(job.id) == 0


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
        pricing=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert calls == 3
    assert result.evidence[0].page == 1


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
        pricing=_pricing(),
    )
    with pytest.raises(StructuredOutputError):
        first.summarize(_document(), job_id=job.id, artifacts=artifacts)

    second = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=1),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no repeated HTTP"))),
        pricing=_pricing(),
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
        pricing=_pricing(),
    )

    result = summarizer.summarize_artifacts(artifacts.bundle, job_id=job.id)

    assert result.relevance_score == 4
    assert artifacts.bundle.summary_json.exists()
