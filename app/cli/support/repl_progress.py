"""Compatibility alias for interactive shell REPL progress helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "repl_progress"

if TYPE_CHECKING:
    from app.cli.interactive_shell.runtime.repl_progress import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.runtime import repl_progress as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
