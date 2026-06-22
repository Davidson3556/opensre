"""Compatibility alias for interactive shell CLI error mapping."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "cli_error_mapping"

if TYPE_CHECKING:
    from app.cli.interactive_shell.error_handling.cli_error_mapping import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.error_handling import cli_error_mapping as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
