# TIP Pipeline Recovery Design

## Goal

Resume all Zotero `TIP` papers without weakening evidence verification, while
repairing deterministic conversion false positives, enabling native OCR, using
the RTX 5090 for Marker, and replacing the repeatedly non-compliant extraction
model.

## Current evidence

- All 15 previously missing PDFs now resolve to one readable Zotero attachment.
- OCRmyPDF 16.13.0 is installed, but Tesseract is absent, causing exit code 3.
- The NVIDIA RTX 5090 and driver are visible, but the project has
  `torch 2.13.0+cpu`; Marker therefore selects CPU.
- Seven quality failures are caused by Marker heading jumps and an invalid
  global one-figure/one-caption assumption, not by missing source pages.
- Three structured-output failures occurred after bounded retries with
  `google/gemini-2.5-flash`.

## Design

### OCR runtime

Install 64-bit Tesseract 5 for Windows and make the wrapper resolve Tesseract
from PATH or standard Windows installation directories. Pass the resolved
directory only to the OCRmyPDF child process, avoiding a process-wide PATH
mutation. Preserve the current optional `unpaper` downgrade behavior.

### GPU conversion

Replace the CPU PyTorch wheel with an official Windows CUDA wheel compatible
with the installed NVIDIA driver. Marker already auto-selects CUDA through
`torch.cuda.is_available()`; set `TORCH_DEVICE=cuda` for pipeline subprocesses
so a missing CUDA runtime fails visibly instead of silently falling back to
CPU. Tesseract/OCRmyPDF remains CPU-bound.

### Quality normalization

Normalize section levels before writing `document.json`: the first section is
level 1 and later levels may descend by at most one level while retaining the
relative hierarchy detected by Marker. Do not change heading text.

Match figure captions by figure label and page proximity. Decorative image
blocks and compound/multi-panel figures do not require one-to-one global
counts. The gate still blocks page loss, empty/low-density text, garbled text,
malformed tables, empty equations, and a caption referring to a missing
non-decorative figure.

### Structured output model

Use `openai/gpt-5.6-luna` for extraction, reduction, and bounded structured
repair. Keep `openai/gpt-5.6-sol` for final synthesis. Luna is one of the three
user-approved model IDs and supports JSON Schema structured output. Existing
Pydantic validation and exact-source evidence verification remain mandatory;
all quotes must be copied verbatim and be no longer than 4000 characters.

### Artifact linkage

Keep the existing linkage contract: SQLite and `manifest.json` bind
`paper.md` hashes to the Zotero parent/attachment keys and Notion page ID;
Notion stores the validated Japanese summary plus Zotero link and item key.
Do not write Markdown attachments or Notion backlinks into existing Zotero
items because Zotero-derived records are intentionally read-only.

### Recovery

After focused and full verification, resume the 15 newly resolvable items, 13
OCR failures, 7 quality-gate failures, and 3 structured-output failures. Keep
the USD 3.00 per-paper ceiling. Record any remaining PDF/OCR/quality/structured
failure in separate lists.

## Safety and acceptance

- Add each regression test before its production change and observe the
  expected failure.
- Do not weaken exact evidence verification or permit unvalidated output into
  Notion.
- Do not repeat paid calls already cached and validated.
- A true quality failure remains fail-closed and is not uploaded to Notion.
- Verify Tesseract, CUDA visibility, one OCR smoke fixture, the three previously
  failed structured papers, then the full batch.
