from pathlib import Path

from src.evaluate_quality import evaluate_markdown, write_quality_report


SAMPLE_MD = """# Title

## Abstract

Body of abstract.

## 1. Introduction

Some text with $x$ inline math.

$$
y = Ax
$$

![overview](figures/figure_001.png)

**Figure 1.** Overview of the proposed method.

## 2. Method

| a | b |
|---|---|
| 1 | 2 |

**Table 1.** A table.

## References

[1] First reference.
[2] Second reference.
"""


def test_evaluate_markdown_counts(tmp_path: Path) -> None:
    md = tmp_path / "paper.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    result = evaluate_markdown(md)
    assert result["section_count"] >= 3   # Abstract + Introduction + Method + References
    assert result["block_equation_count"] == 1
    assert result["figure_count"] == 1
    assert result["figure_caption_count"] == 1
    assert result["table_caption_count"] == 1
    assert result["reference_count"] == 2
    assert result["has_references_section"] is True


def test_write_quality_report_creates_markdown(tmp_path: Path) -> None:
    md = tmp_path / "paper.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    logs_dir = tmp_path / "logs"
    report = write_quality_report(
        markdown_path=md,
        source_pdf=tmp_path / "paper.pdf",
        logs_dir=logs_dir,
        engine="marker",
        has_text_layer=True,
        ocr_used=False,
        page_count=12,
    )
    assert report == logs_dir / "quality_report.md"
    body = report.read_text(encoding="utf-8")
    assert body.startswith("# Conversion Quality Report")
    assert "Engine: marker" in body
    assert "Pages: 12" in body
