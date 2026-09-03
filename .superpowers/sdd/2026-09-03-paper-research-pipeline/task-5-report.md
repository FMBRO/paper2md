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

---

## Review remediation (2026-09-03)

Status: complete in implementation commit `a570ec7` (`fix: harden OpenRouter summarization`). This section supersedes the earlier static-pricing concern above: hardcoded model prices were removed.

### Review findings resolved

1. The chat endpoint is now validated as exactly `https://openrouter.ai/api/v1/chat/completions` both in configuration construction and again at summarization entry/request time. Revalidation occurs before catalog/auth access. Extraction and synthesis model IDs remain configurable and may be swapped; each request contains only the selected `model`, and a response naming another model is rejected.
2. A conservative tokenizer-independent ceiling counts UTF-8 bytes for the complete canonical serialized request, including messages and JSON Schema, before every extraction, repair, reduction, and synthesis call. Oversized atomic tables/equations/captions fail closed. Large extraction sets are greedily combined through bounded hierarchical reduction levels until final synthesis fits; inability to combine two results or exhaustion of the configured level bound fails closed.
3. Static pricing was replaced by `OpenRouterModelCatalog`, which fetches current configured-model pricing from the official `/api/v1/models` catalog with structured-output and ZDR filters. The catalog client is injectable. Every completion payload pins `provider.max_price.prompt` and `.completion` to the authorized catalog rates. The exact model/rates/catalog version are persisted with every attempt. Missing, ambiguous, malformed, unsupported, or mismatched pricing fails closed without an LLM call.
4. Every 2xx response is first parsed into billable metadata (`id`, model, provider, usage, cost) before content-envelope or structured-output validation. Malformed content/envelopes with resolved cost are persisted and charged before bounded retry. Missing/unparseable cost creates a durable unresolved audit row and stops immediately; resume remains stopped rather than risking a duplicate unknown charge.
5. Budget consumption now sums all job costs belonging to the current job's `paper_id`; jobs without a canonical paper retain job-local accounting.
6. Both extraction and final schemas require at least one evidence item. Page and section are mandatory. NFKC/case/whitespace-normalized quotes must be substrings of a retained source block whose page and section match the same real source span.
7. SQLite now provides transactional cache-key claims with expiring leases. An owner rechecks cache state under the lease before dispatch, and concurrent contenders wait for the cached result or terminal attempt state. The lease covers the complete bounded repair sequence, preventing duplicate identical paid requests while retaining every real charge.

The two Minor review findings were intentionally not changed in this round, per the task instruction.

### Review-fix TDD evidence

#### Cycle 7: endpoint, configurable role models, catalog prices, max_price, pricing audit

RED:

```text
uv run pytest -q tests/test_openrouter.py tests/test_config.py -k "structured_payload or persists_usage or configured_role_model or unknown or non_openrouter"
```

```text
FFFFFFFF                                                                 [100%]
ImportError: cannot import name 'PricingSnapshot' from 'src.openrouter'
TypeError: OpenRouterSummarizer.__init__() got an unexpected keyword argument 'catalog'
Failed: DID NOT RAISE ValueError
8 failed, 30 deselected in 0.37s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py tests/test_config.py -k "structured_payload or persists_usage or configured_role_model or unknown or non_openrouter"
```

```text
........                                                                 [100%]
8 passed, 30 deselected in 0.42s
```

#### Cycle 8: serialized input ceilings, oversized atoms, hierarchical reduction

RED:

```text
uv run pytest -q tests/test_openrouter.py tests/test_config.py -k "oversized_indivisible or serialized_map or hierarchically or max_reduction_levels"
```

```text
FFFF                                                                     [100%]
ImportError: cannot import name 'InputLimitExceededError' from 'src.openrouter'
ImportError: cannot import name 'estimate_serialized_tokens' from 'src.openrouter'
TypeError: OpenRouterSettings.__init__() got an unexpected keyword argument 'max_reduction_levels'
4 failed, 38 deselected in 0.40s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py tests/test_config.py -k "oversized_indivisible or serialized_map or hierarchically or max_reduction_levels"
```

```text
....                                                                     [100%]
4 passed, 38 deselected in 0.31s
```

#### Cycle 9: malformed 2xx billing metadata and unresolved cost

Corrected RED (after increasing the test fixture's otherwise unrelated request ceiling so the tests reached the intended behavior):

```text
uv run pytest -q tests/test_openrouter.py -k "billable_malformed or missing_cost"
```

```text
FF                                                                       [100%]
assert 0.02 == 0.03 ± 3.0e-08
ImportError: cannot import name 'UnresolvedUsageError' from 'src.openrouter'
2 failed, 22 deselected in 0.32s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "billable_malformed or missing_cost"
```

```text
..                                                                       [100%]
2 passed, 22 deselected in 0.23s
```

#### Cycle 10: canonical-paper budget aggregation

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "aggregates_cost"
```

```text
F                                                                        [100%]
Failed: no HTTP
1 failed, 24 deselected in 0.32s
```

The deliberate `no HTTP` fake proved the old job-local check allowed a call despite USD 0.49 already charged to another job for the same paper.

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "aggregates_cost"
```

```text
.                                                                        [100%]
1 passed, 24 deselected in 0.19s
```

#### Cycle 11: required source-backed evidence and real page/section pairs

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "requires_evidence or fabricated_evidence or mismatched_real"
```

```text
FFF                                                                      [100%]
Failed: DID NOT RAISE ValidationError
assert 2 == 3
assert 3 == 4
3 failed, 25 deselected in 0.36s
```

The first GREEN attempt exposed a `NameError` in the new source-text binding (`2 failed, 1 passed, 25 deselected in 0.31s`). After correcting that implementation defect, the required GREEN run was:

```text
uv run pytest -q tests/test_openrouter.py -k "requires_evidence or fabricated_evidence or mismatched_real"
```

```text
...                                                                      [100%]
3 passed, 25 deselected in 0.24s
```

#### Cycle 12: transactional concurrent request claim

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "concurrent_identical"
```

```text
F                                                                        [100%]
AssertionError: duplicate extraction and synthesis model calls were observed
1 failed, 28 deselected in 0.50s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "concurrent_identical"
```

```text
.                                                                        [100%]
1 passed, 28 deselected in 0.46s
```

#### Cycle 13: mutable endpoint revalidation

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "revalidates_mutated"
```

```text
F                                                                        [100%]
src.openrouter.OpenRouterConfigurationError: OPENROUTER_API_KEY is required
1 failed, 29 deselected in 0.30s
```

This showed catalog work occurred and credential lookup was reached before the mutated endpoint was rejected.

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "revalidates_mutated"
```

```text
.                                                                        [100%]
1 passed, 29 deselected in 0.18s
```

### Review-fix final verification

Command:

```text
uv run pytest -q tests/test_openrouter.py tests/test_job_store.py tests/test_config.py
uv run pytest -q
uv run python -m compileall -q src tests
uv run python -c "from src.openrouter import OpenRouterModelCatalog, OpenRouterSummarizer, PricingSnapshot; print('Task 5 imports OK')"
git diff --check
```

Observed output before commit:

```text
............................................................             [100%]
60 passed in 1.65s
........................................................................ [ 40%]
........................................................................ [ 80%]
..................................                                       [100%]
178 passed in 2.65s
Task 5 imports OK
```

`compileall` and `git diff --check` exited 0. Git emitted only Windows LF-to-CRLF notices.

### Review-fix files

- `src/config.py` — exact endpoint invariant and bounded reduction configuration.
- `src/openrouter.py` — dynamic catalog pricing, provider max-price pinning, full-payload input estimates, hierarchical reduction, billable metadata audit, source-backed evidence, and request claims.
- `src/job_store.py` — additive billing/pricing audit migration, paper-level cost query, unresolved-call lookup, and transactional leases.
- `tests/test_config.py` — endpoint and reduction-bound validation.
- `tests/test_openrouter.py` — all seven review findings, using injected catalogs/transports only.

### Review-fix self-review and concerns

- Rechecked all seven load-bearing findings against the committed diff; each has a direct behavior test.
- Confirmed model IDs remain role-configurable and response equality prevents cross-model substitution.
- Confirmed the API key is absent from request hashes, persisted responses, pricing snapshots, and errors.
- Confirmed every actual test completion response uses an injected `httpx.MockTransport`; no live OpenRouter request ran.
- Confirmed legacy Task 1 cache methods retain their original return shape and charging behavior.
- Conservative UTF-8-byte token estimates can reject requests that a model tokenizer might accept; this is intentional fail-closed behavior.
- Uncached production work now depends on availability of the official OpenRouter model catalog. Catalog failure blocks paid completion requests instead of using stale prices.

## Review round 2

Addressed the five remaining load-bearing findings. The deferred Minor findings were not changed.

### Round-2 TDD evidence

#### Cycle 14: pricing-independent logical request identity

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "price_change_does_not"
```

```text
F                                                                        [100%]
Failed: no resend
1 failed, 30 deselected in 0.34s
```

Changing the catalog price changed `provider.max_price`, produced a new full-payload key, and allowed the logically identical unresolved request to be sent again.

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "price_change_does_not"
```

```text
.                                                                        [100%]
1 passed, 30 deselected in 0.24s
```

The durable logical key now hashes semantic request fields only. The separately persisted request hash and pricing snapshot still audit the exact dispatched payload.

#### Cycle 15: whitespace-only evidence

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "whitespace_only_evidence"
```

```text
F                                                                        [100%]
assert 2 == 3
1 failed, 31 deselected in 0.35s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "whitespace_only_evidence"
```

```text
.                                                                        [100%]
1 passed, 31 deselected in 0.21s
```

#### Cycle 16: atomic paper-scoped budget reservation

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "atomically_reserve_one_shared"
```

```text
F                                                                        [100%]
assert 2 == 1
1 failed, 32 deselected in 0.54s
```

Both concurrent jobs completed against the same paper budget, proving that read/check/charge was not atomic.

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "atomically_reserve_one_shared"
```

```text
.                                                                        [100%]
1 passed, 32 deselected in 0.45s
```

SQLite `BEGIN IMMEDIATE` now serializes paper-scoped authorization. Resolved attempts consume their authorized slice, non-billable failures release it, and billed attempts with unresolved cost retain a conservative unresolved reservation.

#### Cycle 17: request lease derived from the bounded retry window

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "outlives_former_fixed_lease"
```

```text
F                                                                        [100%]
TypeError: OpenRouterSettings.__init__() got an unexpected keyword argument 'request_timeout_seconds'
1 failed, 33 deselected in 0.30s
```

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "outlives_former_fixed_lease"
```

```text
.                                                                        [100%]
1 passed, 33 deselected in 0.53s
```

The claim lease is now `request_timeout_seconds * maximum_attempts + 30 seconds`; each POST receives the configured timeout. The test advances the claim clock by 301 seconds while the first owner remains active and proves that the second caller cannot dispatch a duplicate.

#### Cycle 18: mandatory final-synthesis headroom

RED:

```text
uv run pytest -q tests/test_openrouter.py -k "reserved_final_synthesis_headroom"
```

```text
F                                                                        [100%]
assert 22 == 21
1 failed, 34 deselected in 0.68s
```

All reduction calls consumed budget before the final synthesis authorization failed.

GREEN:

```text
uv run pytest -q tests/test_openrouter.py -k "reserved_final_synthesis_headroom"
```

```text
.                                                                        [100%]
1 passed, 34 deselected in 0.61s
```

Final synthesis headroom is reserved atomically before maps/reductions. Every extraction and reduction independently reserves its own bounded attempt cost. The low-budget multi-level regression stops before a reduction can consume final headroom and never dispatches synthesis without authorization.

### Round-2 verification

Focused round-2 behaviors:

```text
uv run pytest -q tests/test_openrouter.py -k "price_change_does_not or atomically_reserve_one_shared or whitespace_only_evidence or outlives_former_fixed_lease or reserved_final_synthesis_headroom"
```

```text
.....                                                                    [100%]
5 passed, 30 deselected in 1.35s
```

Scoped suite:

```text
uv run pytest -q tests/test_openrouter.py tests/test_job_store.py tests/test_config.py
```

```text
.................................................................        [100%]
65 passed in 3.69s
```

Full suite:

```text
uv run pytest -q
```

```text
........................................................................ [ 39%]
........................................................................ [ 78%]
.......................................                                  [100%]
183 passed in 4.31s
```

Additional checks:

```text
uv run python -m compileall -q src tests
uv run python -c "from src.openrouter import OpenRouterModelCatalog, OpenRouterSummarizer, PricingSnapshot; print('Task 5 round 2 imports OK')"
git diff --check
```

`compileall` and `git diff --check` exited 0; the import smoke test printed `Task 5 round 2 imports OK`. Git emitted only Windows LF-to-CRLF notices.

### Round-2 files

- `src/config.py` — configurable positive request timeout used to derive the safe claim lease.
- `src/job_store.py` — transactional paper/job budget reservations with settle, release, and unresolved retention operations.
- `src/openrouter.py` — semantic logical keys, per-attempt transactional authorization, derived leases, synthesis-headroom reservation, and empty-normalized-quote rejection.
- `tests/test_openrouter.py` — price-change resume, two-job budget race, whitespace quote, former-lease-overrun, and low-budget reduction regressions.

### Round-2 self-review and concerns

- Rechecked all five round-2 findings against the diff and their focused behavior tests.
- Confirmed cache, unresolved-attempt, retry-exhaustion, and request-claim lookups all use the stable semantic key; exact payload hashes and catalog pricing versions remain persisted separately.
- Confirmed `BEGIN IMMEDIATE` makes the paper cost plus active reservation decision atomic across separate jobs and connections.
- Confirmed final synthesis authorization is reserved before any paid map or reduction work and is retained when its billable response has unresolved cost.
- Confirmed HTTP/API failures release ordinary reservations, while malformed billable 2xx responses retain one conservative attempt allocation.
- Confirmed all tests use injected transports/catalogs and no live OpenRouter request was made.
- The conservative reservations use configured maximum input/output ceilings, so low budgets can reject work even when likely actual token use would cost less. This is intentional fail-closed authorization.
- No deferred Minor finding was addressed in this round.
