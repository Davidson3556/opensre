"""Resolution of the Windows PowerShell interpreter used by uninstall helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def windows_powershell_executable() -> str:
    """Return the system Windows PowerShell path, preferring the one under SYSTEMROOT."""
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"
