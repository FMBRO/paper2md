# Task 1 report — domain model, configuration, artifacts, and job state

## Implemented behavior

- Added `src.research_models` with `InputKind`, `InputSpec`, `PaperMetadata`,
  `ArtifactBundle`, `EvidenceAnchor`, `PaperSummary`, `JobState`, and
  `JobRecord`. DOI and arXiv values are normalized before canonical identity
  selection. Identity priority is DOI, arXiv, Zotero library/item, then PDF
  SHA-256.
- Extended the existing `Settings` loader without changing its legacy fields.
  It now exposes a default `<output>/paper2md.sqlite3` state path,
  research-interest setting, Zotero defaults, OpenRouter endpoint/model/privacy/
  budget defaults, and Notion data-source/property mappings. YAML API-key/
  secret fields are rejected so secrets remain environment-only. A CLI output
  override moves the derived state path unless YAML explicitly supplied one.
- Added `ArtifactManager`, which creates a per-paper layout with `source.pdf`,
  `metadata.json`, `document.json`, `paper.md`, `figures/`, `summary.json`,
  `manifest.json`, and `logs/`. Text and JSON writes use same-directory
  temporary files, `fsync`, and `os.replace`.
- Added `JobStore` using standard `sqlite3`. It initializes `papers`, `jobs`,
  and `llm_calls`, persists jobs and canonical papers, validates ordered state
  changes, locates non-completed resumable jobs, caches LLM responses, and
  records actual per-job cost without double-charging a cached request.

## Files changed

- Modified: `src/config.py`, `tests/test_config.py`
- Added: `src/research_models.py`, `src/artifacts.py`, `src/job_store.py`
- Added: `tests/test_research_models.py`, `tests/test_artifacts.py`,
  `tests/test_job_store.py`

## RED/GREEN evidence

The checked-in `.venv` launcher could not start because it references the
missing `C:\Users\yoshi\AppData\Local\Programs\Python\Python312\python.exe`.
All red/green runs therefore used `uv run --isolated --python 3.12 --with pytest`.

| Behavior | RED command and relevant output | GREEN command and relevant output |
|---|---|---|
| Domain models / normalized identity | `uv run --isolated --python 3.12 --with pytest python -m pytest tests\test_research_models.py -q` → `3 failed`, `ModuleNotFoundError: No module named 'src.research_models'` | Same command → `3 passed in 0.01s` |
| Pipeline configuration defaults | `... pytest tests\test_config.py -q` → `2 failed`, `AttributeError: 'Settings' object has no attribute 'state_path'` | Same command → `6 passed in 0.10s` |
| Atomic artifact manager | `... pytest tests\test_artifacts.py -q` → `1 failed`, `ModuleNotFoundError: No module named 'src.artifacts'` | Same command → `1 passed in 0.02s` |
| SQLite state transitions | `... pytest tests\test_job_store.py -q` → `1 failed`, `ModuleNotFoundError: No module named 'src.job_store'` | Same command → `1 passed in 0.12s` |
| State path follows output override | `... pytest tests\test_config.py::test_output_override_also_moves_default_state_database -q` → assertion expected `cli-out/paper2md.sqlite3`, got `yaml-out/paper2md.sqlite3` | Same command → `1 passed in 0.07s` |
| Cached LLM calls do not double-charge | `... pytest tests\test_job_store.py::test_replacing_a_cached_call_does_not_charge_a_job_twice -q` → assertion expected `0.12`, got `0.24` | Same command → `1 passed in 0.04s` |
| Paused jobs can be queued for resume | `... pytest tests\test_job_store.py::test_a_job_needing_input_can_be_queued_again_for_resume -q` → `ValueError: Invalid transition: needs_input -> queued` | Same command → `1 passed in 0.05s` |

## Full verification

Command:

```powershell
uv run --isolated --python 3.12 --with pytest python -m pytest -q
uv run --isolated --python 3.12 --with pytest python -m compileall -q src
git diff --check
```

Result: `68 passed in 0.71s`; the compile check and whitespace check exited
successfully.

## Self-review

- Public legacy settings fields and the existing batch-conversion suite remain
  intact.
- SQLite uses parameterized values; no ORM or secrets are persisted.
- Cache insertion is idempotent, so a resume cannot add the same call's cost
  twice.
- The artifact manager keeps partial text/JSON files out of the final paths.

## Concerns

- The repository's committed virtual environment is stale; use the recorded
  isolated Python 3.12 command until it is repaired. No project dependency or
  lockfile was changed for this task.

## Review fix round 1

### Implemented fixes

- Secret detection now recursively walks all YAML maps/lists, normalizes key
  spelling by removing punctuation and case, and rejects a denylist of API-key,
  access-key, secret, token, authorization, password, private-key, and bearer-
  token aliases. Composite aliases ending in a sensitive key name are also
  rejected. Secret values are never interpolated into the error.
- Every SQLite connection executes `PRAGMA foreign_keys = ON` immediately
  after opening, so `jobs.paper_id` and `llm_calls.job_id` references are
  enforced by SQLite.
- Paper upserts now find existing records by canonical identity and every
  available normalized identifier. They retain the original row, merge missing
  identifiers, promote canonical identity according to DOI/arXiv/Zotero/SHA
  priority, remap jobs if duplicate records must be consolidated, and allow
  lookup by a former SHA identity after DOI promotion.

### Files changed

- Modified: `src/config.py`, `src/job_store.py`, `tests/test_config.py`,
  `tests/test_job_store.py`

### RED/GREEN evidence

| Finding | RED command and output | GREEN command and output |
|---|---|---|
| Recursive YAML secret guard | `uv run --isolated --python 3.12 --with pytest python -m pytest tests\test_config.py::test_load_settings_rejects_common_yaml_secret_key_variants -q` → `4 failed`, each `Failed: DID NOT RAISE ValueError` for `apiKey`, `token`, `authorization`, and `password` | Same command → `4 passed in 0.12s` |
| Composite API-key alias | Same focused command after adding `openrouterApiKey` → `1 failed, 4 passed`, `Failed: DID NOT RAISE ValueError` | Same command → `5 passed in 0.08s` |
| SQLite foreign keys | `uv run --isolated --python 3.12 --with pytest python -m pytest tests\test_job_store.py::test_job_store_rejects_job_for_nonexistent_paper tests\test_job_store.py::test_job_store_rejects_llm_call_for_nonexistent_job -q` → `2 failed`, both `Failed: DID NOT RAISE Exception` | Same command → `2 passed in 0.06s` |
| Identifier reconciliation | `uv run --isolated --python 3.12 --with pytest python -m pytest tests\test_job_store.py::test_job_store_promotes_sha_identity_to_doi_and_merges_identifiers -q` → `1 failed`, expected original row ID `1`, got duplicate ID `2` | Same command → `1 passed in 0.05s` |

The first focused job/config aggregate run exposed an existing identity-
preservation expectation: a repeated DOI must not replace its already-known
SHA-256 with a conflicting value. The implementation was adjusted to merge
only missing identifier values; rerunning the focused suites produced `19
passed in 0.25s`.

### Final verification

```powershell
uv run --isolated --python 3.12 --with pytest python -m pytest -q
uv run --isolated --python 3.12 --with pytest python -m compileall -q src
git diff --check
```

Result: `76 passed in 0.83s`; compile and whitespace checks completed without
errors.

### Self-review

- The sensitive-key guard checks structure rather than values, so no secret is
  copied into an exception, log, artifact, or database.
- Foreign-key enforcement is configured per connection, as SQLite requires.
- Reconciliation is transactional under the existing connection context;
  duplicate paper jobs are reassigned before deleting the duplicate row.
- The deferred arXiv-prefix issue was not changed.
