"""Lazy public exports for :mod:`surfaces.interactive_shell.runtime`."""

from __future__ import annotations

import importlib
import sys
from typing import Any

_PACKAGE = "surfaces.interactive_shell.runtime"
_EXPORT_MODULES: dict[str, str] = {
    "ReplRuntime": "surfaces.interactive_shell.runtime.context",
    "Session": "surfaces.interactive_shell.session.session",
    "SessionBootstrapSpec": "surfaces.interactive_shell.runtime.context",
    "TaskKind": "infrastructure.scheduling.task_types",
    "TaskRecord": "infrastructure.scheduling.task_types",
    "TaskRegistry": "infrastructure.scheduling.task_registry",
    "TaskStatus": "infrastructure.scheduling.task_types",
    "create_repl_runtime": "surfaces.interactive_shell.runtime.context",
    "prepare_repl_session": "surfaces.interactive_shell.runtime.context",
}

__all__ = tuple(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    """Resolve a public export or runtime submodule on first access."""
    package = sys.modules[_PACKAGE]
    module_path = _EXPORT_MODULES.get(name)
    if module_path is not None:
        value = getattr(importlib.import_module(module_path), name)
        setattr(package, name, value)
        return value

    submodule_path = f"{_PACKAGE}.{name}"
    try:
        value = importlib.import_module(submodule_path)
    except ModuleNotFoundError as exc:
        if exc.name != submodule_path:
            raise
        raise AttributeError(f"module {_PACKAGE!r} has no attribute {name!r}") from exc
    setattr(package, name, value)
    return value


def __dir__() -> list[str]:
    """Return loaded package names plus the lazy public interface."""
    package = sys.modules[_PACKAGE]
    return sorted(set(package.__dict__) | set(__all__))
