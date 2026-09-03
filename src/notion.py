"""Idempotent, schema-validated Notion summary synchronization."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import os
import re
import time
from typing import Any, Protocol

import httpx

from src.config import NotionSettings
from src.research_models import PaperMetadata, PaperSummary


NOTION_API_BASE_URL = "https://api.notion.com/v1"
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_PROPERTY_TYPES = {
    "title": {"title"},
    "authors": {"rich_text"},
    "published_date": {"date"},
    "doi": {"rich_text"},
    "arxiv_id": {"rich_text"},
    "source_url": {"url"},
    "zotero_link": {"url"},
    "zotero_item_key": {"rich_text"},
    "processing_status": {"status", "select"},
    "relevance_score": {"number"},
    "score_rationale": {"rich_text"},
    "topics": {"multi_select"},
    "ai_keywords": {"multi_select"},
    "imported_at": {"date"},
    "model_prompt_version": {"rich_text"},
}


class NotionHTTPClient(Protocol):
    """Minimal injectable HTTP boundary; tests can use ``httpx.MockTransport``."""

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


class NotionConfigurationError(RuntimeError):
    """Raised before a request when required local configuration is absent."""


class NotionSchemaError(RuntimeError):
    """Raised for diagnostic-only schema validation failures."""


class NotionAPIError(RuntimeError):
    """Raised for sanitized Notion transport and API failures."""

    def __init__(
        self, message: str, *, retryable: bool = False, retry_delay: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_delay = retry_delay


class NotionSummaryUpserter:
    """Validate an existing data source and create or update one paper page."""

    def __init__(
        self,
        *,
        settings: NotionSettings,
        client: NotionHTTPClient,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.5,
    ) -> None:
        if not settings.data_source_id:
            raise NotionConfigurationError("notion.data_source_id is required")
        if settings.api_version != "2026-03-11":
            raise NotionConfigurationError("Notion API version must be 2026-03-11")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be positive")
        api_key = os.environ.get("NOTION_API_KEY")
        if not api_key:
            raise NotionConfigurationError("NOTION_API_KEY is required")
        self.settings = settings
        self.client = client
        self._api_key = api_key
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._schema_property_types: dict[str, str] = {}

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self.settings.api_version,
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
        acceptable_statuses: set[int] | None = None, max_attempts: int | None = None,
    ) -> httpx.Response:
        """Issue a retried request without exposing response bodies or credentials."""
        accepted = acceptable_statuses or set()
        url = f"{NOTION_API_BASE_URL}{path}"
        attempts = max_attempts or self._max_attempts
        last_network_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                response = self.client.request(method, url, headers=self._headers, json=json)
            except httpx.HTTPError as error:
                last_network_error = error
                if attempt == attempts - 1:
                    break
                self._sleep(self._initial_backoff_seconds * (2 ** attempt))
                continue
            if 200 <= response.status_code < 300 or response.status_code in accepted:
                return response
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not retryable or attempt == attempts - 1:
                raise NotionAPIError(
                    f"Notion API request failed with status {response.status_code}",
                    retryable=retryable,
                    retry_delay=self._retry_delay(response, attempt) if retryable else None,
                )
            self._sleep(self._retry_delay(response, attempt))
        if last_network_error is not None:
            raise NotionAPIError(
                "Notion API network failure", retryable=True,
                retry_delay=self._initial_backoff_seconds * (2 ** (attempts - 1)),
            ) from last_network_error
        raise NotionAPIError("Notion API request failed")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        now = self._now()
                        if now.tzinfo is None:
                            now = now.replace(tzinfo=UTC)
                        return max(0.0, (retry_at - now).total_seconds())
                    except (TypeError, ValueError):
                        pass
        return self._initial_backoff_seconds * (2 ** attempt)

    def validate_schema(self) -> None:
        """Read and diagnose the configured data source; never write its schema."""
        response = self._request("GET", f"/data_sources/{self.settings.data_source_id}")
        try:
            properties = response.json()["properties"]
        except (KeyError, TypeError, ValueError) as error:
            raise NotionSchemaError("Data source response has no properties schema") from error
        if not isinstance(properties, dict):
            raise NotionSchemaError("Data source response has an invalid properties schema")
        diagnostics: list[str] = []
        schema_property_types: dict[str, str] = {}
        for key, configured_name in self.settings.properties.items():
            property_schema = properties.get(configured_name)
            if not isinstance(property_schema, dict):
                diagnostics.append(f"{configured_name}: missing")
                continue
            actual_type = property_schema.get("type")
            expected_types = _PROPERTY_TYPES[key]
            if actual_type not in expected_types:
                diagnostics.append(
                    f"{configured_name}: expected {' or '.join(sorted(expected_types))}, got {actual_type}"
                )
            else:
                schema_property_types[key] = actual_type
        if diagnostics:
            raise NotionSchemaError("Notion data source schema invalid: " + "; ".join(diagnostics))
        self._schema_property_types = schema_property_types

    @staticmethod
    def _rich_text(value: str | None) -> dict[str, list[dict[str, Any]]]:
        return {
            "rich_text": [] if not value else [
                {"type": "text", "text": {"content": value}},
            ],
        }

    @staticmethod
    def _title(value: str | None) -> dict[str, list[dict[str, Any]]]:
        return {
            "title": [] if not value else [
                {"type": "text", "text": {"content": value}},
            ],
        }

    @staticmethod
    def _multi_select(values: Iterable[str]) -> dict[str, list[dict[str, str]]]:
        return {"multi_select": [{"name": value} for value in values if value]}

    @staticmethod
    def _zotero_link(metadata: PaperMetadata) -> str | None:
        if metadata.zotero_item_key:
            return f"zotero://select/library/items/{metadata.zotero_item_key}"
        return None

    def _validated_summary_markdown(self, summary: PaperSummary) -> str:
        narratives = (
            summary.background, summary.question, summary.novelty, summary.methods,
            summary.results, summary.strengths, summary.limitations, summary.takeaways,
            summary.score_rationale,
        )
        if (
            summary.relevance_score is None or not 1 <= summary.relevance_score <= 5
            or any(not value.strip() or not _JAPANESE.search(value) for value in narratives)
        ):
            raise ValueError("Notion accepts only a validated Japanese PaperSummary")
        sections = [
            ("背景", summary.background), ("研究課題", summary.question),
            ("新規性", summary.novelty), ("手法", summary.methods),
            ("データセット", "\n".join(f"- {item}" for item in summary.datasets) or "- 該当なし"),
            ("結果", summary.results), ("強み", summary.strengths),
            ("限界", summary.limitations), ("要点", summary.takeaways),
        ]
        return "\n\n".join(f"## {heading}\n{body}" for heading, body in sections)

    def _properties(
        self, metadata: PaperMetadata, summary: PaperSummary, *, topics: Iterable[str],
        processing_status: str, model_prompt_version: str,
    ) -> dict[str, Any]:
        names = self.settings.properties
        imported_at = self._now().isoformat()
        return {
            names["title"]: self._title(metadata.title),
            names["authors"]: self._rich_text(", ".join(metadata.authors)),
            names["published_date"]: {"date": {"start": metadata.published_date} if metadata.published_date else None},
            names["doi"]: self._rich_text(metadata.doi),
            names["arxiv_id"]: self._rich_text(metadata.arxiv_id),
            names["source_url"]: {"url": metadata.source_url},
            names["zotero_link"]: {"url": self._zotero_link(metadata)},
            names["zotero_item_key"]: self._rich_text(metadata.zotero_item_key),
            names["processing_status"]: {
                self._schema_property_types.get("processing_status", "status"): {
                    "name": processing_status,
                },
            },
            names["relevance_score"]: {"number": summary.relevance_score},
            names["score_rationale"]: self._rich_text(summary.score_rationale),
            names["topics"]: self._multi_select(topics),
            names["ai_keywords"]: self._multi_select(summary.keywords),
            names["imported_at"]: {"date": {"start": imported_at}},
            names["model_prompt_version"]: self._rich_text(model_prompt_version),
        }

    def _find_page(self, metadata: PaperMetadata, stored_page_id: str | None) -> str | None:
        if stored_page_id:
            stored = self._request(
                "GET", f"/pages/{stored_page_id}", acceptable_statuses={404},
            )
            if stored.status_code != 404:
                try:
                    return str(stored.json()["id"])
                except (KeyError, TypeError, ValueError) as error:
                    raise NotionAPIError("Stored Notion page response has no id") from error
        identities = (
            ("doi", metadata.doi, "rich_text"),
            ("arxiv_id", metadata.arxiv_id, "rich_text"),
            ("zotero_item_key", metadata.zotero_item_key, "rich_text"),
        )
        for key, value, property_type in identities:
            if not value:
                continue
            payload = {
                "filter": {
                    "property": self.settings.properties[key],
                    property_type: {"equals": value},
                },
                "page_size": 2,
            }
            response = self._request(
                "POST", f"/data_sources/{self.settings.data_source_id}/query", json=payload,
            )
            try:
                matches = response.json().get("results", [])
            except ValueError as error:
                raise NotionAPIError("Notion query returned invalid JSON") from error
            if not isinstance(matches, list):
                raise NotionAPIError("Notion query returned invalid results")
            if len(matches) > 1:
                raise NotionAPIError(f"Notion query found multiple pages for {key}")
            if matches:
                page_id = matches[0].get("id") if isinstance(matches[0], dict) else None
                if not page_id:
                    raise NotionAPIError("Notion query result has no page id")
                return str(page_id)
        return None

    def _create_or_recover(
        self, metadata: PaperMetadata, properties: dict[str, Any], markdown: str,
    ) -> tuple[str, bool]:
        """Create once per confirmed absence, recovering ambiguous writes by identity."""
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": self.settings.data_source_id},
            "properties": properties,
            "markdown": markdown,
        }
        for attempt in range(self._max_attempts):
            try:
                response = self._request("POST", "/pages", json=payload, max_attempts=1)
            except NotionAPIError as error:
                if not error.retryable:
                    raise
                recovered_page_id = self._find_page(metadata, None)
                if recovered_page_id:
                    return recovered_page_id, True
                if attempt == self._max_attempts - 1:
                    raise
                self._sleep(error.retry_delay or self._initial_backoff_seconds * (2 ** attempt))
                continue
            try:
                page_id = response.json()["id"]
            except (KeyError, TypeError, ValueError) as error:
                raise NotionAPIError("Created Notion page response has no id") from error
            if not page_id:
                raise NotionAPIError("Created Notion page response has no id")
            return str(page_id), False
        raise NotionAPIError("Notion page creation failed")

    def upsert(
        self,
        metadata: PaperMetadata,
        summary: PaperSummary,
        *,
        stored_page_id: str | None = None,
        topics: Iterable[str] = (),
        processing_status: str = "Completed",
        model_prompt_version: str = "",
    ) -> str:
        """Update a uniquely identified page, or create one only when none exists."""
        markdown = self._validated_summary_markdown(summary)
        self.validate_schema()
        properties = self._properties(
            metadata, summary, topics=topics, processing_status=processing_status,
            model_prompt_version=model_prompt_version,
        )
        page_id = self._find_page(metadata, stored_page_id)
        if page_id:
            self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})
            self._request(
                "PATCH", f"/pages/{page_id}/markdown", json={"markdown": markdown},
            )
            return page_id
        page_id, recovered = self._create_or_recover(metadata, properties, markdown)
        if recovered:
            self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})
            self._request(
                "PATCH", f"/pages/{page_id}/markdown", json={"markdown": markdown},
            )
        return page_id
