"""Quality evaluation — counts structural elements and writes a Markdown report (spec §10)."""
from __future__ import annotations

import re
from pathlib import Path


_SECTION = re.compile(r"^##\s+\S.*$", re.MULTILINE)
_BLOCK_EQ = re.compile(r"^\$\$\s*$", re.MULTILINE)
_FIG_LINK = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_FIG_CAP = re.compile(r"^\*\*Figure\s+\d+\.\*\*", re.MULTILINE)
_TABLE_CAP = re.compile(r"^\*\*Table\s+\d+\.\*\*", re.MULTILINE)
_REFS_HEADING = re.compile(r"^##\s+References\s*$", re.MULTILINE)
_REF_ENTRY = re.compile(r"^\[\d+\]\s+", re.MULTILINE)


def evaluate_markdown(markdown_path: Path | str) -> dict:
    text = Path(markdown_path).read_text(encoding="utf-8")
    refs_match = _REFS_HEADING.search(text)
    refs_body = text[refs_match.end():] if refs_match else ""
    return {
        "section_count": len(_SECTION.findall(text)),
        "block_equation_count": len(_BLOCK_EQ.findall(text)) // 2,
        "figure_count": len(_FIG_LINK.findall(text)),
        "figure_caption_count": len(_FIG_CAP.findall(text)),
        "table_caption_count": len(_TABLE_CAP.findall(text)),
        "has_references_section": refs_match is not None,
        "reference_count": len(_REF_ENTRY.findall(refs_body)),
    }


def _verdict(condition: bool, ok_note: str, warn_note: str) -> tuple[str, str]:
    return ("OK", ok_note) if condition else ("Warning", warn_note)


def write_quality_report(
    *,
    markdown_path: Path | str,
    source_pdf: Path | str,
    logs_dir: Path | str,
    engine: str,
    has_text_layer: bool,
    ocr_used: bool,
    page_count: int,
) -> Path:
    metrics = evaluate_markdown(markdown_path)
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    headings_v, headings_note = _verdict(
        metrics["section_count"] >= 1,
        f"{metrics['section_count']} sections detected",
        "No `##` sections detected",
    )
    eq_v, eq_note = _verdict(
        metrics["block_equation_count"] >= 1,
        f"{metrics['block_equation_count']} block equations",
        "No block equations detected",
    )
    fig_v, fig_note = _verdict(
        metrics["figure_count"] == metrics["figure_caption_count"]
        or metrics["figure_count"] == 0,
        f"{metrics['figure_count']} figures / {metrics['figure_caption_count']} captions",
        f"figure count {metrics['figure_count']} != caption count {metrics['figure_caption_count']}",
    )
    refs_v, refs_note = _verdict(
        metrics["has_references_section"],
        f"{metrics['reference_count']} references detected",
        "No `## References` section",
    )

    body = (
        "# Conversion Quality Report\n\n"
        "## Summary\n\n"
        f"- Input PDF: {source_pdf}\n"
        f"- Pages: {page_count}\n"
        f"- Text layer: {str(has_text_layer).lower()}\n"
        f"- OCR used: {str(ocr_used).lower()}\n"
        f"- Engine: {engine}\n\n"
        "## Checks\n\n"
        "| Check | Result | Notes |\n"
        "|---|---|---|\n"
        f"| Headings | {headings_v} | {headings_note} |\n"
        f"| Equations | {eq_v} | {eq_note} |\n"
        f"| Figures | {fig_v} | {fig_note} |\n"
        f"| Tables | OK | {metrics['table_caption_count']} table captions |\n"
        f"| References | {refs_v} | {refs_note} |\n"
    )

    out = logs_dir / "quality_report.md"
    out.write_text(body, encoding="utf-8")
    return out
