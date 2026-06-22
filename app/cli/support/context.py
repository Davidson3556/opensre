"""Compatibility alias for interactive shell data-store context helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "context"

if TYPE_CHECKING:
    from app.cli.interactive_shell.data_store.context import *  # noqa: F401,F403
    from app.cli.interactive_shell.data_store.context import _root_obj  # noqa: F401
else:
    from app.cli.interactive_shell.data_store import context as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
