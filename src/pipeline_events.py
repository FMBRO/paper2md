"""Structured progress events shared by the CLI pipeline and GUI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


EventKind = Literal[
    "batch_started",
    "pdf_started",
    "stage_changed",
    "log",
    "pdf_succeeded",
    "pdf_failed",
    "pdf_skipped",
    "batch_finished",
]


@dataclass(frozen=True)
class PipelineEvent:
    """One observable state change during a batch conversion."""

    kind: EventKind
    pdf_name: str | None = None
    stage: str | None = None
    current: int | None = None
    total: int | None = None
    message: str = ""


EventCallback = Callable[[PipelineEvent], None]

