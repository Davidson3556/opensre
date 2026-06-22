"""Compatibility alias for interactive shell prompt support helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "prompt_support"

if TYPE_CHECKING:
    from app.cli.interactive_shell.ui.prompt_support import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.ui import prompt_support as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
