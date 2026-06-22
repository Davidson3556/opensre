"""Compatibility alias for interactive shell error helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "errors"

if TYPE_CHECKING:
    from app.cli.interactive_shell.error_handling.errors import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.error_handling import errors as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
