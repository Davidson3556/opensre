"""Compatibility alias for interactive shell data-store argument helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "args"

if TYPE_CHECKING:
    from app.cli.interactive_shell.data_store.args import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.data_store import args as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
