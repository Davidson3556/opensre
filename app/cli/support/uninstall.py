"""Compatibility alias for interactive shell data-store uninstall helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "uninstall"

if TYPE_CHECKING:
    from app.cli.interactive_shell.data_store.uninstall import *  # noqa: F401,F403
    from app.cli.interactive_shell.data_store.uninstall import (
        _data_dirs,  # noqa: F401
        _is_binary_install,  # noqa: F401
        _is_windows,  # noqa: F401
        _pip_uninstall,  # noqa: F401
        _remove_path,  # noqa: F401
    )
else:
    from app.cli.interactive_shell.data_store import uninstall as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
