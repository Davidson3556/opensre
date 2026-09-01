from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config.constants.installer import (
    WINDOWS_APP_DIR_NAME,
    WINDOWS_BINARY_FILENAME,
    WINDOWS_CURRENT_POINTER_FILENAME,
    WINDOWS_INSTALL_LOCK_FILENAME,
    WINDOWS_LAUNCHER_FILENAME,
    WINDOWS_LAUNCHER_MARKER,
    WINDOWS_LAYOUT_MARKER_FILENAME,
    WINDOWS_LAYOUT_MARKER_TEXT,
    WINDOWS_VERSIONS_DIR_NAME,
)
from config.constants.paths import OPENSRE_HOME_DIR


class _MalformedWindowsInstallError(RuntimeError):
    """Raised when a version-shaped Windows install cannot prove ownership."""


@dataclass(frozen=True)
class _WindowsBinaryInstall:
    executable: Path
    app_root: Path | None
    launcher: Path | None
    legacy_executable: Path | None
    paths: tuple[Path, ...]


_WINDOWS_CLEANUP_SCRIPT = r"""
param(
    [int]$ParentProcessId,
    [string]$CleanupPayload,
    [string]$CleanupScriptPath
)

$ErrorActionPreference = 'Stop'

function Exit-OpenSreCleanup {
    param([int]$ExitCode)

    Remove-Item -LiteralPath $CleanupScriptPath -Force -ErrorAction SilentlyContinue
    exit $ExitCode
}

trap {
    Remove-Item -LiteralPath $CleanupScriptPath -Force -ErrorAction SilentlyContinue
    exit 1
}

$payloadJson = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($CleanupPayload)
)
$payload = ConvertFrom-Json -InputObject $payloadJson

function ConvertTo-OpenSreExtendedPath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith('\\?\')) {
        return $fullPath
    }
    if ($fullPath.StartsWith('\\')) {
        return '\\?\UNC\' + $fullPath.Substring(2)
    }
    return '\\?\' + $fullPath
}

function Test-OpenSreCleanupTarget {
    param([string]$Path)

    try {
        if (Test-Path -LiteralPath $Path) {
            return $true
        }
    }
    catch {
        # Fall through to extended-length path checks.
    }
    $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
    return (
        [System.IO.Directory]::Exists($extendedPath) -or
        [System.IO.File]::Exists($extendedPath)
    )
}

function Remove-OpenSreCleanupTarget {
    param([string]$Path)

    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        return
    }
    catch {
        $extendedPath = ConvertTo-OpenSreExtendedPath -Path $Path
        if ([System.IO.Directory]::Exists($extendedPath)) {
            [System.IO.Directory]::Delete($extendedPath, $true)
            return
        }
        if ([System.IO.File]::Exists($extendedPath)) {
            [System.IO.File]::Delete($extendedPath)
        }
    }
}

function Test-OpenSrePathContains {
    param(
        [string]$Root,
        [string]$Candidate
    )

    try {
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
    }
    catch {
        return $false
    }
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Test-OpenSreTargetInUse {
    param(
        [string]$Path,
        [switch]$TreatAsDirectory
    )

    $targetIsDirectory = $TreatAsDirectory -or [System.IO.Directory]::Exists(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    try {
        $processes = @(
            Get-Process -ErrorAction Stop |
                Where-Object { $_.ProcessName -ieq 'opensre' }
        )
    }
    catch {
        return $true
    }
    foreach ($process in $processes) {
        try {
            $processPath = [string]$process.Path
        }
        catch {
            return $true
        }
        if (-not $processPath) {
            return $true
        }
        if ($targetIsDirectory) {
            if (Test-OpenSrePathContains -Root $Path -Candidate $processPath) {
                return $true
            }
        }
        else {
            try {
                $targetPath = [System.IO.Path]::GetFullPath($Path)
                $runningPath = [System.IO.Path]::GetFullPath($processPath)
            }
            catch {
                return $true
            }
            if ($runningPath.Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Move-OpenSreTargetIfUnused {
    param([string]$Path)

    if (-not (Test-OpenSreCleanupTarget -Path $Path)) {
        return ''
    }
    if (Test-OpenSreTargetInUse -Path $Path) {
        throw "OpenSRE cleanup target is still in use: $Path"
    }

    $guard = $null
    $targetWasDirectory = [System.IO.Directory]::Exists(
        (ConvertTo-OpenSreExtendedPath -Path $Path)
    )
    try {
        $guardPath = if ($targetWasDirectory) {
            Join-Path $Path 'opensre.exe'
        }
        else {
            $Path
        }
        if ([System.IO.File]::Exists((ConvertTo-OpenSreExtendedPath -Path $guardPath))) {
            $guard = [System.IO.File]::Open(
                $guardPath,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Delete
            )
        }
        if (Test-OpenSreTargetInUse -Path $Path) {
            throw "OpenSRE cleanup target became busy: $Path"
        }
        if ($null -ne $guard) {
            $guard.Dispose()
            $guard = $null
        }
        $retiredPath = "$Path.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
        Move-Item -LiteralPath $Path -Destination $retiredPath -ErrorAction Stop
        if ((Test-OpenSreTargetInUse -Path $Path -TreatAsDirectory:$targetWasDirectory) -or
            (Test-OpenSreTargetInUse -Path $retiredPath -TreatAsDirectory:$targetWasDirectory)) {
            Move-Item -LiteralPath $retiredPath -Destination $Path -ErrorAction Stop
            throw "OpenSRE cleanup target became busy during retirement: $Path"
        }
        return $retiredPath
    }
    catch {
        throw
    }
    finally {
        if ($null -ne $guard) {
            $guard.Dispose()
        }
    }
}

function Test-OpenSreManagedLauncher {
    param([string]$Path)

    try {
        $lines = @(Get-Content -LiteralPath $Path)
        return (
            $lines.Count -ge 2 -and
            $lines[0].Trim() -ieq '@echo off' -and
            $lines[1].Trim() -ceq ':: OpenSRE Windows launcher v1'
        )
    }
    catch {
        return $false
    }
}

for ($waitAttempt = 0; $waitAttempt -lt 2400; $waitAttempt++) {
    $parent = Get-Process -Id $parentProcessId -ErrorAction SilentlyContinue
    if ($null -eq $parent) {
        break
    }
    if ($waitAttempt -eq 2399) {
        Exit-OpenSreCleanup -ExitCode 1
    }
    Start-Sleep -Milliseconds 250
}

$managed = $payload.managed
$retiredTargets = @()
$deleteLockPath = ''
$deleteData = $null -eq $managed
$lockHandle = $null
$cleanupLockPath = ''
if ($null -ne $managed) {
    $cleanupLockPath = [string]$managed.lock_path
}
elseif ($payload.lock_path) {
    $cleanupLockPath = [string]$payload.lock_path
}
if ($cleanupLockPath) {
    $lockDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
    while ($null -eq $lockHandle -and [System.DateTime]::UtcNow -lt $lockDeadline) {
        try {
            $lockHandle = [System.IO.File]::Open(
                $cleanupLockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $lockHandle) {
        Exit-OpenSreCleanup -ExitCode 1
    }
}

if ($null -ne $managed) {
    $movedLauncher = ''
    $versionGuards = @()
    try {
        $appRoot = [string]$managed.app_root
        $pointerPath = Join-Path $appRoot 'current.txt'
        $currentInstallId = ''
        if (Test-Path -LiteralPath $pointerPath -PathType Leaf) {
            $currentInstallId = ([string](Get-Content -LiteralPath $pointerPath -Raw)).Trim()
        }

        if ($currentInstallId -ne [string]$managed.expected_install_id) {
            $currentVersionPath = Join-Path (Join-Path $appRoot 'versions') $currentInstallId
            $currentExecutable = Join-Path $currentVersionPath 'opensre.exe'
            if (-not $currentInstallId -or
                -not (Test-Path -LiteralPath $currentExecutable -PathType Leaf)) {
                Exit-OpenSreCleanup -ExitCode 1
            }
            $retiredVersion = Move-OpenSreTargetIfUnused -Path ([string]$managed.active_version)
            if ($retiredVersion) {
                $retiredTargets += $retiredVersion
            }
        }
        else {
            $markerPath = Join-Path $appRoot 'layout-v1.marker'
            $markerText = ''
            if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
                $markerText = ([string](Get-Content -LiteralPath $markerPath -Raw)).Trim()
            }
            if ($markerText -cne 'OpenSRE Windows bundle layout v1') {
                Exit-OpenSreCleanup -ExitCode 1
            }

            $launcher = [string]$managed.launcher
            if ($launcher -and (Test-Path -LiteralPath $launcher -PathType Leaf)) {
                if (-not (Test-OpenSreManagedLauncher -Path $launcher)) {
                    Exit-OpenSreCleanup -ExitCode 1
                }
                $movedLauncher = "$launcher.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
                Move-Item -LiteralPath $launcher -Destination $movedLauncher -ErrorAction Stop
            }

            if (Test-OpenSreTargetInUse -Path $appRoot) {
                throw 'OpenSRE bundle is still in use.'
            }
            $versionsRoot = Join-Path $appRoot 'versions'
            if (Test-Path -LiteralPath $versionsRoot -PathType Container) {
                foreach ($versionDirectory in @(Get-ChildItem -LiteralPath $versionsRoot -Directory -Force)) {
                    $versionExecutable = Join-Path $versionDirectory.FullName 'opensre.exe'
                    if (Test-Path -LiteralPath $versionExecutable -PathType Leaf) {
                        $versionGuards += [System.IO.File]::Open(
                            $versionExecutable,
                            [System.IO.FileMode]::Open,
                            [System.IO.FileAccess]::Read,
                            [System.IO.FileShare]::Delete
                        )
                    }
                }
            }
            if (Test-OpenSreTargetInUse -Path $appRoot) {
                throw 'OpenSRE bundle became busy during uninstall.'
            }
            foreach ($guard in $versionGuards) {
                $guard.Dispose()
            }
            $versionGuards = @()

            $movedAppRoot = "$appRoot.uninstall-$([System.Guid]::NewGuid().ToString('N'))"
            Move-Item -LiteralPath $appRoot -Destination $movedAppRoot -ErrorAction Stop
            if ((Test-OpenSreTargetInUse -Path $appRoot -TreatAsDirectory) -or
                (Test-OpenSreTargetInUse -Path $movedAppRoot -TreatAsDirectory)) {
                try {
                    Move-Item -LiteralPath $movedAppRoot -Destination $appRoot -ErrorAction Stop
                }
                catch {
                    $movedLauncher = ''
                    throw 'OpenSRE bundle retirement could not be rolled back safely.'
                }
                throw 'OpenSRE bundle became busy during retirement.'
            }
            $retiredTargets += $movedAppRoot
            $deleteData = $true
            if ($movedLauncher) {
                $retiredTargets += $movedLauncher
                $movedLauncher = ''
            }
            $deleteLockPath = [string]$managed.lock_path
        }

        foreach ($targetValue in @($payload.targets)) {
            $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue)
            if ($retiredTarget) {
                $retiredTargets += $retiredTarget
            }
        }
    }
    catch {
        if ($movedLauncher -and
            (Test-Path -LiteralPath ([string]$managed.app_root) -PathType Container) -and
            (Test-Path -LiteralPath $movedLauncher -PathType Leaf) -and
            -not (Test-Path -LiteralPath ([string]$managed.launcher))) {
            Move-Item `
                -LiteralPath $movedLauncher `
                -Destination ([string]$managed.launcher) `
                -ErrorAction Stop
        }
        Exit-OpenSreCleanup -ExitCode 1
    }
    finally {
        foreach ($guard in $versionGuards) {
            $guard.Dispose()
        }
    }
}
else {
    foreach ($targetValue in @($payload.targets)) {
        $retiredTarget = Move-OpenSreTargetIfUnused -Path ([string]$targetValue)
        if ($retiredTarget) {
            $retiredTargets += $retiredTarget
        }
    }
}

$failed = $false
foreach ($target in $retiredTargets) {
    $removed = $false
    for ($removeAttempt = 0; $removeAttempt -lt 150; $removeAttempt++) {
        if (-not (Test-OpenSreCleanupTarget -Path $target)) {
            $removed = $true
            break
        }
        try {
            Remove-OpenSreCleanupTarget -Path $target
            if (-not (Test-OpenSreCleanupTarget -Path $target)) {
                $removed = $true
                break
            }
        }
        catch {
            # Retried only after the live path has been atomically retired.
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $removed) {
        $failed = $true
    }
}

if ($failed) {
    if ($null -ne $lockHandle) {
        $lockHandle.Dispose()
        $lockHandle = $null
    }
    Exit-OpenSreCleanup -ExitCode 1
}

# User data is the final phase of the transaction.  A refusal or failure while
# retiring/removing the executable installation must leave it untouched so an
# otherwise working installation never loses its configuration.
if ($deleteData) {
    foreach ($guardPathValue in @($payload.data_guard_paths)) {
        if (Test-OpenSreCleanupTarget -Path ([string]$guardPathValue)) {
            $deleteData = $false
            break
        }
    }
}
$dataFailed = $false
if ($deleteData) {
    foreach ($dataTargetValue in @($payload.data_targets)) {
        $dataTarget = [string]$dataTargetValue
        if (-not (Test-OpenSreCleanupTarget -Path $dataTarget)) {
            continue
        }
        try {
            Remove-OpenSreCleanupTarget -Path $dataTarget
            if (Test-OpenSreCleanupTarget -Path $dataTarget) {
                $dataFailed = $true
            }
        }
        catch {
            $dataFailed = $true
        }
    }
    if (-not $deleteLockPath -and $cleanupLockPath) {
        $deleteLockPath = $cleanupLockPath
    }
}
if ($null -ne $lockHandle) {
    $lockHandle.Dispose()
    $lockHandle = $null
}
if ($deleteLockPath -and
    ($null -eq $managed -or -not (Test-Path -LiteralPath ([string]$managed.app_root)))) {
    Remove-Item -LiteralPath $deleteLockPath -Force -ErrorAction SilentlyContinue
}
if ($dataFailed) {
    Exit-OpenSreCleanup -ExitCode 1
}
Exit-OpenSreCleanup -ExitCode 0
"""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_binary_install() -> bool:
    return bool(getattr(sys, "frozen", False))


def _remove_path(p: Path) -> tuple[bool, str | None]:
    if not p.exists() and not p.is_symlink():
        return True, None
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True, None
    except OSError as exc:
        return False, str(exc)


def _pip_uninstall() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "--yes", "opensre"],
        check=False,
        capture_output=True,
    )
    return result.returncode


def _data_dirs() -> list[Path]:
    return [
        OPENSRE_HOME_DIR,
        Path.home() / ".config" / "opensre",
    ]


def _is_onedir_binary(exe_path: Path) -> bool:
    return exe_path.parent.name == ".opensre-app" and (exe_path.parent / "_internal").is_dir()


def _launcher_for_binary(exe_path: Path) -> Path | None:
    launcher = shutil.which("opensre")
    if not launcher:
        return None
    launcher_path = Path(launcher)
    try:
        if launcher_path.resolve() == exe_path.resolve():
            return launcher_path
    except OSError:
        return None
    return None


def _binary_install_paths(exe_path: Path | None = None) -> list[Path]:
    exe = exe_path or Path(sys.executable)
    paths: list[Path] = []
    if launcher := _launcher_for_binary(exe):
        paths.append(launcher)
    if _is_onedir_binary(exe):
        paths.append(exe.parent)
    else:
        paths.append(exe)

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _is_managed_windows_launcher(launcher_path: Path) -> bool:
    if not launcher_path.is_file():
        return False
    try:
        lines = launcher_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return False
    return (
        len(lines) >= 2
        and lines[0].strip().casefold() == "@echo off"
        and lines[1].strip() == WINDOWS_LAUNCHER_MARKER
    )


def _windows_versioned_app_root(exe_path: Path) -> Path | None:
    exe = Path(os.path.abspath(exe_path))
    if exe.name.casefold() != WINDOWS_BINARY_FILENAME.casefold():
        return None

    version_dir = exe.parent
    versions_dir = version_dir.parent
    app_root = versions_dir.parent
    if (
        versions_dir.name.casefold() == WINDOWS_VERSIONS_DIR_NAME.casefold()
        and app_root.name.casefold() == WINDOWS_APP_DIR_NAME.casefold()
    ):
        return app_root

    if any(parent.name.casefold() == WINDOWS_APP_DIR_NAME.casefold() for parent in exe.parents):
        raise _MalformedWindowsInstallError(
            "managed Windows executable path is malformed; expected "
            f"{WINDOWS_APP_DIR_NAME}\\{WINDOWS_VERSIONS_DIR_NAME}\\<install-id>\\"
            f"{WINDOWS_BINARY_FILENAME}"
        )
    return None


def _windows_app_root(exe_path: Path) -> Path | None:
    app_root = _windows_versioned_app_root(exe_path)
    if app_root is None:
        return None

    marker = app_root / WINDOWS_LAYOUT_MARKER_FILENAME
    try:
        marker_text = marker.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError as exc:
        raise _MalformedWindowsInstallError(
            f"managed Windows installation marker is missing or unreadable: {marker}"
        ) from exc
    if marker_text != WINDOWS_LAYOUT_MARKER_TEXT:
        raise _MalformedWindowsInstallError(
            f"managed Windows installation marker is invalid: {marker}"
        )

    pointer = app_root / WINDOWS_CURRENT_POINTER_FILENAME
    try:
        install_id = pointer.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError as exc:
        raise _MalformedWindowsInstallError(
            f"managed Windows current-version pointer is missing or unreadable: {pointer}"
        ) from exc
    if not install_id or Path(install_id).name != install_id or install_id in {".", ".."}:
        raise _MalformedWindowsInstallError(
            f"managed Windows current-version pointer is invalid: {pointer}"
        )
    current_executable = app_root / WINDOWS_VERSIONS_DIR_NAME / install_id / WINDOWS_BINARY_FILENAME
    if not current_executable.is_file():
        raise _MalformedWindowsInstallError(
            f"managed Windows current-version pointer is dangling: {pointer}"
        )
    if os.path.normcase(os.path.abspath(current_executable)) != os.path.normcase(
        os.path.abspath(exe_path)
    ):
        raise _MalformedWindowsInstallError(
            "this OpenSRE process is not the version selected by the managed Windows "
            "current-version pointer; close it and run uninstall from a new PowerShell window"
        )
    return app_root


def _classify_windows_binary_install(exe_path: Path | None = None) -> _WindowsBinaryInstall:
    exe = Path(os.path.abspath(exe_path or Path(sys.executable)))
    app_root = _windows_app_root(exe)
    if app_root is None:
        if (exe.parent / "_internal").is_dir():
            raise _MalformedWindowsInstallError(
                "this executable belongs to an unpacked Windows onedir bundle, not an "
                "install.ps1-managed installation; install OpenSRE with install.ps1 before "
                "running uninstall"
            )
        return _WindowsBinaryInstall(
            executable=exe,
            app_root=None,
            launcher=None,
            legacy_executable=None,
            paths=(exe,),
        )

    install_dir = app_root.parent
    launcher_path = install_dir / WINDOWS_LAUNCHER_FILENAME
    launcher = launcher_path if _is_managed_windows_launcher(launcher_path) else None
    paths: list[Path] = []
    if launcher is not None:
        paths.append(launcher)
    paths.append(app_root)
    install_lock = install_dir / WINDOWS_INSTALL_LOCK_FILENAME
    if install_lock.exists() or install_lock.is_symlink():
        paths.append(install_lock)

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return _WindowsBinaryInstall(
        executable=exe,
        app_root=app_root,
        launcher=launcher,
        legacy_executable=None,
        paths=tuple(deduped),
    )


def _windows_binary_install_paths(exe_path: Path | None = None) -> list[Path]:
    return list(_classify_windows_binary_install(exe_path).paths)


def _windows_powershell_executable() -> str:
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"


def _windows_processes_using_tree(
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
                _windows_powershell_executable(),
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


def _schedule_windows_cleanup(
    paths: list[Path],
    *,
    parent_pid: int,
    data_paths: list[Path] | None = None,
    install_lock_path: Path | None = None,
    data_guard_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    return _schedule_windows_cleanup_payload(
        paths,
        parent_pid=parent_pid,
        managed=None,
        data_paths=data_paths,
        install_lock_path=install_lock_path,
        data_guard_paths=data_guard_paths,
    )


def _schedule_windows_cleanup_payload(
    paths: list[Path],
    *,
    parent_pid: int,
    managed: dict[str, str] | None,
    data_paths: list[Path] | None = None,
    install_lock_path: Path | None = None,
    data_guard_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    payload_json = json.dumps(
        {
            "targets": [str(path) for path in paths],
            "managed": managed,
            "data_targets": [str(path) for path in data_paths or []],
            "lock_path": str(install_lock_path) if install_lock_path is not None else "",
            "data_guard_paths": [str(path) for path in data_guard_paths or []],
        },
        ensure_ascii=True,
    )
    cleanup_payload = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    cleanup_fd, cleanup_name = tempfile.mkstemp(prefix="opensre-uninstall-", suffix=".ps1")
    cleanup_path = Path(cleanup_name)
    with os.fdopen(cleanup_fd, "w", encoding="utf-8-sig", newline="") as cleanup_file:
        cleanup_file.write(_WINDOWS_CLEANUP_SCRIPT)

    try:
        subprocess.Popen(
            [
                _windows_powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(cleanup_path),
                "-ParentProcessId",
                str(parent_pid),
                "-CleanupPayload",
                cleanup_payload,
                "-CleanupScriptPath",
                str(cleanup_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            cwd=tempfile.gettempdir(),
        )
    except OSError as exc:
        cleanup_path.unlink(missing_ok=True)
        return False, str(exc)
    return True, None


def _schedule_windows_managed_cleanup(
    *,
    executable: Path,
    app_root: Path,
    launcher: Path | None,
    legacy_executable: Path | None,
    parent_pid: int,
    data_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    install_dir = app_root.parent
    managed = {
        "active_version": str(executable.parent),
        "app_root": str(app_root),
        "expected_install_id": executable.parent.name,
        "launcher": str(launcher) if launcher is not None else "",
        "lock_path": str(install_dir / WINDOWS_INSTALL_LOCK_FILENAME),
    }
    extra_paths = [legacy_executable] if legacy_executable is not None else []
    return _schedule_windows_cleanup_payload(
        extra_paths,
        parent_pid=parent_pid,
        managed=managed,
        data_paths=data_paths,
        install_lock_path=install_dir / WINDOWS_INSTALL_LOCK_FILENAME,
        data_guard_paths=[
            install_dir / WINDOWS_BINARY_FILENAME,
            install_dir / WINDOWS_LAUNCHER_FILENAME,
        ],
    )


def run_uninstall(*, yes: bool = False) -> int:
    dirs = _data_dirs()
    binary = _is_binary_install()
    windows_binary = binary and _is_windows()
    windows_install: _WindowsBinaryInstall | None = None
    if windows_binary:
        try:
            windows_install = _classify_windows_binary_install()
        except _MalformedWindowsInstallError as exc:
            print(f"  error    {exc}", file=sys.stderr)
            print("           Nothing was deleted.", file=sys.stderr)
            print(
                "           Inspect or move the unverified .opensre-app directory aside, "
                "then reinstall with install.ps1 before retrying uninstall.",
                file=sys.stderr,
            )
            return 1
        binary_paths = list(windows_install.paths)
    else:
        binary_paths = _binary_install_paths() if binary else []

    print()
    print("  The following will be permanently deleted:")
    print()
    for d in dirs:
        tag = "found" if d.exists() else "not found"
        print(f"    {d}  ({tag})")
    if binary:
        for path in binary_paths:
            print(f"    {path}  (binary)")
    else:
        print("    pip package: opensre")
    print()

    if not yes:
        try:
            import questionary

            confirmed = questionary.confirm(
                "  Uninstall opensre from this machine?", default=False
            ).ask()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return 1
        if not confirmed:
            print("  Cancelled.")
            return 0

    if windows_binary:
        try:
            windows_install = _classify_windows_binary_install()
        except _MalformedWindowsInstallError as exc:
            print(f"  error    {exc}", file=sys.stderr)
            print("           Nothing was deleted.", file=sys.stderr)
            return 1
        binary_paths = list(windows_install.paths)
        if windows_install.app_root is not None:
            running, process_error = _windows_processes_using_tree(
                windows_install.app_root,
                current_pid=os.getpid(),
            )
            if process_error is not None:
                print(f"  error    {process_error}", file=sys.stderr)
                print(
                    "           Nothing was deleted. Close other OpenSRE processes and retry.",
                    file=sys.stderr,
                )
                return 1
            if running:
                print(
                    "  error    another OpenSRE process is using this Windows bundle:",
                    file=sys.stderr,
                )
                for pid, process_path in running:
                    print(f"           PID {pid}: {process_path}", file=sys.stderr)
                print(
                    "           Nothing was deleted. Close the other OpenSRE process and retry.",
                    file=sys.stderr,
                )
                return 1

    print()

    any_error = False
    deferred_cleanup = False
    if windows_binary:
        assert windows_install is not None
        if windows_install.app_root is not None:
            ok, err = _schedule_windows_managed_cleanup(
                executable=windows_install.executable,
                app_root=windows_install.app_root,
                launcher=windows_install.launcher,
                legacy_executable=windows_install.legacy_executable,
                parent_pid=os.getpid(),
                data_paths=dirs,
            )
        else:
            install_dir = windows_install.executable.parent
            ok, err = _schedule_windows_cleanup(
                binary_paths,
                parent_pid=os.getpid(),
                data_paths=dirs,
                install_lock_path=install_dir / WINDOWS_INSTALL_LOCK_FILENAME,
                data_guard_paths=[
                    install_dir / WINDOWS_APP_DIR_NAME,
                    install_dir / WINDOWS_LAUNCHER_FILENAME,
                ],
            )
        if not ok:
            print(f"  error    could not schedule binary cleanup: {err}", file=sys.stderr)
            print("           Nothing was deleted.", file=sys.stderr)
            return 1
        deferred_cleanup = True
        for path in binary_paths:
            print(f"  scheduled {path}  (after this process exits)")
        for path in dirs:
            print(f"  scheduled {path}  (after binary cleanup succeeds)")

    if not windows_binary:
        for d in dirs:
            if not d.exists():
                print(f"  skipped  {d}  (not found)")
                continue
            ok, err = _remove_path(d)
            if ok:
                print(f"  deleted  {d}")
            else:
                print(f"  error    {d}: {err}", file=sys.stderr)
                any_error = True

    if binary:
        if not windows_binary:
            for path in binary_paths:
                ok, err = _remove_path(path)
                if ok:
                    print(f"  deleted  {path}")
                else:
                    print(f"  error    {path}: {err}", file=sys.stderr)
                    any_error = True
    else:
        print("  running  pip uninstall opensre")
        rc = _pip_uninstall()
        if rc == 0:
            print("  deleted  pip package opensre")
        else:
            print(f"  error    pip uninstall failed (exit {rc})", file=sys.stderr)
            if _is_windows():
                hint = "pip uninstall opensre"
            else:
                hint = "pip uninstall opensre  (or: pipx uninstall opensre)"
            print(f"           retry manually: {hint}", file=sys.stderr)
            any_error = True

    print()

    if any_error:
        print("  Uninstall finished with errors. See above for details.", file=sys.stderr)
        return 1

    if deferred_cleanup:
        print("  opensre will finish uninstalling after this process exits.")
        print()
        print("  Your config and data will be removed after binary cleanup succeeds.")
    else:
        print("  opensre has been uninstalled.")
        print()
        print("  Your config and data have been removed.")
    if _is_windows():
        print("  To reinstall: irm https://install.opensre.com | iex")
    else:
        print("  To reinstall: curl -fsSL https://install.opensre.com | bash")
    return 0
