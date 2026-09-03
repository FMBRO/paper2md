"""Load settings from ``configs/config.yaml`` and merge CLI overrides."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

import yaml


DEFAULT_NOTION_PROPERTIES = {
    "title": "Title",
    "authors": "Authors",
    "published_date": "Published Date",
    "doi": "DOI",
    "arxiv_id": "arXiv ID",
    "source_url": "Source URL",
    "zotero_link": "Zotero Link",
    "zotero_item_key": "Zotero Item Key",
    "processing_status": "Processing Status",
    "relevance_score": "Relevance Score",
    "score_rationale": "Score Rationale",
    "topics": "Topics",
    "ai_keywords": "AI Keywords",
    "imported_at": "Imported At",
    "model_prompt_version": "Model / Prompt Version",
}

_SENSITIVE_CONFIG_KEYS = {
    "apikey", "accesskey", "secret", "clientsecret", "token", "accesstoken",
    "authorization", "password", "passwd", "privatekey", "bearertoken",
}


def _has_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (normalized_key in _SENSITIVE_CONFIG_KEYS
                    or any(normalized_key.endswith(key) for key in _SENSITIVE_CONFIG_KEYS)
                    or _has_sensitive_key(child)):
                return True
    elif isinstance(value, list):
        return any(_has_sensitive_key(child) for child in value)
    return False


@dataclass
class ZoteroSettings:
    base_url: str = "http://localhost:23119/api/"
    user_id: int = 0


@dataclass
class OpenRouterSettings:
    endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    extraction_model: str = "google/gemini-3.8-flash"
    synthesis_model: str = "openai/gpt-5.6-sol"
    require_parameters: bool = True
    zdr: bool = True
    data_collection: bool = False
    paper_budget_usd: float = 0.50


@dataclass
class NotionSettings:
    data_source_id: str | None = None
    api_version: str = "2026-03-11"
    properties: dict[str, str] | None = None

    def __post_init__(self) -> None:
        self.properties = {**DEFAULT_NOTION_PROPERTIES, **(self.properties or {})}


@dataclass
class Settings:
    input_dir: Path
    output_dir: Path
    engine: str = "marker"
    language: str = "eng"
    enable_ocr: bool = True
    force_ocr: bool = False
    ocr_deskew: bool = True
    ocr_clean: bool = True
    workers: int = 1
    skip_existing: bool = False
    overwrite: bool = False
    continue_on_error: bool = True
    save_logs: bool = True
    research_interest: str = ""
    state_path: Path | None = None
    zotero: ZoteroSettings | None = None
    openrouter: OpenRouterSettings | None = None
    notion: NotionSettings | None = None

    def __post_init__(self) -> None:
        self.input_dir = Path(self.input_dir)
        self.output_dir = Path(self.output_dir)
        self.state_path = Path(self.state_path) if self.state_path else self.output_dir / "paper2md.sqlite3"
        self.zotero = self.zotero or ZoteroSettings()
        self.openrouter = self.openrouter or OpenRouterSettings()
        self.notion = self.notion or NotionSettings()


def load_settings(config_path: Path | str, overrides: dict[str, Any] | None = None) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    overrides = overrides or {}
    state_path_was_configured = raw.get("state_path") is not None

    ocr = raw.get("ocr") or {}
    ocr_options = ocr.get("options") or {}
    converter = raw.get("converter") or {}
    batch = raw.get("batch") or {}
    zotero = raw.get("zotero") or {}
    openrouter = raw.get("openrouter") or {}
    notion = raw.get("notion") or {}
    if _has_sensitive_key(raw):
        raise ValueError("Secrets must be supplied through environment variables")

    s = Settings(
        input_dir=Path(raw.get("input_dir", "input")),
        output_dir=Path(raw.get("output_dir", "output")),
        engine=converter.get("engine", "marker"),
        language=raw.get("language", "eng"),
        enable_ocr=ocr.get("enabled", True) is not False,
        force_ocr=bool(ocr.get("force", False)),
        ocr_deskew=bool(ocr_options.get("deskew", True)),
        ocr_clean=bool(ocr_options.get("clean", True)),
        workers=int(batch.get("workers", 1)),
        skip_existing=bool(batch.get("skip_existing", False)),
        overwrite=bool(batch.get("overwrite", False)),
        continue_on_error=bool(batch.get("continue_on_error", True)),
        research_interest=str(raw.get("research_interest", "")),
        state_path=raw.get("state_path"),
        zotero=ZoteroSettings(
            base_url=str(zotero.get("base_url", "http://localhost:23119/api/")),
            user_id=int(zotero.get("user_id", 0)),
        ),
        openrouter=OpenRouterSettings(
            endpoint=str(openrouter.get("endpoint", "https://openrouter.ai/api/v1/chat/completions")),
            extraction_model=str(openrouter.get("extraction_model", "google/gemini-3.8-flash")),
            synthesis_model=str(openrouter.get("synthesis_model", "openai/gpt-5.6-sol")),
            require_parameters=bool(openrouter.get("require_parameters", True)),
            zdr=bool(openrouter.get("zdr", True)),
            data_collection=bool(openrouter.get("data_collection", False)),
            paper_budget_usd=float(openrouter.get("paper_budget_usd", 0.50)),
        ),
        notion=NotionSettings(
            data_source_id=notion.get("data_source_id"),
            api_version=str(notion.get("api_version", "2026-03-11")),
            properties=notion.get("properties"),
        ),
    )

    for key, value in overrides.items():
        if value is None:
            continue
        if not hasattr(s, key):
            raise KeyError(f"Unknown setting override: {key}")
        setattr(s, key, value)

    if "output_dir" in overrides and not state_path_was_configured:
        s.state_path = Path(s.output_dir) / "paper2md.sqlite3"
    s.__post_init__()
    return s
