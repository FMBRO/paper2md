"""Helpers for running external tools while streaming their combined output."""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence


OutputCallback = Callable[[str], None]


def run_streaming_command(
    command: Sequence[str],
    on_output: OutputCallback | None = None,
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
    ) as process:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if on_output is None:
                    print(line, flush=True)
                else:
                    on_output(line)
        return process.wait()

