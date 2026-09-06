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


def test_section_chunks_do_not_combine_evidence_across_page_boundaries() -> None:
    from src.openrouter import build_document_chunks

    document = {
        "sections": [{"id": "methods", "title": "Methods", "page": 3}],
        "paragraphs": [
            {"text": "We use the multipole dif-", "page": 3, "section_id": "methods"},
            {"text": "fusion model for rendering.", "page": 4, "section_id": "methods"},
        ],
        "tables": [],
        "equations": [],
        "captions": [],
    }

    chunks = build_document_chunks(document, max_chars=100)

    assert [(chunk.start_page, chunk.end_page, chunk.text) for chunk in chunks] == [
        (3, 3, "We use the multipole dif-"),
        (4, 4, "fusion model for rendering."),
    ]


def test_chunk_builder_uses_renderer_ordinal_across_mixed_block_types() -> None:
    from src.openrouter import build_document_chunks

    document = {
        "sections": [{"id": "results", "title": "Results", "page": 1}],
        "paragraphs": [
            {"text": "third", "page": 1, "section_id": "results", "ordinal": 30,
             "source_position": {"page": 1, "bbox": [10, 10, 20, 20]}},
            {"text": "first", "page": 1, "section_id": "results", "ordinal": 10,
             "source_position": {"page": 1, "bbox": [300, 300, 400, 320]}},
        ],
        "tables": [{
            "markdown": "<table><tr><td>second</td></tr></table>",
            "page": 1, "section_id": "results", "ordinal": 20,
            "source_position": {"page": 1, "bbox": [500, 5, 550, 30]},
        }],
        "equations": [],
        "captions": [],
    }

    chunks = build_document_chunks(document, max_chars=100)

    assert [chunk.text for chunk in chunks] == [
        "first", "<table><tr><td>second</td></tr></table>", "third",
    ]


def test_chunk_builder_falls_back_to_page_y_x_when_ordinal_is_absent() -> None:
    from src.openrouter import build_document_chunks

    document = {
        "sections": [],
        "paragraphs": [
            {"text": "lower-left", "page": 1,
             "source_position": {"page": 1, "bbox": [20, 200, 200, 240]}},
            {"text": "upper-right", "page": 1,
             "source_position": {"page": 1, "bbox": [320, 50, 560, 90]}},
        ],
        "tables": [], "equations": [], "captions": [],
    }

    chunks = build_document_chunks(document, max_chars=12)

    assert [chunk.text for chunk in chunks] == ["upper-right", "lower-left"]


def test_chunk_builder_splits_oversized_paragraphs_only_at_stable_boundaries() -> None:
    from src.openrouter import build_document_chunks

    source_text = "First sentence. Second sentence.\nThird words here."
    document = {
        "sections": [{"id": "intro", "title": "Introduction", "page": 3}],
        "paragraphs": [{
            "text": source_text,
            "page": 3,
            "section_id": "intro",
            "ordinal": 7,
            "source_position": {"page": 3, "bbox": [10, 20, 300, 80]},
        }],
        "tables": [], "equations": [], "captions": [],
    }

    chunks = build_document_chunks(document, max_chars=20)

    assert [chunk.text for chunk in chunks] == [
        "First sentence.", "Second sentence.", "Third words here.",
    ]
    assert all(chunk.start_page == chunk.end_page == 3 for chunk in chunks)
    assert [chunk.evidence[0].source_text for chunk in chunks] == [
        "First sentence.", "Second sentence.", "Third words here.",
    ]
    assert [chunk.evidence[0].source_position["fragment"] for chunk in chunks] == [
        1, 2, 3,
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


def test_openai_gpt5_payload_uses_only_supported_sampling_parameters() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import PaperSummaryResponse, PricingSnapshot, build_structured_payload

    payload = build_structured_payload(
        settings=OpenRouterSettings(),
        model="openai/gpt-5.6-sol",
        pricing=PricingSnapshot(
            model="openai/gpt-5.6-sol",
            input_per_million=Decimal("1.00"),
            output_per_million=Decimal("4.00"),
            version="catalog-v1",
        ),
        messages=[{"role": "user", "content": "summarize"}],
        response_model=PaperSummaryResponse,
        max_output_tokens=2_000,
    )

    assert payload["max_completion_tokens"] == 2_000
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_luna_extraction_payload_uses_strict_schema_and_completion_tokens() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import ChunkExtractionResponse, PricingSnapshot, build_structured_payload

    payload = build_structured_payload(
        settings=OpenRouterSettings(),
        model="openai/gpt-5.6-luna",
        pricing=PricingSnapshot(
            model="openai/gpt-5.6-luna",
            input_per_million=Decimal("0.20"),
            output_per_million=Decimal("1.20"),
            version="catalog-v1",
        ),
        messages=[{"role": "user", "content": "extract"}],
        response_model=ChunkExtractionResponse,
        max_output_tokens=4_000,
    )

    assert payload["model"] == "openai/gpt-5.6-luna"
    assert payload["max_completion_tokens"] == 4_000
    assert "max_tokens" not in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_model_catalog_prices_only_zdr_structured_output_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.openrouter import OpenRouterModelCatalog

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/endpoints/zdr"
        return httpx.Response(200, json={"data": [
            {
                "model_id": "openai/gpt-5.6-sol",
                "provider_name": "Azure",
                "supported_parameters": [
                    "max_completion_tokens", "response_format", "structured_outputs",
                ],
                "pricing": {"prompt": "0.000005", "completion": "0.000030"},
            },
            {
                "model_id": "openai/gpt-5.6-sol",
                "provider_name": "Azure",
                "supported_parameters": [
                    "max_completion_tokens", "response_format", "structured_outputs",
                ],
                "pricing": {"prompt": "0.0000055", "completion": "0.000033"},
            },
            {
                "model_id": "openai/gpt-5.6-sol",
                "provider_name": "Other",
                "supported_parameters": ["max_tokens"],
                "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            },
        ]}, headers={"etag": "zdr-v1"})

    pricing = OpenRouterModelCatalog(
        httpx.Client(transport=httpx.MockTransport(handler)),
    ).pricing_for("openai/gpt-5.6-sol")

    assert pricing.input_per_million == Decimal("5.5")
    assert pricing.output_per_million == Decimal("33")
    assert pricing.version == "zdr-v1"


def test_synthesis_prompt_requires_verbatim_untranslated_evidence() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import ChunkExtractionResponse, OpenRouterSummarizer, PricingSnapshot

    summarizer = object.__new__(OpenRouterSummarizer)
    summarizer.settings = OpenRouterSettings()
    payload = summarizer._synthesis_payload(
        [ChunkExtractionResponse.model_validate(_extraction())],
        "",
        PricingSnapshot(
            model=summarizer.settings.synthesis_model,
            input_per_million=Decimal("5.5"),
            output_per_million=Decimal("33"),
            version="zdr-v1",
        ),
    )

    instruction = payload["messages"][0]["content"]
    assert "原言語" in instruction
    assert "一字も変更せず" in instruction
    assert "翻訳" in instruction
    assert "最大8件" in instruction
    assert "短い引用" in instruction


def test_reduction_prompt_states_structured_size_and_evidence_limits() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import ChunkExtractionResponse, OpenRouterSummarizer, PricingSnapshot

    summarizer = object.__new__(OpenRouterSummarizer)
    summarizer.settings = OpenRouterSettings()
    payload = summarizer._reduction_payload(
        [ChunkExtractionResponse.model_validate(_extraction())],
        "",
        PricingSnapshot(
            model=summarizer.settings.extraction_model,
            input_per_million=Decimal("0.30"),
            output_per_million=Decimal("2.50"),
            version="catalog-v1",
        ),
    )

    instruction = payload["messages"][0]["content"]
    assert "summaryは2500字以内" in instruction
    assert "evidenceは最大8件" in instruction
    assert "原文を一字も変更せず" in instruction
    assert "page・section・quoteの3項目" in instruction
    assert "同じevidenceオブジェクトから一字も変更せずコピー" in instruction
    assert "新規作成は禁止" in instruction


def test_map_prompt_requires_verbatim_evidence_with_4000_character_limit() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import (
        ChunkEvidence,
        DocumentChunk,
        OpenRouterSummarizer,
        PricingSnapshot,
    )

    summarizer = object.__new__(OpenRouterSummarizer)
    summarizer.settings = OpenRouterSettings()
    chunk = DocumentChunk(
        text="Source sentence.",
        kind="paragraph",
        section="Introduction",
        start_page=1,
        end_page=1,
        evidence=(ChunkEvidence("paragraph", 1, "intro", "Source sentence."),),
    )
    payload = summarizer._map_payload(
        chunk,
        "",
        PricingSnapshot(
            model=summarizer.settings.extraction_model,
            input_per_million=Decimal("0.30"),
            output_per_million=Decimal("2.50"),
            version="catalog-v1",
        ),
    )

    instruction = payload["messages"][0]["content"]
    assert "原文を一字も変えず" in instruction
    assert "4000字以内" in instruction
    assert "chunk.textから連続する完全一致部分" in instruction
    assert "HTML/Markdown" in instruction
    assert "短い引用" in instruction
    assert "単一の原文スパン" in instruction
    assert "原則200文字以内" in instruction
    assert "数式を避け" in instruction
    assert "summaryは必ず日本語" in instruction
    assert "英語のみのsummaryは禁止" in instruction
    assert "固有名詞の列挙だけで終えず" in instruction
    assert "日本語の内容説明を十分に" in instruction
    assert "evidence.sectionにはchunk.sectionを一字も変えずコピー" in instruction
    assert "chunk.sectionは位置ラベル" in instruction
    assert "quoteとして使わない" in instruction
    assert "最良の1件だけ" in instruction
    assert "section_id" not in payload["messages"][1]["content"]


def test_equation_map_prompt_requires_a_short_verbatim_fragment() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import (
        ChunkEvidence,
        DocumentChunk,
        OpenRouterSummarizer,
        PricingSnapshot,
    )

    summarizer = object.__new__(OpenRouterSummarizer)
    summarizer.settings = OpenRouterSettings()
    formula = r"M(\xi|\phi) = \iint \mu(\mathbf{u}, \phi) d\mathbf{u}"
    chunk = DocumentChunk(
        text=formula,
        kind="equation",
        section="Error Covariance",
        start_page=5,
        end_page=5,
        evidence=(ChunkEvidence("equation", 5, "covariance", formula),),
    )

    payload = summarizer._map_payload(
        chunk,
        "",
        PricingSnapshot(
            model=summarizer.settings.extraction_model,
            input_per_million=Decimal("0.30"),
            output_per_million=Decimal("2.50"),
            version="catalog-v1",
        ),
    )

    instruction = payload["messages"][0]["content"]
    assert "chunk.kindがequation" in instruction
    assert "20〜80文字" in instruction
    assert "数式全体ではなく" in instruction
    assert "連続部分を原文から一字も変えずコピー" in instruction


def test_chunk_extraction_schema_bounds_response_size() -> None:
    from src.config import OpenRouterSettings
    from src.openrouter import ChunkExtractionResponse, PricingSnapshot, build_structured_payload

    payload = build_structured_payload(
        settings=OpenRouterSettings(),
        model="google/gemini-2.5-flash",
        pricing=PricingSnapshot(
            model="google/gemini-2.5-flash",
            input_per_million=Decimal("0.30"),
            output_per_million=Decimal("2.50"),
            version="catalog-v1",
        ),
        messages=[{"role": "user", "content": "extract"}],
        response_model=ChunkExtractionResponse,
        max_output_tokens=1_000,
    )

    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["summary"]["maxLength"] == 2_500
    assert schema["properties"]["datasets"]["maxItems"] == 10
    assert schema["properties"]["datasets"]["items"]["maxLength"] == 200
    assert schema["properties"]["metrics"]["maxItems"] == 20
    assert schema["properties"]["keywords"]["maxItems"] == 20
    assert schema["properties"]["evidence"]["maxItems"] == 8
    evidence = schema["$defs"]["EvidenceResponse"]["properties"]
    assert evidence["section"]["maxLength"] == 200
    assert evidence["quote"]["maxLength"] == 4_000


def test_cache_key_includes_structured_validation_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.openrouter as openrouter

    summarizer = object.__new__(openrouter.OpenRouterSummarizer)
    payload = {
        "model": "example/model",
        "messages": [{"role": "user", "content": "extract"}],
        "max_tokens": 100,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_schema"},
    }
    original = summarizer._cache_key(payload)
    original_dispatch = summarizer._dispatch_hash(payload, original)

    monkeypatch.setattr(openrouter, "STRUCTURED_VALIDATION_VERSION", "next")

    updated = summarizer._cache_key(payload)
    assert updated != original
    assert summarizer._dispatch_hash(payload, updated) != original_dispatch


def test_chunk_extraction_accepts_detailed_bounded_summary_and_evidence() -> None:
    from src.openrouter import ChunkExtractionResponse

    response = {
        "summary": "要" * 1_932,
        "datasets": [],
        "metrics": [],
        "keywords": ["hyperspectral imaging"],
        "evidence": [
            {
                "page": 1,
                "section": "Introduction",
                "quote": "e" * 3_500,
            }
        ],
    }

    validated = ChunkExtractionResponse.model_validate(response)

    assert len(validated.summary) == 1_932
    assert len(validated.evidence[0].quote) == 3_500


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


def test_final_summary_schema_limits_evidence_to_eight_items() -> None:
    from pydantic import ValidationError

    from src.openrouter import PaperSummaryResponse

    payload = _summary()
    payload["evidence"] = payload["evidence"] * 9

    with pytest.raises(ValidationError, match="evidence"):
        PaperSummaryResponse.model_validate(payload)

    payload["relevance_score"] = 4
    payload["background"] = "English only"
    with pytest.raises(ValidationError):
        PaperSummaryResponse.model_validate(payload)


def test_openrouter_rejects_mostly_english_narrative_with_one_japanese_character() -> None:
    from pydantic import ValidationError

    from src.openrouter import PaperSummaryResponse

    payload = _summary()
    payload["background"] = (
        "This is an otherwise entirely English background with many ordinary words 結"
    )

    with pytest.raises(ValidationError, match="Japanese"):
        PaperSummaryResponse.model_validate(payload)


def test_openrouter_accepts_normal_japanese_narrative_with_english_terms() -> None:
    from src.openrouter import PaperSummaryResponse

    payload = _summary()
    payload["background"] = (
        "本研究ではTransformerとOpenAI APIを用いて検索精度を改善する。"
    )

    assert PaperSummaryResponse.model_validate(payload).background == payload["background"]


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


def test_standard_openrouter_response_resolves_provider_from_generation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    fixture_dir = Path(__file__).parent / "fixtures" / "openrouter"
    completion_fixture = json.loads(
        (fixture_dir / "chat_completion_standard.json").read_text(encoding="utf-8")
    )
    generation_fixture = json.loads(
        (fixture_dir / "generation_metadata.json").read_text(encoding="utf-8")
    )
    posted: list[httpx.Request] = []
    generation_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request)
            request_payload = json.loads(request.content)
            response_payload = json.loads(json.dumps(completion_fixture))
            response_payload["id"] = f"generation-{len(posted)}"
            response_payload["model"] = request_payload["model"]
            response_payload["choices"][0]["message"]["content"] = json.dumps(
                _extraction() if len(posted) == 1 else _summary(), ensure_ascii=False,
            )
            return httpx.Response(200, json=response_payload)
        assert request.url.path == "/api/v1/generation"
        generation_id = request.url.params["id"]
        generation_ids.append(generation_id)
        metadata = json.loads(json.dumps(generation_fixture))
        metadata["data"]["id"] = generation_id
        return httpx.Response(200, json=metadata)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    result = summarizer.summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert result.relevance_score == 4
    assert generation_ids == ["generation-1", "generation-2"]
    assert all(request.headers["x-openrouter-metadata"] == "enabled" for request in posted)
    with sqlite3.connect(store.path) as connection:
        providers = [row[0] for row in connection.execute(
            "SELECT provider FROM llm_calls ORDER BY id"
        )]
    assert providers == ["Google", "Google"]


def test_generation_lookup_prefers_openrouter_response_header_over_upstream_body_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    looked_up: list[str] = []
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            looked_up.append(request.url.params["id"])
            return httpx.Response(200, json={"data": {"provider_name": "Azure"}})
        post_calls += 1
        payload = json.loads(request.content)
        content = _extraction() if post_calls == 1 else _summary()
        response = _completion(
            content, model=payload["model"], provider="", cost=0.01,
        )
        response["id"] = f"chatcmpl-upstream-{post_calls}"
        return httpx.Response(
            200,
            headers={"X-Generation-Id": f"gen-openrouter-{post_calls}"},
            json=response,
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
    assert looked_up == ["gen-openrouter-1", "gen-openrouter-2"]
    with sqlite3.connect(store.path) as connection:
        generation_ids = [row[0] for row in connection.execute(
            "SELECT generation_id FROM llm_calls ORDER BY id"
        )]
    assert generation_ids == ["gen-openrouter-1", "gen-openrouter-2"]


def test_generation_provider_lookup_retries_eventual_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer

    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(404, json={"error": "generation not indexed yet"})
        return httpx.Response(200, json={"data": {"provider_name": "Azure"}})

    monkeypatch.setattr("src.openrouter.time.sleep", delays.append)
    summarizer = OpenRouterSummarizer(
        JobStore(tmp_path / "state.sqlite3"),
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    provider = summarizer._provider_from_generation("generation-1", api_key="secret")

    assert provider == "Azure"
    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_unavailable_provider_metadata_settles_known_cost_before_rejecting_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, StructuredOutputError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr("src.openrouter.time.sleep", lambda _delay: None)
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            return httpx.Response(404, json={"error": "generation unavailable"})
        post_calls += 1
        payload = json.loads(request.content)
        response = _completion(
            _extraction(), model=payload["model"], provider="", cost=0.01,
        )
        return httpx.Response(200, json=response)

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    )

    with pytest.raises(StructuredOutputError):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert post_calls == 1
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT generation_id, provider, cost_usd, cost_resolved, validated, "
            "dispatch_state FROM llm_calls"
        ).fetchone()
    assert row == ("generation-id", None, 0.01, 1, 0, "settled")


def test_http_200_rate_limit_envelope_is_settled_without_cost_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr("src.openrouter.time.sleep", lambda _delay: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"X-Generation-Id": "gen-rate-limited"},
                json={
                    "id": "gen-rate-limited",
                    "error": {"code": 429, "message": "temporarily rate-limited"},
                },
            )
        payload = json.loads(request.content)
        content = _extraction() if calls == 2 else _summary()
        return httpx.Response(
            200,
            json=_completion(
                content, model=payload["model"], provider="Azure", cost=0.01,
            ),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=2),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert result.relevance_score == 4
    assert store.total_cost(job.id) == pytest.approx(0.02)
    with sqlite3.connect(store.path) as connection:
        first = connection.execute(
            "SELECT generation_id, cost_usd, cost_resolved, validated, dispatch_state "
            "FROM llm_calls ORDER BY id LIMIT 1"
        ).fetchone()
    assert first == ("gen-rate-limited", 0.0, 1, 0, "settled")


def test_http_200_string_rate_limit_code_is_settled_without_provider_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr("src.openrouter.time.sleep", lambda _delay: None)
    calls = 0
    generation_lookups = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, generation_lookups
        if request.method == "GET":
            generation_lookups += 1
            return httpx.Response(404, json={"error": "generation unavailable"})
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"X-Generation-Id": "gen-string-rate-limit"},
                json={
                    "id": "gen-string-rate-limit",
                    "error": {"code": "429", "message": "temporarily rate-limited"},
                },
            )
        payload = json.loads(request.content)
        content = _extraction() if calls == 2 else _summary()
        return httpx.Response(
            200,
            json=_completion(
                content, model=payload["model"], provider="Azure", cost=0.01,
            ),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=2),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert result.relevance_score == 4
    assert generation_lookups == 0
    with sqlite3.connect(store.path) as connection:
        first = connection.execute(
            "SELECT generation_id, cost_usd, cost_resolved, validated, dispatch_state "
            "FROM llm_calls ORDER BY id LIMIT 1"
        ).fetchone()
    assert first == ("gen-string-rate-limit", 0.0, 1, 0, "settled")


def test_rate_limits_do_not_consume_structured_validation_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr("src.openrouter.time.sleep", lambda _delay: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 5:
            return httpx.Response(200, json={"error": {"code": 429}})
        payload = json.loads(request.content)
        content = _extraction() if calls == 6 else _summary()
        return httpx.Response(
            200,
            json=_completion(
                content, model=payload["model"], provider="Azure", cost=0.01,
            ),
        )

    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    result = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        _document(), job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    assert result.relevance_score == 4
    assert calls == 7
    assert store.total_cost(job.id) == pytest.approx(0.02)


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


def test_transport_timeout_after_dispatch_is_durable_and_never_auto_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, UnresolvedUsageError
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    observed_before_post: list[tuple[int, int, float]] = []
    calls = 0

    def timeout_after_dispatch(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        with sqlite3.connect(store.path) as connection:
            attempt = connection.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE cost_resolved = 0"
            ).fetchone()[0]
            reservation = connection.execute(
                "SELECT COUNT(*), MIN(amount_usd) FROM llm_budget_reservations "
                "WHERE unresolved = 1"
            ).fetchone()
        observed_before_post.append((attempt, reservation[0], reservation[1]))
        raise httpx.ReadTimeout("response lost", request=request)

    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(timeout_after_dispatch)),
        catalog=_pricing(),
    )

    with pytest.raises(UnresolvedUsageError, match="unresolved"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    assert calls == 1
    assert observed_before_post[0][0:2] == (1, 1)
    assert observed_before_post[0][2] >= 0.00375
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT cost_resolved, validated, dispatch_state, authorized_amount_usd "
            "FROM llm_calls"
        ).fetchone()
    assert row == (0, 0, "dispatched", pytest.approx(0.00375))

    resumed = OpenRouterSummarizer(
        store,
        settings=_settings(),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _: pytest.fail("ambiguous dispatch must not be resent")
        )),
        catalog=_pricing(),
    )
    with pytest.raises(UnresolvedUsageError):
        resumed.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper-resume"),
        )
    assert calls == 1


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


def test_map_chunks_pack_against_complete_serialized_schema_budget(
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
    document["paragraphs"][0]["text"] = " ".join(
        "This paper evaluates a compact method." for _ in range(200)
    )
    settings = _settings(
        extraction_max_input_tokens=2_600,
        synthesis_max_input_tokens=20_000,
        chunk_max_chars=12_000,
        max_validation_retries=0,
    )
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))

    OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        document, job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    map_payloads = [
        payload for payload in payloads
        if payload["model"] == "google/gemini-3.8-flash"
    ]
    assert len(map_payloads) > 1
    assert all(estimate_serialized_tokens(payload) <= 2_600 for payload in map_payloads)


def test_map_chunks_reserve_input_space_for_validation_retry_instruction(
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
        if payload["model"] == "openai/gpt-5.6-sol":
            content = _summary()
        elif len(payload["messages"]) == 2:
            content = _extraction()
            content["evidence"][0]["quote"] = "absent from the source"
        else:
            content = _extraction()
        return httpx.Response(
            200, json=_completion(content, model=payload["model"], provider="p", cost=0.001),
        )

    document = _document()
    document["paragraphs"][0]["text"] = " ".join(
        "This paper evaluates a compact method." for _ in range(40)
    )
    settings = _settings(
        # v7's stricter verbatim-evidence instructions add request overhead;
        # keep the fixture just above the smallest retry-capable payload.
        extraction_max_input_tokens=2_800,
        synthesis_max_input_tokens=20_000,
        chunk_max_chars=12_000,
        max_validation_retries=1,
    )
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))

    OpenRouterSummarizer(
        store,
        settings=settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        catalog=_pricing(),
    ).summarize(
        document, job_id=job.id,
        artifacts=ArtifactManager(tmp_path / "out", "paper"),
    )

    map_payloads = [
        payload for payload in payloads
        if payload["model"] == "google/gemini-3.8-flash"
    ]
    assert any(len(payload["messages"]) == 3 for payload in map_payloads)
    assert all(
        estimate_serialized_tokens(payload) <= settings.extraction_max_input_tokens
        for payload in map_payloads
    )


def test_atomic_table_fails_closed_when_schema_overhead_exceeds_input_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import InputLimitExceededError, OpenRouterSummarizer
    from src.research_models import InputKind, InputSpec

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    document = {
        "sections": [{"id": "results", "title": "Results", "page": 1}],
        "paragraphs": [],
        "tables": [{
            "markdown": "<table><tr><td>" + "x " * 250 + "</td></tr></table>",
            "page": 1,
            "section_id": "results",
        }],
        "equations": [],
        "captions": [],
    }
    store = JobStore(tmp_path / "state.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    summarizer = OpenRouterSummarizer(
        store,
        settings=_settings(extraction_max_input_tokens=1_800),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))),
        catalog=_pricing(),
    )

    with pytest.raises(InputLimitExceededError, match="indivisible table"):
        summarizer.summarize(
            document, job_id=job.id,
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


def test_evidence_validator_accepts_quote_spanning_adjacent_source_blocks() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    chunk = DocumentChunk(
        text="NUS\n\nRMSE 14.8785 8.9766 6.1825 5.2426",
        kind="section",
        section="4.2 Evaluation on HSI Recovery",
        start_page=11,
        end_page=11,
        evidence=(
            ChunkEvidence("paragraph", 11, "results", "NUS"),
            ChunkEvidence(
                "paragraph",
                11,
                "results",
                "RMSE 14.8785 8.9766 6.1825 5.2426",
            ),
        ),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "定量評価結果。",
        "datasets": ["NUS"],
        "metrics": ["RMSE"],
        "keywords": ["HSI recovery"],
        "evidence": [{
            "page": 11,
            "section": "4.2 Evaluation on HSI Recovery",
            "quote": "NUS RMSE 14.8785 8.9766 6.1825 5.2426",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)


def test_evidence_validator_corrects_page_when_quote_has_unique_section_match() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    chunk = DocumentChunk(
        text="References",
        kind="section",
        section="References",
        start_page=15,
        end_page=16,
        evidence=(
            ChunkEvidence("paragraph", 15, "refs", "17. Previous reference"),
            ChunkEvidence("paragraph", 16, "refs", "18. Unique reference text"),
        ),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "参考文献の抽出。",
        "datasets": [],
        "metrics": [],
        "keywords": ["reference"],
        "evidence": [{
            "page": 15,
            "section": "References",
            "quote": "18. Unique reference text",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)

    assert response.evidence[0].page == 16


def test_evidence_validator_ignores_latex_display_delimiters() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    formula = r"\mathcal{Y}_t = \operatorname{stack}(\mathbf{Y}_{1,t})"
    chunk = DocumentChunk(
        text=formula,
        kind="section",
        section="3.3 Optimal CSS Selection",
        start_page=8,
        end_page=8,
        evidence=(ChunkEvidence("equation", 8, "css", formula + r". \tag{12}"),),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "スタック演算を定義する。",
        "datasets": [],
        "metrics": [],
        "keywords": ["stack"],
        "evidence": [{
            "page": 8,
            "section": "3.3 Optimal CSS Selection",
            "quote": rf"\({formula}\)",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)


def test_evidence_validator_restores_unique_source_markup_before_accepting_quote() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    source = "estimated by a previous paper<sup>1</sup>."
    chunk = DocumentChunk(
        text=source,
        kind="paragraph",
        section="Methods",
        start_page=3,
        end_page=3,
        evidence=(ChunkEvidence("paragraph", 3, "methods", source),),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "既報の手法を用いる。",
        "datasets": [],
        "metrics": [],
        "keywords": ["estimation"],
        "evidence": [{
            "page": 3,
            "section": "Methods",
            "quote": "estimated by a previous paper1.",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)

    assert response.evidence[0].quote == source


def test_evidence_validator_clips_overextended_quote_to_unique_claimed_page_source() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    page_two = "Acquisition evolved over several decades."
    page_three = "Newer systems acquire images line by line."
    chunk = DocumentChunk(
        text=f"{page_two}\n\n{page_three}",
        kind="mixed",
        section="Related Work",
        start_page=2,
        end_page=3,
        evidence=(
            ChunkEvidence("paragraph", 2, "related", page_two),
            ChunkEvidence("paragraph", 3, "related", page_three),
        ),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "分光計測の発展を説明する。",
        "datasets": [],
        "metrics": [],
        "keywords": ["spectrometer"],
        "evidence": [{
            "page": 2,
            "section": "Related Work",
            "quote": f"{page_two} {page_three}",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)

    assert response.evidence[0].quote == page_two


def test_evidence_validator_tolerates_ocr_hyphen_joining() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    source = "We compare with three state-ofthe-art recovery methods."
    chunk = DocumentChunk(
        text=source,
        kind="section",
        section="Results",
        start_page=10,
        end_page=10,
        evidence=(ChunkEvidence("paragraph", 10, "results", source),),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "既存法と比較する。",
        "datasets": [],
        "metrics": [],
        "keywords": ["comparison"],
        "evidence": [{
            "page": 10,
            "section": "Results",
            "quote": "We compare with three state-of-the-art recovery methods.",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)

    assert response.evidence[0].quote == source


def test_evidence_validator_restores_source_case_and_context_exactly() -> None:
    from src.openrouter import (
        ChunkEvidence,
        ChunkExtractionResponse,
        DocumentChunk,
        OpenRouterSummarizer,
    )

    source = (
        "First, measurements are calibrated. "
        "Third, the spatial variations are caused by two pigments. "
        "Fourth, the quantities are independent."
    )
    chunk = DocumentChunk(
        text=source,
        kind="paragraph",
        section="Skin Color Model",
        start_page=2,
        end_page=2,
        evidence=(ChunkEvidence("paragraph", 2, "skin", source),),
    )
    response = ChunkExtractionResponse.model_validate({
        "summary": "二つの色素を仮定する。",
        "datasets": [],
        "metrics": [],
        "keywords": ["pigments"],
        "evidence": [{
            "page": 2,
            "section": "Skin Color Model",
            "quote": "The spatial variations are caused by two pigments.",
        }],
    })

    OpenRouterSummarizer._evidence_validator([chunk])(response)

    assert response.evidence[0].quote == (
        "Third, the spatial variations are caused by two pigments."
    )


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


def test_failed_budget_settlement_leaves_durable_unresolved_attempt_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.artifacts import ArtifactManager
    from src.job_store import JobStore
    from src.openrouter import OpenRouterSummarizer, UnresolvedUsageError
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

    with pytest.raises(UnresolvedUsageError, match="settlement"):
        summarizer.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "paper"),
        )

    with sqlite3.connect(store.path) as connection:
        recorded_attempt = connection.execute(
            "SELECT cost_resolved, validated, dispatch_state FROM llm_calls"
        ).fetchone()
        unresolved_reservation = connection.execute(
            "SELECT unresolved FROM llm_budget_reservations"
        ).fetchone()[0]
    assert recorded_attempt == (0, 0, "dispatched")
    assert unresolved_reservation == 1
    assert store.total_cost(job.id) == 0

    resumed = OpenRouterSummarizer(
        store,
        settings=_settings(max_validation_retries=0),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _: pytest.fail("failed settlement must not be resent")
        )),
        catalog=_pricing(),
    )
    with pytest.raises(UnresolvedUsageError):
        resumed.summarize(
            _document(), job_id=job.id,
            artifacts=ArtifactManager(tmp_path / "out", "resume"),
        )
