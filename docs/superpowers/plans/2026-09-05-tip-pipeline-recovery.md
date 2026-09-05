# TIP Pipeline Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Tesseract and CUDA conversion, eliminate quality-gate false positives, switch structured extraction to GPT-5.6 Luna, and resume every pending TIP paper.

**Architecture:** Keep the deterministic fail-closed pipeline. Repair Marker structure before quality evaluation, resolve native OCR tools explicitly for child processes, and let Marker use a verified CUDA PyTorch build. Use GPT-5.6 Luna for extraction/reduction while retaining strict local schema and verbatim-evidence validation.

**Tech Stack:** Python 3.12, pytest, OCRmyPDF, Tesseract 5, Marker/Surya, PyTorch CUDA, SQLite, OpenRouter, Notion API, Zotero local API.

**Spec:** `docs/superpowers/specs/2026-09-05-tip-pipeline-recovery-design.md`

## Global Constraints

- Evidence quotes must preserve the source text exactly and be at most 4000 characters.
- Strict evidence verification, Japanese narrative validation, JSON Schema validation, and Pydantic validation remain enabled.
- The per-paper cost ceiling is USD 3.00.
- Existing Zotero-derived items remain read-only.
- True page/text/table/equation quality failures remain fail-closed.
- Tests must be written and observed failing before production changes.

---

### Task 1: Windows OCR tool resolution

**Files:**
- Modify: `src/subprocess_utils.py`
- Modify: `src/run_ocr.py`
- Test: `tests/test_subprocess_utils.py`
- Test: `tests/test_run_ocr.py`

**Interfaces:**
- Consumes: `run_streaming_command(command, on_output)`.
- Produces: `run_streaming_command(command, on_output, *, env=None)` and `ocr_environment()` for child-only PATH augmentation.

- [ ] **Step 1: Write failing tests** proving an explicit environment reaches `subprocess.Popen`, a standard Windows Tesseract directory is appended only for OCR, and missing Tesseract raises a diagnostic before OCRmyPDF runs.
- [ ] **Step 2: Run RED tests:** `uv run --frozen pytest tests/test_subprocess_utils.py tests/test_run_ocr.py -q` and confirm failures are caused by the missing `env`/tool-resolution behavior.
- [ ] **Step 3: Implement minimal resolution** using `shutil.which`, `ProgramFiles`, and `ProgramFiles(x86)` candidates; pass a copied child environment to OCRmyPDF.
- [ ] **Step 4: Run GREEN tests:** `uv run --frozen pytest tests/test_subprocess_utils.py tests/test_run_ocr.py -q`.

### Task 2: Deterministic section and figure/caption normalization

**Files:**
- Modify: `src/document_normalizer.py`
- Modify: `src/quality_gate.py`
- Test: `tests/test_document_normalizer.py`
- Test: `tests/test_quality_gate.py`

**Interfaces:**
- Consumes: Marker `document.json` sections, figures, captions, pages, and ordinals.
- Produces: valid normalized heading levels and `evaluate_document_quality(...)` results based on labeled/page-local figure relationships.

- [ ] **Step 1: Write failing heading regression tests** using the observed Title→Abstract H4 and H1→H2→H4 sequences; expected normalized levels are `1,2` and `1,2,3` without title changes.
- [ ] **Step 2: Run heading RED tests** and confirm the raw level jump remains.
- [ ] **Step 3: Implement level clamping** during deterministic normalization and materialization.
- [ ] **Step 4: Run heading GREEN tests**.
- [ ] **Step 5: Write failing figure regressions** for decorative images, multi-panel figures, table captions, and a numbered caption whose image is on the same/adjacent page.
- [ ] **Step 6: Run figure RED tests** and confirm the global count comparison blocks valid documents.
- [ ] **Step 7: Implement page/label-aware matching** while preserving a blocking result for a genuinely missing numbered figure.
- [ ] **Step 8: Run quality GREEN tests:** `uv run --frozen pytest tests/test_document_normalizer.py tests/test_quality_gate.py -q`.

### Task 3: Structured model migration

**Files:**
- Modify: `configs/config.yaml`
- Modify: `tests/test_config.py`
- Test: `tests/test_openrouter.py`

**Interfaces:**
- Consumes: `OpenRouterSettings.extraction_model` and existing structured-output request builder.
- Produces: extraction/reduction/repair requests using `openai/gpt-5.6-luna`; final synthesis remains `openai/gpt-5.6-sol`.

- [ ] **Step 1: Write a failing configuration/request test** that loads the production config and asserts Luna is used for extraction/reduction with `max_completion_tokens` and JSON Schema response format.
- [ ] **Step 2: Run RED test:** `uv run --frozen pytest tests/test_config.py tests/test_openrouter.py -q` and confirm production config still selects Gemini 2.5.
- [ ] **Step 3: Change the production extraction model** to `openai/gpt-5.6-luna`; keep synthesis and all validators unchanged.
- [ ] **Step 4: Run GREEN tests:** `uv run --frozen pytest tests/test_config.py tests/test_openrouter.py -q`.

### Task 4: CUDA runtime and OCR installation

**Files:**
- Modify: `pyproject.toml` only if a platform-specific PyTorch source is required.
- Modify: `uv.lock` through `uv lock`, never by hand.

**Interfaces:**
- Produces: `torch.cuda.is_available() == True`, an RTX 5090 CUDA device, and callable `tesseract`/`ocrmypdf` commands.

- [ ] **Step 1: Install native 64-bit Tesseract 5** from a trusted Windows distribution and verify `tesseract --version` plus `tesseract --list-langs` includes `eng`.
- [ ] **Step 2: Select an official PyTorch Windows CUDA wheel** compatible with the driver and Marker dependencies, update the locked environment, and sync it.
- [ ] **Step 3: Verify CUDA:** `uv run --frozen python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))"` must report CUDA, `True`, and the RTX 5090.
- [ ] **Step 4: Run an OCR smoke conversion** on a controlled image-only PDF and verify OCRmyPDF exits 0 with extractable output text.
- [ ] **Step 5: Run one Marker smoke conversion** and observe nonzero GPU utilization plus a valid Markdown artifact.

### Task 5: Verification and batch recovery

**Files:**
- Update: `output/paper2md.sqlite3` through pipeline operations only.
- Create: recovery status reports under `output/reports/`.

**Interfaces:**
- Consumes: fixed runtime, persisted checkpoints, TIP collection and five subcollections.
- Produces: completed Notion pages/manifests and separate unresolved PDF, OCR, quality, and structured-failure lists.

- [ ] **Step 1: Run the focused regression suite** for OCR, normalization, quality, config, OpenRouter, pipeline, and Notion.
- [ ] **Step 2: Run the full suite:** `uv run --frozen pytest -q` and require zero failures.
- [ ] **Step 3: Re-run the three known structured failures** (`WKMGNQP4`, `XU5BWD37`, `WPDIAZHX`) within their remaining USD 3.00 ceilings and verify strict evidence validation.
- [ ] **Step 4: Resume the 13 OCR failures and 7 quality failures** without deleting validated paid-call cache entries.
- [ ] **Step 5: Ingest the 15 newly resolvable Zotero items** from their parent keys.
- [ ] **Step 6: Reconcile all 40 TIP papers** against SQLite and Notion by Zotero item key; do not create duplicates.
- [ ] **Step 7: Write separate final reports** for missing PDFs, Tesseract/OCR failures, quality failures, structured failures, completions, and total incremental cost.
- [ ] **Step 8: Verify manifests** contain `paper.md` hashes, Zotero parent/attachment keys, and Notion page IDs for every completed paper.
