"""Windows integration tests for the versioned onedir installer layout."""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from config.constants.installer import POWERSHELL_MODULE_PATH_ENV
from surfaces.cli.lifecycle.windows import (
    schedule_windows_managed_cleanup,
    windows_processes_using_tree,
)

INSTALL_PS1 = Path(__file__).parents[2] / "install.ps1"
_RESULT_PREFIX = "__OPENSRE_INSTALL_RESULT__"
_PROBE_PREFIX = "__OPENSRE_LAUNCHER_PROBE__"
_CONTEXT_PREFIX = "__OPENSRE_INSTALL_CONTEXT__"
_FAKE_BINARY_ROOT: Path | None = None
_FAILED_PROCESS_ENUMERATOR = r"""
function Get-Process {
    [CmdletBinding()]
    param([int]$Id, [string]$Name)
    if ($PSBoundParameters.ContainsKey('Id')) {
        return $null
    }
    Write-Error 'forced process enumeration failure'
}
"""


def _inject_failed_process_enumerator(source: str, *, preference: str) -> str:
    anchor = f"$ErrorActionPreference = {preference}\n"
    assert source.count(anchor) == 1
    return source.replace(anchor, anchor + _FAILED_PROCESS_ENUMERATOR, 1)


def _powershell() -> str | None:
    """Resolve Windows PowerShell, which is the interpreter the product itself uses.

    ``install.ps1`` is only ever invoked through Windows PowerShell (``opensre
    update`` and the documented ``irm | iex`` command), so the tests exercise it
    there too. PowerShell 7 is also unusable here: ``Add-Type -OutputType
    ConsoleApplication`` builds the fake ``opensre.exe`` and is unsupported on
    .NET Core.
    """
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe")


_POWERSHELL = _powershell()
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or _POWERSHELL is None,
    reason="The versioned onedir installer contract requires Windows PowerShell.",
)


def _ps_literal(value: str | Path) -> str:
    """Return a single-quoted PowerShell literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_env() -> dict[str, str]:
    env = os.environ.copy()
    env["OPENSRE_AUTO_LAUNCH"] = "0"
    env["OPENSRE_SKIP_GH_INSTALL"] = "1"
    # A PSModulePath inherited from a different host - PowerShell 7 on a CI runner -
    # can stop Windows PowerShell resolving its own modules, which makes install.ps1
    # fail on Get-FileHash and Expand-Archive. Pin the interpreter's own locations.
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    for name in tuple(env):
        if name.casefold() == POWERSHELL_MODULE_PATH_ENV.casefold():
            del env[name]
    env[POWERSHELL_MODULE_PATH_ENV] = os.pathsep.join(
        [
            str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"),
            str(Path(program_files) / "WindowsPowerShell" / "Modules"),
        ]
    )
    return env


def _run_powershell(script: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    assert _POWERSHELL is not None
    return subprocess.run(
        [
            _POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        cwd=cwd,
        env=_powershell_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def _fake_opensre_executable() -> Path:
    global _FAKE_BINARY_ROOT

    if _FAKE_BINARY_ROOT is not None:
        executable = _FAKE_BINARY_ROOT / "opensre.exe"
        if executable.is_file():
            return executable

    assert _POWERSHELL is not None
    root = Path(tempfile.mkdtemp(prefix="opensre-installer-test-binary-"))
    executable = root / "opensre.exe"
    source = r"""
using System;
using System.Threading;

public static class Program
{
    public static int Main(string[] args)
    {
        string guardedExecutable = Environment.GetEnvironmentVariable(
            "OPENSRE_TEST_GUARDED_EXECUTABLE"
        );
        string executionMarker = Environment.GetEnvironmentVariable(
            "OPENSRE_TEST_EXECUTION_MARKER"
        );
        string runningExecutable = System.Reflection.Assembly.GetExecutingAssembly().Location;
        if (!String.IsNullOrEmpty(guardedExecutable) &&
            !String.IsNullOrEmpty(executionMarker) &&
            String.Equals(
                System.IO.Path.GetFullPath(guardedExecutable),
                System.IO.Path.GetFullPath(runningExecutable),
                StringComparison.OrdinalIgnoreCase
            ))
        {
            System.IO.File.WriteAllText(executionMarker, "executed");
        }
        if (args.Length > 0 && args[0] == "--version")
        {
            Console.WriteLine("opensre, version 0.1.2026.8.31");
            return 0;
        }
        if (args.Length > 0 && args[0] == "_package-smoke")
        {
            string failureMarker = System.IO.Path.Combine(
                AppContext.BaseDirectory,
                "_internal",
                "package-smoke-fail.txt"
            );
            if (System.IO.File.Exists(failureMarker))
            {
                Console.WriteLine("{\"status\":\"failed\"}");
                return 1;
            }
            Console.WriteLine("{\"status\":\"ok\"}");
            return 0;
        }
        if (args.Length > 0 && args[0] == "hold")
        {
            int milliseconds = args.Length > 1 ? Int32.Parse(args[1]) : 30000;
            Thread.Sleep(milliseconds);
            return 0;
        }
        for (int index = 0; index < args.Length; index++)
        {
            if (String.Equals(args[index], "echo", StringComparison.OrdinalIgnoreCase))
            {
                Console.WriteLine(String.Join(" ", args, index + 1, args.Length - index - 1));
                return 0;
            }
            if (String.Equals(args[index], "exit", StringComparison.OrdinalIgnoreCase) &&
                index + 1 < args.Length)
            {
                return Int32.Parse(args[index + 1]);
            }
            if (String.Equals(args[index], "ping", StringComparison.OrdinalIgnoreCase))
            {
                int seconds = 3;
                for (int pingIndex = index + 1; pingIndex + 1 < args.Length; pingIndex++)
                {
                    if (args[pingIndex] == "-n")
                    {
                        seconds = Math.Max(1, Int32.Parse(args[pingIndex + 1]) - 1);
                        break;
                    }
                }
                Thread.Sleep(seconds * 1000);
                return 0;
            }
        }
        return 0;
    }
}
"""
    source_payload = base64.b64encode(source.encode("utf-8")).decode("ascii")
    completed = _run_powershell(
        f"""
$source = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{source_payload}')
)
Add-Type `
    -TypeDefinition $source `
    -Language CSharp `
    -OutputAssembly {_ps_literal(executable)} `
    -OutputType ConsoleApplication
""",
        cwd=root,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert executable.is_file()
    _FAKE_BINARY_ROOT = root
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return executable


def _prefixed_json(stdout: str, prefix: str) -> dict[str, Any]:
    matches = [line[len(prefix) :] for line in stdout.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, f"missing {prefix!r} payload in output:\n{stdout}"
    payload = json.loads(matches[0])
    assert isinstance(payload, dict)
    return payload


def _install_bundle(
    *,
    binary_path: Path,
    install_dir: Path,
    install_id: str,
    cwd: Path,
    parent_process_id: int = 0,
    verified_legacy_binary_path: Path | None = None,
    installer_override: str = "",
    installer_path: Path = INSTALL_PS1,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    verified_legacy_path = verified_legacy_binary_path or ""
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(installer_path)} -SkipMain
{installer_override}
$result = Install-OpenSreVerifiedBundle `
    -BinaryPath {_ps_literal(binary_path)} `
    -InstallDir {_ps_literal(install_dir)} `
    -InstallId {_ps_literal(install_id)} `
    -ParentProcessId {parent_process_id} `
    -VerifiedLegacyBinaryPath {_ps_literal(verified_legacy_path)}
$payload = [ordered]@{{
    BinaryPath = [string]$result.BinaryPath
    LauncherPath = [string]$result.LauncherPath
    AppRoot = [string]$result.AppRoot
    LayoutRoot = if ($result.PSObject.Properties['LayoutRoot']) {{
        [string]$result.LayoutRoot
    }} else {{
        ''
    }}
    CleanupPath = if ($result.PSObject.Properties['CleanupPath']) {{
        [string]$result.CleanupPath
    }} else {{
        ''
    }}
    DeferredCleanup = [bool]$result.DeferredCleanup
}}
Write-Output ({_ps_literal(_RESULT_PREFIX)} + ($payload | ConvertTo-Json -Compress))
"""
    completed = _run_powershell(script, cwd=cwd)
    if check:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return completed, _prefixed_json(completed.stdout, _RESULT_PREFIX)
    if completed.returncode == 0:
        return completed, _prefixed_json(completed.stdout, _RESULT_PREFIX)
    return completed, None


def _make_onedir_bundle(
    root: Path,
    *,
    payload: dict[str, str] | None = None,
) -> Path:
    app_root = root / "opensre"
    internal = app_root / "_internal"
    internal.mkdir(parents=True)
    binary = app_root / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), binary)
    for relative_name, content in (payload or {"payload.txt": "bundled"}).items():
        destination = internal / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return binary


def _make_invalid_onedir_bundle(root: Path) -> Path:
    app_root = root / "opensre"
    internal = app_root / "_internal"
    internal.mkdir(parents=True)
    (internal / "invalid-build.txt").write_text("must not activate", encoding="utf-8")
    binary = app_root / "opensre.exe"
    binary.write_bytes(b"not a Windows executable")
    return binary


def _probe_launcher(launcher: Path, *, cwd: Path) -> dict[str, Any]:
    script = f"""
$ErrorActionPreference = 'Continue'
$versionOutput = @(& {_ps_literal(launcher)} --version 2>&1) | Out-String
$versionExit = $LASTEXITCODE
$argumentOutput = @(& {_ps_literal(launcher)} /d /c echo 'value with spaces' 2>&1) | Out-String
$argumentExit = $LASTEXITCODE
& {_ps_literal(launcher)} /d /c exit 37
$forwardedExit = $LASTEXITCODE
$payload = [ordered]@{{
    VersionOutput = $versionOutput.Trim()
    VersionExit = $versionExit
    ArgumentOutput = $argumentOutput.Trim()
    ArgumentExit = $argumentExit
    ForwardedExit = $forwardedExit
}}
Write-Output ({_ps_literal(_PROBE_PREFIX)} + ($payload | ConvertTo-Json -Compress))
"""
    completed = _run_powershell(script, cwd=cwd)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return _prefixed_json(completed.stdout, _PROBE_PREFIX)


def _path(payload: dict[str, Any], key: str) -> Path:
    value = payload.get(key)
    assert isinstance(value, str) and value, f"installer result omitted {key}"
    return Path(value)


def _resolve_install_context(
    *,
    cwd: Path,
    update_executable: Path | None = None,
    explicit_install_dir: Path | None = None,
    parent_process_id: int = 0,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    update_line = (
        f"$env:OPENSRE_UPDATE_EXECUTABLE = {_ps_literal(update_executable)}"
        if update_executable is not None
        else "Remove-Item Env:OPENSRE_UPDATE_EXECUTABLE -ErrorAction SilentlyContinue"
    )
    install_line = (
        f"$env:OPENSRE_INSTALL_DIR = {_ps_literal(explicit_install_dir)}"
        if explicit_install_dir is not None
        else "Remove-Item Env:OPENSRE_INSTALL_DIR -ErrorAction SilentlyContinue"
    )
    parent_line = (
        f"$env:OPENSRE_UPDATE_PARENT_PID = '{parent_process_id}'"
        if parent_process_id > 0
        else "Remove-Item Env:OPENSRE_UPDATE_PARENT_PID -ErrorAction SilentlyContinue"
    )
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
{update_line}
{install_line}
{parent_line}
$context = Resolve-OpenSreInstallContext
$payload = [ordered]@{{
    InstallDir = [string]$context.InstallDir
    ParentProcessId = [int]$context.ParentProcessId
    IsUpdate = [bool]$context.IsUpdate
    LegacyBinaryPath = [string]$context.LegacyBinaryPath
}}
Write-Output ({_ps_literal(_CONTEXT_PREFIX)} + ($payload | ConvertTo-Json -Compress))
"""
    completed = _run_powershell(script, cwd=cwd)
    if check:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return completed, _prefixed_json(completed.stdout, _CONTEXT_PREFIX)
    if completed.returncode == 0:
        return completed, _prefixed_json(completed.stdout, _CONTEXT_PREFIX)
    return completed, None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert predicate()


def test_onedir_bundle_survives_source_removal_and_runs_installed_version(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "download extraction"
    binary = _make_onedir_bundle(
        source_root,
        payload={"nested/payload.txt": "complete bundle"},
    )
    install_dir = tmp_path / "installed"

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="build-one",
        cwd=tmp_path,
    )
    assert result is not None
    installed_binary = _path(result, "BinaryPath")
    launcher = _path(result, "LauncherPath")
    app_root = _path(result, "AppRoot")

    shutil.rmtree(source_root)

    assert installed_binary == app_root / "opensre.exe"
    assert installed_binary.is_file()
    assert launcher.is_file()
    assert (app_root / "_internal" / "nested" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "complete bundle"
    probe = _probe_launcher(launcher, cwd=tmp_path)
    assert probe["VersionExit"] == 0
    assert probe["VersionOutput"]


def test_legacy_flat_onefile_is_removed_during_migration(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    legacy_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), legacy_binary)
    legacy_hash = _sha256(legacy_binary)
    sentinel = install_dir / "unrelated-tool.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    binary = _make_onedir_bundle(tmp_path / "new bundle")

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="migrated-build",
        cwd=tmp_path,
        verified_legacy_binary_path=legacy_binary,
    )
    assert result is not None
    launcher = _path(result, "LauncherPath")

    assert not legacy_binary.exists()
    retired = list((install_dir / ".opensre-app").glob("retired-*"))
    assert retired
    assert all(_sha256(path) == legacy_hash for path in retired)
    assert _path(result, "BinaryPath").parent != install_dir
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0


def test_installer_never_executes_unverified_preexisting_flat_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "unverified legacy executable"
    install_dir.mkdir()
    preexisting_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), preexisting_binary)
    original_hash = _sha256(preexisting_binary)
    execution_marker = tmp_path / "preexisting-executable-ran.txt"
    monkeypatch.setenv("OPENSRE_TEST_GUARDED_EXECUTABLE", str(preexisting_binary))
    monkeypatch.setenv("OPENSRE_TEST_EXECUTION_MARKER", str(execution_marker))
    replacement = _make_onedir_bundle(tmp_path / "unverified replacement")

    completed, result = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="must-not-run-preexisting",
        cwd=tmp_path,
        check=False,
    )

    assert completed.returncode != 0
    assert result is None
    assert "Refusing to replace unverified pre-existing executable" in completed.stderr
    assert not execution_marker.exists()
    assert preexisting_binary.is_file()
    assert _sha256(preexisting_binary) == original_hash
    assert not (install_dir / "opensre.cmd").exists()
    assert not (install_dir / ".opensre-app" / "current.txt").exists()


def test_running_legacy_onefile_cleanup_waits_for_process_exit(tmp_path: Path) -> None:
    install_dir = tmp_path / "legacy install with spaces"
    install_dir.mkdir()
    legacy_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), legacy_binary)
    replacement_binary = _make_onedir_bundle(tmp_path / "replacement bundle")
    running_legacy = subprocess.Popen([str(legacy_binary), "hold", "10000"])

    try:
        _, result = _install_bundle(
            binary_path=replacement_binary,
            install_dir=install_dir,
            install_id="deferred-migration",
            cwd=tmp_path,
            parent_process_id=running_legacy.pid,
            verified_legacy_binary_path=legacy_binary,
        )
        assert result is not None
        assert result["DeferredCleanup"] is True
        assert not legacy_binary.exists()
        assert running_legacy.poll() is None

        running_legacy.wait(timeout=10)
        deadline = time.monotonic() + 30
        layout_root = install_dir / ".opensre-app"
        while list(layout_root.glob("retired-*")) and time.monotonic() < deadline:
            time.sleep(0.1)

        assert list(layout_root.glob("retired-*")) == []
        assert _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0
    finally:
        if running_legacy.poll() is None:
            running_legacy.terminate()
            running_legacy.wait(timeout=10)


def test_running_legacy_onefile_without_parent_pid_defers_locked_cleanup(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "legacy install without pid"
    install_dir.mkdir()
    legacy_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), legacy_binary)
    replacement_binary = _make_onedir_bundle(tmp_path / "replacement without pid")
    running_legacy = subprocess.Popen([str(legacy_binary), "hold", "6000"])

    try:
        _, result = _install_bundle(
            binary_path=replacement_binary,
            install_dir=install_dir,
            install_id="deferred-without-pid",
            cwd=tmp_path,
            verified_legacy_binary_path=legacy_binary,
        )
        assert result is not None
        assert result["DeferredCleanup"] is True
        assert not legacy_binary.exists()
        assert running_legacy.poll() is None

        running_legacy.wait(timeout=15)
        deadline = time.monotonic() + 30
        layout_root = install_dir / ".opensre-app"
        while list(layout_root.glob("retired-*")) and time.monotonic() < deadline:
            time.sleep(0.1)

        assert list(layout_root.glob("retired-*")) == []
        launcher = _path(result, "LauncherPath")
        assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0
    finally:
        if running_legacy.poll() is None:
            running_legacy.terminate()
            running_legacy.wait(timeout=10)


def test_launcher_forwards_arguments_and_exit_code_from_paths_with_spaces(
    tmp_path: Path,
) -> None:
    binary = _make_onedir_bundle(tmp_path / "source bundle with spaces")
    install_dir = tmp_path / "installed application with spaces"

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="space-safe-build",
        cwd=tmp_path,
    )
    assert result is not None

    probe = _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)

    assert probe["ArgumentExit"] == 0
    assert "value with spaces" in probe["ArgumentOutput"]
    assert probe["ForwardedExit"] == 37


def test_installer_copies_onedir_files_beyond_max_path(tmp_path: Path) -> None:
    binary = _make_onedir_bundle(
        tmp_path / "long destination source",
        payload={"base.txt": "base payload"},
    )
    deep_relative = Path("_internal")
    for index in range(5):
        deep_relative /= f"segment-{index}-" + ("x" * 28)
    deep_relative /= "payload.txt"
    source_payload = binary.parent / deep_relative
    extended_source_parent = "\\\\?\\" + str(source_payload.parent)
    created = _run_powershell(
        f"""
$directory = [System.IO.Directory]::CreateDirectory({_ps_literal(extended_source_parent)})
[System.IO.File]::WriteAllText(
    [System.IO.Path]::Combine($directory.FullName, 'payload.txt'),
    'long path payload'
)
""",
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stdout + created.stderr

    install_id = "long-path-build"
    install_dir = tmp_path / "long destination with spaces"
    expected_payload = install_dir / ".opensre-app" / "versions" / install_id / deep_relative
    assert len(str(expected_payload)) > 260

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id=install_id,
        cwd=tmp_path,
    )

    assert result is not None
    extended_payload = Path("\\\\?\\" + str(expected_payload))
    assert extended_payload.read_text(encoding="utf-8") == "long path payload"
    assert _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_stage_to_final_move_retries_transient_access_denied(tmp_path: Path) -> None:
    install_id = "transient-stage-move"
    install_dir = tmp_path / "transient stage move install"
    binary = _make_onedir_bundle(
        tmp_path / "transient stage move bundle",
        payload={"nested/payload.dat": "complete after retry"},
    )
    layout_root = install_dir / ".opensre-app"
    stage_path = layout_root / f"stage-{install_id}"
    final_path = layout_root / "versions" / install_id
    attempt_marker = tmp_path / "stage-move-attempts.txt"
    installer_override = rf"""
$script:OpenSreInjectedMoveAttempts = 0
function Move-Item {{
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,
        [Parameter(Mandatory = $true)]
        [string]$Destination
    )
    if ([System.IO.Path]::GetFullPath($LiteralPath) -eq {_ps_literal(stage_path)} -and
        [System.IO.Path]::GetFullPath($Destination) -eq {_ps_literal(final_path)}) {{
        $script:OpenSreInjectedMoveAttempts += 1
        [System.IO.File]::WriteAllText(
            {_ps_literal(attempt_marker)},
            [string]$script:OpenSreInjectedMoveAttempts
        )
        if ($script:OpenSreInjectedMoveAttempts -eq 1) {{
            throw [System.UnauthorizedAccessException]::new('injected transient access denied')
        }}
    }}
    Microsoft.PowerShell.Management\Move-Item @PSBoundParameters
}}
"""

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id=install_id,
        cwd=tmp_path,
        installer_override=installer_override,
    )

    assert result is not None
    assert attempt_marker.read_text(encoding="utf-8") == "2"
    assert not stage_path.exists()
    assert final_path.is_dir()
    assert (final_path / "_internal" / "nested" / "payload.dat").read_text(
        encoding="utf-8"
    ) == "complete after retry"
    assert _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_marker_owned_launcher_is_atomically_rewritten_to_canonical_content(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "owned launcher install"
    first_binary = _make_onedir_bundle(tmp_path / "owned launcher first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="owned-first",
        cwd=tmp_path,
    )
    assert first is not None
    launcher = _path(first, "LauncherPath")
    canonical_content = launcher.read_bytes()
    tamper_marker = tmp_path / "tampered-launcher-ran.txt"
    launcher.write_bytes(
        "\r\n".join(
            (
                "@echo off",
                ":: OpenSRE Windows launcher v1",
                f'echo tampered>"{tamper_marker}"',
                f'"{_path(first, "BinaryPath")}" %*',
                "exit /b %ERRORLEVEL%",
                "",
            )
        ).encode("utf-8")
    )

    second_binary = _make_onedir_bundle(tmp_path / "owned launcher second")
    _, second = _install_bundle(
        binary_path=second_binary,
        install_dir=install_dir,
        install_id="owned-second",
        cwd=tmp_path,
    )

    assert second is not None
    assert launcher.read_bytes() == canonical_content
    assert not tamper_marker.exists()
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "owned-second"
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionOutput"].startswith("opensre, version ")


def test_installer_refuses_genuinely_unowned_launcher(tmp_path: Path) -> None:
    install_dir = tmp_path / "unowned launcher install"
    install_dir.mkdir()
    launcher = install_dir / "opensre.cmd"
    original_content = b"@echo off\r\necho user-owned\r\n"
    launcher.write_bytes(original_content)
    binary = _make_onedir_bundle(tmp_path / "unowned launcher bundle")

    completed, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="must-not-activate",
        cwd=tmp_path,
        check=False,
    )

    assert completed.returncode != 0
    assert result is None
    assert "Refusing to replace unowned launcher" in completed.stderr
    assert launcher.read_bytes() == original_content
    assert not (install_dir / ".opensre-app" / "current.txt").exists()


@pytest.mark.parametrize(
    ("body", "expected_error"),
    (
        ("@echo off\r\nexit /b 0\r\n", "empty output"),
        ("@echo off\r\necho another-tool 9.9\r\nexit /b 0\r\n", "valid OpenSRE"),
        (
            "@echo off\r\necho opensre, version 0.1\r\necho unexpected extra output\r\nexit /b 0\r\n",
            "valid OpenSRE",
        ),
    ),
)
def test_version_validation_rejects_empty_or_non_opensre_output(
    tmp_path: Path,
    body: str,
    expected_error: str,
) -> None:
    invalid_launcher = tmp_path / "invalid-version.cmd"
    invalid_launcher.write_text(body, encoding="utf-8")
    completed = _run_powershell(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
Get-OpenSreBinaryVersionInfo -BinaryPath {_ps_literal(invalid_launcher)}
""",
        cwd=tmp_path,
    )

    assert completed.returncode != 0
    assert expected_error in completed.stderr


def test_cleanup_launch_failure_after_activation_keeps_new_bundle_working(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "cleanup launch failure"
    first_binary = _make_onedir_bundle(tmp_path / "cleanup failure first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="cleanup-stable",
        cwd=tmp_path,
    )
    assert first is not None
    first_root = _path(first, "AppRoot")
    replacement = _make_onedir_bundle(tmp_path / "cleanup failure replacement")

    completed, second = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="cleanup-activated",
        cwd=tmp_path,
        installer_override="""
function Start-OpenSreDeferredCleanup {
    param([string]$LayoutRoot, [string[]]$TargetPaths, [int]$ParentProcessId)
    throw 'forced cleanup launch failure'
}
""",
    )

    assert second is not None
    second_root = _path(second, "AppRoot")
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "cleanup-activated"
    assert first_root.is_dir()
    assert second_root.is_dir()
    assert "retained for a later safe cleanup" in completed.stdout
    assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_obsolete_enumeration_failure_after_activation_keeps_new_bundle_working(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "obsolete enumeration failure"
    first_binary = _make_onedir_bundle(tmp_path / "obsolete enumeration first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="enumeration-stable",
        cwd=tmp_path,
    )
    assert first is not None
    first_root = _path(first, "AppRoot")
    first_manifest = {
        path.relative_to(first_root): _sha256(path)
        for path in first_root.rglob("*")
        if path.is_file()
    }
    replacement = _make_onedir_bundle(tmp_path / "obsolete enumeration replacement")

    completed, second = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="enumeration-activated",
        cwd=tmp_path,
        installer_override="""
function Get-OpenSreObsoleteVersionPaths {
    param([string]$LayoutRoot, [string]$ActiveInstallId)
    throw 'forced obsolete-version enumeration failure'
}
""",
    )

    assert second is not None
    second_root = _path(second, "AppRoot")
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "enumeration-activated"
    assert first_root.is_dir()
    assert {
        path.relative_to(first_root): _sha256(path)
        for path in first_root.rglob("*")
        if path.is_file()
    } == first_manifest
    assert second_root.is_dir()
    assert second["DeferredCleanup"] is True
    assert "obsolete Windows files could not be enumerated" in completed.stdout
    assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_legacy_cleanup_launch_failure_is_retried_by_a_later_install(tmp_path: Path) -> None:
    install_dir = tmp_path / "legacy cleanup retry"
    install_dir.mkdir()
    legacy_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), legacy_binary)
    legacy_hash = _sha256(legacy_binary)
    replacement = _make_onedir_bundle(tmp_path / "legacy cleanup replacement")

    failed_cleanup, migrated = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="legacy-cleanup-retained",
        cwd=tmp_path,
        verified_legacy_binary_path=legacy_binary,
        installer_override="""
function Start-OpenSreDeferredCleanup {
    param([string]$LayoutRoot, [string[]]$TargetPaths, [int]$ParentProcessId)
    throw 'forced cleanup launch failure'
}
""",
    )

    assert migrated is not None
    layout_root = install_dir / ".opensre-app"
    retained = list(layout_root.glob("retired-*"))
    assert len(retained) == 1
    assert _sha256(retained[0]) == legacy_hash
    assert "retained for a later safe cleanup" in failed_cleanup.stdout

    next_binary = _make_onedir_bundle(tmp_path / "legacy cleanup retry bundle")
    _, retried = _install_bundle(
        binary_path=next_binary,
        install_dir=install_dir,
        install_id="legacy-cleanup-retried",
        cwd=tmp_path,
    )

    assert retried is not None
    _wait_until(lambda: not retained[0].exists())
    assert _probe_launcher(_path(retried, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_update_retains_complete_old_bundle_used_by_second_process(tmp_path: Path) -> None:
    install_dir = tmp_path / "two process update"
    first_binary = _make_onedir_bundle(
        tmp_path / "two process first",
        payload={"lazy/module.dat": "must remain complete", "shared.dat": "old"},
    )
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="two-process-old",
        cwd=tmp_path,
    )
    assert first is not None
    old_root = _path(first, "AppRoot")
    old_manifest = {
        path.relative_to(old_root): _sha256(path) for path in old_root.rglob("*") if path.is_file()
    }
    short_process = subprocess.Popen([str(old_root / "opensre.exe"), "hold", "2000"])
    long_process = subprocess.Popen([str(old_root / "opensre.exe"), "hold", "30000"])

    try:
        second_binary = _make_onedir_bundle(tmp_path / "two process second")
        _, second = _install_bundle(
            binary_path=second_binary,
            install_dir=install_dir,
            install_id="two-process-new",
            cwd=tmp_path,
            parent_process_id=short_process.pid,
        )
        assert second is not None
        short_process.wait(timeout=10)
        cleanup_path = _path(second, "CleanupPath")
        assert cleanup_path.is_file()
        _wait_until(lambda: not cleanup_path.exists())

        assert long_process.poll() is None
        assert old_root.is_dir()
        assert {
            path.relative_to(old_root): _sha256(path)
            for path in old_root.rglob("*")
            if path.is_file()
        } == old_manifest
        assert (old_root / "_internal" / "lazy" / "module.dat").read_text(
            encoding="utf-8"
        ) == "must remain complete"
        assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0

        long_process.terminate()
        long_process.wait(timeout=10)
        third_binary = _make_onedir_bundle(tmp_path / "two process third")
        third_completed, third = _install_bundle(
            binary_path=third_binary,
            install_dir=install_dir,
            install_id="two-process-cleanup-retry",
            cwd=tmp_path,
        )
        assert third is not None
        assert third["DeferredCleanup"] is True
        deadline = time.monotonic() + 30
        while old_root.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not old_root.exists(), third_completed.stdout + third_completed.stderr
    finally:
        for process in (short_process, long_process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_update_cleanup_worker_retains_old_tree_when_process_scan_fails(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "process scan failure update"
    first_binary = _make_onedir_bundle(
        tmp_path / "process scan failure first",
        payload={"lazy/module.dat": "must remain complete", "shared.dat": "old"},
    )
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="scan-failure-old",
        cwd=tmp_path,
    )
    assert first is not None
    old_root = _path(first, "AppRoot")
    old_manifest = {
        path.relative_to(old_root): _sha256(path) for path in old_root.rglob("*") if path.is_file()
    }

    installer_source = INSTALL_PS1.read_text(encoding="utf-8")
    faulted_installer = tmp_path / "install-process-scan-failure.ps1"
    faulted_installer.write_text(
        _inject_failed_process_enumerator(
            installer_source,
            preference='"SilentlyContinue"',
        ),
        encoding="utf-8",
    )

    replacement = _make_onedir_bundle(tmp_path / "process scan failure replacement")
    _, second = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="scan-failure-new",
        cwd=tmp_path,
        installer_path=faulted_installer,
    )
    assert second is not None
    layout_root = install_dir / ".opensre-app"
    cleanup_path = _path(second, "CleanupPath")
    assert cleanup_path.is_file()
    _wait_until(lambda: not cleanup_path.exists(), timeout=15)

    assert (layout_root / "current.txt").read_text(encoding="utf-8").strip() == ("scan-failure-new")
    assert old_root.is_dir()
    assert {
        path.relative_to(old_root): _sha256(path) for path in old_root.rglob("*") if path.is_file()
    } == old_manifest
    assert list(layout_root.glob("retired-*")) == []
    assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_uninstall_preflight_detects_two_native_processes_in_managed_tree(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "two process uninstall"
    binary = _make_onedir_bundle(tmp_path / "two process uninstall bundle")
    _, installed = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="two-process-uninstall",
        cwd=tmp_path,
    )
    assert installed is not None
    app_root = _path(installed, "AppRoot").parents[1]
    executable = _path(installed, "BinaryPath")
    first_process = subprocess.Popen([str(executable), "hold", "30000"])
    second_process = subprocess.Popen([str(executable), "hold", "30000"])

    try:
        _wait_until(lambda: first_process.poll() is None and second_process.poll() is None)
        running, error = windows_processes_using_tree(app_root, current_pid=os.getpid())

        assert error is None
        assert {pid for pid, _path_value in running} >= {
            first_process.pid,
            second_process.pid,
        }
    finally:
        for process in (first_process, second_process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_managed_uninstall_worker_retains_complete_tree_used_by_second_process(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "two process uninstall worker"
    binary = _make_onedir_bundle(
        tmp_path / "two process uninstall worker bundle",
        payload={"lazy/module.dat": "still needed"},
    )
    _, installed = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="two-process-uninstall-worker",
        cwd=tmp_path,
    )
    assert installed is not None
    version_root = _path(installed, "AppRoot")
    app_root = version_root.parents[1]
    executable = _path(installed, "BinaryPath")
    launcher = _path(installed, "LauncherPath")
    before = {
        path.relative_to(app_root): _sha256(path) for path in app_root.rglob("*") if path.is_file()
    }
    caller = subprocess.Popen([str(executable), "hold", "2000"])
    other = subprocess.Popen([str(executable), "hold", "30000"])

    try:
        ok, error = schedule_windows_managed_cleanup(
            executable=executable,
            app_root=app_root,
            launcher=launcher,
            parent_pid=caller.pid,
        )
        assert ok is True
        assert error is None
        caller.wait(timeout=10)
        time.sleep(2)

        assert other.poll() is None
        assert launcher.is_file()
        assert app_root.is_dir()
        assert {
            path.relative_to(app_root): _sha256(path)
            for path in app_root.rglob("*")
            if path.is_file()
        } == before
        assert (version_root / "_internal" / "lazy" / "module.dat").read_text(
            encoding="utf-8"
        ) == "still needed"

        other.terminate()
        other.wait(timeout=10)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        try:
            ok, error = schedule_windows_managed_cleanup(
                executable=executable,
                app_root=app_root,
                launcher=launcher,
                parent_pid=holder.pid,
            )
            assert ok is True
            assert error is None
            holder.wait(timeout=10)
            _wait_until(lambda: not app_root.exists())
            _wait_until(lambda: not launcher.exists())
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=10)
    finally:
        for process in (caller, other):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_historical_flat_onefile_archive_migrates_to_managed_layout(tmp_path: Path) -> None:
    install_dir = tmp_path / "historical archive migration"
    install_dir.mkdir()
    legacy_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), legacy_binary)
    archive = tmp_path / "opensre_0.1.2026.7.30_windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
        bundle_zip.write(_fake_opensre_executable(), arcname="opensre.exe")
    extraction_root = tmp_path / "historical extraction"
    completed = _run_powershell(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
Expand-Archive -LiteralPath {_ps_literal(archive)} -DestinationPath {_ps_literal(extraction_root)}
$binary = Get-OpenSreBinaryPathFromArchive `
    -ExtractionRoot {_ps_literal(extraction_root)} `
    -BinaryName 'opensre.exe'
Write-Output $binary
""",
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    extracted_binary = extraction_root / "opensre.exe"
    assert extracted_binary.is_file()

    _, result = _install_bundle(
        binary_path=extracted_binary,
        install_dir=install_dir,
        install_id="historical-flat-archive",
        cwd=tmp_path,
        verified_legacy_binary_path=legacy_binary,
    )
    assert result is not None
    shutil.rmtree(extraction_root)
    archive.unlink()

    assert not legacy_binary.exists()
    installed_binary = _path(result, "BinaryPath")
    assert installed_binary.is_file()
    assert not (installed_binary.parent / "_internal").exists()
    assert _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_onedir_archive_installs_from_a_literal_extraction_path(tmp_path: Path) -> None:
    source_binary = _make_onedir_bundle(
        tmp_path / "archive source",
        payload={"nested/payload.dat": "complete archive"},
    )
    source_root = source_binary.parent
    archive = tmp_path / "opensre_main_windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle_zip:
        for path in source_root.rglob("*"):
            if path.is_file():
                bundle_zip.write(path, arcname=Path("opensre") / path.relative_to(source_root))
    extraction_root = tmp_path / "extraction [literal] path"
    completed = _run_powershell(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
Expand-Archive -LiteralPath {_ps_literal(archive)} -DestinationPath {_ps_literal(extraction_root)}
$binary = Get-OpenSreBinaryPathFromArchive `
    -ExtractionRoot {_ps_literal(extraction_root)} `
    -BinaryName 'opensre.exe'
Write-Output $binary
""",
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    extracted_binary = extraction_root / "opensre" / "opensre.exe"
    assert str(extracted_binary) in completed.stdout

    install_dir = tmp_path / "archive install [literal] path"
    _, result = _install_bundle(
        binary_path=extracted_binary,
        install_dir=install_dir,
        install_id="archive-literal-path",
        cwd=tmp_path,
    )
    assert result is not None
    shutil.rmtree(extraction_root)

    app_root = _path(result, "AppRoot")
    assert (app_root / "_internal" / "nested" / "payload.dat").read_text(
        encoding="utf-8"
    ) == "complete archive"
    assert _probe_launcher(_path(result, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_update_context_uses_custom_managed_install_directory(tmp_path: Path) -> None:
    install_dir = tmp_path / "custom managed install"
    app_root = install_dir / ".opensre-app"
    version_root = app_root / "versions" / "build-one"
    version_root.mkdir(parents=True)
    executable = version_root / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (install_dir / "opensre.cmd").write_text(
        "@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8"
    )

    _, context = _resolve_install_context(
        cwd=tmp_path,
        update_executable=executable,
        parent_process_id=5844,
    )

    assert context is not None
    assert Path(context["InstallDir"]) == install_dir
    assert context["ParentProcessId"] == 5844
    assert context["IsUpdate"] is True


def test_update_context_uses_custom_legacy_onefile_directory(tmp_path: Path) -> None:
    install_dir = tmp_path / "custom legacy install"
    install_dir.mkdir()
    executable = install_dir / "opensre.exe"
    executable.write_bytes(b"MZ")

    _, context = _resolve_install_context(
        cwd=tmp_path,
        update_executable=executable,
        parent_process_id=731,
    )

    assert context is not None
    assert Path(context["InstallDir"]) == install_dir
    assert context["ParentProcessId"] == 731
    assert context["LegacyBinaryPath"] == ""


def test_update_context_does_not_trust_spoofed_nonparent_legacy_process(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "spoofed legacy context"
    install_dir.mkdir()
    executable = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), executable)
    unrelated_process = subprocess.Popen([str(executable), "hold", "30000"])

    try:
        _, context = _resolve_install_context(
            cwd=tmp_path,
            update_executable=executable,
            parent_process_id=unrelated_process.pid,
        )

        assert context is not None
        assert Path(context["InstallDir"]) == install_dir
        assert context["ParentProcessId"] == unrelated_process.pid
        assert context["LegacyBinaryPath"] == ""
    finally:
        unrelated_process.terminate()
        unrelated_process.wait(timeout=10)


def test_explicit_install_directory_wins_over_update_executable(tmp_path: Path) -> None:
    explicit_dir = tmp_path / "explicit install"
    unrelated_executable = tmp_path / "unexpected-name.exe"

    _, context = _resolve_install_context(
        cwd=tmp_path,
        update_executable=unrelated_executable,
        explicit_install_dir=explicit_dir,
        parent_process_id=812,
    )

    assert context is not None
    assert Path(context["InstallDir"]) == explicit_dir
    assert context["ParentProcessId"] == 812


def test_update_context_rejects_unowned_versioned_layout(tmp_path: Path) -> None:
    executable = tmp_path / "unowned" / ".opensre-app" / "versions" / "build-one" / "opensre.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")

    completed, context = _resolve_install_context(
        cwd=tmp_path,
        update_executable=executable,
        parent_process_id=913,
        check=False,
    )

    assert completed.returncode != 0
    assert context is None
    assert "unowned OpenSRE versioned update path" in completed.stderr


@pytest.mark.parametrize("malformed_parent", ("build-one", "versions"))
def test_update_context_rejects_malformed_managed_layout_path(
    tmp_path: Path,
    malformed_parent: str,
) -> None:
    executable = tmp_path / "malformed" / ".opensre-app" / malformed_parent / "opensre.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")

    completed, context = _resolve_install_context(
        cwd=tmp_path,
        update_executable=executable,
        parent_process_id=914,
        check=False,
    )

    assert completed.returncode != 0
    assert context is None
    assert "malformed OpenSRE versioned update path" in completed.stderr


def test_historical_onefile_parent_infers_custom_update_context(tmp_path: Path) -> None:
    assert _POWERSHELL is not None
    install_dir = tmp_path / "historical custom install"
    install_dir.mkdir()
    legacy_executable = install_dir / "opensre.exe"
    shutil.copy2(Path(os.environ["COMSPEC"]), legacy_executable)
    probe_script = tmp_path / "parent-probe.ps1"
    probe_script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
Remove-Item Env:OPENSRE_INSTALL_DIR -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_UPDATE_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_UPDATE_PARENT_PID -ErrorAction SilentlyContinue
$context = Resolve-OpenSreInstallContext
$payload = [ordered]@{{
    InstallDir = [string]$context.InstallDir
    ParentProcessId = [int]$context.ParentProcessId
    IsUpdate = [bool]$context.IsUpdate
    LegacyBinaryPath = [string]$context.LegacyBinaryPath
}}
Write-Output ({_ps_literal(_CONTEXT_PREFIX)} + ($payload | ConvertTo-Json -Compress))
""",
        encoding="utf-8",
    )
    command = (
        f"{_POWERSHELL} -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -File {probe_script}"
    )
    env = _powershell_env()
    env.pop("OPENSRE_INSTALL_DIR", None)
    env.pop("OPENSRE_UPDATE_EXECUTABLE", None)
    env.pop("OPENSRE_UPDATE_PARENT_PID", None)

    completed = subprocess.run(
        [str(legacy_executable), "/d", "/s", "/c", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    context = _prefixed_json(completed.stdout, _CONTEXT_PREFIX)
    assert Path(context["InstallDir"]) == install_dir
    assert context["ParentProcessId"] > 0
    assert context["IsUpdate"] is True
    assert Path(context["LegacyBinaryPath"]) == legacy_executable


def test_onedir_upgrade_replaces_internal_tree_and_cleans_old_version(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "bin"
    first_binary = _make_onedir_bundle(
        tmp_path / "first bundle",
        payload={
            "old-only.txt": "old",
            "shared.txt": "old value",
        },
    )
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="build-one",
        cwd=tmp_path,
    )
    assert first is not None
    first_app_root = _path(first, "AppRoot")

    second_binary = _make_onedir_bundle(
        tmp_path / "second bundle",
        payload={
            "new-only.txt": "new",
            "shared.txt": "new value",
        },
    )
    _, second = _install_bundle(
        binary_path=second_binary,
        install_dir=install_dir,
        install_id="build-two",
        cwd=tmp_path,
    )
    assert second is not None
    second_app_root = _path(second, "AppRoot")
    second_internal = second_app_root / "_internal"

    assert second_app_root != first_app_root
    _wait_until(lambda: not first_app_root.exists())
    assert not (second_internal / "old-only.txt").exists()
    assert (second_internal / "new-only.txt").read_text(encoding="utf-8") == "new"
    assert (second_internal / "shared.txt").read_text(encoding="utf-8") == "new value"
    assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_deferred_cleanup_never_deletes_a_newer_active_bundle(tmp_path: Path) -> None:
    install_dir = tmp_path / "overlapping updates"
    first_binary = _make_onedir_bundle(tmp_path / "overlap first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="overlap-one",
        cwd=tmp_path,
    )
    assert first is not None

    holder = subprocess.Popen([os.environ["COMSPEC"], "/d", "/c", "ping -n 10 127.0.0.1 >nul"])
    try:
        second_binary = _make_onedir_bundle(tmp_path / "overlap second")
        _, second = _install_bundle(
            binary_path=second_binary,
            install_dir=install_dir,
            install_id="overlap-two",
            cwd=tmp_path,
            parent_process_id=holder.pid,
        )
        assert second is not None
        assert second["DeferredCleanup"] is True

        third_binary = _make_onedir_bundle(tmp_path / "overlap third")
        _, third = _install_bundle(
            binary_path=third_binary,
            install_dir=install_dir,
            install_id="overlap-three",
            cwd=tmp_path,
        )
        assert third is not None
        third_root = _path(third, "AppRoot")

        holder.wait(timeout=15)
        time.sleep(1)

        assert third_root.is_dir()
        pointer = install_dir / ".opensre-app" / "current.txt"
        assert pointer.read_text(encoding="utf-8").strip() == "overlap-three"
        assert _probe_launcher(_path(third, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


def test_deferred_upgrade_removes_long_old_version_tree(tmp_path: Path) -> None:
    short_install_dir = tmp_path / "i"
    old_install_id = "long-old-" + ("o" * 32)
    first_binary = _make_onedir_bundle(tmp_path / "long cleanup first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=short_install_dir,
        install_id=old_install_id,
        cwd=tmp_path,
    )
    assert first is not None
    old_root = _path(first, "AppRoot")
    payload_dir = old_root / "_internal"
    while len(str(payload_dir / "payload.txt")) <= 220:
        payload_dir /= "nested-content-filter"
    payload_dir.mkdir(parents=True)
    relative_payload = (payload_dir / "payload.txt").relative_to(short_install_dir)
    (payload_dir / "payload.txt").write_text("old", encoding="utf-8")

    install_name_prefix = "upgrade path with spaces-"
    unpadded_layout_root = tmp_path / install_name_prefix / ".opensre-app"
    padding_length = 220 - len(str(unpadded_layout_root))
    assert padding_length > 0
    install_dir = tmp_path / (install_name_prefix + ("x" * padding_length))
    short_install_dir.rename(install_dir)
    old_root = install_dir / ".opensre-app" / "versions" / old_install_id
    layout_root = install_dir / ".opensre-app"
    assert len(str(layout_root / "stage-long-new")) < 248
    assert len(str(layout_root / ("current-" + ("f" * 32) + ".tmp"))) > 260
    assert len(str(old_root / "opensre.exe")) >= 260
    assert len(str(install_dir / relative_payload)) > 260
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    holder = subprocess.Popen([os.environ["COMSPEC"], "/d", "/c", "ping -n 10 127.0.0.1 >nul"])

    try:
        second_binary = _make_onedir_bundle(tmp_path / "long cleanup second")
        _, second = _install_bundle(
            binary_path=second_binary,
            install_dir=install_dir,
            install_id="long-new",
            cwd=tmp_path,
            parent_process_id=holder.pid,
        )
        assert second is not None
        assert second["DeferredCleanup"] is True
        cleanup_path = _path(second, "CleanupPath")
        assert os.path.normcase(str(cleanup_path.parent)) == os.path.normcase(tempfile.gettempdir())
        new_root = _path(second, "AppRoot")

        holder.wait(timeout=15)
        deadline = time.monotonic() + 30
        while old_root.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not old_root.exists()
        assert new_root.is_dir()
        assert unrelated.read_text(encoding="utf-8") == "keep"
        pointer = install_dir / ".opensre-app" / "current.txt"
        assert pointer.read_text(encoding="utf-8").strip() == "long-new"
        assert _probe_launcher(_path(second, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


def test_install_revalidates_layout_after_uninstall_worker_wins_lock(tmp_path: Path) -> None:
    assert _POWERSHELL is not None
    install_dir = tmp_path / "worker wins reinstall race"
    app_root = install_dir / ".opensre-app"
    old_version = app_root / "versions" / "old-build"
    old_version.mkdir(parents=True)
    old_executable = old_version / "opensre.exe"
    shutil.copy2(Path(os.environ["COMSPEC"]), old_executable)
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("old-build\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    lock_path = install_dir / ".opensre-app.install.lock"
    lock_path.write_bytes(b"")

    replacement = _make_onedir_bundle(tmp_path / "worker wins replacement")
    lock_waiting = tmp_path / "installer-reached-lock.marker"
    allow_lock = tmp_path / "allow-installer-lock.marker"
    installer_script = tmp_path / "worker-wins-installer.ps1"
    installer_script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
function Open-OpenSreInstallLock {{
    param([string]$InstallDir, [int]$TimeoutSeconds = 30)
    [System.IO.File]::WriteAllText({_ps_literal(lock_waiting)}, 'waiting')
    $gateDeadline = [System.DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path -LiteralPath {_ps_literal(allow_lock)})) {{
        if ([System.DateTime]::UtcNow -ge $gateDeadline) {{ throw 'installer gate timed out' }}
        Start-Sleep -Milliseconds 50
    }}
    $lockDeadline = [System.DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([System.DateTime]::UtcNow -lt $lockDeadline) {{
        try {{
            return [System.IO.File]::Open(
                (Join-Path $InstallDir '.opensre-app.install.lock'),
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }}
        catch [System.IO.IOException] {{
            Start-Sleep -Milliseconds 50
        }}
    }}
    throw 'installer lock timed out'
}}
$result = Install-OpenSreVerifiedBundle `
    -BinaryPath {_ps_literal(replacement)} `
    -InstallDir {_ps_literal(install_dir)} `
    -InstallId 'new-build'
Write-Output ({_ps_literal(_RESULT_PREFIX)} + ($result | ConvertTo-Json -Compress))
""",
        encoding="utf-8",
    )
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    installer: subprocess.Popen[str] | None = None

    try:
        ok, err = schedule_windows_managed_cleanup(
            executable=old_executable,
            app_root=app_root,
            launcher=launcher,
            parent_pid=holder.pid,
        )
        assert ok is True
        assert err is None
        installer = subprocess.Popen(
            [
                _POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installer_script),
            ],
            cwd=tmp_path,
            env=_powershell_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        deadline = time.monotonic() + 30
        while not lock_waiting.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lock_waiting.is_file(), "installer never reached its lock acquisition"

        holder.terminate()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 30
        while app_root.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not app_root.exists(), "uninstall worker did not win and move the old app root"

        allow_lock.write_text("continue", encoding="utf-8")
        stdout, stderr = installer.communicate(timeout=90)

        assert installer.returncode == 0, stdout + stderr
        marker = app_root / "layout-v1.marker"
        pointer = app_root / "current.txt"
        new_executable = app_root / "versions" / "new-build" / "opensre.exe"
        assert marker.read_text(encoding="utf-8").strip() == ("OpenSRE Windows bundle layout v1")
        assert pointer.read_text(encoding="utf-8").strip() == "new-build"
        assert new_executable.is_file()
        assert launcher.is_file()
        _, context = _resolve_install_context(
            cwd=tmp_path,
            update_executable=new_executable,
            parent_process_id=5844,
        )
        assert context is not None
        assert Path(context["InstallDir"]) == install_dir
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        if installer is not None and installer.poll() is None:
            installer.kill()
            installer.wait(timeout=10)


def test_failed_staged_verification_keeps_previous_install_active(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    first_binary = _make_onedir_bundle(
        tmp_path / "working bundle",
        payload={"working.txt": "still current"},
    )
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="working-build",
        cwd=tmp_path,
    )
    assert first is not None
    launcher = _path(first, "LauncherPath")
    first_app_root = _path(first, "AppRoot")
    before = _probe_launcher(launcher, cwd=tmp_path)
    invalid_binary = _make_invalid_onedir_bundle(tmp_path / "invalid bundle")

    failed, failed_result = _install_bundle(
        binary_path=invalid_binary,
        install_dir=install_dir,
        install_id="invalid-build",
        cwd=tmp_path,
        check=False,
    )

    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert failed_result is None
    assert first_app_root.is_dir()
    assert (first_app_root / "_internal" / "working.txt").is_file()
    assert launcher.is_file()
    after = _probe_launcher(launcher, cwd=tmp_path)
    assert before["VersionExit"] == after["VersionExit"] == 0
    assert before["VersionOutput"] == after["VersionOutput"]
    assert not any(path.name == "invalid-build" for path in install_dir.rglob("*"))


def test_failed_onedir_package_smoke_keeps_previous_install_active(tmp_path: Path) -> None:
    install_dir = tmp_path / "package smoke rollback"
    first_binary = _make_onedir_bundle(tmp_path / "package smoke stable")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="package-smoke-stable",
        cwd=tmp_path,
    )
    assert first is not None
    launcher = _path(first, "LauncherPath")
    first_app_root = _path(first, "AppRoot")
    before = _probe_launcher(launcher, cwd=tmp_path)
    failing_binary = _make_onedir_bundle(
        tmp_path / "package smoke failure",
        payload={"package-smoke-fail.txt": "fail before activation"},
    )

    failed, failed_result = _install_bundle(
        binary_path=failing_binary,
        install_dir=install_dir,
        install_id="package-smoke-failed",
        cwd=tmp_path,
        check=False,
    )

    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert failed_result is None
    assert "bundle smoke check failed" in failed.stderr
    assert first_app_root.is_dir()
    assert launcher.is_file()
    after = _probe_launcher(launcher, cwd=tmp_path)
    assert before["VersionExit"] == after["VersionExit"] == 0
    assert before["VersionOutput"] == after["VersionOutput"]
    assert not any(path.name == "package-smoke-failed" for path in install_dir.rglob("*"))


def test_later_install_retries_a_deep_failed_staging_directory(tmp_path: Path) -> None:
    install_dir = tmp_path / "deep stage retry"
    first_binary = _make_onedir_bundle(tmp_path / "deep stage stable")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="deep-stage-stable",
        cwd=tmp_path,
    )
    assert first is not None
    invalid_binary = _make_invalid_onedir_bundle(tmp_path / "deep stage invalid")
    deep_directory = invalid_binary.parent / "_internal"
    for index in range(8):
        deep_directory /= f"segment-{index}-" + ("x" * 28)
    extended_deep_directory = "\\\\?\\" + str(deep_directory)
    created = _run_powershell(
        f"""
$directory = [System.IO.Directory]::CreateDirectory({_ps_literal(extended_deep_directory)})
[System.IO.File]::WriteAllText(
    [System.IO.Path]::Combine($directory.FullName, 'payload.dat'),
    'orphan candidate'
)
""",
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stdout + created.stderr

    failed, failed_result = _install_bundle(
        binary_path=invalid_binary,
        install_dir=install_dir,
        install_id="deep-stage-invalid",
        cwd=tmp_path,
        check=False,
        installer_override="""
function Remove-OpenSreInstallPath {
    param([string]$Path)
    throw 'forced stage cleanup failure'
}
""",
    )

    assert failed.returncode != 0
    assert failed_result is None
    orphaned_stage = install_dir / ".opensre-app" / "stage-deep-stage-invalid"
    assert orphaned_stage.is_dir()

    recovery_binary = _make_onedir_bundle(tmp_path / "deep stage recovery")
    _, recovered = _install_bundle(
        binary_path=recovery_binary,
        install_dir=install_dir,
        install_id="deep-stage-recovered",
        cwd=tmp_path,
    )

    assert recovered is not None
    _wait_until(lambda: not orphaned_stage.exists())
    assert _probe_launcher(_path(recovered, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0


def test_failed_post_switch_verification_rolls_back_pointer_and_retains_bundle(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "post switch rollback"
    first_binary = _make_onedir_bundle(tmp_path / "post switch first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="stable-build",
        cwd=tmp_path,
    )
    assert first is not None
    replacement_binary = _make_onedir_bundle(tmp_path / "post switch replacement")
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
function Get-OpenSreBinaryVersionInfo {{
    param([string]$BinaryPath)
    if ([System.IO.Path]::GetExtension($BinaryPath) -ieq '.cmd') {{
        throw 'forced launcher verification failure'
    }}
    return [pscustomobject]@{{ Text = 'opensre, version 0.1'; Version = '0.1' }}
}}
Install-OpenSreVerifiedBundle `
    -BinaryPath {_ps_literal(replacement_binary)} `
    -InstallDir {_ps_literal(install_dir)} `
    -InstallId 'failed-after-switch'
"""

    failed = _run_powershell(script, cwd=tmp_path)

    assert failed.returncode != 0
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "stable-build"
    assert (install_dir / ".opensre-app" / "versions" / "failed-after-switch").is_dir()
    assert "retained for a later safe cleanup" in failed.stdout
    launcher = _path(first, "LauncherPath")
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0


def test_rollback_never_partially_deletes_activated_bundle_in_use(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "busy rollback"
    first_binary = _make_onedir_bundle(tmp_path / "busy rollback stable")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="busy-rollback-stable",
        cwd=tmp_path,
    )
    assert first is not None
    replacement_binary = _make_onedir_bundle(
        tmp_path / "busy rollback replacement",
        payload={
            "lazy/module.dat": "must remain complete",
            "nested/shared.dat": "also retained",
        },
    )
    replacement_manifest = {
        path.relative_to(replacement_binary.parent): _sha256(path)
        for path in replacement_binary.parent.rglob("*")
        if path.is_file()
    }
    child_pid_path = tmp_path / "busy-rollback-child.pid"
    failed_install_id = "busy-rollback-failed"
    failed_root = install_dir / ".opensre-app" / "versions" / failed_install_id
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
function Set-OpenSreCurrentInstallId {{
    param([string]$LayoutRoot, [string]$InstallId)
    [System.IO.File]::WriteAllText(
        (Join-Path $LayoutRoot 'current.txt'),
        "$InstallId`r`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    if ($InstallId -eq {_ps_literal(failed_install_id)}) {{
        $activatedBinary = Join-Path {_ps_literal(failed_root)} 'opensre.exe'
        $childProcess = Start-Process `
            -FilePath $activatedBinary `
            -ArgumentList @('hold', '30000') `
            -WindowStyle Hidden `
            -PassThru
        [System.IO.File]::WriteAllText(
            {_ps_literal(child_pid_path)},
            [string]$childProcess.Id
        )
        Start-Sleep -Milliseconds 250
        if ($childProcess.HasExited) {{
            throw 'activated test process exited unexpectedly'
        }}
        throw 'forced pointer failure after publishing and concurrent launch'
    }}
}}
Install-OpenSreVerifiedBundle `
    -BinaryPath {_ps_literal(replacement_binary)} `
    -InstallDir {_ps_literal(install_dir)} `
    -InstallId {_ps_literal(failed_install_id)}
"""

    failed = _run_powershell(script, cwd=tmp_path)
    assert child_pid_path.is_file(), failed.stdout + failed.stderr
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    try:
        assert failed.returncode != 0
        assert "retained for a later safe cleanup" in failed.stdout
        pointer = install_dir / ".opensre-app" / "current.txt"
        assert pointer.read_text(encoding="utf-8").strip() == "busy-rollback-stable"
        running = _run_powershell(
            f"Get-Process -Id {child_pid} -ErrorAction Stop | Out-Null",
            cwd=tmp_path,
        )
        assert running.returncode == 0, running.stdout + running.stderr
        assert failed_root.is_dir()
        assert {
            path.relative_to(failed_root): _sha256(path)
            for path in failed_root.rglob("*")
            if path.is_file()
        } == replacement_manifest
        assert (failed_root / "_internal" / "lazy" / "module.dat").read_text(
            encoding="utf-8"
        ) == "must remain complete"
        assert _probe_launcher(_path(first, "LauncherPath"), cwd=tmp_path)["VersionExit"] == 0
    finally:
        _run_powershell(
            f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
            cwd=tmp_path,
        )


def test_failed_launcher_rewrite_restores_the_previous_launcher(tmp_path: Path) -> None:
    install_dir = tmp_path / "launcher rollback"
    first_binary = _make_onedir_bundle(tmp_path / "launcher rollback first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="launcher-stable",
        cwd=tmp_path,
    )
    assert first is not None
    launcher = _path(first, "LauncherPath")
    launcher_before = launcher.read_bytes()
    replacement = _make_onedir_bundle(tmp_path / "launcher rollback replacement")

    failed, result = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="launcher-invalid",
        cwd=tmp_path,
        check=False,
        installer_override=r"""
function Get-OpenSreBinaryVersionInfo {
    param([string]$BinaryPath)
    if ([System.IO.Path]::GetExtension($BinaryPath) -ieq '.cmd') {
        throw 'forced launcher verification failure'
    }
    return [pscustomobject]@{ Text = 'opensre, version 0.1'; Version = '0.1' }
}
""",
    )

    assert failed.returncode != 0
    assert result is None
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "launcher-stable"
    assert launcher.read_bytes() == launcher_before
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0
    assert (install_dir / ".opensre-app" / "versions" / "launcher-invalid").is_dir()
    assert "retained for a later safe cleanup" in failed.stdout


def test_failed_activation_does_not_restore_modified_marker_owned_launcher(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "modified launcher rollback"
    first_binary = _make_onedir_bundle(tmp_path / "modified launcher first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="modified-launcher-stable",
        cwd=tmp_path,
    )
    assert first is not None
    launcher = _path(first, "LauncherPath")
    canonical_content = launcher.read_bytes()
    tamper_marker = tmp_path / "modified-launcher-ran.txt"
    launcher.write_bytes(
        "\r\n".join(
            (
                "@echo off",
                ":: OpenSRE Windows launcher v1",
                f'echo tampered>"{tamper_marker}"',
                f'"{_path(first, "BinaryPath")}" %*',
                "exit /b %ERRORLEVEL%",
                "",
            )
        ).encode("utf-8")
    )
    replacement = _make_onedir_bundle(tmp_path / "modified launcher replacement")

    failed, result = _install_bundle(
        binary_path=replacement,
        install_dir=install_dir,
        install_id="modified-launcher-invalid",
        cwd=tmp_path,
        check=False,
        installer_override=r"""
function Get-OpenSreBinaryVersionInfo {
    param([string]$BinaryPath)
    if ([System.IO.Path]::GetExtension($BinaryPath) -ieq '.cmd') {
        throw 'forced launcher verification failure'
    }
    return [pscustomobject]@{ Text = 'opensre, version 0.1'; Version = '0.1' }
}
""",
    )

    assert failed.returncode != 0
    assert result is None
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "modified-launcher-stable"
    assert launcher.read_bytes() == canonical_content
    assert not tamper_marker.exists()
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0
    assert (install_dir / ".opensre-app" / "versions" / "modified-launcher-invalid").is_dir()
    assert "retained for a later safe cleanup" in failed.stdout


def test_failed_pointer_rollback_preserves_the_pointer_live_target(tmp_path: Path) -> None:
    install_dir = tmp_path / "rollback failure safety"
    first_binary = _make_onedir_bundle(tmp_path / "rollback failure first")
    _, first = _install_bundle(
        binary_path=first_binary,
        install_dir=install_dir,
        install_id="rollback-stable",
        cwd=tmp_path,
    )
    assert first is not None
    replacement_binary = _make_onedir_bundle(tmp_path / "rollback failure replacement")
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
function Set-OpenSreCurrentInstallId {{
    param([string]$LayoutRoot, [string]$InstallId)
    if ($InstallId -eq 'rollback-stable') {{
        throw 'forced rollback write failure'
    }}
    [System.IO.File]::WriteAllText(
        (Join-Path $LayoutRoot 'current.txt'),
        "$InstallId`r`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
}}
function Get-OpenSreBinaryVersionInfo {{
    param([string]$BinaryPath)
    if ([System.IO.Path]::GetExtension($BinaryPath) -ieq '.cmd') {{
        throw 'forced launcher verification failure'
    }}
    return [pscustomobject]@{{ Text = 'opensre, version 0.1'; Version = '0.1' }}
}}
Install-OpenSreVerifiedBundle `
    -BinaryPath {_ps_literal(replacement_binary)} `
    -InstallDir {_ps_literal(install_dir)} `
    -InstallId 'rollback-live-target'
"""

    failed = _run_powershell(script, cwd=tmp_path)

    assert failed.returncode != 0
    pointer = install_dir / ".opensre-app" / "current.txt"
    assert pointer.read_text(encoding="utf-8").strip() == "rollback-live-target"
    live_root = install_dir / ".opensre-app" / "versions" / "rollback-live-target"
    assert live_root.is_dir()
    launcher = _path(first, "LauncherPath")
    assert _probe_launcher(launcher, cwd=tmp_path)["VersionExit"] == 0


def test_install_preserves_unrelated_install_directory_entries(tmp_path: Path) -> None:
    install_dir = tmp_path / "shared bin"
    unrelated_dir = install_dir / "another-application"
    unrelated_dir.mkdir(parents=True)
    sentinel = install_dir / "sentinel.dat"
    nested_sentinel = unrelated_dir / "state.json"
    sentinel.write_bytes(b"do not replace")
    nested_sentinel.write_text('{"owned_by":"someone_else"}', encoding="utf-8")
    binary = _make_onedir_bundle(tmp_path / "bundle")

    _, result = _install_bundle(
        binary_path=binary,
        install_dir=install_dir,
        install_id="safe-build",
        cwd=tmp_path,
    )

    assert result is not None
    assert sentinel.read_bytes() == b"do not replace"
    assert nested_sentinel.read_text(encoding="utf-8") == '{"owned_by":"someone_else"}'


_E2E_RELEASE_VERSION = "0.1.2026.8.31"
_CONFIRMATION_PROMPT_PREFIX = "__OPENSRE_CONFIRMATION_PROMPT__"


def _make_release_archive(root: Path, *, payload: dict[str, str] | None = None) -> Path:
    """Build a release-format zip that contains the complete onedir bundle."""
    bundle_root = root / "release bundle"
    _make_onedir_bundle(bundle_root, payload=payload)
    archive = root / "opensre-windows-x86_64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle_zip:
        for path in sorted(bundle_root.rglob("*")):
            if path.is_file():
                bundle_zip.write(path, path.relative_to(bundle_root).as_posix())
    return archive


def _install_e2e_overrides(archive: Path, *, confirmation: str | None) -> str:
    """Stub only release discovery and download so install-context resolution stays real."""
    interactive_override = ""
    if confirmation is not None:
        interactive_override = f"""
function Test-OpenSreInteractiveHost {{
    return $true
}}
function Read-OpenSreConfirmationResponse {{
    param([Parameter(Mandatory = $true)][string]$Prompt)
    Write-Host ({_ps_literal(_CONFIRMATION_PROMPT_PREFIX)} + $Prompt)
    return {_ps_literal(confirmation)}
}}
"""
    return f"""
function Get-OpenSreReleaseMetadata {{
    param([string]$Repo, [string]$Channel, [string]$RequestedVersion)
    return [pscustomobject]@{{
        Version = {_ps_literal(_E2E_RELEASE_VERSION)}
        Release = [pscustomobject]@{{ tag_name = 'main-build' }}
    }}
}}
function Resolve-OpenSreArchiveDownload {{
    param($Release, [string]$Version, [string]$Channel, [string]$TargetArch)
    return [pscustomobject]@{{
        ArchiveName = 'opensre-windows-x86_64.zip'
        ArchiveUrl = {_ps_literal(archive)}
        ChecksumUrl = ''
        ChecksumName = ''
        ResolvedArch = $TargetArch
    }}
}}
function Invoke-OpenSreDownloadFileWithProgress {{
    param([string]$Uri, [string]$OutFile, [string]$Label)
    Copy-Item -LiteralPath $Uri -Destination $OutFile -Force
}}
function Ensure-OpenSreGithubCli {{ }}
function Test-OpenSreDirectoryOnPath {{
    param([string]$Directory)
    return $true
}}
function Start-OpenSreOnboardingAfterInstall {{
    param([string]$BinaryPath, [string]$DisplayName)
}}
{interactive_override}
"""


def _install_end_to_end(
    *,
    archive: Path,
    install_dir: Path,
    cwd: Path,
    confirmation: str | None,
    opt_in: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the full installer entry point through normal install-context resolution."""
    opt_in_line = (
        f"$env:OPENSRE_INSTALL_REPLACE_EXISTING_BINARY = {_ps_literal(opt_in)}"
        if opt_in is not None
        else "Remove-Item Env:OPENSRE_INSTALL_REPLACE_EXISTING_BINARY -ErrorAction SilentlyContinue"
    )
    script = f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
Write-Host (
    '__OPENSRE_PS_DIAG__' +
    $PSVersionTable.PSVersion.ToString() + '|' +
    [string][bool](Get-Command Get-FileHash -ErrorAction SilentlyContinue) + '|' +
    [string]$env:PSModulePath
)
{_install_e2e_overrides(archive, confirmation=confirmation)}
$env:OPENSRE_INSTALL_DIR = {_ps_literal(install_dir)}
Remove-Item Env:OPENSRE_UPDATE_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_UPDATE_PARENT_PID -ErrorAction SilentlyContinue
{opt_in_line}
Install-OpenSre
"""
    return _run_powershell(script, cwd=cwd)


def _tree_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        snapshot[key] = _sha256(path) if path.is_file() else "<dir>"
    return snapshot


def _preexisting_flat_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a historical flat installation and guard its executable against execution."""
    install_dir = tmp_path / "historical install dir"
    install_dir.mkdir()
    preexisting_binary = install_dir / "opensre.exe"
    shutil.copy2(_fake_opensre_executable(), preexisting_binary)
    (install_dir / "unrelated-tool.txt").write_text("keep me", encoding="utf-8")
    monkeypatch.setenv("OPENSRE_TEST_GUARDED_EXECUTABLE", str(preexisting_binary))
    monkeypatch.setenv(
        "OPENSRE_TEST_EXECUTION_MARKER", str(tmp_path / "preexisting-executable-ran.txt")
    )
    return install_dir


def test_reinstall_over_flat_install_replaces_only_after_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = _preexisting_flat_install(tmp_path, monkeypatch)
    preexisting_binary = install_dir / "opensre.exe"
    archive = _make_release_archive(tmp_path / "release")

    completed = _install_end_to_end(
        archive=archive,
        install_dir=install_dir,
        cwd=tmp_path,
        confirmation="y",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    prompts = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith(_CONFIRMATION_PROMPT_PREFIX)
    ]
    assert len(prompts) == 1
    assert "[y/N]" in prompts[0]
    assert str(preexisting_binary) in completed.stdout
    assert not preexisting_binary.exists()
    assert (install_dir / "opensre.cmd").is_file()
    assert (install_dir / ".opensre-app" / "current.txt").is_file()
    assert (install_dir / "unrelated-tool.txt").read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "preexisting-executable-ran.txt").exists()


def test_reinstall_over_flat_install_declined_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = _preexisting_flat_install(tmp_path, monkeypatch)
    archive = _make_release_archive(tmp_path / "release")
    before = _tree_snapshot(install_dir)

    completed = _install_end_to_end(
        archive=archive,
        install_dir=install_dir,
        cwd=tmp_path,
        confirmation="n",
    )

    assert completed.returncode != 0
    assert "Refusing to replace unverified pre-existing executable" in completed.stderr
    assert _tree_snapshot(install_dir) == before
    assert not (tmp_path / "preexisting-executable-ran.txt").exists()


def test_reinstall_over_flat_install_is_fail_closed_without_a_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = _preexisting_flat_install(tmp_path, monkeypatch)
    archive = _make_release_archive(tmp_path / "release")
    before = _tree_snapshot(install_dir)

    completed = _install_end_to_end(
        archive=archive,
        install_dir=install_dir,
        cwd=tmp_path,
        confirmation=None,
    )

    assert completed.returncode != 0
    assert "Refusing to replace unverified pre-existing executable" in completed.stderr
    assert "OPENSRE_INSTALL_REPLACE_EXISTING_BINARY=1" in completed.stderr
    assert _tree_snapshot(install_dir) == before
    assert not (tmp_path / "preexisting-executable-ran.txt").exists()


def test_reinstall_over_flat_install_honors_the_automation_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = _preexisting_flat_install(tmp_path, monkeypatch)
    preexisting_binary = install_dir / "opensre.exe"
    archive = _make_release_archive(tmp_path / "release")

    completed = _install_end_to_end(
        archive=archive,
        install_dir=install_dir,
        cwd=tmp_path,
        confirmation=None,
        opt_in="1",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not any(
        line.startswith(_CONFIRMATION_PROMPT_PREFIX) for line in completed.stdout.splitlines()
    )
    assert not preexisting_binary.exists()
    assert (install_dir / "opensre.cmd").is_file()
    assert (install_dir / "unrelated-tool.txt").read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "preexisting-executable-ran.txt").exists()


def test_update_migration_from_flat_install_needs_no_confirmation(tmp_path: Path) -> None:
    """`opensre update` migration stays authorized by process identity, with no prompt."""
    assert _POWERSHELL is not None
    install_dir = tmp_path / "historical update install"
    install_dir.mkdir()
    legacy_executable = install_dir / "opensre.exe"
    shutil.copy2(Path(os.environ["COMSPEC"]), legacy_executable)
    archive = _make_release_archive(tmp_path / "release")

    probe_script = tmp_path / "update-migration-probe.ps1"
    probe_script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
. {_ps_literal(INSTALL_PS1)} -SkipMain
{_install_e2e_overrides(archive, confirmation=None)}
Remove-Item Env:OPENSRE_INSTALL_DIR -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_UPDATE_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_UPDATE_PARENT_PID -ErrorAction SilentlyContinue
Remove-Item Env:OPENSRE_INSTALL_REPLACE_EXISTING_BINARY -ErrorAction SilentlyContinue
Install-OpenSre
""",
        encoding="utf-8",
    )
    command = (
        f"{_POWERSHELL} -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -File {probe_script}"
    )
    env = _powershell_env()
    for name in (
        "OPENSRE_INSTALL_DIR",
        "OPENSRE_UPDATE_EXECUTABLE",
        "OPENSRE_UPDATE_PARENT_PID",
        "OPENSRE_INSTALL_REPLACE_EXISTING_BINARY",
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [str(legacy_executable), "/d", "/s", "/c", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not any(
        line.startswith(_CONFIRMATION_PROMPT_PREFIX) for line in completed.stdout.splitlines()
    )
    assert (install_dir / "opensre.cmd").is_file()
    assert (install_dir / ".opensre-app" / "current.txt").is_file()
    assert not legacy_executable.exists()
