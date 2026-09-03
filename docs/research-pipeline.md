# Research pipeline setup and operation

The research pipeline acquires one paper, keeps its complete converted content
locally, creates a structured Japanese summary through OpenRouter, and upserts
that summary into an existing Notion data source. It can also begin from an
existing item or collection in a personal Zotero library.

The automated test suite uses injected fakes and deterministic local fixtures.
It does not verify your live OpenRouter, Notion, Zotero, arXiv, or Crossref
account or service availability. Use **Run diagnostics** in the GUI to check
your local configuration; diagnostics do not send an OpenRouter completion or
write Notion content.

## Prerequisites and secrets

Install the project dependencies as described in the main README. Set secrets
only in the process environment; never put them in `configs/config.yaml`:

```powershell
$env:OPENROUTER_API_KEY = "your-openrouter-key"
$env:NOTION_API_KEY = "your-notion-integration-secret"
```

On POSIX shells, use `export OPENROUTER_API_KEY=...` and
`export NOTION_API_KEY=...`. paper2md does not persist or intentionally log
these values.

### OpenRouter

The configured endpoint is fixed to
`https://openrouter.ai/api/v1/chat/completions`. The defaults are
`google/gemini-3.8-flash` for extraction and `openai/gpt-5.6-sol` for synthesis.
The request requires JSON Schema support and a provider that honors required
parameters, zero-data-retention routing, and disabled data collection. Model
IDs are not silently substituted. Your account must be able to use both
configured models and expose current pricing and resolved usage cost.

### Notion

Create a Notion integration, share an existing database/data source with that
integration, and put the integration secret in `NOTION_API_KEY`. Set
`notion.data_source_id` to the data source ID—not a page or database view ID.
The API version is `2026-03-11`.

paper2md validates the configured property names and types, but never modifies
the schema. The default names are listed in `configs/config.yaml`. In Notion,
create matching properties for title, authors, dates, identifiers, links,
status, relevance, topics, keywords, import time, and model/prompt version.
The controlled status/select and topic options must already exist. Only the
validated Japanese summary is written to the page body; full Markdown and the
structured document remain local.

### Zotero local API

Start the Zotero desktop application and enable its local API. paper2md uses
API v3 at `http://localhost:23119/api/` with personal-library user ID `0` by
default. Existing Zotero items are read-only. For a non-Zotero source, the
pipeline may create one regular parent item and one stored PDF attachment after
local Zotero write authorization. It first searches DOI/arXiv identifiers to
avoid duplicates.

If a Zotero parent has multiple PDFs, the job stops at `needs_input` before any
LLM call. Copy the desired child attachment key into the GUI or pass
`--attachment-key` while resuming. Linked or imported files must be accessible
to the Zotero desktop application.

## Configuration

Edit `configs/config.yaml`. The most important research settings are:

```yaml
research_interest: "graph learning and trustworthy AI"

zotero:
  base_url: http://localhost:23119/api/
  user_id: 0

openrouter:
  extraction_model: google/gemini-3.8-flash
  synthesis_model: openai/gpt-5.6-sol
  paper_budget_usd: 0.50

notion:
  data_source_id: "your-data-source-id"
  api_version: "2026-03-11"
```

`research_interest` is the default used to score relevance. A CLI or GUI value
overrides it for that job. Changing it on resume invalidates only the summary
and Notion stages, not acquisition or conversion.

Before each paid request, the application reserves a conservative worst-case
cost based on current model pricing and token limits. It fails closed when
pricing is unavailable or the request could exceed the paper budget. Actual
`usage.cost` is persisted after every successful call and shown in CLI/GUI job
status. A higher resume budget authorizes future work; it does not erase cost
already incurred. Cached successful calls are reused on resume.

## Command line

Run commands from the project root:

```text
uv run python -m src.cli ingest 2401.01234
uv run python -m src.cli ingest 10.1000/example --research-interest "causal ML"
uv run python -m src.cli ingest C:\papers\paper.pdf --max-cost-usd 0.35
uv run python -m src.cli ingest zotero://select/library/items/ABCD1234
uv run python -m src.cli ingest collection:ABCD1234 --only-unprocessed
uv run python -m src.cli status JOB_ID
uv run python -m src.cli resume JOB_ID --attachment-key ATTACHMENT_KEY
uv run python -m src.cli resume JOB_ID --max-cost-usd 0.75
```

Add `--json` for machine-readable output and `--config PATH` to use another
configuration file. `--only-unprocessed` is valid only for a Zotero collection.
Exit code 2 means user input or configuration is needed; exit code 3 means the
budget was exceeded; other failures return 1.

## Desktop GUI

Start the app with:

```text
uv run python -m src.gui
```

The **Batch conversion** tab preserves the legacy folder-based converter. The
**Research pipeline** tab accepts arXiv, DOI, PDF URL, local PDF, Zotero item,
and Zotero collection inputs. **Browse PDF** fills a local-PDF input. Set an
optional attachment key, research-interest override, and budget, then start the
pipeline. The stage, persisted actual cost, artifact path, status, and progress
log update through the Tk event queue while all pipeline work stays on a worker
thread.

For an interrupted or terminal job, paste its job ID and use **Refresh status**
or **Resume**. A completed resume validates checkpoints and has no duplicate
external side effects when artifacts are unchanged. **Run diagnostics** checks
local storage, the presence of the OpenRouter key, Zotero reachability, and the
Notion data-source schema. It performs no conversion, paid LLM request, or
Notion content write.

## Local artifacts and state

Per-paper files live under `output/papers/{stable-id}/`:

- `source.pdf`, `metadata.json`, `document.json`, and `paper.md`
- `figures/` and `logs/`, including the quality result
- `summary.json` and pipeline-produced metadata/checkpoints

Job, paper, Notion-page, checkpoint, LLM-cache, and cost state is in
`output/paper2md.sqlite3` unless `state_path` is configured. Preserve both the
artifact directory and database when moving or backing up an installation.

## Troubleshooting

- **`OPENROUTER_API_KEY is required`**: set it in the environment that launches
  the CLI or GUI. A shell variable set after the GUI starts is not inherited.
- **Pricing unavailable / budget exceeded**: verify both configured model IDs
  exist in OpenRouter's catalog. Increase the per-job cap only after reviewing
  the estimate; the pipeline never silently spends past the cap.
- **Notion key, permission, or schema error**: share the data source with the
  integration, confirm the data-source ID, API version, mapped names/types, and
  controlled options, then rerun diagnostics. The application will not repair
  or mutate the schema.
- **Zotero unreachable**: launch Zotero, enable its local API, keep the default
  personal-library URL/user ID unless you intentionally configured otherwise,
  and check local firewall/proxy rules.
- **Multiple or missing Zotero PDFs**: select an accessible PDF child and resume
  with its attachment key. No LLM cost is incurred before this is resolved.
- **Quality check blocks summarization**: inspect `paper.md`, `document.json`,
  and `logs/quality_result.json`; correct the source/OCR issue and resume.
- **Resume repeats conversion**: a missing or changed source, document,
  Markdown, or quality artifact deliberately invalidates dependent checkpoints.

## Privacy, scope, and licenses

The full PDF, Markdown, structured document, figures, and logs stay on the
local machine. The OpenRouter stages receive only the document chunks required
for structured extraction/synthesis under the enforced privacy-routing flags.
Notion receives only the validated Japanese summary and mapped metadata.

The project does not bypass paywalls or access controls. It does not implement
watchers, RAG/vector databases, Notero, group libraries, cloud deployment,
Zotero tag/note writeback, or full-Markdown publishing to Notion.

paper2md source code is licensed under AGPL-3.0-only; see `LICENSE`. That license
does not grant rights to papers you process, model outputs, third-party APIs,
models, Zotero, or Notion. You are responsible for source-document copyright,
privacy, and the separate terms, quotas, and charges of every external service.
