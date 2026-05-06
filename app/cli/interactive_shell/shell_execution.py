"""Structured shell command execution helpers for the interactive REPL."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ShellExecutionResult:
    """Normalized command execution output."""

    command: str
    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool


def _truncate_output(text: str, *, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return f"{text[:max_chars].rstrip()}\n... output truncated ...", True


def execute_shell_command(
    *,
    command: str,
    argv: list[str],
    timeout_seconds: int,
    max_output_chars: int,
) -> ShellExecutionResult:
    """Execute ``argv`` with ``shell=False`` and return a structured result."""
    completed = subprocess.run(  # noqa: S603 - argv is parsed and policy-gated upstream
        argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )

    stdout, truncated_stdout = _truncate_output(
        completed.stdout or "",
        max_chars=max_output_chars,
    )
    stderr, truncated_stderr = _truncate_output(
        completed.stderr or "",
        max_chars=max_output_chars,
    )
    return ShellExecutionResult(
        command=command,
        argv=argv,
        stdout=stdout,
        stderr=stderr,
        exit_code=completed.returncode,
        timed_out=False,
        truncated=truncated_stdout or truncated_stderr,
    )


__all__ = ["ShellExecutionResult", "execute_shell_command"]
