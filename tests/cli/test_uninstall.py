from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from config.constants.installer import POWERSHELL_MODULE_PATH_ENV
from surfaces.cli.app import cli
from surfaces.cli.lifecycle.uninstall import _remove_path, run_uninstall
from surfaces.cli.lifecycle.windows import (
    WindowsProcessIdentity,
    read_cleanup_script,
    schedule_windows_cleanup,
    schedule_windows_managed_cleanup,
    windows_binary_install_paths,
    windows_process_identity,
    windows_processes_using_tree,
)

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

_EMPTY_PROCESS_ENUMERATOR = r"""
function Get-Process {
    [CmdletBinding()]
    param([int]$Id, [string]$Name)
    if ($PSBoundParameters.ContainsKey('Id')) {
        return $null
    }
    return @()
}
"""


def _missing_process_identity(
    pid: int, *, expected_executable: Path | None = None
) -> tuple[WindowsProcessIdentity, None]:
    del expected_executable
    return WindowsProcessIdentity(pid, Path(sys.executable).resolve(), 1), None


def _inject_failed_process_enumerator(source: str, *, preference: str) -> str:
    anchor = f"$ErrorActionPreference = {preference}\n"
    assert source.count(anchor) == 1
    return source.replace(anchor, anchor + _FAILED_PROCESS_ENUMERATOR, 1)


def _inject_empty_process_enumerator(source: str) -> str:
    anchor = "$ErrorActionPreference = 'Stop'\n"
    assert source.count(anchor) == 1
    return source.replace(anchor, anchor + _EMPTY_PROCESS_ENUMERATOR, 1)


def _inject_failed_retired_target_removal(source: str) -> str:
    retry_loop = "for ($removeAttempt = 0; $removeAttempt -lt 150; $removeAttempt++) {"
    assert source.count(retry_loop) == 1
    source = source.replace(
        retry_loop,
        "for ($removeAttempt = 0; $removeAttempt -lt 1; $removeAttempt++) {",
        1,
    )
    anchor = "$failed = $false\n"
    assert source.count(anchor) == 1
    failure = r"""
function Remove-OpenSreCleanupTarget {
    param([string]$Path)
    throw "forced retired-target removal failure: $Path"
}

"""
    return source.replace(anchor, failure + anchor, 1)


def _inject_data_decision_barrier(source: str, *, ready: Path, release: Path) -> str:
    anchor = "$dataFailed = $false\n"
    assert source.count(anchor) == 1
    ready_payload = base64.b64encode(str(ready).encode("utf-8")).decode("ascii")
    release_payload = base64.b64encode(str(release).encode("utf-8")).decode("ascii")
    barrier = f"""
$dataDecisionReady = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{ready_payload}')
)
$dataDecisionRelease = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{release_payload}')
)
[System.IO.File]::WriteAllText($dataDecisionReady, 'ready')
while (-not (Test-Path -LiteralPath $dataDecisionRelease -PathType Leaf)) {{
    Start-Sleep -Milliseconds 50
}}

"""
    return source.replace(anchor, barrier + anchor, 1)


def _inject_before_data_guard_barrier(source: str, *, ready: Path, release: Path) -> str:
    anchor = "if ($deleteData) {\n    foreach ($guardPathValue in @($payload.data_guard_paths)) {\n"
    assert source.count(anchor) == 1
    ready_payload = base64.b64encode(str(ready).encode("utf-8")).decode("ascii")
    release_payload = base64.b64encode(str(release).encode("utf-8")).decode("ascii")
    barrier = f"""
$dataGuardReady = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{ready_payload}')
)
$dataGuardRelease = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{release_payload}')
)
[System.IO.File]::WriteAllText($dataGuardReady, 'ready')
while (-not (Test-Path -LiteralPath $dataGuardRelease -PathType Leaf)) {{
    Start-Sleep -Milliseconds 50
}}

"""
    return source.replace(anchor, barrier + anchor, 1)


def _short_path_or_skip(path: Path) -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path = kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    length = get_short_path(str(path), buffer, len(buffer))
    if length == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    assert length < len(buffer)
    short_path = Path(buffer.value)
    if not short_path or str(short_path).casefold() == str(path).casefold():
        pytest.skip("8.3 aliases are disabled on the test volume")
    return short_path


def _start_hidden_windows_process(executable: Path) -> int:
    powershell = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    literal = "'" + str(executable).replace("'", "''") + "'"
    completed = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            (
                "$ErrorActionPreference = 'Stop'; "
                f"$process = Start-Process -FilePath {literal} "
                "-ArgumentList @('hold', '120000') -PassThru -WindowStyle Hidden; "
                "if ($null -eq $process) { throw 'Start-Process returned no process' }; "
                "[Console]::Out.WriteLine([int]$process.Id)"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return int(completed.stdout.strip().splitlines()[-1])


def _stop_windows_process(pid: int, executable: Path) -> None:
    identity, _error = windows_process_identity(pid, expected_executable=executable)
    if identity is None:
        return
    powershell = (
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or elevation")
        raise


def test_remove_path_removes_file(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("data")
    ok, err = _remove_path(f)
    assert ok is True
    assert err is None
    assert not f.exists()


def test_remove_path_removes_directory(tmp_path: Path) -> None:
    d = tmp_path / "subdir"
    d.mkdir()
    (d / "child.txt").write_text("x")
    ok, err = _remove_path(d)
    assert ok is True
    assert err is None
    assert not d.exists()


def test_remove_path_nonexistent_returns_ok(tmp_path: Path) -> None:
    ok, err = _remove_path(tmp_path / "does_not_exist")
    assert ok is True
    assert err is None


def test_remove_path_removes_broken_symlink(tmp_path: Path) -> None:
    link = tmp_path / "broken"
    _symlink_or_skip(link, tmp_path / "missing")

    ok, err = _remove_path(link)

    assert ok is True
    assert err is None
    assert not link.exists()
    assert not link.is_symlink()


def test_remove_path_returns_error_on_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "locked"
    d.mkdir()

    def _raise(path: str) -> None:
        raise OSError("Permission denied")

    monkeypatch.setattr("shutil.rmtree", _raise)
    ok, err = _remove_path(d)
    assert ok is False
    assert "Permission denied" in (err or "")


def test_run_uninstall_cancelled_by_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)

    import questionary as _q

    def _confirm_no(*_args: object, **_kwargs: object) -> object:
        return type("Q", (), {"ask": lambda _self: False})()

    monkeypatch.setattr(_q, "confirm", _confirm_no)

    rc = run_uninstall(yes=False)

    assert rc == 0
    assert "Cancelled" in capsys.readouterr().out


def test_run_uninstall_aborted_by_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)

    import questionary as _q

    def _raise_interrupt(*a: object, **kw: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(_q, "confirm", _raise_interrupt)

    rc = run_uninstall(yes=False)

    assert rc == 1
    assert "Aborted" in capsys.readouterr().out


def test_run_uninstall_skips_missing_dirs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [missing])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 0)

    rc = run_uninstall(yes=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "not found" in out
    assert "skipped" in out


def test_run_uninstall_removes_existing_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    d = tmp_path / "tracer_home"
    d.mkdir()
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [d])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 0)

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert not d.exists()
    assert "deleted" in capsys.readouterr().out


def test_run_uninstall_pip_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 0)

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert "opensre has been uninstalled" in capsys.readouterr().out


def test_run_uninstall_pip_failure_shows_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 1)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: False)

    rc = run_uninstall(yes=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "pip uninstall failed" in err
    assert "retry manually" in err


def test_run_uninstall_pip_failure_windows_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 1)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)

    rc = run_uninstall(yes=True)

    assert rc == 1
    assert "pip uninstall" in capsys.readouterr().err


def test_run_uninstall_binary_removes_executable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake_exe = tmp_path / "opensre"
    fake_exe.write_bytes(b"\x7fELF")
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(fake_exe))

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert not fake_exe.exists()
    assert "binary" in capsys.readouterr().out


def test_run_uninstall_onedir_binary_removes_launcher_and_app_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    install_dir = tmp_path / "bin"
    app_dir = install_dir / ".opensre-app"
    internal = app_dir / "_internal"
    internal.mkdir(parents=True)
    fake_exe = app_dir / "opensre"
    fake_exe.write_bytes(b"\x7fELF")
    launcher = install_dir / "opensre"
    _symlink_or_skip(launcher, fake_exe)

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(fake_exe))
    monkeypatch.setattr("shutil.which", lambda _name: str(launcher))

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert not launcher.exists()
    assert not launcher.is_symlink()
    assert not app_dir.exists()
    out = capsys.readouterr().out
    assert str(launcher) in out
    assert str(app_dir) in out


def test_windows_install_paths_find_only_owned_layout_files(tmp_path: Path) -> None:
    install_dir = tmp_path / "install dir"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    legacy_executable = install_dir / "opensre.exe"
    legacy_executable.write_bytes(b"MZ")
    unrelated = install_dir / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")

    paths = windows_binary_install_paths(executable)

    assert paths == [launcher, app_root, install_lock]
    assert legacy_executable not in paths
    assert unrelated not in paths
    assert install_dir not in paths


def test_windows_install_paths_preserve_unowned_launcher(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    version_dir.mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\r\necho user-owned\r\n", encoding="utf-8")

    paths = windows_binary_install_paths(executable)

    assert paths == [app_root]
    assert launcher not in paths


@pytest.mark.parametrize("marker_text", (None, "not an OpenSRE ownership marker\n"))
def test_windows_uninstall_refuses_malformed_managed_layout_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    marker_text: str | None,
) -> None:
    install_dir = tmp_path / "malformed managed install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    marker = app_root / "layout-v1.marker"
    if marker_text is not None:
        marker.write_text(marker_text, encoding="utf-8")
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "user data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("malformed layout must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _unexpected_schedule
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "marker is missing or unreadable" in captured.err or "marker is invalid" in captured.err
    assert "Nothing was deleted" in captured.err
    assert "install.ps1" in captured.err
    assert executable.is_file()
    assert app_root.is_dir()
    assert launcher.is_file()
    assert (data_dir / "state.json").read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_refuses_unmanaged_onedir_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "extracted release" / "opensre"
    payload = bundle_root / "_internal" / "payload.dat"
    payload.parent.mkdir(parents=True)
    payload.write_text("keep the complete bundle", encoding="utf-8")
    executable = bundle_root / "opensre.exe"
    executable.write_bytes(b"MZ")
    data_dir = tmp_path / "user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("unmanaged onedir must not schedule partial cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _unexpected_schedule
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "unpacked Windows onedir bundle" in captured.err
    assert "install.ps1" in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.is_file()
    assert payload.read_text(encoding="utf-8") == "keep the complete bundle"
    assert data_file.read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_refuses_renamed_flat_binary_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "renamed frozen executable"
    install_dir.mkdir()
    executable = install_dir / "other-product.exe"
    executable.write_bytes(b"MZ")
    data_dir = tmp_path / "renamed binary user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("a renamed executable must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _unexpected_schedule
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "historical Windows binary name is invalid" in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.read_bytes() == b"MZ"
    assert data_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_windows_uninstall_rejects_junction_anywhere_in_raw_executable_ancestors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real parent"
    install_dir = real_parent / "nested" / "bin"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    version_dir.mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "junction user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    alias_parent = tmp_path / "aliased parent"
    linked = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(alias_parent), str(real_parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    aliased_executable = alias_parent / "nested" / "bin" / executable.relative_to(install_dir)

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("a junctioned executable path must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(aliased_executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    try:
        rc = run_uninstall(yes=True)

        captured = capsys.readouterr()
        assert rc == 1
        assert "reparse point" in captured.err
        assert "Nothing was deleted" in captured.err
        assert executable.read_bytes() == b"MZ"
        assert launcher.is_file()
        assert data_file.read_text(encoding="utf-8") == "keep"
    finally:
        if alias_parent.is_junction():
            alias_parent.rmdir()


def test_windows_uninstall_refuses_malformed_managed_executable_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "bin" / ".opensre-app"
    malformed_version = app_root / "build-without-versions-parent"
    malformed_version.mkdir(parents=True)
    executable = malformed_version / "opensre.exe"
    executable.write_bytes(b"MZ")
    unrelated = tmp_path / "user-data.json"
    unrelated.write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("malformed managed path must not schedule partial cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [unrelated])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _unexpected_schedule
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "managed Windows executable path is malformed" in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.is_file()
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("pointer_text", "expected_error"),
    (
        (None, "pointer is missing or unreadable"),
        ("../outside\n", "pointer is invalid"),
        ("..\n", "pointer is invalid"),
        (".\n", "pointer is invalid"),
        (".build\n", "pointer is invalid"),
        ("build.\n", "pointer is invalid"),
        ("-build\n", "pointer is invalid"),
        ("build-\n", "pointer is invalid"),
        (" build-1\n", "pointer is invalid"),
        ("missing-build\n", "pointer is dangling"),
    ),
)
def test_windows_uninstall_refuses_malformed_current_pointer_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pointer_text: str | None,
    expected_error: str,
) -> None:
    install_dir = tmp_path / "malformed pointer install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    if pointer_text is not None:
        (app_root / "current.txt").write_text(pointer_text, encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "pointer user data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("malformed layout must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert expected_error in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.is_file()
    assert launcher.is_file()
    assert (data_dir / "state.json").read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_rejects_dot_pointer_even_when_it_names_same_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "dot pointer install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    os.link(executable, app_root / "opensre.exe")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("..\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "dot pointer data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("a traversal pointer must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "current-version pointer is invalid" in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.is_file()
    assert launcher.is_file()
    assert data_file.read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_refuses_stale_managed_version_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "stale managed process"
    app_root = install_dir / ".opensre-app"
    old_version = app_root / "versions" / "old-build"
    current_version = app_root / "versions" / "current-build"
    (old_version / "_internal").mkdir(parents=True)
    (current_version / "_internal").mkdir(parents=True)
    executable = old_version / "opensre.exe"
    executable.write_bytes(b"MZ-old")
    current_executable = current_version / "opensre.exe"
    current_executable.write_bytes(b"MZ-current")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("current-build\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "stale process data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("stale managed process must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _unexpected_schedule
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "not the version selected" in captured.err
    assert "new PowerShell window" in captured.err
    assert "Nothing was deleted" in captured.err
    assert executable.is_file()
    assert current_executable.is_file()
    assert launcher.is_file()
    assert data_file.read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_refuses_second_process_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "busy managed install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "busy user data"
    data_dir.mkdir()
    scheduled = False

    def _running_processes(
        root: Path, *, current_pid: int
    ) -> tuple[list[tuple[int, str]], str | None]:
        assert root == app_root
        assert current_pid == 5844
        return [(9001, str(executable))], None

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        nonlocal scheduled
        scheduled = True
        return True, None

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.os.getpid", lambda: 5844)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.windows_processes_using_tree", _running_processes
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "another OpenSRE process" in captured.err
    assert "PID 9001" in captured.err
    assert "Nothing was deleted" in captured.err
    assert not scheduled
    assert executable.is_file()
    assert launcher.is_file()
    assert data_dir.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process scan only")
def test_windows_process_scan_reports_incomplete_enumeration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_run = subprocess.run
    monkeypatch.setenv(POWERSHELL_MODULE_PATH_ENV.upper(), r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("OPENSRE_TEST_PARENT_VALUE", "preserved")

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        child_env = kwargs.get("env")
        assert isinstance(child_env, dict)
        assert not any(
            name.casefold() == POWERSHELL_MODULE_PATH_ENV.casefold() for name in child_env
        )
        assert child_env["OPENSRE_TEST_PARENT_VALUE"] == "preserved"
        injected_args = list(args)
        command_index = injected_args.index("-Command") + 1
        injected_args[command_index] = _inject_failed_process_enumerator(
            injected_args[command_index],
            preference="'Stop'",
        )
        return real_run(injected_args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.processes.subprocess.run", _run)

    running, error = windows_processes_using_tree(tmp_path, current_pid=os.getpid())

    assert running == []
    assert error == "could not verify every running OpenSRE process path"


def test_windows_uninstall_refuses_incomplete_process_scan_before_deleting_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "incomplete process scan install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "incomplete process scan data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("keep", encoding="utf-8")

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("an unverifiable process scan must not schedule cleanup")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.windows_processes_using_tree",
        lambda _root, **_kwargs: ([], "could not verify every running OpenSRE process path"),
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "could not verify every running OpenSRE process path" in captured.err
    assert "Nothing was deleted" in captured.err
    assert launcher.is_file()
    assert executable.is_file()
    assert (data_dir / "state.json").read_text(encoding="utf-8") == "keep"


def test_windows_uninstall_rechecks_processes_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "prompt race install"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "prompt race data"
    data_dir.mkdir()
    confirmed = False

    def _ask(_self: object) -> bool:
        nonlocal confirmed
        confirmed = True
        return True

    def _confirm(*_args: object, **_kwargs: object) -> object:
        return type("Confirmation", (), {"ask": _ask})()

    def _running_processes(
        root: Path, *, current_pid: int
    ) -> tuple[list[tuple[int, str]], str | None]:
        assert confirmed
        assert root == app_root
        assert current_pid == 5844
        return [(9002, str(executable))], None

    def _unexpected_schedule(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise AssertionError("busy layout must not schedule cleanup")

    import questionary as _q

    monkeypatch.setattr(_q, "confirm", _confirm)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.os.getpid", lambda: 5844)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.windows_processes_using_tree", _running_processes
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        _unexpected_schedule,
    )

    rc = run_uninstall(yes=False)

    captured = capsys.readouterr()
    assert rc == 1
    assert "PID 9002" in captured.err
    assert "Nothing was deleted" in captured.err
    assert launcher.is_file()
    assert executable.is_file()
    assert data_dir.is_dir()


def test_run_uninstall_windows_layout_schedules_owned_paths_after_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install dir"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    legacy_executable = install_dir / "opensre.exe"
    legacy_executable.write_bytes(b"MZ")
    unrelated = install_dir / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    data_dir = tmp_path / "user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep until worker succeeds", encoding="utf-8")
    scheduled: list[dict[str, object]] = []

    def _schedule(**kwargs: object) -> tuple[bool, str | None]:
        scheduled.append(kwargs)
        return True, None

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.os.getpid", lambda: 731)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.windows_processes_using_tree",
        lambda _root, **_kwargs: ([], None),
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup", _schedule
    )

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert scheduled == [
        {
            "executable": executable,
            "app_root": app_root,
            "launcher": launcher,
            "parent_pid": 731,
            "data_paths": [data_dir],
        }
    ]
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert data_file.read_text(encoding="utf-8") == "keep until worker succeeds"
    assert legacy_executable.read_bytes() == b"MZ"
    assert executable.exists()
    output = capsys.readouterr().out
    assert "after this process exits" in output
    assert "after binary cleanup succeeds" in output


def test_windows_cleanup_launch_failure_preserves_data_and_installation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "cleanup launch failure"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    data_dir = tmp_path / "cleanup launch data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("keep", encoding="utf-8")

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.windows_processes_using_tree",
        lambda _root, **_kwargs: ([], None),
    )
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.uninstall.schedule_windows_managed_cleanup",
        lambda **_kwargs: (False, "forced launch failure"),
    )

    rc = run_uninstall(yes=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "could not schedule binary cleanup" in captured.err
    assert "Nothing was deleted" in captured.err
    assert launcher.is_file()
    assert executable.is_file()
    assert (data_dir / "state.json").read_text(encoding="utf-8") == "keep"


def test_run_uninstall_windows_legacy_binary_defers_exact_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "legacy install" / "opensre.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")
    data_dir = tmp_path / "legacy data"
    data_dir.mkdir()
    scheduled: list[dict[str, object]] = []

    def _schedule(
        paths: list[Path],
        *,
        parent_pid: int,
        data_paths: list[Path] | None = None,
        install_lock_path: Path | None = None,
        data_guard_paths: list[Path] | None = None,
    ) -> tuple[bool, str | None]:
        assert parent_pid == 812
        scheduled.append(
            {
                "paths": paths,
                "data_paths": data_paths,
                "install_lock_path": install_lock_path,
                "data_guard_paths": data_guard_paths,
            }
        )
        return True, None

    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [data_dir])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_windows", lambda: True)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.sys.executable", str(executable))
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.os.getpid", lambda: 812)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall.schedule_windows_cleanup", _schedule)

    rc = run_uninstall(yes=True)

    assert rc == 0
    assert scheduled == [
        {
            "paths": [executable],
            "data_paths": [data_dir],
            "install_lock_path": executable.parent / ".opensre-app.install.lock",
            "data_guard_paths": [
                executable.parent / ".opensre-app",
                executable.parent / "opensre.cmd",
                executable,
            ],
        }
    ]
    assert executable.exists()
    assert data_dir.is_dir()


def test_schedule_windows_cleanup_uses_hidden_background_powershell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "path with spaces" / "opensre.cmd"
    target.parent.mkdir()
    target.write_bytes(b"MZ-legacy")
    missing_target = target.with_name("missing.exe")
    captured: dict[str, object] = {}
    monkeypatch.setenv(POWERSHELL_MODULE_PATH_ENV.upper(), r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("OPENSRE_TEST_PARENT_VALUE", "preserved")

    def _popen(args: list[str], **kwargs: object) -> object:
        captured["args"] = args
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.powershell.windows_powershell_executable",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.subprocess.Popen", _popen)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
        _missing_process_identity,
    )

    ok, err = schedule_windows_cleanup([target, missing_target], parent_pid=934)

    assert ok is True
    assert err is None
    args = captured["args"]
    assert isinstance(args, list)
    assert "-WindowStyle" in args
    assert "Hidden" in args
    file_index = args.index("-File")
    cleanup_path = Path(args[file_index + 1])
    try:
        script = cleanup_path.read_text(encoding="utf-8-sig")
        assert "param(" in script
        assert "Move-OpenSreTargetIfUnused" in script
        parent_index = args.index("-ParentProcessId")
        assert args[parent_index + 1] == "934"
        payload_index = args.index("-CleanupPayload")
        payload = json.loads(base64.b64decode(args[payload_index + 1]))
        assert len(payload["operation_id"]) == 32
        assert payload["parent"] == {
            "pid": 934,
            "path": str(Path(sys.executable).resolve()),
            "started_filetime_utc": 1,
        }
        target_metadata = target.stat(follow_symlinks=False)
        assert payload["targets"] == [
            {
                "path": str(target),
                "kind": "file",
                "sha256": hashlib.sha256(b"MZ-legacy").hexdigest(),
                "volume_serial_number": int(target_metadata.st_dev) & 0xFFFFFFFF,
                "file_index": int(target_metadata.st_ino) & 0xFFFFFFFFFFFFFFFF,
                "creation_filetime_utc": (
                    target_metadata.st_ctime_ns // 100 + 116_444_736_000_000_000
                ),
            },
            {"path": str(missing_target), "kind": "missing", "sha256": ""},
        ]
        assert payload["managed"] is None
        assert payload["data_targets"] == []
        assert payload["lock_path"] == ""
        assert payload["data_guard_paths"] == []
        cleanup_index = args.index("-CleanupScriptPath")
        assert Path(args[cleanup_index + 1]) == cleanup_path
    finally:
        cleanup_path.unlink(missing_ok=True)
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert not any(name.casefold() == POWERSHELL_MODULE_PATH_ENV.casefold() for name in child_env)
    assert child_env["OPENSRE_TEST_PARENT_VALUE"] == "preserved"
    assert Path(str(captured["cwd"])) == cleanup_path.parent
    assert isinstance(captured["creationflags"], int)
    assert captured["creationflags"] != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_schedule_windows_cleanup_removes_path_after_parent_exit(tmp_path: Path) -> None:
    short_target = tmp_path / "a"
    payload_dir = short_target
    while len(str(payload_dir / "payload.txt")) <= 220:
        payload_dir /= "nested-content-filter"
    payload_dir.mkdir(parents=True)
    payload = payload_dir / "payload.txt"
    payload.write_text("temporary", encoding="utf-8")
    relative_payload = payload.relative_to(short_target)
    target = tmp_path / ("path with spaces-" + ("x" * 50))
    short_target.rename(target)
    assert len(str(target / relative_payload)) > 260
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, err = schedule_windows_cleanup([target], parent_pid=holder.pid)
        assert ok is True, err
        assert err is None
        assert target.exists()

        holder.terminate()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 30
        while target.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not target.exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_cleanup_worker_resolves_short_path_parent_and_busy_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.cli.test_install_ps1_onedir import _fake_opensre_executable

    fake_opensre = _fake_opensre_executable()
    process_root = Path(tempfile.mkdtemp(prefix="opensre-short-path-process-"))
    parent_dir = process_root / "Long Parent Directory"
    busy_root = process_root / "Long Busy Bundle Directory"
    parent_dir.mkdir()
    busy_root.mkdir()
    parent_executable = parent_dir / "opensre.exe"
    busy_executable = busy_root / "opensre.exe"
    shutil.copy2(fake_opensre, parent_executable)
    shutil.copy2(fake_opensre, busy_executable)
    short_parent = _short_path_or_skip(parent_executable)
    short_busy = _short_path_or_skip(busy_executable)
    sentinel = tmp_path / "wait-for-the-real-parent.txt"
    sentinel.write_text("remove only after parent exit", encoding="utf-8")
    install_lock = tmp_path / "short-path-cleanup.lock"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    user_data = tmp_path / "user-data" / "state.json"
    user_data.parent.mkdir()
    user_data.write_text("keep", encoding="utf-8")
    workers: list[subprocess.Popen[bytes]] = []
    child_handles: dict[str, int] = {}
    failure_details: dict[str, Any] | None = None
    with tempfile.TemporaryFile() as worker_output:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_int
        observed_name = f"Local\\{process_root.name}-parent-observed"
        resume_name = f"Local\\{process_root.name}-resume-inspection"
        observed_event = kernel32.CreateEventW(None, True, False, observed_name)
        resume_event = kernel32.CreateEventW(None, True, False, resume_name)
        if not observed_event or not resume_event:
            for event in (observed_event, resume_event):
                if event:
                    kernel32.CloseHandle(event)
            raise ctypes.WinError(ctypes.get_last_error())

        def _retain_child(name: str, pid: int) -> None:
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            child_handles[name] = handle

        def _failure_state() -> dict[str, Any]:
            return {
                "parent_pid": parent_pid,
                "busy_pid": busy_pid,
                "workers": [{"pid": worker.pid, "exit_code": worker.poll()} for worker in workers],
                "child_wait_status": {
                    name: kernel32.WaitForSingleObject(handle, 0)
                    for name, handle in child_handles.items()
                },
                "sentinel_exists": sentinel.exists(),
                "installation_exists": busy_root.exists(),
                "busy_executable_exists": busy_executable.exists(),
                "lock_exists": install_lock.exists(),
                "user_data_exists": user_data.exists(),
                "unrelated_exists": unrelated.exists(),
                "quarantines": [str(path) for path in tmp_path.glob("*.uninstall-*")],
            }

        def _synchronized_source() -> str:
            source = read_cleanup_script()
            anchor = (
                "        $parent = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue\n"
            )
            assert source.count(anchor) == 1
            handshake = f"""
            if ($null -ne $parent -and -not $script:parentObserved) {{
                $script:parentObserved = $true
                $observed = [System.Threading.EventWaitHandle]::OpenExisting('{observed_name}')
                $resume = [System.Threading.EventWaitHandle]::OpenExisting('{resume_name}')
                try {{
                    $null = $observed.Set()
                    if (-not $resume.WaitOne(30000)) {{
                        throw 'Parent inspection handshake timed out'
                    }}
                }}
                finally {{ $observed.Dispose(); $resume.Dispose() }}
            }}
    """
            return source.replace(anchor, anchor + handshake, 1)

        real_popen = subprocess.Popen

        def _capture_worker(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            kwargs.update(stdout=worker_output, stderr=subprocess.STDOUT)
            worker = real_popen(args, **kwargs)
            workers.append(worker)
            return worker

        created_cleanup_scripts: list[Path] = []
        real_mkstemp = tempfile.mkstemp

        def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
            descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
            created_cleanup_scripts.append(Path(name))
            return descriptor, name

        parent_pid = 0
        busy_pid = 0

        try:
            parent_pid = _start_hidden_windows_process(short_parent)
            _retain_child("parent", parent_pid)
            busy_pid = _start_hidden_windows_process(short_busy)
            _retain_child("busy", busy_pid)
            parent_identity, identity_error = windows_process_identity(
                parent_pid,
                expected_executable=parent_executable,
            )
            assert parent_identity is not None, identity_error

            def _parent_identity(
                pid: int, *, expected_executable: Path | None = None
            ) -> tuple[WindowsProcessIdentity, None]:
                assert pid == parent_pid
                assert expected_executable is not None
                return parent_identity, None

            monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
            monkeypatch.setattr(
                "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
                _parent_identity,
            )

            with monkeypatch.context() as capture:
                capture.setattr(
                    "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script",
                    _synchronized_source,
                )
                capture.setattr(
                    "surfaces.cli.lifecycle.windows.cleanup.subprocess.Popen", _capture_worker
                )
                ok, error = schedule_windows_cleanup(
                    [sentinel, busy_root],
                    parent_pid=parent_pid,
                    install_lock_path=install_lock,
                )
            assert ok is True, error
            assert len(created_cleanup_scripts) == 1

            ready_status = kernel32.WaitForSingleObject(observed_event, 30_000)
            assert ready_status == 0
            running_parent, parent_error = windows_process_identity(
                parent_pid,
                expected_executable=parent_executable,
            )
            assert running_parent is not None, parent_error
            assert sentinel.read_text(encoding="utf-8") == "remove only after parent exit"

            _stop_windows_process(parent_pid, parent_executable)
            assert kernel32.WaitForSingleObject(child_handles["parent"], 0) == 0
            assert kernel32.SetEvent(resume_event)
            cleanup_script = created_cleanup_scripts[0]
            assert len(workers) == 1
            workers[0].wait(timeout=30)
            assert workers[0].returncode == 1
            assert install_lock.exists()
            assert not cleanup_script.exists()
            assert not sentinel.exists()
            running_busy, busy_error = windows_process_identity(
                busy_pid,
                expected_executable=busy_executable,
            )
            assert running_busy is not None, busy_error
            assert busy_executable.is_file()
            assert unrelated.read_text(encoding="utf-8") == "keep"
            assert user_data.read_text(encoding="utf-8") == "keep"
        except Exception as exc:
            failure_details = {"error": repr(exc), "before_teardown": _failure_state()}
            raise
        finally:
            kernel32.SetEvent(resume_event)
            teardown_errors: list[str] = []
            for child_pid, executable in (
                (parent_pid, parent_executable),
                (busy_pid, busy_executable),
            ):
                try:
                    if child_pid:
                        _stop_windows_process(child_pid, executable)
                except Exception as exc:
                    teardown_errors.append(repr(exc))
            for worker in workers:
                try:
                    if worker.poll() is None:
                        worker.terminate()
                    worker.wait(timeout=10)
                except Exception as exc:
                    teardown_errors.append(repr(exc))
            for name, handle in child_handles.items():
                if kernel32.WaitForSingleObject(handle, 10_000) != 0:
                    teardown_errors.append(f"{name} did not terminate")
            try:
                if failure_details is not None or teardown_errors:
                    details = {
                        "failure": failure_details,
                        "after_teardown": _failure_state(),
                        "teardown_errors": teardown_errors,
                    }
                    (tmp_path / "cleanup-worker-failure.json").write_text(
                        json.dumps(details, indent=2), encoding="utf-8"
                    )
                    worker_output.seek(0)
                    (tmp_path / "cleanup-worker-failure.log").write_bytes(worker_output.read())
            finally:
                for handle in child_handles.values():
                    kernel32.CloseHandle(handle)
                kernel32.CloseHandle(observed_event)
                kernel32.CloseHandle(resume_event)
            assert not teardown_errors, teardown_errors


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_cleanup_worker_rejects_ancestor_junction_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_parent = tmp_path / "install-parent"
    install_parent.mkdir()
    target = install_parent / "opensre.exe"
    target.write_bytes(b"MZ-same-content")
    preserved_parent = tmp_path / "preserved-original"
    outside = tmp_path / "outside-user-data"
    outside.mkdir()
    outside_target = outside / "opensre.exe"
    outside_target.write_bytes(target.read_bytes())
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    lock_path = tmp_path / "cleanup.lock"
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, error = schedule_windows_cleanup(
            [target],
            parent_pid=holder.pid,
            install_lock_path=lock_path,
        )
        assert ok is True, error
        install_parent.rename(preserved_parent)
        linked = subprocess.run(
            [
                os.environ["COMSPEC"],
                "/d",
                "/c",
                "mklink",
                "/J",
                str(install_parent),
                str(outside),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert linked.returncode == 0, linked.stdout + linked.stderr

        holder.terminate()
        holder.wait(timeout=10)
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert (preserved_parent / "opensre.exe").read_bytes() == b"MZ-same-content"
        assert outside_target.read_bytes() == b"MZ-same-content"
        assert sentinel.read_text(encoding="utf-8") == "preserve"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        if install_parent.is_junction():
            install_parent.rmdir()
        for cleanup_script in created_cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_cleanup_worker_treats_dangling_junction_as_an_existing_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "late dangling guard"
    junction_destination = tmp_path / "removed junction destination"
    junction_destination.mkdir()
    data_dir = tmp_path / "preserved dangling guard data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("preserve", encoding="utf-8")
    install_lock = tmp_path / "dangling-guard-cleanup.lock"
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, error = schedule_windows_cleanup(
            [target],
            parent_pid=holder.pid,
            data_paths=[data_dir],
            install_lock_path=install_lock,
            data_guard_paths=[target],
        )
        assert ok is True, error
        linked = subprocess.run(
            [
                os.environ["COMSPEC"],
                "/d",
                "/c",
                "mklink",
                "/J",
                str(target),
                str(junction_destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert linked.returncode == 0, linked.stdout + linked.stderr
        junction_destination.rmdir()
        assert target.is_junction()
        assert not target.exists()

        holder.terminate()
        holder.wait(timeout=10)
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert target.is_junction()
        assert data_file.read_text(encoding="utf-8") == "preserve"
        assert install_lock.is_file()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        if target.is_junction():
            target.rmdir()
        for cleanup_script in created_cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_managed_uninstall_removes_long_quarantine_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_dir = tmp_path / "managed uninstall with spaces"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "old-build"
    payload_dir = version_dir / "_internal"
    while len(str(payload_dir / "payload.txt")) <= 225:
        payload_dir /= "nested-content-filter"
    payload_dir.mkdir(parents=True)
    payload = payload_dir / "payload.txt"
    payload.write_text("temporary", encoding="utf-8")
    assert len(str(payload)) < 260
    assert len(str(payload)) + len(".uninstall-") + 32 > 260

    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("old-build\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    data_dir = tmp_path / "managed uninstall data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("remove", encoding="utf-8")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    cleanup_workers: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen
    worker_log = tmp_path / "cleanup-worker.log"

    with worker_log.open("wb") as log_stream:

        def _capture_cleanup_worker(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            is_cleanup = "-CleanupPayload" in args
            if is_cleanup:
                kwargs["stdout"] = log_stream
                kwargs["stderr"] = subprocess.STDOUT
            process = real_popen(args, **kwargs)
            if is_cleanup:
                cleanup_workers.append(process)
            return process

        try:
            with monkeypatch.context() as context:
                context.setattr(
                    "surfaces.cli.lifecycle.windows.cleanup.subprocess.Popen",
                    _capture_cleanup_worker,
                )
                ok, err = schedule_windows_managed_cleanup(
                    executable=executable,
                    app_root=app_root,
                    launcher=launcher,
                    parent_pid=holder.pid,
                    data_paths=[data_dir],
                )
            assert ok is True, err
            assert len(cleanup_workers) == 1
            worker = cleanup_workers[0]

            holder.terminate()
            holder.wait(timeout=10)
            # Filesystem state alone cannot distinguish worker success from an
            # early refusal or prove the detached child has finished.
            exit_code = worker.wait(timeout=30)
            (tmp_path / "cleanup-worker-exit-code.txt").write_text(str(exit_code), encoding="utf-8")
            assert exit_code == 0, worker_log.read_text(encoding="utf-8", errors="replace")

            assert not app_root.exists()
            assert not launcher.exists()
            assert not install_lock.exists()
            assert not data_dir.exists()
            assert list(install_dir.glob("*.uninstall-*")) == []
            assert unrelated.read_text(encoding="utf-8") == "keep"
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=10)
            for worker in cleanup_workers:
                if worker.poll() is None:
                    worker.terminate()
                worker.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
@pytest.mark.parametrize("residual_name", ["opensre.exe", "opensre.cmd"])
def test_managed_uninstall_worker_preserves_data_for_residual_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    residual_name: str,
) -> None:
    install_dir = tmp_path / f"managed residual {residual_name}"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "old-build"
    version_dir.mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ-managed")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("old-build\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")

    launcher = install_dir / "opensre.cmd"
    launcher_to_remove: Path | None = None
    if residual_name == "opensre.exe":
        launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
        launcher_to_remove = launcher
        residual = install_dir / residual_name
        residual.write_bytes(b"MZ-user-owned")
    else:
        residual = launcher
        residual.write_text("@echo off\necho user-owned\n", encoding="utf-8")

    residual_before = residual.read_bytes()
    data_dir = tmp_path / f"managed residual data {residual_name}"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("preserve while an entrypoint remains", encoding="utf-8")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, error = schedule_windows_managed_cleanup(
            executable=executable,
            app_root=app_root,
            launcher=launcher_to_remove,
            parent_pid=holder.pid,
            data_paths=[data_dir],
        )
        assert ok is True
        assert error is None
        assert len(created_cleanup_scripts) == 1

        holder.terminate()
        holder.wait(timeout=10)
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert not app_root.exists()
        assert not install_lock.exists()
        assert list(install_dir.glob("*.uninstall-*")) == []
        assert residual.read_bytes() == residual_before
        assert data_file.read_text(encoding="utf-8") == ("preserve while an entrypoint remains")
        assert unrelated.read_text(encoding="utf-8") == "keep"
        if launcher_to_remove is not None:
            assert not launcher_to_remove.exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        for cleanup_script in created_cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_managed_uninstall_worker_preserves_a_reinstalled_bundle(tmp_path: Path) -> None:
    install_dir = tmp_path / "reinstall race"
    app_root = install_dir / ".opensre-app"
    old_version = app_root / "versions" / "old-build"
    new_version = app_root / "versions" / "new-build"
    old_version.mkdir(parents=True)
    new_version.mkdir(parents=True)
    executable = old_version / "opensre.exe"
    executable.write_bytes(b"MZ")
    (new_version / "opensre.exe").write_bytes(b"MZ-new")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    pointer = app_root / "current.txt"
    pointer.write_text("old-build\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    data_dir = tmp_path / "reinstalled user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep for new install", encoding="utf-8")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, err = schedule_windows_managed_cleanup(
            executable=executable,
            app_root=app_root,
            launcher=launcher,
            parent_pid=holder.pid,
            data_paths=[data_dir],
        )
        assert ok is True
        assert err is None

        pointer.write_text("new-build\n", encoding="utf-8")
        holder.terminate()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 30
        while old_version.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not old_version.exists()
        assert new_version.is_dir()
        assert app_root.is_dir()
        assert launcher.is_file()
        assert install_lock.is_file()
        assert pointer.read_text(encoding="utf-8").strip() == "new-build"
        assert data_file.read_text(encoding="utf-8") == "keep for new install"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
@pytest.mark.parametrize(
    "invalid_pointer",
    ("..\n", "old-build\nother-build\n"),
    ids=("dotdot", "multiple-lines"),
)
def test_managed_uninstall_worker_rejects_malformed_pointer_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_pointer: str,
) -> None:
    install_dir = tmp_path / "dotdot pointer worker race"
    app_root = install_dir / ".opensre-app"
    old_version = app_root / "versions" / "old-build"
    old_version.mkdir(parents=True)
    executable = old_version / "opensre.exe"
    executable.write_bytes(b"MZ-old")
    alias_executable = app_root / "opensre.exe"
    alias_executable.write_bytes(b"MZ-invalid-pointer-target")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    pointer = app_root / "current.txt"
    pointer.write_text("old-build\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    data_dir = tmp_path / "dotdot pointer user data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")
    cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, err = schedule_windows_managed_cleanup(
            executable=executable,
            app_root=app_root,
            launcher=launcher,
            parent_pid=holder.pid,
            data_paths=[data_dir],
        )
        assert ok is True
        assert err is None
        assert len(cleanup_scripts) == 1

        pointer.write_text(invalid_pointer, encoding="utf-8")
        holder.terminate()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 30
        while cleanup_scripts[0].exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_scripts[0].exists()
        assert old_version.is_dir()
        assert executable.read_bytes() == b"MZ-old"
        assert alias_executable.read_bytes() == b"MZ-invalid-pointer-target"
        assert pointer.read_text(encoding="utf-8") == invalid_pointer
        assert launcher.is_file()
        assert install_lock.is_file()
        assert data_file.read_text(encoding="utf-8") == "keep"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        for cleanup_script in cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_legacy_uninstall_worker_preserves_data_when_onedir_install_wins_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "legacy reinstall race"
    install_dir.mkdir()
    executable = install_dir / "opensre.exe"
    executable.write_bytes(b"MZ-old")
    install_lock = install_dir / ".opensre-app.install.lock"
    app_root = install_dir / ".opensre-app"
    launcher = install_dir / "opensre.cmd"
    data_dir = tmp_path / "legacy reinstall data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep for new install", encoding="utf-8")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, error = schedule_windows_cleanup(
            [executable],
            parent_pid=holder.pid,
            data_paths=[data_dir],
            install_lock_path=install_lock,
            data_guard_paths=[app_root, launcher, executable],
        )
        assert ok is True
        assert error is None
        assert len(created_cleanup_scripts) == 1

        new_executable = app_root / "versions" / "new-build" / "opensre.exe"
        new_executable.parent.mkdir(parents=True)
        new_executable.write_bytes(b"MZ-new")
        (app_root / "layout-v1.marker").write_text(
            "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
        )
        (app_root / "current.txt").write_text("new-build\n", encoding="utf-8")
        launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")

        holder.terminate()
        holder.wait(timeout=10)
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert not executable.exists()
        assert new_executable.read_bytes() == b"MZ-new"
        assert launcher.is_file()
        assert install_lock.is_file()
        assert data_file.read_text(encoding="utf-8") == "keep for new install"
        assert unrelated.read_text(encoding="utf-8") == "keep"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_legacy_uninstall_worker_preserves_data_when_flat_reinstall_wins_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "flat legacy reinstall race"
    install_dir.mkdir()
    executable = install_dir / "opensre.exe"
    executable.write_bytes(b"MZ-old")
    install_lock = install_dir / ".opensre-app.install.lock"
    data_dir = tmp_path / "flat reinstall data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep for replacement", encoding="utf-8")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    ready = tmp_path / "flat-data-guard-ready"
    release = tmp_path / "flat-data-guard-release"
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script",
        lambda: _inject_empty_process_enumerator(
            _inject_before_data_guard_barrier(
                read_cleanup_script(),
                ready=ready,
                release=release,
            )
        ),
    )
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
        _missing_process_identity,
    )

    try:
        ok, error = schedule_windows_cleanup(
            [executable],
            parent_pid=2_147_483_647,
            data_paths=[data_dir],
            install_lock_path=install_lock,
            data_guard_paths=[
                install_dir / ".opensre-app",
                install_dir / "opensre.cmd",
                executable,
            ],
        )
        assert ok is True, error
        assert len(created_cleanup_scripts) == 1

        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.is_file()
        assert not executable.exists()

        executable.write_bytes(b"MZ-new-flat")
        release.write_text("continue", encoding="utf-8")
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert executable.read_bytes() == b"MZ-new-flat"
        assert install_lock.is_file()
        assert data_file.read_text(encoding="utf-8") == "keep for replacement"
        assert unrelated.read_text(encoding="utf-8") == "keep"
    finally:
        release.write_text("continue", encoding="utf-8")
        for cleanup_script in created_cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_legacy_uninstall_worker_preserves_same_content_flat_reinstall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "same content flat reinstall race"
    install_dir.mkdir()
    executable = install_dir / "opensre.exe"
    executable.write_bytes(b"MZ-same-content")
    original_metadata = executable.stat(follow_symlinks=False)
    original_identity = (original_metadata.st_ino, original_metadata.st_ctime_ns)
    install_lock = install_dir / ".opensre-app.install.lock"
    data_dir = tmp_path / "same content reinstall data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep for replacement", encoding="utf-8")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        ok, error = schedule_windows_cleanup(
            [executable],
            parent_pid=holder.pid,
            data_paths=[data_dir],
            install_lock_path=install_lock,
            data_guard_paths=[executable],
        )
        assert ok is True, error

        executable.unlink()
        executable.write_bytes(b"MZ-same-content")
        replacement_metadata = executable.stat(follow_symlinks=False)
        replacement_identity = (
            replacement_metadata.st_ino,
            replacement_metadata.st_ctime_ns,
        )
        assert replacement_identity != original_identity

        holder.terminate()
        holder.wait(timeout=10)
        cleanup_script = created_cleanup_scripts[0]
        deadline = time.monotonic() + 30
        while cleanup_script.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert not cleanup_script.exists()
        assert executable.read_bytes() == b"MZ-same-content"
        assert install_lock.is_file()
        assert data_file.read_text(encoding="utf-8") == "keep for replacement"
        assert unrelated.read_text(encoding="utf-8") == "keep"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)
        for cleanup_script in created_cleanup_scripts:
            cleanup_script.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_managed_uninstall_holds_install_lock_through_data_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "uninstall lock transaction"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    data_dir = tmp_path / "locked transaction data"
    data_dir.mkdir()
    (data_dir / "state.json").write_text("remove", encoding="utf-8")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    ready = tmp_path / "data-decision-ready"
    release = tmp_path / "data-decision-release"
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script",
        lambda: _inject_empty_process_enumerator(
            _inject_data_decision_barrier(
                read_cleanup_script(),
                ready=ready,
                release=release,
            )
        ),
    )
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
        _missing_process_identity,
    )

    ok, error = schedule_windows_managed_cleanup(
        executable=executable,
        app_root=app_root,
        launcher=launcher,
        parent_pid=2_147_483_647,
        data_paths=[data_dir],
    )
    assert ok is True
    assert error is None
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert ready.is_file()
    try:
        with pytest.raises(OSError), install_lock.open("r+b"):
            pass
    finally:
        release.write_text("continue", encoding="utf-8")

    cleanup_script = created_cleanup_scripts[0]
    deadline = time.monotonic() + 30
    while cleanup_script.exists() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert not cleanup_script.exists()
    assert not data_dir.exists()
    assert not app_root.exists()
    assert not launcher.exists()
    assert not install_lock.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
@pytest.mark.parametrize("scan_failure", ["transient", "persistent"])
def test_managed_uninstall_worker_retains_tree_when_process_scan_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scan_failure: str,
) -> None:
    install_dir = tmp_path / "uninstall process scan failure"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    payload = version_dir / "_internal" / "lazy" / "payload.dat"
    payload.parent.mkdir(parents=True)
    payload.write_text("must remain complete", encoding="utf-8")
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    launcher_before = launcher.read_bytes()
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    data_dir = tmp_path / "process scan failure data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")
    before = {
        path.relative_to(app_root): path.read_bytes()
        for path in app_root.rglob("*")
        if path.is_file()
    }

    def _worker_source() -> str:
        source = read_cleanup_script()
        anchor = "$ErrorActionPreference = 'Stop'\n"
        assert source.count(anchor) == 1
        scanner = r"""
$script:scanCount = 0
function Get-Process {
    [CmdletBinding()]
    param([int]$Id, [string]$Name)
    if ($PSBoundParameters.ContainsKey('Id')) { return $null }
    $script:scanCount++
    [Console]::WriteLine("SCAN=$script:scanCount")
    if ($script:scanCount -eq 1 -or '__FAILURE__' -eq 'persistent') {
        $vanished = [pscustomobject]@{ ProcessName = 'opensre' }
        $vanished | Add-Member -MemberType ScriptProperty -Name Path -Value {
            [Console]::WriteLine('PROCESS_DISAPPEARED_DURING_INSPECTION')
            throw 'process exited after enumeration'
        }
        return $vanished
    }
    return @()
}
function Start-Sleep {
    param([int]$Milliseconds)
    [Console]::WriteLine('RETRY')
    if ($script:scanCount -ge 2) {
        # Exhaust the shared deadline deterministically, without wall-clock sleeps.
        $script:lockDeadline = [System.DateTime]::MinValue
    }
}
"""
        return source.replace(anchor, anchor + scanner.replace("__FAILURE__", scan_failure), 1)

    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script", _worker_source
    )
    real_popen = subprocess.Popen
    worker_results: list[tuple[int, str]] = []

    def _run_worker(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
        with real_popen(args, **kwargs) as worker:
            output, _ = worker.communicate(timeout=30)
            diagnostics = output.decode("utf-8", errors="replace")
            worker_results.append((worker.returncode, diagnostics))
            (tmp_path / "cleanup-worker.log").write_text(diagnostics, encoding="utf-8")
            (tmp_path / "cleanup-worker-exit-code.txt").write_text(
                str(worker.returncode), encoding="ascii"
            )
        return worker

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.subprocess.Popen", _run_worker)
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
        _missing_process_identity,
    )

    ok, error = schedule_windows_managed_cleanup(
        executable=executable,
        app_root=app_root,
        launcher=launcher,
        parent_pid=2_147_483_647,
        data_paths=[data_dir],
    )
    assert ok is True
    assert error is None
    assert len(created_cleanup_scripts) == 1
    cleanup_script = created_cleanup_scripts[0]
    assert not cleanup_script.exists()
    assert len(worker_results) == 1
    exit_code, diagnostics = worker_results[0]
    assert "PROCESS_DISAPPEARED_DURING_INSPECTION" in diagnostics
    assert exit_code == (0 if scan_failure == "transient" else 1), diagnostics
    assert "SCAN=2" in diagnostics
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert list(install_dir.glob("*.uninstall-*")) == []
    if scan_failure == "transient":
        assert not app_root.exists()
        assert not launcher.exists()
        assert not install_lock.exists()
        assert not data_dir.exists()
        return
    assert launcher.read_bytes() == launcher_before
    assert install_lock.is_file()
    assert app_root.is_dir()
    assert {
        path.relative_to(app_root): path.read_bytes()
        for path in app_root.rglob("*")
        if path.is_file()
    } == before
    assert list(install_dir.glob("*.uninstall-*")) == []
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert data_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows deferred cleanup only")
def test_managed_uninstall_worker_failure_preserves_data_and_unrelated_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "uninstall removal failure"
    app_root = install_dir / ".opensre-app"
    version_dir = app_root / "versions" / "build-1"
    (version_dir / "_internal").mkdir(parents=True)
    executable = version_dir / "opensre.exe"
    executable.write_bytes(b"MZ")
    (app_root / "layout-v1.marker").write_text(
        "OpenSRE Windows bundle layout v1\n", encoding="utf-8"
    )
    (app_root / "current.txt").write_text("build-1\n", encoding="utf-8")
    launcher = install_dir / "opensre.cmd"
    launcher.write_text("@echo off\n:: OpenSRE Windows launcher v1\n", encoding="utf-8")
    install_lock = install_dir / ".opensre-app.install.lock"
    install_lock.write_bytes(b"")
    unrelated = install_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    data_dir = tmp_path / "removal failure data"
    data_dir.mkdir()
    data_file = data_dir / "state.json"
    data_file.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script",
        lambda: _inject_failed_retired_target_removal(read_cleanup_script()),
    )
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)
    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.windows_process_identity",
        _missing_process_identity,
    )

    ok, error = schedule_windows_managed_cleanup(
        executable=executable,
        app_root=app_root,
        launcher=launcher,
        parent_pid=2_147_483_647,
        data_paths=[data_dir],
    )
    assert ok is True
    assert error is None
    assert len(created_cleanup_scripts) == 1
    cleanup_script = created_cleanup_scripts[0]
    deadline = time.monotonic() + 30
    while cleanup_script.exists() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert not cleanup_script.exists()
    assert data_file.read_text(encoding="utf-8") == "keep"
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert list(install_dir.glob("*.uninstall-*"))


def test_run_uninstall_dir_removal_error_sets_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    d = tmp_path / "locked_dir"
    d.mkdir()
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._data_dirs", lambda: [d])
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._is_binary_install", lambda: False)
    monkeypatch.setattr("surfaces.cli.lifecycle.uninstall._pip_uninstall", lambda: 0)

    def _fail(path: str) -> None:
        raise OSError("Permission denied")

    monkeypatch.setattr("shutil.rmtree", _fail)

    rc = run_uninstall(yes=True)

    assert rc == 1
    assert "errors" in capsys.readouterr().err


def test_uninstall_command_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["uninstall", "--help"])
    assert result.exit_code == 0
    assert "uninstall" in result.output.lower()


def test_uninstall_command_yes_flag_skips_prompt() -> None:
    runner = CliRunner()

    with (
        patch("surfaces.cli.lifecycle.uninstall._data_dirs", return_value=[]),
        patch("surfaces.cli.lifecycle.uninstall._is_binary_install", return_value=False),
        patch("surfaces.cli.lifecycle.uninstall._pip_uninstall", return_value=0),
    ):
        result = runner.invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0
    assert "opensre has been uninstalled" in result.output


def test_uninstall_command_short_yes_flag() -> None:
    runner = CliRunner()

    with (
        patch("surfaces.cli.lifecycle.uninstall._data_dirs", return_value=[]),
        patch("surfaces.cli.lifecycle.uninstall._is_binary_install", return_value=False),
        patch("surfaces.cli.lifecycle.uninstall._pip_uninstall", return_value=0),
    ):
        result = runner.invoke(cli, ["uninstall", "-y"])

    assert result.exit_code == 0


def test_data_dirs_includes_config_opensre_path() -> None:
    from surfaces.cli.lifecycle.uninstall import _data_dirs

    paths = _data_dirs()
    path_strs = [str(p) for p in paths]
    assert any(".opensre" in s for s in path_strs), "main ~/.opensre path missing"
    assert any(".config" in s and "opensre" in s for s in path_strs), (
        "~/.config/opensre cleanup path missing"
    )


def test_uninstall_help_describes_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["uninstall", "--help"])
    assert result.exit_code == 0
    assert "Remove opensre and all local data from this machine." in result.output
