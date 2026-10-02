"""Boot-time import-budget guardrails for the interactive shell.

These lock in the lazy-loading wins so a future eager import in the boot path
(controller construction, banner, background workers, prompt/completion) fails
loudly instead of silently re-inflating REPL startup.
"""

from __future__ import annotations

import subprocess
import sys

# Modules that must NOT be pulled into the graph merely by importing the REPL
# entrypoint. Each maps to the lazy-loading change that keeps it out.
_FORBIDDEN_AT_BOOT: tuple[str, ...] = (
    # Full REPL construction — deferred until the launch indicator is running.
    "surfaces.interactive_shell.controller",
    "surfaces.interactive_shell.runtime.context",
    "surfaces.interactive_shell.runtime.core.state",
    "surfaces.interactive_shell.session.session",
    "surfaces.interactive_shell.ui.terminal_ui",
    "surfaces.shared.terminal.banner.banner",
    "prompt_toolkit",
    # Harness/turn-execution stack — deferred until the first turn is dispatched.
    "surfaces.interactive_shell.runtime.shell_turn_execution",
    "core.agent.agent",
    "core.agent_harness.turns.action_driver",
    # Email delivery — only loads on background-RCA completion.
    "integrations.smtp.delivery",
)

_FORBIDDEN_FROM_RUNTIME_FACADE: tuple[str, ...] = (
    "surfaces.interactive_shell.runtime.context",
    "surfaces.interactive_shell.session.session",
    "infrastructure.scheduling.task_registry",
)


def _modules_loaded_by(import_target: str, candidates: tuple[str, ...]) -> list[str]:
    check = "; ".join(f"print('{name}', '{name}' in sys.modules)" for name in candidates)
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; import {import_target}; {check}"],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded: list[str] = []
    for line in result.stdout.splitlines():
        name, _, flag = line.partition(" ")
        if flag.strip() == "True":
            loaded.append(name)
    return loaded


def test_repl_boot_import_stays_lazy() -> None:
    loaded = _modules_loaded_by(
        "surfaces.interactive_shell.main",
        _FORBIDDEN_AT_BOOT,
    )
    assert loaded == [], (
        "importing surfaces.interactive_shell.main eagerly pulled modules that "
        f"must load lazily: {loaded}"
    )


def test_runtime_package_facade_stays_lazy() -> None:
    loaded = _modules_loaded_by(
        "surfaces.interactive_shell.runtime",
        _FORBIDDEN_FROM_RUNTIME_FACADE,
    )
    assert loaded == [], (
        "importing surfaces.interactive_shell.runtime eagerly pulled public exports "
        f"that must resolve on first access: {loaded}"
    )
