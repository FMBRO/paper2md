# README Mode and GUI Option Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document every CLI engine mode and Desktop GUI checkbox accurately in `README.md`.

**Architecture:** Add two compact Markdown tables adjacent to the existing CLI and GUI instructions. Treat source code as the authority for availability, option precedence, and external-tool behavior.

**Tech Stack:** Markdown, Python CLI and Tkinter implementation references

## Global Constraints

- Marker is the only conversion engine currently wired into `convert_one`.
- Force OCR takes precedence over text-layer detection and Auto OCR.
- Deskew and Clean affect only OCR runs.
- Clean requires `unpaper`; without it, paper2md warns and continues without cleaning.
- Do not change runtime code.

---

### Task 1: Document CLI engines and GUI checkboxes

**Files:**
- Modify: `README.md`
- Verify: `src/batch_convert.py`, `src/convert_one.py`, `src/gui.py`, `src/run_ocr.py`

**Interfaces:**
- Consumes: CLI engine choices and GUI option behavior from source code.
- Produces: User-facing tables under `## Run` and `## Desktop GUI`.

- [ ] **Step 1: Add the engine table**

Document Marker as available and MinerU, Docling, and Nougat as planned but not wired. State that selecting an unwired engine records a per-PDF failure and the batch continues.

- [ ] **Step 2: Add the GUI checkbox table**

Document Auto OCR, Force OCR, Deskew, Clean, and Skip existing, including OCR-only applicability and the `unpaper` fallback.

- [ ] **Step 3: Verify the Markdown diff**

Run: `git diff --check -- README.md`

Expected: exit code 0 with no whitespace errors.

- [ ] **Step 4: Verify labels and implementation status**

Run: `rg -n "Auto OCR|Force OCR|Deskew|Clean|Skip existing|marker|mineru|docling|nougat" README.md src/gui.py src/batch_convert.py src/convert_one.py`

Expected: every documented label and engine maps to its source definition.
