"""Command-line interface for the resumable research pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable, Sequence, TextIO

from src.acquisition import parse_input
from src.config import Settings, load_settings
from src.notion import NotionConfigurationError, NotionSchemaError
from src.openrouter import OpenRouterConfigurationError
from src.pipeline import NeedsInputError, PipelineService
from src.research_models import JobRecord, JobState


ServiceFactory = Callable[[Settings], PipelineService]


def _add_common_options(parser: argparse.ArgumentParser, *, include_json: bool = True) -> None:
    parser.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS,
        help="Pipeline YAML configuration (default: configs/config.yaml)",
    )
    if include_json:
        parser.add_argument("--json", action="store_true", help="Emit stable JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    parser.add_argument(
        "--config", type=Path, default=Path("configs/config.yaml"),
        help="Pipeline YAML configuration (default: configs/config.yaml)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Ingest one paper or Zotero collection")
    ingest.add_argument("source")
    ingest.add_argument("--research-interest")
    ingest.add_argument("--attachment-key")
    ingest.add_argument("--only-unprocessed", action="store_true")
    ingest.add_argument("--max-cost-usd", type=float)
    _add_common_options(ingest)

    resume = commands.add_parser("resume", help="Resume an existing job")
    resume.add_argument("job_id")
    resume.add_argument("--research-interest")
    resume.add_argument("--attachment-key")
    resume.add_argument("--max-cost-usd", type=float)
    _add_common_options(resume)

    status = commands.add_parser("status", help="Show one persisted job")
    status.add_argument("job_id")
    _add_common_options(status)
    return parser


def _job_payload(job: JobRecord) -> dict[str, object]:
    return {
        "id": job.id,
        "paper_id": job.paper_id,
        "state": job.state.value,
        "input_kind": job.input_spec.kind.value,
        "input_source": job.input_spec.source,
        "artifact_dir": str(job.artifact_dir) if job.artifact_dir else None,
        "error": job.error,
        "total_cost_usd": job.total_cost_usd,
        "max_cost_usd": job.max_cost_usd,
    }


def _write_job(job: JobRecord, *, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        stdout.write(json.dumps(_job_payload(job), ensure_ascii=False, sort_keys=True) + "\n")
        return
    line = (
        f"job {job.id}: {job.state.value} | cost_usd={job.total_cost_usd:.6f}"
        f" | max_cost_usd={job.max_cost_usd:.6f}"
    )
    if job.error:
        line += f" | error={job.error}"
    stdout.write(line + "\n")


def _exit_code(jobs: Sequence[JobRecord]) -> int:
    states = {job.state for job in jobs}
    if JobState.FAILED in states:
        return 1
    if JobState.NEEDS_INPUT in states:
        return 2
    if JobState.BUDGET_EXCEEDED in states:
        return 3
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: ServiceFactory = PipelineService,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        service = service_factory(settings)
        if args.command == "ingest":
            spec = parse_input(
                args.source,
                attachment_key=args.attachment_key,
                research_interest=args.research_interest,
            )
            max_cost = (
                settings.openrouter.paper_budget_usd
                if args.max_cost_usd is None else args.max_cost_usd
            )
            if spec.kind.value == "zotero_collection":
                jobs = service.ingest_collection(
                    spec, max_cost_usd=max_cost,
                    only_unprocessed=args.only_unprocessed,
                )
                if args.json:
                    stdout.write(json.dumps(
                        {"type": "batch", "jobs": [_job_payload(job) for job in jobs]},
                        ensure_ascii=False, sort_keys=True,
                    ) + "\n")
                else:
                    for job in jobs:
                        _write_job(job, as_json=False, stdout=stdout)
                    stdout.write(f"batch: {len(jobs)} job(s)\n")
                return _exit_code(jobs)
            if args.only_unprocessed:
                raise ValueError(
                    "--only-unprocessed requires a Zotero collection"
                )
            job = service.ingest(spec, max_cost)
        elif args.command == "resume":
            job = service.resume(
                args.job_id,
                args.max_cost_usd,
                attachment_key=args.attachment_key,
                research_interest=args.research_interest,
            )
        else:
            job = service.status(args.job_id)
        _write_job(job, as_json=args.json, stdout=stdout)
        return _exit_code([job])
    except (
        FileNotFoundError,
        KeyError,
        NeedsInputError,
        NotionConfigurationError,
        NotionSchemaError,
        OpenRouterConfigurationError,
        ValueError,
    ) as error:
        message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
        stderr.write(f"error: {message}\n")
        return 2
    except Exception as error:
        stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
