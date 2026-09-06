import io
from unittest.mock import MagicMock

from src.subprocess_utils import run_streaming_command


def test_run_streaming_command_merges_and_forwards_output(mocker) -> None:
    process = MagicMock()
    process.stdout = ["first line\n", "replacement: �\n"]
    process.wait.return_value = 7
    process.__enter__.return_value = process
    popen = mocker.patch("src.subprocess_utils.subprocess.Popen", return_value=process)
    messages: list[str] = []

    result = run_streaming_command(["tool", "arg"], messages.append)

    assert result == 7
    assert messages == ["first line", "replacement: �"]
    kwargs = popen.call_args.kwargs
    assert kwargs["stderr"] is not None
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_run_streaming_command_safely_writes_unencodable_console_output(
    mocker,
) -> None:
    process = MagicMock()
    process.stdout = ["replacement: �\n"]
    process.wait.return_value = 0
    process.__enter__.return_value = process
    mocker.patch("src.subprocess_utils.subprocess.Popen", return_value=process)
    raw_stdout = io.BytesIO()
    stdout = io.TextIOWrapper(raw_stdout, encoding="cp932", errors="strict")
    mocker.patch("sys.stdout", stdout)

    result = run_streaming_command(["tool"])
    stdout.flush()

    assert result == 0
    rendered = raw_stdout.getvalue().decode("cp932").replace("\r\n", "\n")
    assert rendered == "replacement: \\ufffd\n"


def test_run_streaming_command_passes_explicit_child_environment(mocker) -> None:
    """Dropping the child environment would make OCR tool discovery regress."""
    process = MagicMock()
    process.stdout = []
    process.wait.return_value = 0
    process.__enter__.return_value = process
    popen = mocker.patch("src.subprocess_utils.subprocess.Popen", return_value=process)
    child_environment = {"PATH": "C:\\Tools\\Tesseract-OCR"}

    result = run_streaming_command(
        ["ocrmypdf", "in.pdf", "out.pdf"], env=child_environment,
    )

    assert result == 0
    assert popen.call_args.kwargs["env"] == child_environment
