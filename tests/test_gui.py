import os
import queue
import sys
import threading
from pathlib import Path

import pytest

from src.config import Settings
from src.gui import (
    BatchRunController,
    DiagnosticCheck,
    DiagnosticsFinished,
    GuiOptions,
    PipelineGuiOptions,
    PipelineRunController,
    PipelineRunFinished,
    PipelineStatusFinished,
    ResearchViewModel,
    RunFailed,
    RunFinished,
    build_pipeline_input,
    build_settings,
    list_input_pdfs,
    open_output_folder,
    run_pipeline_diagnostics,
)
from src.pipeline_events import PipelineEvent
from src.research_models import InputKind, InputSpec, JobRecord, JobState


def _pdf_folder(tmp_path: Path) -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "paper.pdf").write_bytes(b"%PDF-1.4")
    return input_dir


def test_build_settings_maps_gui_values(tmp_path: Path) -> None:
    input_dir = _pdf_folder(tmp_path)
    output_dir = tmp_path / "new-output"
    settings = build_settings(GuiOptions(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        enable_ocr=False,
        force_ocr=True,
        language=" deu ",
        deskew=False,
        clean=False,
        skip_existing=True,
    ))

    assert settings.input_dir == input_dir
    assert settings.output_dir == output_dir
    assert output_dir.is_dir()
    assert settings.engine == "marker"
    assert settings.language == "deu"
    assert settings.enable_ocr is False
    assert settings.force_ocr is True
    assert settings.ocr_deskew is False
    assert settings.ocr_clean is False
    assert settings.skip_existing is True
    assert settings.workers == 1


def test_list_input_pdfs_rejects_missing_or_empty_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        list_input_pdfs(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No PDF"):
        list_input_pdfs(empty)


def test_build_settings_rejects_output_file(tmp_path: Path) -> None:
    input_dir = _pdf_folder(tmp_path)
    output_file = tmp_path / "output"
    output_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="not a folder"):
        build_settings(GuiOptions(str(input_dir), str(output_file)))


def test_build_settings_reports_output_creation_failure(mocker, tmp_path: Path) -> None:
    input_dir = _pdf_folder(tmp_path)
    mocker.patch.object(Path, "mkdir", side_effect=PermissionError("denied"))
    with pytest.raises(ValueError, match="Cannot create output folder"):
        build_settings(GuiOptions(str(input_dir), str(tmp_path / "blocked-output")))


def test_batch_run_controller_publishes_events_and_completion(tmp_path: Path) -> None:
    messages = queue.Queue()
    settings = Settings(tmp_path, tmp_path / "out")

    def fake_runner(_settings, on_event):
        on_event(PipelineEvent(kind="batch_started", total=1))
        return {"total": 1, "success": 1, "failed": 0, "failed_files": []}

    controller = BatchRunController(messages, fake_runner)
    controller.start(settings)
    controller.wait(2)

    assert controller.running is False
    assert isinstance(messages.get_nowait(), PipelineEvent)
    finished = messages.get_nowait()
    assert isinstance(finished, RunFinished)
    assert finished.summary["success"] == 1


def test_batch_run_controller_reports_failure(tmp_path: Path) -> None:
    messages = queue.Queue()

    def broken_runner(_settings, _on_event):
        raise RuntimeError("boom")

    controller = BatchRunController(messages, broken_runner)
    controller.start(Settings(tmp_path, tmp_path / "out"))
    controller.wait(2)
    result = messages.get_nowait()
    assert isinstance(result, RunFailed)
    assert result.error == "boom"


def test_batch_run_controller_prevents_overlapping_runs(tmp_path: Path) -> None:
    messages = queue.Queue()
    release = threading.Event()

    def blocking_runner(_settings, _on_event):
        release.wait(2)
        return {"total": 0, "success": 0, "failed": 0, "failed_files": []}

    controller = BatchRunController(messages, blocking_runner)
    settings = Settings(tmp_path, tmp_path / "out")
    controller.start(settings)
    with pytest.raises(RuntimeError, match="already running"):
        controller.start(settings)
    release.set()
    controller.wait(2)


def test_open_output_folder_uses_windows_file_manager(mocker, tmp_path: Path) -> None:
    startfile = mocker.Mock()
    mocker.patch.object(sys, "platform", "win32")
    mocker.patch.object(os, "startfile", startfile, create=True)
    open_output_folder(tmp_path)
    startfile.assert_called_once_with(str(tmp_path.resolve()))


def _job(tmp_path: Path, *, state: JobState = JobState.COMPLETED) -> JobRecord:
    return JobRecord(
        id="job-1",
        paper_id=1,
        state=state,
        input_spec=InputSpec(InputKind.ARXIV, "2401.01234"),
        artifact_dir=tmp_path / "papers" / "paper-1",
        total_cost_usd=0.0125,
        max_cost_usd=0.25,
    )


def test_build_pipeline_input_maps_and_validates_form_values() -> None:
    spec, budget = build_pipeline_input(
        PipelineGuiOptions(
            input_kind="zotero_item",
            input_value=" ABCD1234 ",
            attachment_key=" ATTACH1 ",
            research_interest=" graph learning ",
            max_cost_usd="0.25",
        ),
        default_research_interest="ignored default",
    )

    assert spec == InputSpec(
        InputKind.ZOTERO_ITEM,
        "ABCD1234",
        attachment_key="ATTACH1",
        research_interest="graph learning",
    )
    assert budget == 0.25

    with pytest.raises(ValueError, match="non-negative finite number"):
        build_pipeline_input(PipelineGuiOptions(input_value="x", max_cost_usd="nan"))


def test_build_pipeline_input_uses_configured_research_interest_default() -> None:
    spec, _ = build_pipeline_input(
        PipelineGuiOptions(input_kind="arxiv", input_value="2401.01234"),
        default_research_interest=" trustworthy AI ",
    )

    assert spec.research_interest == "trustworthy AI"


def test_pipeline_gui_defaults_use_configured_budget(tmp_path: Path) -> None:
    settings = Settings(tmp_path, tmp_path / "out")
    settings.openrouter.paper_budget_usd = 0.35

    options = PipelineGuiOptions.from_settings(settings)

    assert options.max_cost_usd == "0.35"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            PipelineGuiOptions(
                input_kind="arxiv",
                input_value="https://arxiv.org/abs/2401.01234v2",
            ),
            InputSpec(InputKind.ARXIV, "2401.01234"),
        ),
        (
            PipelineGuiOptions(
                input_kind="zotero_collection", input_value="abcd1234",
            ),
            InputSpec(InputKind.ZOTERO_COLLECTION, "ABCD1234"),
        ),
    ],
)
def test_build_pipeline_input_normalizes_values_for_selected_type(
    options: PipelineGuiOptions, expected: InputSpec,
) -> None:
    spec, _ = build_pipeline_input(options)

    assert spec == expected


class _FakePipelineService:
    def __init__(self, job: JobRecord) -> None:
        self.job = job
        self.calls: list[tuple[object, ...]] = []
        self.zotero = type("Zotero", (), {"is_available": lambda self: True})()
        self.notion = type("Notion", (), {"validate_schema": lambda self: None})()

    def ingest(self, spec: InputSpec, budget: float) -> JobRecord:
        self.calls.append(("ingest", spec, budget))
        return self.job

    def ingest_collection(
        self, spec: InputSpec, *, max_cost_usd: float, only_unprocessed: bool,
    ) -> list[JobRecord]:
        self.calls.append(("collection", spec, max_cost_usd, only_unprocessed))
        return [self.job]

    def resume(self, job_id: str, budget: float | None, **kwargs) -> JobRecord:
        self.calls.append(("resume", job_id, budget, kwargs))
        return self.job

    def status(self, job_id: str) -> JobRecord:
        self.calls.append(("status", job_id))
        return self.job


def test_pipeline_controller_runs_ingest_on_worker_and_forwards_events(
    tmp_path: Path,
) -> None:
    messages = queue.Queue()
    service = _FakePipelineService(_job(tmp_path))
    worker_threads: list[int] = []

    def factory(_settings, on_event):
        worker_threads.append(threading.get_ident())
        on_event(PipelineEvent(kind="stage_changed", stage="acquiring"))
        return service

    controller = PipelineRunController(messages, service_factory=factory)
    main_thread = threading.get_ident()
    spec = InputSpec(InputKind.ARXIV, "2401.01234")
    controller.start_ingest(Settings(tmp_path, tmp_path / "out"), spec, 0.25)
    controller.wait(2)

    event = messages.get_nowait()
    finished = messages.get_nowait()
    assert isinstance(event, PipelineEvent)
    assert event.stage == "acquiring"
    assert isinstance(finished, PipelineRunFinished)
    assert finished.jobs == (service.job,)
    assert worker_threads != [main_thread]
    assert service.calls == [("ingest", spec, 0.25)]


def test_pipeline_controller_supports_collection_status_and_resume(tmp_path: Path) -> None:
    messages = queue.Queue()
    service = _FakePipelineService(_job(tmp_path, state=JobState.NEEDS_INPUT))
    controller = PipelineRunController(
        messages, service_factory=lambda _settings, _on_event: service,
    )
    settings = Settings(tmp_path, tmp_path / "out")
    collection = InputSpec(InputKind.ZOTERO_COLLECTION, "COLL1")

    controller.start_ingest(settings, collection, 0.4, only_unprocessed=True)
    controller.wait(2)
    assert isinstance(messages.get_nowait(), PipelineRunFinished)
    assert service.calls[-1] == ("collection", collection, 0.4, True)

    controller.start_status(settings, "job-1")
    controller.wait(2)
    assert isinstance(messages.get_nowait(), PipelineStatusFinished)
    assert service.calls[-1] == ("status", "job-1")

    controller.start_resume(
        settings, "job-1", max_cost_usd=0.6,
        attachment_key="ATTACH2", research_interest="causality",
    )
    controller.wait(2)
    resumed = messages.get_nowait()
    assert isinstance(resumed, PipelineRunFinished)
    assert service.calls[-1] == (
        "resume", "job-1", 0.6,
        {"attachment_key": "ATTACH2", "research_interest": "causality"},
    )


def test_pipeline_diagnostics_are_read_only_and_report_each_boundary(tmp_path: Path) -> None:
    settings = Settings(tmp_path, tmp_path / "out")
    service = _FakePipelineService(_job(tmp_path))

    checks = run_pipeline_diagnostics(
        settings,
        service_factory=lambda _settings, _on_event: service,
        environ={"OPENROUTER_API_KEY": "configured", "NOTION_API_KEY": "configured"},
    )

    assert checks == (
        DiagnosticCheck("Local storage", True, str(settings.state_path)),
        DiagnosticCheck("OpenRouter", True, "API key configured; no paid request sent"),
        DiagnosticCheck("Zotero local API", True, settings.zotero.base_url),
        DiagnosticCheck("Notion data source", True, "Schema valid; no content written"),
    )


def test_pipeline_controller_runs_diagnostics_on_worker(tmp_path: Path) -> None:
    messages = queue.Queue()
    expected = (DiagnosticCheck("Zotero local API", False, "not running"),)
    controller = PipelineRunController(
        messages,
        diagnostics_runner=lambda _settings, **_kwargs: expected,
    )

    controller.start_diagnostics(Settings(tmp_path, tmp_path / "out"))
    controller.wait(2)

    result = messages.get_nowait()
    assert isinstance(result, DiagnosticsFinished)
    assert result.checks == expected


def test_research_view_model_projects_stage_cost_status_logs_and_diagnostics(
    tmp_path: Path,
) -> None:
    model = ResearchViewModel()
    model.apply(PipelineEvent(
        kind="stage_changed", pdf_name="job-1", stage="extracting",
        message="Summarizing sections",
    ))
    assert model.job_id == "job-1"
    assert model.stage == "extracting"
    assert model.status == "Running"
    assert model.logs == ["[extracting] Summarizing sections"]

    model.apply(PipelineStatusFinished(_job(tmp_path)))
    assert model.status == "completed"
    assert model.stage == "completed"
    assert model.actual_cost == "$0.012500"
    assert model.artifact_dir == str(tmp_path / "papers" / "paper-1")

    model.apply(DiagnosticsFinished((
        DiagnosticCheck("OpenRouter", True, "API key configured"),
        DiagnosticCheck("Zotero local API", False, "not running"),
    )))
    assert model.status == "Diagnostics: attention needed"
    assert model.logs[-2:] == [
        "[OK] OpenRouter: API key configured",
        "[FAIL] Zotero local API: not running",
    ]


def test_research_view_model_exposes_resumable_terminal_state(tmp_path: Path) -> None:
    model = ResearchViewModel()
    waiting = _job(tmp_path, state=JobState.NEEDS_INPUT)
    waiting.error = "Select one attachment"

    model.apply(PipelineRunFinished((waiting,)))

    assert model.can_resume is True
    assert model.status == "needs_input: Select one attachment"
    assert model.actual_cost == "$0.012500"


def test_research_view_model_logs_stage_events_without_messages() -> None:
    model = ResearchViewModel()

    model.apply(PipelineEvent(kind="stage_changed", stage="quality_check"))

    assert model.logs == ["[quality_check] Started"]
