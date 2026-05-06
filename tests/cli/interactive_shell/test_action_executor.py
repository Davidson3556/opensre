"""Direct unit tests for ``action_executor`` (complement to ``test_agent_actions``)."""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from app.cli.interactive_shell import action_executor, shell_execution
from app.cli.interactive_shell.action_executor import (
    read_diag,
    run_cd_command,
    run_pwd_command,
    run_shell_command,
    run_synthetic_test,
    terminate_child_process,
)
from app.cli.interactive_shell.session import ReplSession
from app.cli.interactive_shell.shell_policy import PolicyDecision


def test_terminate_child_process_noop_when_exited() -> None:
    proc = MagicMock()
    proc.poll.return_value = 0
    terminate_child_process(proc)
    proc.terminate.assert_not_called()


def test_read_diag_respects_byte_cap() -> None:
    buf: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile()  # type: ignore[type-arg]  # noqa: SIM115
    buf.write(b"z" * 5_000)
    text = read_diag(buf)
    buf.close()
    assert len(text) == 2_000


def test_run_pwd_command_prints_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_cwd(_: type[Path]) -> PurePosixPath:
        return PurePosixPath("/shown/pwd")

    monkeypatch.setattr(Path, "cwd", classmethod(_fake_cwd))

    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_pwd_command("pwd", session, console)
    assert "/shown/pwd" in buf.getvalue()
    assert session.history[-1]["type"] == "shell"


def test_run_pwd_command_rejects_multiple_tokens() -> None:
    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_pwd_command("pwd extra", session, console)
    assert "too many arguments" in buf.getvalue().lower()
    assert session.history[-1]["ok"] is False


def test_run_cd_command_chdirs_to_target(monkeypatch: pytest.MonkeyPatch) -> None:
    directories: list[Path] = []

    def _chdir(target: Path) -> None:
        directories.append(target)

    monkeypatch.setattr("app.cli.interactive_shell.action_executor.os.chdir", _chdir)

    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_cd_command("cd /tmp/example", session, console)
    assert directories == [Path("/tmp/example")]
    assert session.history[-1]["type"] == "shell"


def test_run_shell_command_records_when_policy_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    hint = "Run mutating commands directly in your shell if you truly intend them."
    monkeypatch.setattr(
        "app.cli.interactive_shell.action_executor.evaluate_policy",
        lambda **_: PolicyDecision(
            allow=False,
            classification="mutating",
            reason="test block",
            hint=hint,
        ),
    )

    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_shell_command("rm -rf /nope", session, console)

    output = buf.getvalue()
    assert "test block" in output
    assert "directly in your shell" in output
    assert session.history[-1] == {"type": "shell", "text": "rm -rf /nope", "ok": False}


def test_run_shell_command_uses_structured_argv_no_shell_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured execution must always pass shell=False with a parsed argv list."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr(shell_execution.subprocess, "run", _fake_run)

    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_shell_command('cat "/tmp/file with spaces.txt"', session, console)

    assert calls == [
        (
            ["cat", "/tmp/file with spaces.txt"],
            {
                "shell": False,
                "capture_output": True,
                "text": True,
                "timeout": action_executor.SHELL_COMMAND_TIMEOUT_SECONDS,
                "check": False,
            },
        )
    ]
    assert session.history[-1]["ok"] is True


def test_run_shell_command_records_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["echo"], timeout=1)

    monkeypatch.setattr(shell_execution.subprocess, "run", _raise_timeout)

    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_shell_command("echo hello", session, console)

    assert "timed out" in buf.getvalue()
    assert session.history[-1] == {"type": "shell", "text": "echo hello", "ok": False}


def test_run_shell_command_rejects_passthrough_prefix() -> None:
    """`!cmd` no longer escapes structured execution; classification falls through."""
    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_shell_command("!echo hi", session, console)

    output = buf.getvalue()
    assert "command blocked" in output
    assert session.history[-1] == {"type": "shell", "text": "!echo hi", "ok": False}


def test_run_synthetic_test_unknown_suite_records_failure() -> None:
    session = ReplSession()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False)

    run_synthetic_test("nonexistent_suite", session, console)
    assert "unknown synthetic" in buf.getvalue().lower()
    entry = session.history[-1]
    assert entry["type"] == "synthetic_test"
    assert entry["ok"] is False
