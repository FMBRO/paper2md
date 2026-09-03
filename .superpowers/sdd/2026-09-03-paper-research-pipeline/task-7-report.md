# Task 7 report: pipeline orchestration and CLI

## Status

Complete. All service boundaries used by pipeline tests are injected fakes; no live Zotero, OpenRouter, Notion, arXiv, Crossref, or other paid/network call was made.

## Implementation

- Added `PipelineService.ingest`, `resume`, `status`, and `ingest_collection` over the existing acquisition, Zotero, conversion/quality, OpenRouter, Notion, artifact, and job-store interfaces.
- Persists ordered stage states before work and transactional SQLite checkpoints after successful work for `acquiring`, `zotero_sync`, `converting`, `quality_check`, `extracting`, `synthesizing`, and `notion_sync`, ending at `completed`.
- Validates durable source, conversion, and summary artifacts before skipping them. Resume reuses valid outputs; damaged artifacts are recomputed at the narrowest safe boundary. OpenRouter's existing request cache remains the paid-call idempotency boundary.
- Maps known user-remediable configuration, schema, input, attachment, and quality errors to `needs_input`; budget denial to `budget_exceeded`; and unexpected exceptions to `failed`. Every state is persisted and emitted to progress consumers.
- Persists the per-job budget so `resume(job_id)` retains the original cap unless explicitly overridden.
- Persists the returned Notion page ID and the successful Notion checkpoint in one SQLite transaction. Later ingestion passes that stored ID to the existing idempotent Notion upserter.
- Expands Zotero collections, deduplicates repeated item keys, and implements collection-scoped `--only-unprocessed` by checking completed canonical Zotero identities before item resolution.
- Added `python -m src.cli ingest`, `resume`, and `status`, including research-interest, attachment, cost, configuration, `--only-unprocessed`, JSON, stable text, and exit-code handling. `--only-unprocessed` on a non-collection returns an explicit diagnostic rather than being ignored.
- Left `src.batch_convert` unchanged and retained its regression coverage.

## Strict TDD evidence

### Cycle 1: successful orchestration and durable checkpoints

RED command:

```text
uv run pytest -q tests/test_pipeline.py -k successful_local_pipeline
```

RED output:

```text
E   ModuleNotFoundError: No module named 'src.pipeline'
1 error in 0.16s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k successful_local_pipeline
.                                                                        [100%]
1 passed in 0.35s
```

### Cycle 2: resume skips successful expensive stages

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k resume_reuses
F                                                                        [100%]
E       assert 2 == 1
E        +  where 2 = <tests.test_pipeline.FakeAcquirer object ...>.calls
1 failed, 1 deselected in 0.47s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py
..                                                                       [100%]
2 passed in 0.48s
```

### Cycle 3: user-actionable configuration and budget states

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k 'missing_service_configuration or budget_denial'
F.                                                                       [100%]
E       AssertionError: assert <JobState.FAILED: 'failed'> is <JobState.NEEDS_INPUT: 'needs_input'>
1 failed, 1 passed, 2 deselected in 0.49s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k 'missing_service_configuration or budget_denial'
..                                                                       [100%]
2 passed, 2 deselected in 0.34s
```

### Cycle 4: attachment selection on resume

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k attachment_selection
F                                                                        [100%]
E       TypeError: PipelineService.resume() got an unexpected keyword argument 'attachment_key'
1 failed, 4 deselected in 0.29s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k attachment_selection
.                                                                        [100%]
1 passed, 4 deselected in 0.28s
```

### Cycle 5: persisted budget on resume

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k persisted_job_budget
F                                                                        [100%]
E       assert [0.2, 0.5] == [0.2, 0.2]
1 failed, 5 deselected in 0.40s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k persisted_job_budget tests/test_job_store.py
.                                                                        [100%]
1 passed, 19 deselected in 0.31s
```

### Cycle 6: collection batching and only-unprocessed

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k collection_batch
F                                                                        [100%]
E       AttributeError: 'PipelineService' object has no attribute 'ingest_collection'
1 failed, 6 deselected in 0.30s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k collection_batch
.                                                                        [100%]
1 passed, 6 deselected in 0.35s
```

### Cycle 7: CLI surface and output contracts

RED command/output:

```text
uv run pytest -q tests/test_cli.py
E   ModuleNotFoundError: No module named 'src.cli'
1 error in 0.12s
```

GREEN command/output:

```text
uv run pytest -q tests/test_cli.py
.......                                                                  [100%]
7 passed in 0.18s
```

### Cycle 8: automatically selected Zotero attachment checkpoint

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k collection_batch
F                                                                        [100%]
E       AssertionError: assert None == 'ATTACH01'
1 failed, 6 deselected in 0.44s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py
.......                                                                  [100%]
7 passed in 1.05s
```

### Cycle 9: complete ordered progress events

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k successful_local_pipeline
F                                                                        [100%]
E       AssertionError: assert ['acquiring', ...] == ['queued', 'acquiring', ...]
1 failed, 8 deselected in 0.39s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k successful_local_pipeline
.                                                                        [100%]
1 passed, 8 deselected in 0.32s
```

### Cycle 10: terminal-state progress events

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k 'missing_service_configuration or budget_denial'
FF                                                                       [100%]
E       AssertionError: assert 'notion_sync' == 'needs_input'
E       AssertionError: assert 'extracting' == 'budget_exceeded'
2 failed, 7 deselected in 0.60s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k 'missing_service_configuration or budget_denial'
..                                                                       [100%]
2 passed, 7 deselected in 0.35s
```

### Cycle 11: damaged summary invalidates its checkpoint

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k corrupted_summary
F                                                                        [100%]
E       assert 1 == 2
E        +  where 1 = <tests.test_pipeline.FakeSummarizer object ...>.calls
1 failed, 9 deselected in 0.38s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k corrupted_summary
.                                                                        [100%]
1 passed, 9 deselected in 0.35s
```

### Cycle 12: source checkpoint integrity

RED command/output:

```text
uv run pytest -q tests/test_pipeline.py -k source_pdf_no_longer
F                                                                        [100%]
E       assert 1 == 2
E        +  where 1 = <tests.test_pipeline.FakeAcquirer object ...>.calls
1 failed, 10 deselected in 0.45s
```

GREEN command/output:

```text
uv run pytest -q tests/test_pipeline.py -k source_pdf_no_longer
.                                                                        [100%]
1 passed, 10 deselected in 0.37s
```

### Cycle 13: non-collection only-unprocessed diagnostic

RED command/output:

```text
uv run pytest -q tests/test_cli.py -k only_unprocessed_rejects
F                                                                        [100%]
E       assert 0 == 2
1 failed, 7 deselected in 0.25s
```

GREEN command/output:

```text
uv run pytest -q tests/test_cli.py
........                                                                 [100%]
8 passed in 0.18s
```

### Cycle 14: configuration option before or after the subcommand

RED command/output:

```text
uv run pytest -q tests/test_cli.py -k config_option_is_also
F                                                                        [100%]
E   SystemExit: 2
E   argparse.ArgumentError: argument command: invalid choice: '<config path>'
1 failed, 8 deselected in 0.33s
```

GREEN command/output:

```text
uv run pytest -q tests/test_cli.py
.........                                                                [100%]
9 passed in 0.19s
```

## Final verification

Commands:

```text
git diff --check
uv run pytest -q
uv run python -m compileall -q src tests
uv run python -c "from src.pipeline import PipelineService, NeedsInputError; from src.cli import main; from src.job_store import JobStore; print('Task 7 imports OK')"
uv run python -m src.cli --help
```

Output:

```text
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 97%]
......                                                                   [100%]
222 passed in 6.76s
Task 7 imports OK
usage: python -m src.cli [-h] {ingest,resume,status} ...
```

`git diff --check` and `compileall` exited 0 with no errors. Git printed only the repository's Windows LF-to-CRLF notices for two tracked files. CLI help exited 0 and listed `ingest`, `resume`, and `status`.

## Files changed

- `src/pipeline.py` — shared injectable, resumable stage orchestration and collection batching.
- `src/cli.py` — ingest/resume/status parser, stable JSON/text rendering, diagnostics, and exit codes.
- `src/job_store.py` — job budget migration, input/budget updates, job-paper linking, transactional checkpoints, completed-identity lookup, and atomic Notion ID/checkpoint persistence.
- `src/research_models.py` — persisted `JobRecord.max_cost_usd` field.
- `tests/test_pipeline.py` — local/mocked-remote success, state mappings, resume, integrity, collection, duplicate avoidance, events, budget, and Notion-ID tests.
- `tests/test_cli.py` — parsing, override forwarding, text/JSON contracts, diagnostics, and exit-code tests.
- `.superpowers/sdd/2026-09-03-paper-research-pipeline/task-7-report.md` — this report.

## Self-review

- Rechecked every Task 7 bullet against a test or explicit implementation path.
- Mutation checks covered missing checkpoints, missing queued/terminal events, repeated acquisition/conversion/LLM work on resume, stale budgets, lost attachment keys, ignored `--only-unprocessed`, wrong state mapping, damaged source/summary artifacts, absent Notion-ID persistence, and collection duplicate keys.
- The Notion response ID and its job checkpoint share one SQLite transaction. If a crash occurs before that transaction, the existing Notion client's identity recovery prevents a duplicate page; after it commits, resume skips Notion entirely.
- OpenRouter extraction/synthesis remains one existing public summarizer call. The pipeline emits and checkpoints both stages, while Task 5's durable logical-request cache prevents a successful paid call from repeating after interruption.
- The requested no-subagent constraint prevented dispatching a separate reviewer; self-review plus the fresh full suite, compile/import smoke, CLI smoke, and diff check were used instead.
- `src.batch_convert` was not edited; its tests are part of the 222-test full run.

## Concerns

- `--only-unprocessed` is intentionally collection-scoped. A single source with this flag exits 2 with an explicit diagnostic instead of silently ignoring it.
- Pipeline-level orchestration assumes one active worker per job. The OpenRouter paid-call layer has its own claim/budget fencing, but non-paid stage checkpoints do not add a whole-job distributed lease.
- Extraction and synthesis are exposed together by the existing `OpenRouterSummarizer.summarize_artifacts` interface. Their individual paid calls are still cached transactionally by Task 5, but pipeline-level `extracting` and `synthesizing` checkpoints are written after the combined validated result returns.

---

## Critical review remediation (2026-09-03)

The GUI finding remains assigned to Task 8 by the approved plan. No change was made to `src/gui.py` in this remediation.

### Findings resolved

1. Resume input overrides now update the job and invalidate dependent checkpoints in one SQLite transaction. A changed research interest invalidates extraction, synthesis, and Notion; a changed attachment invalidates acquisition and every later checkpoint. Later-stage-failure tests prove both restart boundaries.
2. Recomputed stages transactionally invalidate every downstream checkpoint. Acquisition compares the newly acquired PDF SHA-256 with its prior checkpoint; changed bytes invalidate Zotero through Notion, while reacquiring the same bytes preserves valid later work. Conversion checkpoints now include a version plus source, document, Markdown, and quality-result hashes, so a valid-JSON but modified `document.json` forces conversion, quality, summary, and Notion to rerun.
3. Added `AcquisitionInputError` and `ZoteroInputError`. Missing/unreadable/non-PDF sources and invalid/missing attachment selections map to `needs_input`; network, 5xx, 408/429, and unexpected Zotero service errors remain `failed` paths. Removing preflight availability probes preserves the concrete Zotero transport error classification.
4. Zotero metadata resolution now propagates `ZoteroSettings.user_id` instead of hard-coding library `0`. Both canonical identity and collection `only_unprocessed` use the same configured library ID, with client and pipeline tests for user ID 42.

### Focused RED/GREEN evidence

#### Resume override invalidation

RED:

```text
uv run pytest -q tests/test_pipeline.py -k 'research_interest_override_after or attachment_override_after'
FF                                                                       [100%]
E       assert 1 == 2
E       AssertionError: assert ['ATTACH01'] == ['ATTACH01', 'ATTACH02']
2 failed, 11 deselected in 0.61s
```

GREEN:

```text
uv run pytest -q tests/test_pipeline.py -k 'research_interest_override_after or attachment_override_after'
..                                                                       [100%]
2 passed, 11 deselected in 0.53s
```

#### Changed reacquired PDF invalidates downstream work

RED:

```text
uv run pytest -q tests/test_pipeline.py -k changed_reacquired
F                                                                        [100%]
E       assert 1 == 2
E        +  where 1 = <tests.test_pipeline.FakeZotero object ...>.upsert_calls
1 failed, 13 deselected in 0.67s
```

GREEN:

```text
uv run pytest -q tests/test_pipeline.py -k 'changed_reacquired or source_pdf_no_longer'
..                                                                       [100%]
2 passed, 12 deselected in 0.51s
```

#### Conversion artifact hashes invalidate stale dependents

RED:

```text
uv run pytest -q tests/test_pipeline.py -k recomputed_conversion
F                                                                        [100%]
E       assert 1 == 2
E        +  where 1 = <tests.test_pipeline.FakeConverter object ...>.calls
1 failed, 18 deselected in 0.41s
```

GREEN:

```text
uv run pytest -q tests/test_pipeline.py -k 'recomputed_conversion or changed_reacquired or resume_reuses or source_pdf_no_longer'
....                                                                     [100%]
4 passed, 15 deselected in 0.84s
```

#### Explicit user-correctable input exception classes

RED:

```text
uv run pytest -q tests/test_acquisition.py -k user_correctable tests/test_zotero.py -k user_correctable
FF                                                                       [100%]
E       ImportError: cannot import name 'AcquisitionInputError' from 'src.acquisition'
E       ImportError: cannot import name 'ZoteroInputError' from 'src.zotero'
2 failed, 42 deselected in 0.14s
```

GREEN:

```text
uv run pytest -q tests/test_acquisition.py tests/test_zotero.py
............................................                             [100%]
44 passed in 0.15s
```

Pipeline mapping verification:

```text
uv run pytest -q tests/test_pipeline.py -k 'user_correctable_source or transient_acquisition'
..                                                                       [100%]
2 passed, 15 deselected in 0.32s
```

#### Missing local source creates a resumable job

RED:

```text
uv run pytest -q tests/test_pipeline.py -k missing_local_source
F                                                                        [100%]
E       FileNotFoundError: [Errno 2] No such file or directory: '<temp>/missing.pdf'
1 failed, 17 deselected in 0.30s
```

GREEN:

```text
uv run pytest -q tests/test_pipeline.py -k missing_local_source
.                                                                        [100%]
1 passed, 17 deselected in 0.22s
```

#### Rate limits remain transient failures

RED:

```text
uv run pytest -q tests/test_acquisition.py -k rate_limit_remains
F                                                                        [100%]
E       AssertionError: assert not True
E        +  where True = isinstance(AcquisitionInputError('HTTP 429 ...'), AcquisitionInputError)
1 failed, 29 deselected in 0.11s
```

GREEN:

```text
uv run pytest -q tests/test_acquisition.py
..............................                                           [100%]
30 passed in 0.11s
```

#### Zotero transport failures remain failed

RED:

```text
uv run pytest -q tests/test_pipeline.py -k transient_acquisition
F                                                                        [100%]
E       AssertionError: assert <JobState.NEEDS_INPUT: 'needs_input'> is <JobState.FAILED: 'failed'>
1 failed, 18 deselected in 0.37s
```

GREEN:

```text
uv run pytest -q tests/test_pipeline.py -k 'transient_acquisition or user_correctable_source'
..                                                                       [100%]
2 passed, 17 deselected in 0.32s
```

#### Configured Zotero user ID

RED:

```text
uv run pytest -q tests/test_zotero.py -k configured_zotero_user_id
F                                                                        [100%]
E       AssertionError: assert '0' == '42'
1 failed, 15 deselected in 0.14s
```

GREEN:

```text
uv run pytest -q tests/test_zotero.py
................                                                         [100%]
16 passed in 0.06s
```

The collection-level non-default identity regression is included in the focused suite:

```text
uv run pytest -q tests/test_pipeline.py tests/test_cli.py tests/test_job_store.py tests/test_acquisition.py tests/test_zotero.py
........................................................................ [ 81%]
................                                                         [100%]
88 passed in 3.25s
```

### Remediation verification

```text
uv run pytest -q
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
..................                                                       [100%]
234 passed in 7.40s

uv run python -m compileall -q src tests
[exit 0, no output]

uv run python -c "from src.acquisition import AcquisitionInputError; from src.zotero import ZoteroInputError; from src.pipeline import PipelineService; print('Task 7 review imports OK')"
Task 7 review imports OK
```

No live or paid external call was made. All pipeline and client tests use injected fakes or deterministic transports.
