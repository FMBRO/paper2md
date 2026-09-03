# Task 6 report: Notion validation and idempotent summary upsert

## Status

Complete. No live Notion request was made: every test injects an `httpx.MockTransport` and uses a no-op sleeper.

## Implementation

- Added `src.notion.NotionSummaryUpserter`, with an injectable HTTP boundary, exact `2026-03-11` header, existing `data_source_id`, and credentials read only from `NOTION_API_KEY`.
- Reads the data-source schema and produces diagnostics for every required mapped property without issuing schema-write requests. It validates title, rich text, date, URL, number, multi-select, and status/select property types.
- Maps metadata and validated summary fields to Notion properties. The page content is Enhanced Markdown containing only the Japanese summary sections; no local `paper.md` content is accepted or sent.
- Finds a page in order: persisted page ID, DOI, arXiv ID, then Zotero item key. It updates properties plus `/pages/{id}/markdown` for exact single matches, and creates only when no match exists.
- Handles `429` Retry-After seconds and HTTP-date values; network and 5xx requests use three bounded attempts with exponential backoff. 400/401/403 and all other non-retryable responses fail immediately with sanitized diagnostics.
- Avoids duplicate pages after an ambiguous create result: before any create retry it re-queries the canonical identities, recovers the page if found, and updates it.
- Added a backward-compatible `papers.notion_page_id` migration plus `JobStore.get_notion_page_id` / `set_notion_page_id` so a resumed pipeline can retain the page ID.

## Strict TDD evidence

### Cycle 1: offline Notion contract surface

RED command:

```text
uv run pytest -q tests/test_notion.py
```

RED output:

```text
FFFFFFF                                                                  [100%]
ModuleNotFoundError: No module named 'src.notion'
7 failed in 0.18s
```

GREEN command:

```text
uv run pytest -q tests/test_notion.py
```

GREEN output:

```text
.......                                                                  [100%]
7 passed in 0.09s
```

This covers schema diagnostics, property and Enhanced Markdown payloads, stored-ID precedence, DOI precedence, creation, Retry-After seconds, exponential retry, and non-retryable authentication errors.

### Cycle 2: durable stored page ID

RED command:

```text
uv run pytest -q tests/test_job_store.py -k notion_page_id
```

RED output:

```text
F                                                                        [100%]
AttributeError: 'JobStore' object has no attribute 'get_notion_page_id'
1 failed, 13 deselected in 0.22s
```

GREEN command:

```text
uv run pytest -q tests/test_job_store.py -k notion_page_id
```

GREEN output:

```text
.                                                                        [100%]
1 passed, 13 deselected in 0.18s
```

### Cycle 3: mapped status type

RED command:

```text
uv run pytest -q tests/test_notion.py -k select_processing
```

RED output:

```text
F                                                                        [100%]
AssertionError: {'status': {'name': 'Completed'}} != {'select': {'name': 'Completed'}}
1 failed, 7 deselected in 0.21s
```

GREEN command:

```text
uv run pytest -q tests/test_notion.py tests/test_job_store.py
```

GREEN output:

```text
......................                                                   [100%]
22 passed in 0.63s
```

### Cycle 4: HTTP-date rate-limit handling

RED command:

```text
uv run pytest -q tests/test_notion.py -k http_date
```

RED output:

```text
F                                                                        [100%]
assert [0.5] == [3.0]
1 failed, 8 deselected in 0.16s
```

GREEN command:

```text
uv run pytest -q tests/test_notion.py tests/test_job_store.py
```

GREEN output:

```text
.......................                                                  [100%]
23 passed in 0.52s
```

### Cycle 5: ambiguous create recovery

RED command:

```text
uv run pytest -q tests/test_notion.py -k ambiguous_create
```

RED output:

```text
F                                                                        [100%]
src.notion.NotionAPIError: Notion API network failure
1 failed, 9 deselected in 0.19s
```

GREEN command:

```text
uv run pytest -q tests/test_notion.py tests/test_job_store.py
```

GREEN output:

```text
........................                                                 [100%]
24 passed in 0.52s
```

## Final verification

```text
git diff --check
uv run pytest -q
uv run python -m compileall -q src tests
uv run python -c "from src.notion import NotionSummaryUpserter, NotionSchemaError; from src.job_store import JobStore; print('Task 6 imports OK')"
```

Output:

```text
........................................................................ [ 36%]
........................................................................ [ 72%]
.......................................................                  [100%]
199 passed in 4.96s
Task 6 imports OK
```

`git diff --check` and `compileall` exited 0 with no output. Git emitted only Windows LF-to-CRLF notices while inspecting tracked files.

## Files changed

- `src/notion.py` — injected Notion client, schema diagnostics, payload/Enhanced Markdown rendering, lookup/upsert, retry and ambiguous-create recovery.
- `src/job_store.py` — additive persisted Notion page ID migration and accessors.
- `tests/test_notion.py` — deterministic contract tests; no live service access.
- `tests/test_job_store.py` — Notion page-ID persistence test.

## Self-review

- Checked each Task 6 requirement against the implementation and test surface.
- Mutation checks covered: missing or wrong schema property type, writing a `status` payload to a `select` property, changing lookup precedence, creating after an existing DOI match, retrying auth failures, ignoring Retry-After, treating an HTTP date as the default backoff, serializing the API key, accepting an invalid Japanese summary, and retrying an uncertain page creation without an identity recheck.
- The client has no endpoint that can create or update a data-source schema. It only retrieves the schema, queries entries, and creates/updates pages.
- The generic retry loop is used only for safe/repeatable reads and page updates. Page creation uses one attempt at a time plus identity recovery before any retry, preserving idempotency after a lost response.
- Errors intentionally omit response bodies and authorization values.

## Concerns

- The existing Notion data source must already contain the mapped properties and a `Completed` option in its Processing Status property; schema type validation is diagnostic-only and intentionally never adds options or properties.
- Task 7 must call `set_notion_page_id` after a successful upsert and pass `get_notion_page_id` into the next upsert to use the durable fast path.

---

## Critical review remediation (2026-09-03)

### Findings resolved

1. `PATCH /v1/pages/{page_id}/markdown` now sends the documented Enhanced Markdown replace operation exactly:

   ```json
   {"type":"replace_content","replace_content":{"new_str":"..."}}
   ```

   Contract tests assert this complete shape for both the stored-page-ID update path and the DOI identity-match update path.

2. Schema validation now retains the configured Processing Status/select and Topics/multi-select option name-to-ID mappings. Property writes use only existing IDs, reject an unknown status or topic before any query/page write, and therefore cannot add arbitrary options to the schema. `AI Keywords` is now required and written as rich text (the comma-separated validated keyword list), never as a multi-select value.

### Strict TDD evidence

RED command:

```text
uv run pytest -q tests/test_notion.py -k "create_payload or stored_page_id or identity_lookup or select_processing or unknown_controlled or ai_keywords"
```

RED output:

```text
FFFFFFF                                                                  [100%]
Notion data source schema invalid: AI Keywords: expected multi_select, got rich_text
AssertionError: {'status': {'name': 'Completed'}} != {'status': {'id': 'status-complete'}}
Failed: DID NOT RAISE NotionSchemaError
7 failed, 6 deselected in 0.22s
```

The failures demonstrated the prior multi-select AI Keyword contract, arbitrary status/topic name writes, and invalid markdown update body. The tests were adjusted to the approved existing-data-source schema before implementation; the remaining production behavior failed for the stated safety contract.

GREEN command:

```text
uv run pytest -q tests/test_notion.py
```

GREEN output:

```text
.............                                                            [100%]
13 passed in 0.09s
```

### Remediation verification

```text
git diff --check
uv run pytest -q
uv run python -m compileall -q src tests
uv run python -c "from src.notion import NotionSummaryUpserter, NotionSchemaError; print('Task 6 review imports OK')"
```

Output:

```text
........................................................................ [ 35%]
........................................................................ [ 71%]
..........................................................               [100%]
202 passed in 4.78s
Task 6 review imports OK
```

`git diff --check` and `compileall` exited 0. No live Notion requests were made.

### Remediation self-review

- Mutating `replace_content` back to a bare `markdown` key fails the stored-page and DOI-update payload assertions.
- Mutating controlled writes from option IDs back to names fails the create payload contract.
- Removing an option mapping or passing an unknown topic/status fails before any page-side request; only the read-only schema retrieval is observed.
- Changing `AI Keywords` back to multi-select fails schema validation and the rich-text create-payload assertion.
- No endpoint updates a data source or its properties; all schema option handling is read-only.
