# Task 5 report: OpenRouter structured summarization and budget guard

## Status

Complete. Implementation commit: `af9d2dbc68129f5555dacf6d5cd9811c663117cd` (`feat: add resumable OpenRouter summarization`).

No live OpenRouter calls were made. Every HTTP test used `httpx.MockTransport` with deterministic local responses.

## Implemented behavior

- Builds section-local map chunks from normalized `document.json` data.
- Keeps tables, display equations, and captions as indivisible chunk atoms, even when an atom exceeds the nominal character limit.
- Carries block kind, page range, section ID/title, and source-position evidence into extraction prompts.
- Adds an `ArtifactBundle` entry point that reads `document.json` and atomically writes `summary.json` through `ArtifactManager`.
- Uses the required endpoint and exact model IDs:
  - `https://openrouter.ai/api/v1/chat/completions`
  - extraction: `google/gemini-3.8-flash`
  - synthesis: `openai/gpt-5.6-sol`
- Sends a single `model` field (never a `models` fallback list), allowing only provider fallback for that model.
- Sends strict JSON Schema response formats generated from Pydantic models.
- Enforces `provider.require_parameters=true`, `provider.zdr=true`, and `provider.data_collection="deny"`; weakened settings fail closed before HTTP.
- Validates the OpenRouter response envelope, usage, extraction structure, final summary structure, Japanese narrative fields, relevance score range, and source evidence page/section membership with Pydantic plus provenance validation.
- Retries malformed structured output only up to the configured bound. Invalid content is persisted for accounting/audit but is never supplied to synthesis.
- Persists every validated-envelope paid attempt with request hash, semantic cache key, model, provider, response artifact, usage JSON, actual `usage.cost`, and validation status.
- Reuses only validated cached results. Exhausted malformed retries remain exhausted after restart, avoiding duplicate paid calls.
- Migrates the Task 1 `llm_calls` schema in place and preserves the original `cache_llm_call` / `get_cached_llm_call` behavior.
- Authorizes a conservative plan using configured maximum input/output tokens and bounded retry counts before the first uncached request.
- Uses an allowlist of price snapshots checked against official OpenRouter model pages on 2026-09-03:
  - Gemini 3.8 Flash: USD 0.75/M input, USD 3.75/M output
  - GPT-5.6 Sol: USD 2.00/M input, USD 10.00/M output
- Fails closed when either configured model has no trusted price.
- Reads the credential only from `OPENROUTER_API_KEY`. It is used only in the HTTP Authorization header and is excluded from request hashes, SQLite, artifacts, and error messages.
- Declares `httpx>=0.28` and `pydantic>=2.10` as direct runtime dependencies and updates `uv.lock`.

Official API details checked:

- https://openrouter.ai/docs/guides/features/structured-outputs
- https://openrouter.ai/docs/guides/routing/provider-selection
- https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request
- https://openrouter.ai/google/gemini-3.8-flash
- https://openrouter.ai/openai/gpt-5.6-sol/

## Strict TDD evidence

### Cycle 1: section-aware atomic chunks

RED command:

```text
uv run pytest -q tests/test_openrouter.py
```

Observed RED output:

```text
FF                                                                       [100%]
ModuleNotFoundError: No module named 'src.openrouter'
2 failed in 0.08s
```

GREEN command:

```text
uv run pytest -q tests/test_openrouter.py
```

Observed GREEN output:

```text
..                                                                       [100%]
2 passed in 0.01s
```

### Cycle 2: payload privacy/schema, Japanese validation, and budget math

RED command:

```text
uv run pytest -q tests/test_openrouter.py -k "structured_payload or final_summary or worst_case or budget_guard"
```

Observed RED output:

```text
FFFFFFF                                                                  [100%]
ImportError: cannot import name 'PaperSummaryResponse' from 'src.openrouter'
ImportError: cannot import name 'ModelPricing' from 'src.openrouter'
ImportError: cannot import name 'BudgetExceededError' from 'src.openrouter'
7 failed, 2 deselected in 0.12s
```

GREEN command:

```text
uv run pytest -q tests/test_openrouter.py -k "structured_payload or final_summary or worst_case or budget_guard"
```

Observed GREEN output:

```text
.......                                                                  [100%]
7 passed, 2 deselected in 0.22s
```

### Cycle 3: provider/usage persistence and schema migration

RED command:

```text
uv run pytest -q tests/test_job_store.py -k "records_llm_usage or migrates_task_one"
```

Observed RED output:

```text
FF                                                                       [100%]
AttributeError: 'JobStore' object has no attribute 'record_llm_call'
AssertionError: expected {'cache_key', 'provider', 'usage_json', 'validated'} columns
2 failed, 8 deselected in 0.13s
```

GREEN command:

```text
uv run pytest -q tests/test_job_store.py
```

Observed GREEN output:

```text
..........                                                               [100%]
10 passed in 0.31s
```

### Cycle 4: map/reduce service, repair, cost recording, cache resume, and API failures

RED command:

```text
uv run pytest -q tests/test_openrouter.py -k "summarizer_"
```

Observed RED output:

```text
FFFFFF                                                                   [100%]
ImportError: cannot import name 'OpenRouterSummarizer' from 'src.openrouter'
ImportError: cannot import name 'OpenRouterAPIError' from 'src.openrouter'
6 failed, 9 deselected in 0.31s
```

GREEN command:

```text
uv run pytest -q tests/test_openrouter.py -k "summarizer_"
```

Observed GREEN output:

```text
......                                                                   [100%]
6 passed, 9 deselected in 0.40s
```

### Cycle 4b: evidence provenance and retry state across resume

RED command:

```text
uv run pytest -q tests/test_openrouter.py -k "evidence_outside or repeat_exhausted"
```

Observed RED output:

```text
FF                                                                       [100%]
assert 2 == 3
Failed: no repeated HTTP
2 failed, 15 deselected in 0.36s
```

GREEN command:

```text
uv run pytest -q tests/test_openrouter.py -k "evidence_outside or repeat_exhausted"
```

Observed GREEN output:

```text
..                                                                       [100%]
2 passed, 15 deselected in 0.23s
```

### Cycle 5: configured limit validation

RED command:

```text
uv run pytest -q tests/test_config.py -k "token_and_chunk or retry_count"
```

Observed RED output:

```text
FFFFFF                                                                   [100%]
Failed: DID NOT RAISE ValueError
6 failed, 12 deselected in 0.11s
```

GREEN command:

```text
uv run pytest -q tests/test_config.py -k "token_and_chunk or retry_count"
```

Observed GREEN output:

```text
......                                                                   [100%]
6 passed, 12 deselected in 0.03s
```

### Cycle 6: Task 4 ArtifactBundle integration

RED command:

```text
uv run pytest -q tests/test_openrouter.py -k "task_four_artifact_bundle"
```

Observed RED output:

```text
F                                                                        [100%]
AttributeError: 'OpenRouterSummarizer' object has no attribute 'summarize_artifacts'
1 failed, 17 deselected in 0.27s
```

GREEN command:

```text
uv run pytest -q tests/test_openrouter.py -k "task_four_artifact_bundle"
```

Observed GREEN output:

```text
.                                                                        [100%]
1 passed, 17 deselected in 0.20s
```

## Final verification

Command:

```text
uv run pytest -q tests/test_openrouter.py tests/test_job_store.py tests/test_config.py
uv run pytest -q
uv run python -m compileall -q src tests
uv run python -c "from src.openrouter import OpenRouterSummarizer, CURRENT_MODEL_PRICING; print(len(CURRENT_MODEL_PRICING))"
git diff --check
```

Observed output:

```text
..............................................                           [100%]
46 passed in 0.75s
........................................................................ [ 43%]
........................................................................ [ 87%]
....................                                                     [100%]
164 passed in 1.11s
2
```

`compileall`, import smoke check, and `git diff --check` exited 0. Git emitted only Windows LF-to-CRLF conversion notices; there were no whitespace errors.

## Files changed

- `src/openrouter.py` — chunking, Pydantic response contracts, payload builder, pricing/budget guard, resumable map/reduce client, evidence validation, ArtifactBundle entry point.
- `src/job_store.py` — backward-compatible `llm_calls` migration, per-attempt audit fields, validated semantic-cache lookup, retry-attempt count.
- `src/config.py` — token/chunk/retry settings and value validation.
- `tests/test_openrouter.py` — deterministic OpenRouter behavior tests with injected transports.
- `tests/test_job_store.py` — usage/provider/validated-cache and legacy migration tests.
- `tests/test_config.py` — invalid configured limit tests.
- `pyproject.toml` and `uv.lock` — direct `httpx` and `pydantic` dependencies.

## Self-review

- Requirement-by-requirement check completed against `task-5-brief.md`.
- Mutation checks considered and covered: splitting an atomic table, crossing section boundaries, omitting privacy keys, adding a cross-model list, accepting score 6, accepting English narrative, ignoring output-token price, allowing unknown pricing, sending HTTP after budget rejection, caching malformed output as reusable, omitting usage/provider, repeating exhausted retries, accepting invented evidence, persisting the API key, and forwarding malformed extraction content.
- Existing Task 1 cache callers retain their prior return shape and double-charge protection.
- Schema migration is additive and idempotent.
- HTTP errors are sanitized and do not expose response bodies or credentials.
- No unrelated files or prior behavior were changed.

## Concerns

- The trusted price allowlist is intentionally a dated snapshot. OpenRouter pricing can change; callers must update the two audited constants when model-card prices change. Unknown/custom model IDs fail closed, and pricing remains injectable for deterministic tests and controlled deployments.
- The conservative guard budgets every permitted malformed-output retry and configured token maximum. It may reject work whose likely cost is lower, by design, rather than risk exceeding the paper budget.
