"""Helpers for running external tools while streaming their combined output."""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence


OutputCallback = Callable[[str], None]


def _write_console_line(line: str) -> None:
    text = line + "\n"
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    sys.stdout.write(text)
    sys.stdout.flush()


def run_streaming_command(
    command: Sequence[str],
    on_output: OutputCallback | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run *command* and return its exit code, forwarding output line by line.

    Both output streams are merged so callers see messages in process order.
    Invalid UTF-8 is replaced instead of aborting a long conversion.
    """
    with subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=dict(env) if env is not None else None,
    ) as process:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if on_output is None:
                    _write_console_line(line)
                else:
                    on_output(line)
        return process.wait()

