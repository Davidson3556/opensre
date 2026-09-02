"""Classification of an installed Windows OpenSRE layout from its executable path."""

from __future__ import annotations

import os
import sys
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


class MalformedWindowsInstallError(RuntimeError):
    """Raised when a version-shaped Windows install cannot prove ownership."""


@dataclass(frozen=True)
class WindowsBinaryInstall:
    """The managed paths a Windows uninstall may act on, or the bare executable."""

    executable: Path
    app_root: Path | None
    launcher: Path | None
    paths: tuple[Path, ...]


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
        raise MalformedWindowsInstallError(
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
        raise MalformedWindowsInstallError(
            f"managed Windows installation marker is missing or unreadable: {marker}"
        ) from exc
    if marker_text != WINDOWS_LAYOUT_MARKER_TEXT:
        raise MalformedWindowsInstallError(
            f"managed Windows installation marker is invalid: {marker}"
        )

    pointer = app_root / WINDOWS_CURRENT_POINTER_FILENAME
    try:
        install_id = pointer.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError as exc:
        raise MalformedWindowsInstallError(
            f"managed Windows current-version pointer is missing or unreadable: {pointer}"
        ) from exc
    if not install_id or Path(install_id).name != install_id or install_id in {".", ".."}:
        raise MalformedWindowsInstallError(
            f"managed Windows current-version pointer is invalid: {pointer}"
        )
    current_executable = app_root / WINDOWS_VERSIONS_DIR_NAME / install_id / WINDOWS_BINARY_FILENAME
    if not current_executable.is_file():
        raise MalformedWindowsInstallError(
            f"managed Windows current-version pointer is dangling: {pointer}"
        )
    if os.path.normcase(os.path.abspath(current_executable)) != os.path.normcase(
        os.path.abspath(exe_path)
    ):
        raise MalformedWindowsInstallError(
            "this OpenSRE process is not the version selected by the managed Windows "
            "current-version pointer; close it and run uninstall from a new PowerShell window"
        )
    return app_root


def classify_windows_binary_install(exe_path: Path | None = None) -> WindowsBinaryInstall:
    exe = Path(os.path.abspath(exe_path or Path(sys.executable)))
    app_root = _windows_app_root(exe)
    if app_root is None:
        if (exe.parent / "_internal").is_dir():
            raise MalformedWindowsInstallError(
                "this executable belongs to an unpacked Windows onedir bundle, not an "
                "install.ps1-managed installation; install OpenSRE with install.ps1 before "
                "running uninstall"
            )
        return WindowsBinaryInstall(
            executable=exe,
            app_root=None,
            launcher=None,
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
    return WindowsBinaryInstall(
        executable=exe,
        app_root=app_root,
        launcher=launcher,
        paths=tuple(deduped),
    )


def windows_binary_install_paths(exe_path: Path | None = None) -> list[Path]:
    return list(classify_windows_binary_install(exe_path).paths)
