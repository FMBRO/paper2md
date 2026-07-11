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
