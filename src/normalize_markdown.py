"""Normalize Marker/MinerU/Docling Markdown output (spec §5.9)."""
from __future__ import annotations

import re
from pathlib import Path

_ABSTRACT_RE = re.compile(r"^(#{1,6}[^\S\n]*)?abstract[^\S\n]*$", re.IGNORECASE | re.MULTILINE)


def normalize_abstract_heading(markdown: str) -> str:
    return _ABSTRACT_RE.sub("## Abstract", markdown)


# Stubs — filled in below as each TDD cycle completes

_REFS_RE = re.compile(
    r"^(?:#{1,6}[^\S\n]*|\*\*)?references\*?\*?[^\S\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


def normalize_references_heading(markdown: str) -> str:
    return _REFS_RE.sub("## References", markdown)


_EQ_BRACKET = re.compile(r"\\\[\s*\n?(.*?)\n?\s*\\\]", re.DOTALL)


def normalize_equations(markdown: str) -> str:
    """Convert ``\\[ ... \\]`` blocks to ``$$ ... $$``. Leaves existing ``$$`` blocks alone."""
    return _EQ_BRACKET.sub(lambda m: f"$$\n{m.group(1).strip()}\n$$", markdown)


_FIG_CAP = re.compile(
    r"^(?:\*\*)?(?:Figure|Fig\.)\s+(\d+)(?:\*\*)?[:\.]\s+(.+)$",
    re.MULTILINE,
)
_TABLE_CAP = re.compile(
    r"^(?:\*\*)?Table\s+(\d+)(?:\*\*)?[:\.]\s+(.+)$",
    re.MULTILINE,
)


def normalize_captions(markdown: str) -> str:
    markdown = _FIG_CAP.sub(lambda m: f"**Figure {m.group(1)}.** {m.group(2)}", markdown)
    markdown = _TABLE_CAP.sub(lambda m: f"**Table {m.group(1)}.** {m.group(2)}", markdown)
    return markdown


def normalize_markdown(input_path: Path | str, output_path: Path | str) -> Path:
    """Apply every §5.9 rule and write the result to ``output_path``."""
    text = Path(input_path).read_text()
    text = normalize_abstract_heading(text)
    text = normalize_references_heading(text)
    text = normalize_equations(text)
    text = normalize_captions(text)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)
    return output_path
