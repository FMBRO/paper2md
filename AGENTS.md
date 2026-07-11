# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Current state

This repository contains an initial Python implementation under `src/`, a pytest suite under `tests/`, and project documentation under `docs/`. Python dependencies and the virtual environment are managed with uv through `pyproject.toml` and `uv.lock`.

- [docs/spec.md](docs/spec.md) — the design specification for the pipeline (written in Japanese). This is the source of truth for what is being built.
- [docs/Marker.md](docs/Marker.md), [docs/MinerU.md](docs/MinerU.md), [docs/Docling.md](docs/Docling.md), [docs/OCRmyPDF.md](docs/OCRmyPDF.md), [docs/pix2tex.md](docs/pix2tex.md) — verbatim upstream documentation for each candidate tool, kept locally for reference. Do not treat these as project documentation; they are third-party READMEs.

When the user asks to implement something here, they almost always mean: build the pipeline described in `spec.md`. Read or re-read that spec before proposing a design — it specifies CLI flags, directory layout, fallback chains, and the quality-report format.

## What this project is

A batch pipeline that converts English academic-paper PDFs (both text-layer and scanned/image PDFs) into Markdown suitable for downstream LLM / RAG / translation use. Per-PDF output is written to its own directory so figures, tables, page images, and logs never get mixed across papers.

## Planned architecture (from spec.md)

The pipeline branches on whether the input PDF has a text layer:

```
PDF
├─ has text layer  → run converter directly
└─ no text layer   → OCRmyPDF (adds invisible text layer) → run converter
```

Text-layer detection: PyMuPDF (`fitz`) reads the first ~3 pages and checks for ≥100 extracted characters. See `spec.md` §5.1.

**Engine priority** (configurable, but defaults matter):
1. **Marker** — default for both text PDFs and post-OCR PDFs
2. **MinerU** — alternate
3. **Docling** — alternate / fallback after Marker+MinerU fail
4. **Nougat** — math-heavy fallback
5. **OCRmyPDF** — only as a pre-processor for image PDFs, not a converter
6. **pix2tex** — only as a math-OCR rescue step when the chosen engine's equation output is poor

The fallback order on failure (§11) is: Marker → MinerU → Docling → OCRmyPDF+Docling. When the user implements failure handling, follow this chain.

**Planned source layout** (`src/`, per §6) splits one module per responsibility: `batch_convert.py` (CLI entry), `inspect_pdf.py` (text-layer check), `run_ocr.py`, `run_marker.py` / `run_mineru.py` / `run_docling.py` (engine wrappers), `extract_figures.py`, `normalize_markdown.py`, `evaluate_quality.py`. Configuration lives in `configs/config.yaml`.

**Planned output layout** is rigid — preserve it exactly when implementing:

```
output/{paper_name}/
├── paper.md          ← primary deliverable
├── paper.json        ← intermediate structured form for post-processing
├── paper_ocr.pdf     ← only present when OCR ran
├── figures/  tables/  pages/
└── logs/
    ├── pipeline.log
    ├── text_layer_check.json
    ├── conversion_report.json
    └── quality_report.md
```

Plus `output/batch_summary.json` at the top level.

## Non-obvious design rules from the spec

These come from `spec.md` and are easy to miss:

- **Per-PDF failure must not abort the batch.** Record the failure in `batch_summary.json["failed_files"]` and continue. See §9.2 and §11.5.
- **Default `--workers 1`.** Marker and OCR use GPU/memory; only raise after profiling. §7.5, §17.4.
- **Markdown normalization is a distinct stage**, not a per-engine concern: `#` for title, `## Abstract`, `## References`, `$$ ... $$` for block math, `**Figure N.**` / `**Table N.**` captions, relative image paths. §5.9.
- **Complex tables fall back to images**, not broken Markdown tables. Triggers: merged cells, multi-row headers, heavy math, ambiguous reading order. §5.8.3.
- **The spec is in Japanese.** Write code identifiers, comments, log messages, and CLI help text in English (the pipeline targets English papers), but when explaining decisions back to the user, mirror whichever language they used.

## Commands

Use uv for setup and all Python commands:

```bash
uv sync
uv run pytest
uv run python -m src.batch_convert --input_dir input --output_dir output --engine marker
uv run python -m src.batch_convert ... --enable_ocr
uv run python -m src.batch_convert ... --force_ocr
uv run python -m src.batch_convert ... --skip_existing
uv run python -m src.evaluate_quality --markdown output/paper_a/paper.md --source input/paper_a.pdf
uv run python -m src.gui
```

## When proposing changes to spec.md

The spec is detailed and internally cross-referenced (section numbers are used as anchors in the prose). When editing it:

- Preserve the section numbering scheme — other sections reference it.
- Keep the directory-tree code blocks consistent with §2.2 and §6; they're the contract the implementation will follow.
- The fallback chain (§11), CLI surface (§7), and config schema (§8) are tightly coupled — change one and check the other two.
