"""Schedule serialized, identity-bound PowerShell cleanup after the CLI exits."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

import surfaces.cli.lifecycle.windows.powershell as powershell
from config.constants.installer import (
    WINDOWS_BINARY_FILENAME,
    WINDOWS_INSTALL_LOCK_FILENAME,
    WINDOWS_LAUNCHER_FILENAME,
    WINDOWS_LAYOUT_MARKER_TEXT,
)
from surfaces.cli.lifecycle.windows.paths import (
    UnsafeWindowsPathError,
    canonical_existing_path,
    ensure_not_reparse,
    ensure_tree_has_no_reparse_points,
    windows_path_exists,
    windows_path_is_within,
)
from surfaces.cli.lifecycle.windows.processes import (
    WindowsProcessIdentity,
    windows_process_identity,
)

CLEANUP_SCRIPT_PATH = Path(__file__).with_name("uninstall_cleanup.ps1")
_WINDOWS_EPOCH_FILETIME = 116_444_736_000_000_000
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_WINDOWS_OPEN_EXISTING = 3


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


def read_cleanup_script() -> str:
    """Return the packaged cleanup worker source copied to a private temporary file."""
    return CLEANUP_SCRIPT_PATH.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _creation_filetime(path: Path) -> int:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_ctime_ns // 100 + _WINDOWS_EPOCH_FILETIME


def _fallback_file_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    return (
        int(metadata.st_dev) & 0xFFFFFFFF,
        int(metadata.st_ino) & 0xFFFFFFFFFFFFFFFF,
        metadata.st_ctime_ns // 100 + _WINDOWS_EPOCH_FILETIME,
    )


def _windows_file_identity(path: Path) -> tuple[int, int, int]:
    if os.name != "nt":
        return _fallback_file_identity(path)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0,
        _WINDOWS_FILE_SHARE_ALL,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error_code = int(ctypes.get_last_error())  # type: ignore[attr-defined]
        raise OSError(error_code, f"could not inspect Windows file identity: {path}")
    try:
        information = _WindowsByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            error_code = int(ctypes.get_last_error())  # type: ignore[attr-defined]
            raise OSError(error_code, f"could not inspect Windows file identity: {path}")
        if information.file_attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafeWindowsPathError(f"Windows lifecycle path is a reparse point: {path}")
        creation_filetime = (int(information.creation_time.dwHighDateTime) << 32) | int(
            information.creation_time.dwLowDateTime
        )
        file_index = (int(information.file_index_high) << 32) | int(information.file_index_low)
        return int(information.volume_serial_number), file_index, creation_filetime
    finally:
        close_handle(handle)


def _cleanup_target(path: Path) -> dict[str, object]:
    absolute = Path(os.path.abspath(path))
    if not windows_path_exists(absolute):
        return {"path": str(absolute), "kind": "missing", "sha256": ""}
    ensure_not_reparse(absolute)
    canonical = canonical_existing_path(absolute)
    if canonical.is_file():
        volume_serial, file_index, creation_filetime = _windows_file_identity(canonical)
        return {
            "path": str(canonical),
            "kind": "file",
            "sha256": _sha256(canonical),
            "volume_serial_number": volume_serial,
            "file_index": file_index,
            "creation_filetime_utc": creation_filetime,
        }
    if canonical.is_dir():
        ensure_tree_has_no_reparse_points(canonical)
        return {"path": str(canonical), "kind": "directory", "sha256": ""}
    raise UnsafeWindowsPathError(f"cleanup target has an unsupported file type: {path}")


def _parent_payload(identity: WindowsProcessIdentity) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "path": str(identity.executable),
        "started_filetime_utc": identity.started_filetime_utc,
    }


def schedule_windows_cleanup(
    paths: list[Path],
    *,
    parent_pid: int,
    data_paths: list[Path] | None = None,
    install_lock_path: Path | None = None,
    data_guard_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    """Schedule exact legacy targets, preserving replacements created before cleanup."""
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
    managed: dict[str, object] | None,
    data_paths: list[Path] | None = None,
    install_lock_path: Path | None = None,
    data_guard_paths: list[Path] | None = None,
    parent_identity: WindowsProcessIdentity | None = None,
) -> tuple[bool, str | None]:
    if parent_identity is None:
        parent_identity, identity_error = windows_process_identity(
            parent_pid, expected_executable=Path(sys.executable)
        )
        if identity_error is not None or parent_identity is None:
            return False, identity_error or "could not capture cleanup parent identity"
    if parent_identity.pid != parent_pid:
        return False, "cleanup parent identity did not match the requested PID"

    try:
        target_records = [_cleanup_target(path) for path in paths]
    except (OSError, UnsafeWindowsPathError) as exc:
        return False, str(exc)

    payload_json = json.dumps(
        {
            "operation_id": uuid.uuid4().hex,
            "parent": _parent_payload(parent_identity),
            "targets": target_records,
            "managed": managed,
            "data_targets": [str(Path(os.path.abspath(path))) for path in data_paths or []],
            "lock_path": (
                str(Path(os.path.abspath(install_lock_path)))
                if install_lock_path is not None
                else ""
            ),
            "data_guard_paths": [
                str(Path(os.path.abspath(path))) for path in data_guard_paths or []
            ],
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


def _managed_payload(
    *, executable: Path, app_root: Path, launcher: Path | None
) -> dict[str, object]:
    canonical_app_root = canonical_existing_path(app_root)
    canonical_executable = canonical_existing_path(executable)
    ensure_tree_has_no_reparse_points(canonical_app_root)
    if not windows_path_is_within(canonical_app_root, canonical_executable):
        raise UnsafeWindowsPathError("managed executable escapes its application root")
    return {
        "active_version": str(canonical_executable.parent),
        "active_executable_sha256": _sha256(canonical_executable),
        "app_created_filetime_utc": _creation_filetime(canonical_app_root),
        "app_root": str(canonical_app_root),
        "expected_install_id": canonical_executable.parent.name,
        "launcher": str(Path(os.path.abspath(launcher))) if launcher is not None else "",
        "layout_marker_text": WINDOWS_LAYOUT_MARKER_TEXT,
        "lock_path": str(canonical_app_root.parent / WINDOWS_INSTALL_LOCK_FILENAME),
    }


def schedule_windows_managed_cleanup(
    *,
    executable: Path,
    app_root: Path,
    launcher: Path | None,
    parent_pid: int,
    data_paths: list[Path] | None = None,
) -> tuple[bool, str | None]:
    """Schedule removal of a proven managed layout after revalidating it under lock."""
    try:
        managed = _managed_payload(executable=executable, app_root=app_root, launcher=launcher)
    except (OSError, UnsafeWindowsPathError) as exc:
        return False, str(exc)
    install_dir = canonical_existing_path(app_root).parent
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
