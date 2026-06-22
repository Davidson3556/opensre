"""Compatibility alias for interactive shell data-store update helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

_MODULE_NAME = "update"

if TYPE_CHECKING:
    import subprocess  # noqa: F401

    from app.cli.interactive_shell.data_store.update import *  # noqa: F401,F403
    from app.cli.interactive_shell.data_store.update import (
        _fetch_latest_version,  # noqa: F401
        _is_binary_install,  # noqa: F401
        _is_editable_install,  # noqa: F401
        _is_update_available,  # noqa: F401
        _is_windows,  # noqa: F401
        _upgrade_via_install_script,  # noqa: F401
    )
else:
    from app.cli.interactive_shell.data_store import update as _module

    sys.modules[__name__] = _module
    setattr(sys.modules["app.cli.support"], _MODULE_NAME, _module)
