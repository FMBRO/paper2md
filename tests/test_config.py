from pathlib import Path

from src.config import Settings, load_settings


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
