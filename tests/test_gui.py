import os
import queue
import sys
import threading
from pathlib import Path

import pytest

from src.config import Settings
from src.gui import (
    BatchRunController,
    GuiOptions,
    RunFailed,
    RunFinished,
    build_settings,
    list_input_pdfs,
    open_output_folder,
)
from src.pipeline_events import PipelineEvent


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
