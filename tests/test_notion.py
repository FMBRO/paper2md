from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest


def _summary() -> object:
    from src.research_models import PaperSummary

    return PaperSummary(
        background="既存手法には計算量の課題がある。",
        question="軽量化で精度を維持できるかを調べる。",
        novelty="蒸留と検索を組み合わせた。",
        methods="二段階の学習を行う。",
        datasets=["データセットA"],
        results="精度は三ポイント改善した。",
        strengths="再現可能な評価である。",
        limitations="小規模な検証に限られる。",
        takeaways="軽量化の候補として有望である。",
        relevance_score=4,
        score_rationale="研究関心に直接関連する。",
        keywords=["検索", "蒸留"],
    )


def _metadata() -> object:
    from src.research_models import PaperMetadata

    return PaperMetadata(
        title="日本語要約の論文",
        authors=["Ada Lovelace", "Grace Hopper"],
        published_date="2026-01-02",
        doi="10.1000/EXAMPLE",
        arxiv_id="2401.01234v2",
        source_url="https://example.test/paper",
        zotero_library_id="0",
        zotero_item_key="ABC123",
    )


def _schema(
    *, omit: str | None = None, doi_type: str = "rich_text",
    processing_type: str = "status",
) -> dict[str, Any]:
    from src.config import DEFAULT_NOTION_PROPERTIES

    types = {
        "title": "title", "authors": "rich_text", "published_date": "date",
        "doi": doi_type, "arxiv_id": "rich_text", "source_url": "url",
        "zotero_link": "url", "zotero_item_key": "rich_text",
        "processing_status": processing_type, "relevance_score": "number",
        "score_rationale": "rich_text", "topics": "multi_select",
        "ai_keywords": "rich_text", "imported_at": "date",
        "model_prompt_version": "rich_text",
    }
    properties = {
        name: {"id": key, "type": types[key]}
        for key, name in DEFAULT_NOTION_PROPERTIES.items() if key != omit
    }
    if "processing_status" in types and omit != "processing_status":
        properties["Processing Status"][processing_type] = {
            "options": [{"id": "status-complete", "name": "Completed"}],
        }
    if omit != "topics":
        properties["Topics"]["multi_select"] = {
            "options": [{"id": "topic-ir", "name": "情報検索"}],
        }
    return {
        "object": "data_source",
        "properties": properties,
    }


def _upserter(
    handler: httpx.MockTransport | Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sleeps: list[float] | None = None,
) -> object:
    from src.config import NotionSettings
    from src.notion import NotionSummaryUpserter

    monkeypatch.setenv("NOTION_API_KEY", "notion-secret")
    return NotionSummaryUpserter(
        settings=NotionSettings(data_source_id="source-1"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
        now=lambda: datetime(2026, 9, 3, tzinfo=UTC),
    )


def test_schema_validation_reports_missing_and_wrong_mapped_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.notion import NotionSchemaError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_schema(omit="topics", doi_type="url"))

    upserter = _upserter(handler, monkeypatch)

    with pytest.raises(NotionSchemaError, match="DOI.*Topics") as error:
        upserter.validate_schema()

    assert "missing" in str(error.value)
    assert "expected rich_text, got url" in str(error.value)


def test_create_payload_maps_properties_and_sends_only_japanese_summary_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_schema())
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": "created-page"})

    page_id = _upserter(handler, monkeypatch).upsert(
        _metadata(), _summary(), topics=["情報検索"], model_prompt_version="v1",
    )

    assert page_id == "created-page"
    created = next(request for request in requests if request.method == "POST" and request.url.path == "/v1/pages")
    assert created.headers["notion-version"] == "2026-03-11"
    assert created.headers["authorization"] == "Bearer notion-secret"
    payload = json.loads(created.content)
    assert payload["parent"] == {"type": "data_source_id", "data_source_id": "source-1"}
    assert payload["properties"]["Title"]["title"][0]["text"]["content"] == "日本語要約の論文"
    assert payload["properties"]["Authors"]["rich_text"][0]["text"]["content"] == "Ada Lovelace, Grace Hopper"
    assert payload["properties"]["Published Date"] == {"date": {"start": "2026-01-02"}}
    assert payload["properties"]["DOI"]["rich_text"][0]["text"]["content"] == "10.1000/example"
    assert payload["properties"]["Zotero Link"] == {"url": "zotero://select/library/items/ABC123"}
    assert payload["properties"]["Processing Status"] == {"status": {"id": "status-complete"}}
    assert payload["properties"]["Relevance Score"] == {"number": 4}
    assert payload["properties"]["Topics"] == {"multi_select": [{"id": "topic-ir"}]}
    assert payload["properties"]["AI Keywords"] == {
        "rich_text": [{"type": "text", "text": {"content": "検索, 蒸留"}}],
    }
    assert payload["properties"]["Imported At"] == {"date": {"start": "2026-09-03T00:00:00+00:00"}}
    assert payload["properties"]["Model / Prompt Version"]["rich_text"][0]["text"]["content"] == "v1"
    assert payload["markdown"] == (
        "## 背景\n既存手法には計算量の課題がある。\n\n"
        "## 研究課題\n軽量化で精度を維持できるかを調べる。\n\n"
        "## 新規性\n蒸留と検索を組み合わせた。\n\n"
        "## 手法\n二段階の学習を行う。\n\n"
        "## データセット\n- データセットA\n\n"
        "## 結果\n精度は三ポイント改善した。\n\n"
        "## 強み\n再現可能な評価である。\n\n"
        "## 限界\n小規模な検証に限られる。\n\n"
        "## 要点\n軽量化の候補として有望である。"
    )
    assert b"notion-secret" not in b"".join(request.content for request in requests)


def test_stored_page_id_takes_precedence_and_updates_properties_and_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/data_sources/source-1":
            return httpx.Response(200, json=_schema())
        if request.method == "GET" and request.url.path == "/v1/pages/stored-page":
            return httpx.Response(200, json={"id": "stored-page"})
        return httpx.Response(200, json={"id": "stored-page"})

    page_id = _upserter(handler, monkeypatch).upsert(
        _metadata(), _summary(), stored_page_id="stored-page",
    )

    assert page_id == "stored-page"
    assert [request.url.path for request in requests] == [
        "/v1/data_sources/source-1", "/v1/pages/stored-page",
        "/v1/pages/stored-page", "/v1/pages/stored-page/markdown",
    ]
    assert json.loads(requests[-1].content) == {
        "type": "replace_content",
        "replace_content": {"new_str": "## 背景\n既存手法には計算量の課題がある。\n\n## 研究課題\n軽量化で精度を維持できるかを調べる。\n\n## 新規性\n蒸留と検索を組み合わせた。\n\n## 手法\n二段階の学習を行う。\n\n## データセット\n- データセットA\n\n## 結果\n精度は三ポイント改善した。\n\n## 強み\n再現可能な評価である。\n\n## 限界\n小規模な検証に限られる。\n\n## 要点\n軽量化の候補として有望である。"},
    }


def test_identity_lookup_uses_doi_before_arxiv_and_updates_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_schema())
        if request.url.path.endswith("/query"):
            payload = json.loads(request.content)
            if payload["filter"]["property"] == "DOI":
                return httpx.Response(200, json={"results": [{"id": "doi-page"}]})
            raise AssertionError("arXiv and Zotero lookups must not run after DOI match")
        return httpx.Response(200, json={"id": "doi-page"})

    page_id = _upserter(handler, monkeypatch).upsert(_metadata(), _summary())

    assert page_id == "doi-page"
    doi_query = json.loads(requests[1].content)
    assert doi_query == {
        "filter": {"property": "DOI", "rich_text": {"equals": "10.1000/example"}},
        "page_size": 2,
    }
    assert [request.method for request in requests] == ["GET", "POST", "PATCH", "PATCH"]
    assert json.loads(requests[-1].content)["type"] == "replace_content"
    assert json.loads(requests[-1].content)["replace_content"]["new_str"].startswith("## 背景")


def test_select_processing_status_schema_uses_a_select_property_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_schema(processing_type="select"))
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": "created-page"})

    _upserter(handler, monkeypatch).upsert(_metadata(), _summary())

    payload = json.loads(requests[-1].content)
    assert payload["properties"]["Processing Status"] == {"select": {"id": "status-complete"}}


@pytest.mark.parametrize(
    ("topics", "processing_status", "expected"),
    [(["未登録トピック"], "Completed", "Topics"), ([], "Unrecognized", "Processing Status")],
)
def test_unknown_controlled_options_fail_before_any_page_write(
    monkeypatch: pytest.MonkeyPatch,
    topics: list[str],
    processing_status: str,
    expected: str,
) -> None:
    from src.notion import NotionSchemaError

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_schema())

    with pytest.raises(NotionSchemaError, match=expected):
        _upserter(handler, monkeypatch).upsert(
            _metadata(), _summary(), topics=topics, processing_status=processing_status,
        )

    assert [request.method for request in requests] == ["GET"]


def test_schema_rejects_ai_keywords_that_are_not_rich_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.notion import NotionSchemaError

    def handler(request: httpx.Request) -> httpx.Response:
        schema = _schema()
        schema["properties"]["AI Keywords"]["type"] = "multi_select"
        return httpx.Response(200, json=schema)

    with pytest.raises(NotionSchemaError, match="AI Keywords.*rich_text"):
        _upserter(handler, monkeypatch).validate_schema()


def test_rate_limit_honors_retry_after_without_live_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=_schema())

    _upserter(handler, monkeypatch, sleeps=sleeps).validate_schema()

    assert calls == 2
    assert sleeps == [3.0]


def test_rate_limit_honors_http_date_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []
    retry_at = format_datetime(datetime(2026, 9, 3, 0, 0, 3, tzinfo=UTC), usegmt=True)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_at})
        return httpx.Response(200, json=_schema())

    _upserter(handler, monkeypatch, sleeps=sleeps).validate_schema()

    assert calls == 2
    assert sleeps == [3.0]


def test_network_and_server_failures_use_bounded_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_schema())

    _upserter(handler, monkeypatch, sleeps=sleeps).validate_schema()

    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_ambiguous_create_recovers_by_identity_before_retrying_to_avoid_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_schema())
        if request.url.path.endswith("/query"):
            query_count += 1
            if query_count == 4:
                return httpx.Response(200, json={"results": [{"id": "recovered-page"}]})
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            raise httpx.ConnectError("response lost", request=request)
        return httpx.Response(200, json={"id": "recovered-page"})

    page_id = _upserter(handler, monkeypatch).upsert(_metadata(), _summary())

    assert page_id == "recovered-page"
    assert len([request for request in requests if request.method == "POST" and request.url.path == "/v1/pages"]) == 1
    assert [request.method for request in requests[-2:]] == ["PATCH", "PATCH"]


def test_authentication_error_is_non_retryable_and_does_not_expose_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.notion import NotionAPIError

    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="notion-secret")

    with pytest.raises(NotionAPIError, match="401") as error:
        _upserter(handler, monkeypatch, sleeps=sleeps).validate_schema()

    assert calls == 1
    assert sleeps == []
    assert "notion-secret" not in str(error.value)
