"""Compatibility alias for interactive shell data-store constants."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "constants"

if TYPE_CHECKING:
    from app.cli.interactive_shell.data_store.constants import *  # noqa: F401,F403
else:
    from app.cli.interactive_shell.data_store import constants as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
