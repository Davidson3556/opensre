from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from surfaces.cli.app import cli
from surfaces.cli.lifecycle.uninstall import _remove_path, run_uninstall
from surfaces.cli.lifecycle.windows import (
    read_cleanup_script,
    schedule_windows_cleanup,
    schedule_windows_managed_cleanup,
    windows_binary_install_paths,
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


def _inject_failed_process_enumerator(source: str, *, preference: str) -> str:
    anchor = f"$ErrorActionPreference = {preference}\n"
    assert source.count(anchor) == 1
    return source.replace(anchor, anchor + _FAILED_PROCESS_ENUMERATOR, 1)


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

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
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
    captured: dict[str, object] = {}

    def _popen(args: list[str], **kwargs: object) -> object:
        captured["args"] = args
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.powershell.windows_powershell_executable",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.subprocess.Popen", _popen)

    ok, err = schedule_windows_cleanup([target], parent_pid=934)

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
        assert json.loads(base64.b64decode(args[payload_index + 1])) == {
            "targets": [str(target)],
            "managed": None,
            "data_targets": [],
            "lock_path": "",
            "data_guard_paths": [],
        }
        cleanup_index = args.index("-CleanupScriptPath")
        assert Path(args[cleanup_index + 1]) == cleanup_path
    finally:
        cleanup_path.unlink(missing_ok=True)
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
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
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])

    try:
        ok, err = schedule_windows_cleanup([target], parent_pid=holder.pid)
        assert ok is True
        assert err is None
        assert target.exists()

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
def test_managed_uninstall_removes_long_quarantine_tree(tmp_path: Path) -> None:
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
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])

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

        holder.wait(timeout=10)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            quarantines = list(install_dir.glob("*.uninstall-*"))
            if (
                not app_root.exists()
                and not launcher.exists()
                and not install_lock.exists()
                and not data_dir.exists()
                and not quarantines
            ):
                break
            time.sleep(0.1)

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
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])

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
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])

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
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])

    try:
        ok, error = schedule_windows_cleanup(
            [executable],
            parent_pid=holder.pid,
            data_paths=[data_dir],
            install_lock_path=install_lock,
            data_guard_paths=[app_root, launcher],
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
        lambda: _inject_data_decision_barrier(
            read_cleanup_script(),
            ready=ready,
            release=release,
        ),
    )
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)

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
def test_managed_uninstall_worker_retains_tree_when_process_scan_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    monkeypatch.setattr(
        "surfaces.cli.lifecycle.windows.cleanup.read_cleanup_script",
        lambda: _inject_failed_process_enumerator(
            read_cleanup_script(),
            preference="'Stop'",
        ),
    )
    created_cleanup_scripts: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def _mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)
        created_cleanup_scripts.append(Path(name))
        return descriptor, name

    monkeypatch.setattr("surfaces.cli.lifecycle.windows.cleanup.tempfile.mkstemp", _mkstemp)

    ok, error = schedule_windows_managed_cleanup(
        executable=executable,
        app_root=app_root,
        launcher=launcher,
        parent_pid=0,
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
