"""Lazy public facade for the interactive-shell runtime package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from surfaces.interactive_shell.runtime.exports import __all__ as __all__
from surfaces.interactive_shell.runtime.exports import __dir__ as __dir__
from surfaces.interactive_shell.runtime.exports import __getattr__ as __getattr__

if TYPE_CHECKING:
    from infrastructure.scheduling.task_registry import TaskRegistry as TaskRegistry
    from infrastructure.scheduling.task_types import TaskKind as TaskKind
    from infrastructure.scheduling.task_types import TaskRecord as TaskRecord
    from infrastructure.scheduling.task_types import TaskStatus as TaskStatus
    from surfaces.interactive_shell.runtime.context import ReplRuntime as ReplRuntime
    from surfaces.interactive_shell.runtime.context import (
        SessionBootstrapSpec as SessionBootstrapSpec,
    )
    from surfaces.interactive_shell.runtime.context import (
        create_repl_runtime as create_repl_runtime,
    )
    from surfaces.interactive_shell.runtime.context import (
        prepare_repl_session as prepare_repl_session,
    )
    from surfaces.interactive_shell.session.session import Session as Session
