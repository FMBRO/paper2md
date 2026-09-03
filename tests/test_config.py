from pathlib import Path

from src.config import Settings, load_settings
import pytest


def test_load_settings_adds_research_pipeline_defaults_without_breaking_legacy_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("input_dir: in\noutput_dir: out\n", encoding="utf-8")

    settings = load_settings(cfg)

    assert settings.state_path == Path("out") / "paper2md.sqlite3"
    assert settings.research_interest == ""
    assert settings.zotero.base_url == "http://localhost:23119/api/"
    assert settings.zotero.user_id == 0
    assert settings.openrouter.endpoint == "https://openrouter.ai/api/v1/chat/completions"
    assert settings.openrouter.extraction_model == "google/gemini-3.8-flash"
    assert settings.openrouter.synthesis_model == "openai/gpt-5.6-sol"
    assert settings.openrouter.paper_budget_usd == 0.50
    assert settings.notion.properties["title"] == "Title"


def test_load_settings_reads_pipeline_sections_but_rejects_secret_values(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "output_dir: research-output\n"
        "research_interest: information retrieval\n"
        "zotero: {base_url: http://zotero.test/api/, user_id: 12}\n"
        "openrouter: {paper_budget_usd: 1.25, extraction_model: custom/extract}\n"
        "notion:\n  data_source_id: data-source\n  properties: {doi: DOI Property}\n",
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    assert settings.state_path == Path("research-output") / "paper2md.sqlite3"
    assert settings.research_interest == "information retrieval"
    assert settings.zotero.user_id == 12
    assert settings.openrouter.paper_budget_usd == 1.25
    assert settings.openrouter.extraction_model == "custom/extract"
    assert settings.notion.data_source_id == "data-source"
    assert settings.notion.properties["doi"] == "DOI Property"


def test_output_override_also_moves_default_state_database(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("output_dir: yaml-out\n", encoding="utf-8")

    settings = load_settings(cfg, overrides={"output_dir": Path("cli-out")})

    assert settings.state_path == Path("cli-out") / "paper2md.sqlite3"


@pytest.mark.parametrize("yaml_key", ["apiKey", "token", "authorization", "password", "openrouterApiKey"])
def test_load_settings_rejects_common_yaml_secret_key_variants(tmp_path: Path, yaml_key: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"openrouter:\n  credentials:\n    {yaml_key}: do-not-persist\n", encoding="utf-8")

    with pytest.raises(ValueError, match="environment variables"):
        load_settings(cfg)


def test_load_settings_from_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "input_dir: in\n"
        "output_dir: out\n"
        "language: eng\n"
        "ocr:\n"
        "  enabled: auto\n"
        "  force: false\n"
        "converter:\n"
        "  engine: marker\n"
        "batch:\n"
        "  workers: 1\n"
        "  skip_existing: false\n"
        "  overwrite: false\n"
        "  continue_on_error: true\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert isinstance(settings, Settings)
    assert settings.input_dir == Path("in")
    assert settings.output_dir == Path("out")
    assert settings.engine == "marker"
    assert settings.workers == 1


def test_load_settings_loads_ocr_options(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "input_dir: in\n"
        "output_dir: out\n"
        "ocr:\n"
        "  enabled: auto\n"
        "  force: false\n"
        "  options:\n"
        "    deskew: false\n"
        "    clean: false\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.ocr_deskew is False
    assert settings.ocr_clean is False


def test_load_settings_ocr_options_default_to_true(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("input_dir: in\noutput_dir: out\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.ocr_deskew is True
    assert settings.ocr_clean is True


def test_load_settings_applies_cli_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "input_dir: in\n"
        "output_dir: out\n"
        "ocr: {enabled: auto, force: false}\n"
        "converter: {engine: marker}\n"
        "batch: {workers: 1, skip_existing: false, overwrite: false, continue_on_error: true}\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, overrides={"engine": "docling", "workers": 4, "force_ocr": True})
    assert settings.engine == "docling"
    assert settings.workers == 4
    assert settings.force_ocr is True


@pytest.mark.parametrize(
    "setting,value",
    [
        ("extraction_max_input_tokens", 0),
        ("extraction_max_output_tokens", -1),
        ("synthesis_max_input_tokens", 0),
        ("synthesis_max_output_tokens", -1),
        ("chunk_max_chars", 0),
        ("max_reduction_levels", 0),
    ],
)
def test_openrouter_token_and_chunk_limits_must_be_positive(setting: str, value: int) -> None:
    from src.config import OpenRouterSettings

    with pytest.raises(ValueError, match=setting):
        OpenRouterSettings(**{setting: value})


def test_openrouter_validation_retry_count_cannot_be_negative() -> None:
    from src.config import OpenRouterSettings

    with pytest.raises(ValueError, match="max_validation_retries"):
        OpenRouterSettings(max_validation_retries=-1)


def test_openrouter_rejects_non_openrouter_chat_endpoint_before_client_use() -> None:
    from src.config import OpenRouterSettings

    with pytest.raises(ValueError, match="endpoint"):
        OpenRouterSettings(endpoint="https://attacker.invalid/collect")
