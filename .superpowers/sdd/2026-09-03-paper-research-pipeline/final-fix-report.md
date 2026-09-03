# paper2md research pipeline — final fix wave report

Date: 2026-09-03
Branch: `codex/paper-research-pipeline`
Starting HEAD: `7f9804c`
Baseline: `256 passed in 10.62s`
Final: `302 passed in 15.71s`

## Outcome

All four Critical findings, all seven Important findings, and all five listed
Minor findings are fixed. The implementation retains the existing privacy,
budget, lease fencing, idempotency, legacy batch, GUI, and no-live-call
guarantees. No model was downloaded or executed and no live/paid external call
was made. Marker, OpenRouter, and Zotero compatibility is covered by checked-in
contract fixtures and deterministic transports.

## Numbered finding disposition

1. **Critical 1 — real Marker 1.10.2 contract: fixed.**
   `src/run_marker.py` explicitly requests the JSON renderer, accepts the
   renderer's `p.json` plus `p_meta.json`, materializes compatible multi-page
   Markdown, and preserves the legacy Markdown fallback. `src/document_normalizer.py`
   recognizes recursive `Document`/`Page`/group/leaf `children`, reads
   `page_stats`, carries page IDs, block IDs, polygons/bounding boxes, HTML and
   typed blocks, and emits stable renderer ordinals. `src/converter.py` prefers
   that explicit renderer artifact. The fixture in
   `tests/fixtures/marker-1.10.2/` validates directly against Marker 1.10.2's
   installed `JSONOutput` Pydantic model without loading models.

2. **Critical 2 — OpenRouter provider contract: fixed.**
   `src/openrouter.py` sends `X-OpenRouter-Metadata: enabled`, no longer requires
   an undocumented top-level provider, parses provider labels permissively, and
   resolves a missing label through authenticated
   `GET https://openrouter.ai/api/v1/generation?id=...`. Standard chat and
   generation response fixtures live in `tests/fixtures/openrouter/`.

3. **Critical 3 — ambiguous OpenRouter dispatch window: fixed.**
   `src/job_store.py` migrates/persists `dispatch_state` and
   `authorized_amount_usd`. `begin_llm_dispatch` atomically fences the request,
   records the attempt, and marks the worst-case reservation unresolved before
   the HTTP POST. Transport ambiguity and settlement failure retain the
   unresolved row/reservation; resume fails closed instead of resending.
   Confirmed non-2xx responses settle at zero. Stable logical cache keys,
   request leases, budget leases, heartbeats, and paper-wide budgets remain in
   force.

4. **Critical 44 — durable Zotero multi-step writes: fixed.**
   `src/job_store.py` adds `zotero_operations` and append-only operation events.
   `src/zotero.py` derives a stable operation ID from library, canonical
   identity, and PDF SHA-256; persists predetermined parent/attachment keys and
   separate 64-character write tokens; checkpoints authorization, parent,
   attachment, upload authorization, upload, registration, and completion
   before/after their side effects; and reconciles exact planned items on
   resume. Existing DOI/arXiv parents enter the same durable operation and have
   a missing planned PDF attachment completed rather than returning parent-only.
   New writes fail closed without an operation store. DOI, arXiv, and SHA-only
   cases are response-loss tested. API authorization keys are never persisted.

5. **Important 1 — OCR mode selection: fixed.**
   `src/converter.py` selects `redo` for sparse/garbled nonempty pages,
   `skip_text` only for mixed documents' truly textless pages, `auto` for fully
   textless documents, and `force` only for explicit force. `src/run_ocr.py`
   maps these to `--redo-ocr`, `--skip-text`, no text-policy flag, and
   `--force-ocr` respectively.

6. **Important 2 — complete serialized chunk budget: fixed.**
   `src/openrouter.py` packs against the conservative UTF-8 size of the complete
   request, including messages, research interest, provenance, provider policy,
   and JSON schema. Oversized paragraphs split deterministically at
   sentence/line/word boundaries with fragment provenance. Tables, equations,
   and captions stay atomic and fail closed if one cannot fit.

7. **Important 3 — reading order: fixed.**
   Recursive renderer order is persisted as a global ordinal and consumed
   directly across mixed block types. Legacy documents without an ordinal fall
   back to `(page, y, x)` rather than page/X ordering.

8. **Important 4 — canonical staging/reuse/locking/force: fixed.**
   `src/pipeline.py`, `src/job_store.py`, and `src/artifacts.py` acquire into
   `papers/.staging`, transactionally resolve/merge the canonical paper and
   completed/resumable job, atomically promote the source, and serialize work
   with a fenced per-paper lease. Equivalent URL spellings and concurrent
   ingests share one job/generation; waiting callers also receive the same
   terminal failure without rerunning. An identity-less attachment-selection
   resume adopts an already-completed canonical generation. `--force-reprocess`
   is the only ordinary ingest path that creates a distinct generation/root and
   is forwarded for single and collection CLI paths. The reserved `.staging`
   parent may remain, but E2E tests require it to be empty and exclude it from
   artifact-root counts.

9. **Important 5 — suffixless HTTP(S) PDFs: fixed.**
   `src/acquisition.py` classifies remaining HTTP(S) values after DOI/arXiv
   recognition as `PDF_URL`; acquisition still requires successful status and
   PDF magic.

10. **Important 6 — Japanese narrative validation: fixed.**
    `src/research_models.py` provides one deterministic NFKC-normalized
    Japanese-script minimum/ratio validator. It discounts citations, URLs,
    DOI/arXiv identifiers, measurements, and technical tokens. Both Pydantic
    OpenRouter boundaries and the Notion write boundary reject mostly-English
    prose with a token Japanese character while accepting normal Japanese prose
    containing technical English terms.

11. **Important 7 — Zotero/Notion dates: fixed.**
    `src/zotero.py` prefers `meta.parsedDate`, normalizes seasons, year,
    year-month, and full date to valid ISO precision (including rejecting year
    zero and invalid months), and retains raw local text. `src/notion.py` sends
    only a validated ISO value; otherwise it omits the date and exposes a
    sanitized diagnostic.

12. **Minor 1 — reuse already-read local bytes: fixed.**
    Local acquisition validates, hashes, and persists the single byte buffer via
    `ArtifactManager.write_source_pdf`; a volatile-source test proves no second
    source read is required.

13. **Minor 2 — Zotero header/file URL normalization: fixed.**
    Header lookup is case-insensitive. `file_url_to_path` preserves Windows
    drive paths, UNC netlocs, and POSIX roots with percent decoding.

14. **Minor 3 — central Zotero key normalization: fixed.**
    `InputSpec.__post_init__` trims and uppercases valid item, collection, and
    attachment keys, so CLI, persisted resume, and programmatic callers share
    one normalization boundary.

15. **Minor 4 — GUI job ID preservation: fixed.**
    `ResearchViewModel.begin_job_operation` records the typed ID before async
    resume/status work; a later worker error changes status without erasing the
    user's identifier.

16. **Minor 5 — prompt-schema provenance: fixed.**
    Explicit extraction/synthesis schema constants are included alongside the
    configurable model IDs in Notion `Model / Prompt Version` and in manifest
    `prompt_schema_versions`.

## Files changed

Production and documentation:

- `src/acquisition.py`, `src/artifacts.py`, `src/cli.py`
- `src/converter.py`, `src/document_normalizer.py`, `src/run_marker.py`,
  `src/run_ocr.py`
- `src/openrouter.py`, `src/job_store.py`, `src/pipeline.py`
- `src/zotero.py`, `src/notion.py`, `src/research_models.py`, `src/gui.py`
- `docs/research-pipeline.md`

Tests and fixtures:

- `tests/test_acquisition.py`, `tests/test_cli.py`, `tests/test_converter.py`,
  `tests/test_document_normalizer.py`, `tests/test_gui.py`,
  `tests/test_job_store.py` (existing coverage exercised), `tests/test_notion.py`,
  `tests/test_openrouter.py`, `tests/test_pipeline.py`,
  `tests/test_pipeline_e2e.py`, `tests/test_research_models.py`,
  `tests/test_run_marker.py`, `tests/test_run_ocr.py`, `tests/test_zotero.py`
- `tests/fixtures/marker-1.10.2/p.json`
- `tests/fixtures/marker-1.10.2/p_meta.json`
- `tests/fixtures/openrouter/chat_completion_standard.json`
- `tests/fixtures/openrouter/generation_metadata.json`
- `tests/fixtures/zotero/write_contract.json`

## TDD RED/GREEN evidence

All pytest invocations used the repository's locked site packages without
network access:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages').Path
.\.uv-python\cpython-3.12.13-windows-x86_64-none\python.exe -m pytest ... -p no:cacheprovider
```

1. Marker 1.10.2 contract, recursive normalization, converter preference, and
   OCR behavior:

   ```text
   RED: pytest -q tests\test_run_marker.py tests\test_document_normalizer.py tests\test_run_ocr.py tests\test_converter.py --basetemp=...\pytest-temp-marker-red -p no:cacheprovider
   6 failed, 19 passed in 0.33s

   RED (all-textless auto policy): pytest -q tests\test_run_ocr.py::test_run_ocrmypdf_auto_mode_leaves_text_policy_to_ocrmypdf tests\test_converter.py::test_converter_uses_auto_ocr_for_a_fully_textless_document --basetemp=...\pytest-temp-ocr-auto-red -p no:cacheprovider
   2 failed in 0.11s

   GREEN: pytest -q tests\test_run_marker.py tests\test_document_normalizer.py tests\test_run_ocr.py tests\test_converter.py --basetemp=...\pytest-temp-marker-green -p no:cacheprovider
   27 passed in 1.86s
   ```

2. Serialized-payload packing, stable paragraph splitting, atomic structured
   blocks, and reading order:

   ```text
   RED: pytest -q tests\test_openrouter.py::test_chunk_builder_uses_renderer_ordinal_across_mixed_block_types tests\test_openrouter.py::test_chunk_builder_falls_back_to_page_y_x_when_ordinal_is_absent tests\test_openrouter.py::test_chunk_builder_splits_oversized_paragraphs_only_at_stable_boundaries tests\test_openrouter.py::test_map_chunks_pack_against_complete_serialized_schema_budget tests\test_openrouter.py::test_atomic_table_fails_closed_when_schema_overhead_exceeds_input_budget --basetemp=...\pytest-temp-chunks-red -p no:cacheprovider
   5 failed in 0.50s

   GREEN: same node IDs with --basetemp=...\pytest-temp-chunks-green
   5 passed in 1.20s
   ```

3. Input/GUI/Japanese/Zotero boundary fixes:

   ```text
   RED: pytest -q tests\test_acquisition.py tests\test_research_models.py tests\test_notion.py tests\test_gui.py tests\test_zotero.py --basetemp=...\pytest-temp-boundaries-red -p no:cacheprovider
   15 failed, 13 passed in 0.48s

   GREEN: same files with --basetemp=...\pytest-temp-boundaries-green
   28 passed in 0.26s
   ```

4. Invalid Zotero date omission and valid-ISO edge cases:

   ```text
   RED: pytest -q tests\test_notion.py::test_notion_omits_unparseable_zotero_date_and_exposes_diagnostic
   1 failed in 0.14s
   GREEN: same node ID
   1 passed in 0.08s

   RED: pytest -q tests\test_zotero.py::test_zotero_dates_are_normalized_to_notion_safe_iso_precision tests\test_notion.py::test_notion_omits_unparseable_zotero_date_and_exposes_diagnostic --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-date-edge-red -p no:cacheprovider
   3 failed, 5 passed in 0.17s
   GREEN: same node IDs with --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-date-edge-green
   8 passed in 0.09s
   ```

5. Explicit prompt-schema provenance:

   ```text
   RED: pytest -q tests\test_pipeline.py::test_pipeline_records_explicit_prompt_schema_versions_in_notion_and_manifest
   1 failed in 0.27s
   GREEN: same node ID
   1 passed in 0.32s
   ```

6. OpenRouter standard provider response and durable pre-POST dispatch:

   ```text
   RED: pytest -q tests\test_openrouter.py::test_standard_openrouter_response_resolves_provider_from_generation_metadata tests\test_openrouter.py::test_transport_timeout_after_dispatch_is_durable_and_never_auto_resent tests\test_openrouter.py::test_failed_budget_settlement_leaves_durable_unresolved_attempt_without_resend --basetemp=...\pytest-temp-openrouter-contract-red -p no:cacheprovider
   3 failed in 0.48s

   GREEN: same node IDs with --basetemp=...\pytest-temp-openrouter-contract-green
   3 passed in 0.42s

   Concurrency regression: 3 passed in 2.40s
   Full OpenRouter/JobStore focused regression: 60 passed in 7.33s
   ```

7. Durable Zotero operation and fail-closed write boundary:

   ```text
   RED: pytest -q tests\test_zotero.py::test_durable_zotero_write_resumes_exact_operation_without_duplicate_items
   4 failed in 0.38s
   GREEN: same parametrized node
   4 passed in 0.38s

   RED: pytest -q tests\test_zotero.py::test_zotero_new_item_write_fails_closed_without_a_durable_operation_store
   1 failed in 0.10s
   GREEN: same node ID
   1 passed in 0.10s

   RED: pytest -q tests\test_zotero.py::test_durable_upsert_completes_missing_attachment_for_an_existing_parent --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-zotero-existing-red -p no:cacheprovider
   1 failed in 0.14s
   GREEN: same node ID with --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-zotero-existing-green
   1 passed in 0.12s
   ```

8. Canonical staging/reuse/lock/force behavior:

   ```text
   RED: pytest -q tests\test_pipeline.py::test_a_later_ingest_reuses_the_completed_canonical_generation tests\test_pipeline.py::test_pdf_url_spellings_with_identical_bytes_converge_after_staging tests\test_pipeline.py::test_concurrent_equivalent_ingests_share_paper_lock_and_one_generation tests\test_pipeline.py::test_force_reprocess_creates_a_distinct_generation_only_when_requested tests\test_cli.py::test_ingest_force_reprocess_is_forwarded_only_when_explicit --basetemp=...\pytest-temp-canonical-red -p no:cacheprovider
   5 failed in 1.25s
   GREEN: same node IDs with --basetemp=...\pytest-temp-canonical-green
   5 passed in 0.94s

   RED: pytest -q tests\test_pipeline.py::test_unidentified_resume_reuses_a_completed_canonical_generation --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-resume-reuse-red -p no:cacheprovider
   1 failed in 0.55s
   GREEN: same node ID with --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-resume-reuse-green
   1 passed in 0.34s

   RED: pytest -q tests\test_pipeline.py::test_concurrent_equivalent_ingests_share_one_terminal_failure --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-concurrent-fail-red -p no:cacheprovider
   1 failed in 0.47s
   GREEN: same node ID with --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-temp-concurrent-fail-green
   1 passed in 0.35s
   Stability check after deterministic synchronization: five consecutive runs, each `1 passed` (0.29–0.40s).
   ```

9. Focused regression after the final edits:

   ```text
   tests\test_zotero.py tests\test_notion.py tests\test_job_store.py: 63 passed in 1.65s
   tests\test_openrouter.py: 46 passed in 7.02s
   acquisition/research-models/GUI/converter/normalizer/Marker/OCR: 88 passed in 0.75s
   tests\test_zotero.py tests\test_job_store.py: 48 passed in 1.50s
   tests\test_pipeline.py tests\test_cli.py: 40 passed in 6.37s
   ```

10. Full-suite debugging and final success:

    ```text
    First full attempt: 2 failed, 300 passed in 14.73s
    Cause: two legacy E2E assertions counted the reserved empty `.staging` parent as an artifact.
    Focused RED: 2 failed in 0.60s
    E2E expectation corrected to exclude `.staging` while asserting it is empty.
    Focused GREEN: 2 passed in 0.51s

    Second full attempt: 1 failed, 301 passed in 15.65s
    Cause: the new concurrent-failure test allowed the second caller to resolve only after the first had already failed, exercising ordinary retry rather than lock contention.
    The test now synchronizes both canonical resolutions before either worker runs; production code was unchanged.
    Stability GREEN: five consecutive passes.

    FINAL: pytest -q --basetemp=C:\Users\yoshi\Dev\paper-structure\.pytest-final-temp -p no:cacheprovider
    302 passed in 15.71s
    ```

## Compile/import/CLI/contract smoke

```text
python -m compileall -q src
compileall: OK

python -c "import src.acquisition, src.artifacts, src.cli, src.converter, src.document_normalizer, src.gui, src.job_store, src.notion, src.openrouter, src.pipeline, src.research_models, src.run_marker, src.run_ocr, src.zotero; print('imports: OK')"
imports: OK

python -m src.cli --help
exit 0; ingest/resume/status shown

python -m src.cli ingest --help
exit 0; --force-reprocess shown

JSONOutput.model_validate(checked-in p.json + p_meta.json)
marker-pdf 1.10.2 fixture contract: OK
```

## Self-review and hygiene

- Read the final findings and binding plan before changes.
- Used test-first changes for every production behavior; late E2E edits only
  corrected/stabilized tests for the intentional staging design.
- Reviewed every modified production boundary and the new fixtures. Confirmed
  recursive Marker leaf semantics against the locally installed 1.10.2
  renderer source and schema without executing or downloading models.
- Rechecked budget settlement, unresolved reservation retention, lease fencing,
  cache-key stability, paper-wide budget accounting, canonical artifact reuse,
  Zotero operation identity/key/token stability, and secret persistence.
- `git diff --check` exits 0. Git emits only the repository's existing
  LF-to-CRLF working-copy warnings.
- Credential scan found only documented placeholders and the deterministic
  `fixture-write-key`; no real credential is present.
- Workspace-local pytest temporary directories were removed after verification.
- No subagents were dispatched, as required.
- No external network, live API, paid completion, OCR executable, Marker model,
  or model download was invoked.

## Residual concerns

No correctness blocker remains. Two fail-closed operational behaviors are
intentional: an ambiguous OpenRouter billing attempt stays unresolved until an
operator reconciles it, and a Zotero upload whose response is lost may resend
the same bytes while retaining stable parent/attachment IDs and write tokens.
Neither path automatically duplicates an LLM dispatch, Zotero parent, or Zotero
attachment.
