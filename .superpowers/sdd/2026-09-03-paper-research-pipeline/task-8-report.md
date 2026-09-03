# Task 8 report: GUI, documentation, and end-to-end verification

## Status

Complete. The desktop app retains the legacy batch converter in its own tab and
adds a research-pipeline tab backed by the same `PipelineService` as the CLI.
All external and paid boundaries in tests are injected deterministic fakes; no
live or paid request was made.

## Implementation

- Added display-independent `PipelineGuiOptions`, validated/normalized form
  mapping, `PipelineRunController`, queue result types, diagnostics, and
  `ResearchViewModel` behavior.
- The controller constructs the production service only through
  `create_pipeline_service`, runs ingest/collection/resume/status/diagnostics on
  a daemon worker, and publishes progress/results through the existing queue.
- Added a Tk notebook. **Batch conversion** preserves the folder converter;
  **Research pipeline** adds input type/value, local PDF browse, Zotero item or
  collection, attachment key, research-interest override, configured/default
  budget, job ID, stage, actual cost, status, artifact path, logs, diagnostics,
  status refresh, and resume.
- Diagnostics check local state composition, OpenRouter key presence without a
  completion request, Zotero reachability, and Notion schema without content
  writes. Errors are surfaced as individual checks.
- Added deterministic end-to-end coverage over the real orchestration,
  artifacts, SQLite checkpoints, and resume implementation. arXiv-style input
  creates exactly one artifact directory and one Notion upsert. An existing
  Zotero item with multiple attachments pauses with zero LLM calls/cost, then
  completes after selection; a second resume duplicates no conversion,
  summarization, Notion upsert, Zotero write, or artifact directory.
- Added `docs/research-pipeline.md`, linked it from the README, and expanded the
  sample YAML with secret-free research defaults.

## Strict TDD evidence

All commands used the repository's bundled CPython 3.12.13 and the existing
site packages because the checked-out `.venv` points to a removed Python
3.12.10 installation. `PYTHONDONTWRITEBYTECODE` and workspace-local pytest temp
directories avoided unrelated sandbox writes.

### Cycle 1: form mapping, worker, status, resume, and diagnostics

RED command:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); & '.\.task8-venv\Scripts\python.exe' -m pytest -q tests/test_gui.py
```

RED output:

```text
ImportError: cannot import name 'DiagnosticCheck' from 'src.gui'
1 error in 0.14s
```

GREEN command:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.task8-venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.test-tmp\task8-gui' tests/test_gui.py
```

GREEN output:

```text
..............                                                           [100%]
14 passed in 0.19s
```

### Cycle 2: display-independent stage/cost/status/log projection

RED command:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); & '.\.task8-venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.test-tmp\task8-gui-red2' tests/test_gui.py
```

RED output:

```text
ImportError: cannot import name 'ResearchViewModel' from 'src.gui'
1 error in 0.25s
```

GREEN command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.task8-venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.test-tmp\task8-gui-green2' tests/test_gui.py
................                                                         [100%]
16 passed in 0.19s
```

### Cycle 3: selected input-type normalization

RED command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\red-normalization' tests/test_gui.py -k normalizes_values
FF                                                                       [100%]
E   AssertionError: source: 'https://arxiv.org/abs/2401.01234v2' != '2401.01234'
E   AssertionError: source: 'abcd1234' != 'ABCD1234'
2 failed, 16 deselected in 0.24s
```

GREEN command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\green-normalization' tests/test_gui.py
..................                                                       [100%]
18 passed in 0.19s
```

### Cycle 4: configured GUI budget default

RED command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\red-defaults' tests/test_gui.py -k pipeline_gui_defaults
F                                                                        [100%]
E   AttributeError: type object 'PipelineGuiOptions' has no attribute 'from_settings'
1 failed, 18 deselected in 0.23s
```

GREEN command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\green-defaults' tests/test_gui.py
...................                                                      [100%]
19 passed in 0.20s
```

### Cycle 5: successful stages remain visible in the log

RED command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\red-stage-log' tests/test_gui.py -k logs_stage_events
F                                                                        [100%]
E   AssertionError: assert [] == ['[quality_check] Started']
1 failed, 19 deselected in 0.22s
```

GREEN command/output:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-temp\green-stage-log' tests/test_gui.py tests/test_pipeline_e2e.py
......................                                                   [100%]
22 passed in 0.49s
```

The end-to-end acceptance tests were additive verification of Task 7's existing
service behavior and passed on their first run:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.task8-venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.test-tmp\task8-e2e' tests/test_pipeline_e2e.py
..                                                                       [100%]
2 passed in 0.47s
```

Human-facing documentation and the commented sample configuration were edited
directly; they add no executable behavior.

## Final verification

Full suite:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.verification-final\pytest'
........................................................................ [ 28%]
........................................................................ [ 56%]
........................................................................ [ 85%]
.....................................                                    [100%]
253 passed in 10.03s
```

Compile/import and entrypoint smoke:

```text
$env:PYTHONPATH = (Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONPYCACHEPREFIX = (Resolve-Path '.verification-final'); & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m compileall -q src tests
[exit 0, no output]

& '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -c "from src.gui import Paper2MdApp, PipelineRunController, main; from src.pipeline import PipelineService; assert callable(main); print('GUI and pipeline imports OK')"
GUI and pipeline imports OK

& '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m src.cli --help
usage: python -m src.cli [-h] [--config CONFIG] {ingest,resume,status} ...
```

`git diff --check` exits 0; Git emits only its Windows LF-to-CRLF notices.

## Files changed

- `src/gui.py`
- `tests/test_gui.py`
- `tests/test_pipeline_e2e.py`
- `docs/research-pipeline.md`
- `README.md`
- `configs/config.yaml`
- `.superpowers/sdd/2026-09-03-paper-research-pipeline/task-8-report.md`

## Self-review

- Rechecked every Task 8 bullet against the Tk controls, pure view/controller
  tests, deterministic acceptance tests, and operator documentation.
- Mutation checks covered wrong input normalization, lost research-interest
  defaults, invalid/non-finite budgets, main-thread pipeline execution, dropped
  progress events, wrong collection/status/resume dispatch, hidden successful
  stages, missing cost/status projection, and repeated resume side effects.
- The controller and tests instantiate no individual production client. The
  composition factory is the single GUI construction point and accepts an event
  callback; tests replace it with complete fakes.
- Existing `BatchRunController`, settings mapping, batch event handling, and
  batch widgets remain available in the first tab. The full legacy suite passes.
- Diagnostics issue no OpenRouter completion and no Notion write. Notion schema
  validation is a read; Zotero availability is a read. The normal service
  composition initializes the local SQLite schema.
- The docs explicitly distinguish deterministic fake-backed verification from
  live-service checks and state all excluded-scope and license boundaries.
- No subagents were dispatched, as required.

## Concerns

- The repository's `.venv` launcher references a Python installation that no
  longer exists and `uv sync --offline` cannot rebuild all Marker dependencies
  because the cached Torch wheel is absent. Verification therefore used the
  bundled Python plus the existing `.venv/Lib/site-packages`; application users
  should recreate `.venv` in a normal network-enabled environment.
- GUI layout was import-smoked and its behavior was tested below the display
  boundary. No pixel/display automation was used, intentionally avoiding
  platform-fragile Tk tests.
- Live diagnostics can report current account/local-service configuration, but
  the automated suite makes no claim that live credentials or services work.

---

## Important review remediation

### Findings resolved

1. Replaced the shared GUI event queue and `_pipeline_running` source guess with
   source-owned batch and research queues. Both controllers may run
   concurrently, and identical `PipelineEvent` objects plus queued terminal
   results always reach their own handler.
2. Diagnostics now create/write/read/delete an output-directory probe,
   create/open the configured `JobStore` SQLite database, and run SQLite
   `quick_check`. Storage or service-initialization failures are reported under
   **Local storage**; Zotero and Notion are explicitly `Not checked` rather than
   receiving a misattributed error.
3. The Notion guide and sample config now list all 15 exact property names and
   accepted types. They explicitly require the predefined controlled
   `Completed` Processing Status option and predefined Topics multi-select
   options.
4. Every completed pipeline run now atomically writes `manifest.json` before
   the completed state transition. It contains canonical identity, source hash,
   hashes for every produced file (including figures/logs), job stage/status and
   costs, configured model/prompt version, and DOI/arXiv/Zotero/Notion IDs. It
   contains no credentials or artifact contents.

The two ledgered Minor GUI issues were intentionally left unchanged in this
round, per instruction.

### Focused TDD evidence

#### Source-owned concurrent event routing

RED:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\red-routing' tests/test_gui.py -k concurrent_controllers
ImportError: cannot import name 'drain_gui_events' from 'src.gui'
1 error in 0.30s
```

GREEN:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\green-routing' tests/test_gui.py -k concurrent_controllers
.                                                                        [100%]
1 passed, 20 deselected in 0.17s
```

#### Local output/SQLite diagnostics and failure classification

RED:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\red-diagnostics' tests/test_gui.py -k diagnostics
FF..                                                                     [100%]
E   AssertionError: assert settings.state_path.is_file()
E   assert factory_called is False
2 failed, 2 passed, 19 deselected in 0.25s
```

GREEN:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\green-diagnostics' tests/test_gui.py -k 'diagnostics or initialization_failure'
.....                                                                    [100%]
5 passed, 18 deselected in 0.26s
```

#### Complete atomic manifest

RED (manifest absent):

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\red-manifest' tests/test_pipeline_e2e.py
FF                                                                       [100%]
E   Extra items in the left set: 'manifest.json'
2 failed in 0.52s
```

RED (figure hash absent):

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\red-manifest-files' tests/test_pipeline_e2e.py -k arxiv_style
F                                                                        [100%]
E   Extra items in the right set: 'figures/figure_001.png'
1 failed, 1 deselected in 0.37s
```

GREEN:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-fix-temp\green-manifest-files' tests/test_pipeline_e2e.py tests/test_pipeline.py
..........................                                               [100%]
26 passed in 4.43s
```

The Notion schema and manifest documentation/config changes are human-facing
prose and sample configuration, so they were edited directly.

### Remediation verification

Focused GUI/E2E/pipeline suite:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-final\focused' tests/test_gui.py tests/test_pipeline_e2e.py tests/test_pipeline.py
.................................................                        [100%]
49 passed in 5.13s
```

Full suite:

```text
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages'); $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.uv-python\cpython-3.12-windows-x86_64-none\python.exe' -m pytest -q -p no:cacheprovider --basetemp '.review-final\pytest'
........................................................................ [ 28%]
........................................................................ [ 56%]
........................................................................ [ 84%]
........................................                                 [100%]
256 passed in 10.45s
```

Compile/import/entrypoint smoke after remediation:

```text
python -m compileall -q src tests
[exit 0, no output]
python -c "from src.gui import Paper2MdApp, PipelineRunController, drain_gui_events; from src.pipeline import PipelineService; print('Task 8 remediation imports OK')"
Task 8 remediation imports OK
python -m src.cli --help
usage: python -m src.cli [-h] [--config CONFIG] {ingest,resume,status} ...
```

`git diff --check` exited 0 with only the repository's Windows LF-to-CRLF
notices.

### Remediation self-review and concerns

- Verified both queue producers with the same event class and with terminal
  results waiting in each queue; no mode flag participates in dispatch.
- Verified a real state database is created and opened, invalid output storage
  prevents service construction, and initialization errors cannot be displayed
  as Zotero or Notion failures.
- Verified the manifest payload with literal identities/IDs/status, a hand-known
  PDF SHA-256, the exact file-key set, and both arXiv and existing-Zotero flows.
- Existing pipeline resume tests show manifest refresh adds no external or paid
  side effect. Artifact writes remain atomic through `ArtifactManager`.
- The previously recorded stale `.venv`, display-free Tk verification, and lack
  of live-service claims remain the only concerns.
