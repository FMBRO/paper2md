"""Load settings from ``configs/config.yaml`` and merge CLI overrides."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Settings:
    input_dir: Path
    output_dir: Path
    engine: str = "marker"
    language: str = "eng"
    enable_ocr: bool = True
    force_ocr: bool = False
    workers: int = 1
    skip_existing: bool = False
    overwrite: bool = False
    continue_on_error: bool = True
    save_logs: bool = True


def load_settings(config_path: Path | str, overrides: dict[str, Any] | None = None) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    overrides = overrides or {}

    ocr = raw.get("ocr") or {}
    converter = raw.get("converter") or {}
    batch = raw.get("batch") or {}

    s = Settings(
        input_dir=Path(raw.get("input_dir", "input")),
        output_dir=Path(raw.get("output_dir", "output")),
        engine=converter.get("engine", "marker"),
        language=raw.get("language", "eng"),
        enable_ocr=ocr.get("enabled", True) is not False,
        force_ocr=bool(ocr.get("force", False)),
        workers=int(batch.get("workers", 1)),
        skip_existing=bool(batch.get("skip_existing", False)),
        overwrite=bool(batch.get("overwrite", False)),
        continue_on_error=bool(batch.get("continue_on_error", True)),
    )

    for key, value in overrides.items():
        if value is None:
            continue
        if not hasattr(s, key):
            raise KeyError(f"Unknown setting override: {key}")
        setattr(s, key, value)

    if isinstance(s.input_dir, str):
        s.input_dir = Path(s.input_dir)
    if isinstance(s.output_dir, str):
        s.output_dir = Path(s.output_dir)
    return s
