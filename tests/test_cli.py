from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.cli import main
from src.notion import NotionConfigurationError
from src.research_models import InputKind, InputSpec, JobRecord, JobState


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"input_dir: {tmp_path / 'input'}\noutput_dir: {tmp_path / 'output'}\n",
        encoding="utf-8",
    )
    return path


def _job(state: JobState, *, job_id: str = "job-1", error: str | None = None) -> JobRecord:
    return JobRecord(
        id=job_id,
        paper_id=7,
        state=state,
        input_spec=InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"),
        artifact_dir=Path("C:/artifacts/paper"),
        error=error,
        total_cost_usd=0.125,
        max_cost_usd=0.30,
    )


class FakeCLIService:
    def __init__(self, result: JobRecord | None = None) -> None:
        self.result = result or _job(JobState.COMPLETED)
        self.collection_args: tuple[InputSpec, float, bool] | None = None
        self.ingest_args: tuple[InputSpec, float] | None = None
        self.resume_args: tuple[str, float | None, str | None, str | None] | None = None

    def ingest(self, spec: InputSpec, max_cost_usd: float) -> JobRecord:
        self.ingest_args = (spec, max_cost_usd)
        return self.result

    def ingest_collection(
        self, spec: InputSpec, *, max_cost_usd: float, only_unprocessed: bool,
    ) -> list[JobRecord]:
        self.collection_args = (spec, max_cost_usd, only_unprocessed)
        return [self.result]

    def resume(
        self, job_id: str, max_cost_usd: float | None = None, *,
        attachment_key: str | None = None, research_interest: str | None = None,
    ) -> JobRecord:
        self.resume_args = (
            job_id, max_cost_usd, attachment_key, research_interest,
        )
        return self.result

    def status(self, job_id: str) -> JobRecord:
        return self.result


def test_collection_ingest_forwards_all_overrides_and_emits_stable_json(
    tmp_path: Path,
) -> None:
    service = FakeCLIService()
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        [
            "ingest", "collection:COLLECT1", "--config", str(_config(tmp_path)),
            "--research-interest", "graph learning", "--attachment-key", "ATTACH01",
            "--max-cost-usd", "0.30", "--only-unprocessed", "--json",
        ],
        service_factory=lambda settings: service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert service.collection_args is not None
    spec, cost, only_unprocessed = service.collection_args
    assert spec == InputSpec(
        InputKind.ZOTERO_COLLECTION, "COLLECT1", "ATTACH01", "graph learning",
    )
    assert cost == 0.30
    assert only_unprocessed is True
    assert json.loads(stdout.getvalue()) == {
        "jobs": [{
            "artifact_dir": "C:\\artifacts\\paper",
            "error": None,
            "id": "job-1",
            "input_kind": "zotero_item",
            "input_source": "PARENT01",
            "max_cost_usd": 0.3,
            "paper_id": 7,
            "state": "completed",
            "total_cost_usd": 0.125,
        }],
        "type": "batch",
    }


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    [
        (JobState.COMPLETED, 0),
        (JobState.FAILED, 1),
        (JobState.NEEDS_INPUT, 2),
        (JobState.BUDGET_EXCEEDED, 3),
    ],
)
def test_status_uses_stable_text_and_state_exit_codes(
    tmp_path: Path, state: JobState, expected_exit: int,
) -> None:
    service = FakeCLIService(_job(state, error="action required" if expected_exit else None))
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        ["status", "job-1", "--config", str(_config(tmp_path))],
        service_factory=lambda settings: service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == expected_exit
    assert stdout.getvalue() == (
        f"job job-1: {state.value} | cost_usd=0.125000 | "
        f"max_cost_usd=0.300000"
        + (" | error=action required" if expected_exit else "")
        + "\n"
    )
    assert stderr.getvalue() == ""


def test_resume_forwards_user_action_overrides(tmp_path: Path) -> None:
    service = FakeCLIService()
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        [
            "resume", "job-1", "--config", str(_config(tmp_path)),
            "--attachment-key", "ATTACH02", "--research-interest", "causality",
            "--max-cost-usd", "0.75", "--json",
        ],
        service_factory=lambda settings: service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert service.resume_args == ("job-1", 0.75, "ATTACH02", "causality")
    assert json.loads(stdout.getvalue())["state"] == "completed"
    assert stderr.getvalue() == ""


def test_only_unprocessed_rejects_a_non_collection_instead_of_being_ignored(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
    service = FakeCLIService()
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        [
            "ingest", str(source), "--config", str(_config(tmp_path)),
            "--only-unprocessed",
        ],
        service_factory=lambda settings: service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert service.ingest_args is None
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "error: --only-unprocessed requires a Zotero collection\n"
    )


def test_missing_configuration_has_a_sanitized_actionable_diagnostic(
    tmp_path: Path,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        ["status", "job-1", "--config", str(_config(tmp_path))],
        service_factory=lambda settings: (_ for _ in ()).throw(
            NotionConfigurationError("NOTION_API_KEY is required")
        ),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "error: NOTION_API_KEY is required\n"


def test_config_option_is_also_accepted_before_the_subcommand(tmp_path: Path) -> None:
    service = FakeCLIService()
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        ["--config", str(_config(tmp_path)), "status", "job-1"],
        service_factory=lambda settings: service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue().startswith("job job-1: completed")
    assert stderr.getvalue() == ""
