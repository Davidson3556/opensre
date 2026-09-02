"""Detached PowerShell cleanup of a Windows installation after the CLI process exits."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

import surfaces.cli.lifecycle.windows.powershell as powershell
from config.constants.installer import (
    WINDOWS_BINARY_FILENAME,
    WINDOWS_INSTALL_LOCK_FILENAME,
    WINDOWS_LAUNCHER_FILENAME,
)

CLEANUP_SCRIPT_PATH = Path(__file__).with_name("uninstall_cleanup.ps1")


def read_cleanup_script() -> str:
    """Return the packaged cleanup worker source, which is copied to a private temp file."""
    return CLEANUP_SCRIPT_PATH.read_text(encoding="utf-8")


def schedule_windows_cleanup(
    paths: list[Path],
    *,
    parent_pid: int,
    data_paths: list[Path] | None = None,
    install_lock_path: Path | None = None,
    data_guard_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    return _schedule_payload(
        paths,
        parent_pid=parent_pid,
        managed=None,
        data_paths=data_paths,
        install_lock_path=install_lock_path,
        data_guard_paths=data_guard_paths,
    )


def _schedule_payload(
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
        cleanup_file.write(read_cleanup_script())

    try:
        subprocess.Popen(
            [
                powershell.windows_powershell_executable(),
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
            env=powershell.windows_powershell_environment(),
        )
    except OSError as exc:
        cleanup_path.unlink(missing_ok=True)
        return False, str(exc)
    return True, None


def schedule_windows_managed_cleanup(
    *,
    executable: Path,
    app_root: Path,
    launcher: Path | None,
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
    return _schedule_payload(
        [],
        parent_pid=parent_pid,
        managed=managed,
        data_paths=data_paths,
        install_lock_path=install_dir / WINDOWS_INSTALL_LOCK_FILENAME,
        data_guard_paths=[
            install_dir / WINDOWS_BINARY_FILENAME,
            install_dir / WINDOWS_LAUNCHER_FILENAME,
        ],
    )
