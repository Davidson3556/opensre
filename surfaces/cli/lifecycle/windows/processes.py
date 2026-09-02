"""Discovery of running OpenSRE processes that live inside a managed Windows bundle."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import surfaces.cli.lifecycle.windows.powershell as powershell


def windows_processes_using_tree(
    app_root: Path,
    *,
    current_pid: int,
) -> tuple[list[tuple[int, str]], str | None]:
    root_payload = base64.b64encode(str(app_root).encode("utf-8")).decode("ascii")
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('__ROOT_PAYLOAD__')
)
$rootPath = [System.IO.Path]::GetFullPath($root).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
$rootPrefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
$unknown = $false
$matches = @()
try {
    $processes = @(
        Get-Process -ErrorAction Stop |
            Where-Object { $_.ProcessName -ieq 'opensre' }
    )
}
catch {
    $processes = @()
    $unknown = $true
}
foreach ($process in $processes) {
    if ($process.Id -eq __CURRENT_PID__) {
        continue
    }
    try {
        $processPath = [string]$process.Path
    }
    catch {
        $unknown = $true
        continue
    }
    if (-not $processPath) {
        $unknown = $true
        continue
    }
    try {
        $fullProcessPath = [System.IO.Path]::GetFullPath($processPath)
    }
    catch {
        $unknown = $true
        continue
    }
    if ($fullProcessPath.StartsWith(
            $rootPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        $matches += [ordered]@{
            pid = [int]$process.Id
            path = $fullProcessPath
        }
    }
}
[ordered]@{
    unknown = $unknown
    processes = @($matches)
} | ConvertTo-Json -Compress -Depth 4
""".replace("__ROOT_PAYLOAD__", root_payload).replace("__CURRENT_PID__", str(current_pid))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        completed = subprocess.run(
            [
                powershell.windows_powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
            env=powershell.windows_powershell_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"could not inspect running OpenSRE processes: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return [], f"could not inspect running OpenSRE processes: {detail or 'unknown error'}"

    payload: object | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict):
        return [], "could not inspect running OpenSRE processes: invalid PowerShell response"
    if payload.get("unknown") is not False:
        return [], "could not verify every running OpenSRE process path"

    raw_processes = payload.get("processes")
    if not isinstance(raw_processes, list):
        return [], "could not inspect running OpenSRE processes: invalid process list"
    processes: list[tuple[int, str]] = []
    for item in raw_processes:
        if not isinstance(item, dict):
            return [], "could not inspect running OpenSRE processes: invalid process entry"
        pid = item.get("pid")
        path = item.get("path")
        if not isinstance(pid, int) or not isinstance(path, str) or not path:
            return [], "could not inspect running OpenSRE processes: invalid process entry"
        processes.append((pid, path))
    return processes, None
