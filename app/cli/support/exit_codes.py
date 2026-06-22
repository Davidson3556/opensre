"""Compatibility alias for interactive shell exit-code constants."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "exit_codes"

if TYPE_CHECKING:
    from app.cli.interactive_shell.error_handling.exit_codes import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.error_handling import exit_codes as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
