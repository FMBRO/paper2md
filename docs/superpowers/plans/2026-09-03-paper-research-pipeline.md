# paper2md research pipeline implementation plan

## Global constraints

- Extend FMBRO/paper2md in place and keep `python -m src.batch_convert` backward compatible.
- Support Python 3.12 on Windows. GUI and CLI must use the same service layer.
- Use test-driven development: each production behavior starts with a focused failing test, then the minimum implementation, then a green run.
- Keep full Markdown and structured documents local. Send only the validated Japanese summary to Notion.
- Read secrets only from `OPENROUTER_API_KEY` and `NOTION_API_KEY`; never persist or log them.
- OpenRouter defaults are `google/gemini-3.8-flash` for extraction and `openai/gpt-5.6-sol` for synthesis. Require JSON Schema support, `require_parameters`, ZDR, and disabled data collection. Do not silently switch model IDs.
- Enforce a conservative application-side paper budget, default USD 0.50, and persist actual `usage.cost` after each call.
- Use Zotero local API v3 at `http://localhost:23119/api/` with user ID 0. Existing Zotero items are read-only. Only non-Zotero inputs may create a regular parent item and stored PDF attachment.
- Use Notion API version `2026-03-11` and an existing `data_source_id`; validate but never mutate its schema.
- Keep stages idempotent and resumable. Avoid duplicate Zotero items, Notion pages, artifacts, and paid LLM calls.

## Task 1: Domain model, configuration, artifacts, and persistent job state

Implement the shared foundation.

- Add domain models for `InputKind`, `InputSpec`, `PaperMetadata`, `ArtifactBundle`, `PaperSummary`, evidence anchors, `JobState`, and `JobRecord`.
- Parse pipeline configuration while preserving all existing configuration behavior. Add output/state paths, research-interest default, Zotero settings, OpenRouter endpoint/models/privacy/budget settings, and Notion data-source/property mappings.
- Add a per-paper artifact manager producing `source.pdf`, `metadata.json`, `document.json`, `paper.md`, `figures/`, `summary.json`, `manifest.json`, and `logs/`, using temporary files plus atomic replacement for JSON/text writes.
- Add SQLite tables `papers`, `jobs`, and `llm_calls`, schema initialization, canonical identity lookup, state transitions, LLM call caching, cost accounting, and resumable job lookup at `<output>/paper2md.sqlite3`.
- Canonical identity priority is DOI, arXiv ID, Zotero `(library_id,item_key)`, then PDF SHA-256.
- Test validation, identity normalization, transitions, persistence, cached-call lookup, cost totals, atomic artifacts, and legacy configuration compatibility.

## Task 2: Input parsing and document acquisition

Implement external-input normalization and acquisition behind injectable HTTP clients.

- Recognize arXiv IDs/URLs, DOI strings/URLs, direct PDF URLs, local PDFs, `zotero://select/library/items/{KEY}`, explicit Zotero item keys, and Zotero collection keys.
- Acquire official arXiv metadata/PDF, Crossref DOI metadata, lawful direct PDF links exposed by metadata, direct PDF URLs, and local file copies.
- Validate downloaded responses as PDFs and compute SHA-256. A DOI with no accessible PDF becomes `needs_input`; never bypass a paywall or access control.
- Produce normalized `PaperMetadata` and a local source PDF without invoking conversion or LLM services.
- Test all input shapes, malformed values, Crossref/arXiv response parsing, PDF validation, local copying, inaccessible DOI behavior, and HTTP failures using deterministic transports.

## Task 3: Zotero local API integration

Implement Zotero item resolution, collection expansion, and non-Zotero upsert behavior.

- Diagnose availability of `http://localhost:23119/api/` and use Zotero API v3 with user ID 0.
- Resolve attachment keys directly; for parent items list children and select the only PDF. Multiple PDFs without `attachment_key` become `needs_input` before any LLM call.
- Resolve imported and linked local attachments through the file redirect endpoint and report missing/inaccessible files explicitly.
- For non-Zotero sources, find existing items by DOI/arXiv ID before creating a regular parent item plus stored PDF attachment. Existing Zotero-derived items remain read-only.
- Return parent/attachment keys and `zotero://select/library/items/{KEY}`.
- Test parent, attachment, collection, zero/one/multiple PDF cases, linked/imported paths, deduplication, creation payloads, unavailable Zotero, and API errors.

## Task 4: Structured conversion and quality gate

Wrap existing paper2md conversion without breaking batch conversion.

- Add a `Converter.convert(pdf_path, artifact_dir) -> ArtifactBundle` service that preserves `paper.md`, figures, logs, and conversion reports.
- Retain Marker structured output when available and normalize it into `document.json` with pages, sections, paragraphs, tables, equations, figures, captions, and source positions. Provide a Markdown-derived fallback when Marker JSON is unavailable.
- Inspect every page for text coverage. Use Marker auto-OCR normally and run OCRmyPDF only for broken/empty page text or an explicit force setting, avoiding double OCR.
- Add a quality gate for page coverage, character density, replacement/garbled characters, section hierarchy, malformed/empty tables, equation preservation, and figure/caption mismatches. Failed quality blocks LLM processing.
- Keep existing batch behavior and all original tests passing.
- Test structured normalization, fallback behavior, multi-page/mixed-text inspection, quality failures, and converter artifacts with lightweight PDF fixtures and mocked Marker subprocess boundaries.

## Task 5: OpenRouter structured summarization and budget guard

Implement resumable map/reduce summarization.

- Build section-aware chunks from `document.json`; never split tables, equations, or captions and retain page/section evidence spans.
- Use `https://openrouter.ai/api/v1/chat/completions`, extraction model `google/gemini-3.8-flash`, synthesis model `openai/gpt-5.6-sol`, `response_format.type=json_schema`, `provider.require_parameters=true`, `provider.zdr=true`, and provider data collection disabled.
- Validate all responses with Pydantic. Retry malformed structured output only a bounded number of times; never pass unvalidated output downstream.
- Generate Japanese `PaperSummary` fields for background, question, novelty, methods, datasets, results/metrics, strengths, limitations, takeaways, relevance score 1-5, rationale, keywords, and evidence.
- Calculate a conservative worst-case cost from current model pricing and configured token limits before calls. Fail closed if pricing is unavailable or the remaining USD 0.50 default budget cannot authorize the request. Persist actual usage, cost, model, provider, request hash, and response artifact. Reuse successful cached calls on resume.
- Test payload privacy/schema fields, chunk boundaries, validation/repair, cost authorization, budget exhaustion, usage recording, cached resume, and API failures.

## Task 6: Notion validation and idempotent summary upsert

Implement the existing-data-source integration.

- Use Notion API version `2026-03-11`, `data_source_id`, and Enhanced Markdown page content.
- Validate these mapped properties without changing the schema: Title, Authors, Published Date, DOI, arXiv ID, Source URL, Zotero Link, Zotero Item Key, Processing Status, Relevance Score, Score Rationale, Topics, AI Keywords, Imported At, Model / Prompt Version.
- Render only the validated Japanese summary in the page body.
- Locate existing pages by stored Notion page ID, then DOI, arXiv ID, or Zotero item key; update exact matches and create only when none exists.
- Honor `Retry-After` for 429 and bounded exponential backoff for network/5xx failures. Authentication, permission, and schema errors are non-retryable.
- Test schema diagnostics, property/body payloads, lookup precedence, update/create behavior, retries, and non-retryable errors.

## Task 7: Pipeline orchestration and CLI

Wire all services into one resumable `PipelineService` and expose the planned CLI.

- Implement `PipelineService.ingest(InputSpec, max_cost_usd=0.50) -> JobRecord` and `resume(job_id, max_cost_usd=None) -> JobRecord`.
- Execute stages `queued`, `acquiring`, `zotero_sync`, `converting`, `quality_check`, `extracting`, `synthesizing`, `notion_sync`, `completed`, plus `needs_input`, `budget_exceeded`, and `failed`.
- Persist each checkpoint transactionally. Skip valid completed stages on resume and never repeat a successful paid call.
- Implement `python -m src.cli ingest <source>`, collection ingestion, `resume`, `status`, research-interest override, attachment selection, `--only-unprocessed`, and cost override.
- Preserve `python -m src.batch_convert` behavior.
- Test successful local and mocked remote pipelines, state/error mappings, resume behavior, collection batching, duplicate avoidance, CLI parsing/output/exit codes, and missing configuration diagnostics.

## Task 8: GUI, documentation, and end-to-end verification

Extend the Tkinter application while keeping legacy batch conversion accessible.

- Add pipeline input type/value, local browse, Zotero collection/item, attachment selection, research-interest override, budget, job stage, actual cost, logs, status, diagnostics, and resume controls.
- Run the pipeline off the Tk main thread and route progress through the existing event queue pattern.
- Add setup and troubleshooting documentation for OpenRouter, Notion integration/data source, Zotero local API, models, research-interest configuration, privacy, budget behavior, local artifacts, CLI, GUI, and license boundaries.
- Add deterministic end-to-end tests proving arXiv-style and existing-Zotero flows produce one local artifact set and one Notion upsert, multiple attachments incur no LLM cost, and resume does not duplicate side effects.
- Run the full test suite and compile/import smoke checks with pristine output.
