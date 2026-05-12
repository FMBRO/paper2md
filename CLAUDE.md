# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

This repository is **spec-only**. There is no source code, no `requirements.txt`, no `README.md`, no test suite, and it is not a git repository. The only content is `docs/`:

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

There are no build/lint/test commands yet — nothing exists to run. When the implementation lands, the spec defines the CLI surface (§7):

```bash
python src/batch_convert.py --input_dir input --output_dir output --engine marker
python src/batch_convert.py ... --enable_ocr           # auto-OCR when no text layer
python src/batch_convert.py ... --force_ocr            # OCR even if text layer exists
python src/batch_convert.py ... --engine {marker|mineru|docling|nougat}
python src/batch_convert.py ... --skip_existing        # resume failed PDFs only
python src/evaluate_quality.py --markdown output/paper_a/paper.md --source input/paper_a.pdf
```

Add the actual build/test/lint commands to this section once `requirements.txt`, tests, or a linter config exist.

## When proposing changes to spec.md

The spec is detailed and internally cross-referenced (section numbers are used as anchors in the prose). When editing it:

- Preserve the section numbering scheme — other sections reference it.
- Keep the directory-tree code blocks consistent with §2.2 and §6; they're the contract the implementation will follow.
- The fallback chain (§11), CLI surface (§7), and config schema (§8) are tightly coupled — change one and check the other two.
