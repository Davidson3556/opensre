"""Windows PowerShell process helpers for CLI lifecycle operations."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from config.constants.installer import POWERSHELL_MODULE_PATH_ENV


def windows_powershell_environment() -> dict[str, str]:
    """Return a child environment that lets Windows PowerShell build its native module path."""
    env = os.environ.copy()
    module_path_name = POWERSHELL_MODULE_PATH_ENV.casefold()
    for name in tuple(env):
        if name.casefold() == module_path_name:
            del env[name]
    return env


def windows_powershell_executable() -> str:
    """Return the system Windows PowerShell path, preferring the one under SYSTEMROOT."""
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"
