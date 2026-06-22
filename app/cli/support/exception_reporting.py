"""Compatibility alias for interactive shell exception reporting helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "exception_reporting"

if TYPE_CHECKING:
    from app.cli.interactive_shell.error_handling.exception_reporting import *  # noqa: F401,F403
    from app.cli.interactive_shell.error_handling.exception_reporting import (
        capture_exception,  # noqa: F401
    )
else:
    from app.cli.interactive_shell.error_handling import exception_reporting as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
