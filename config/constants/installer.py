"""Shared constants for installer layouts and lifecycle handoffs."""

from __future__ import annotations

from typing import Final

OPENSRE_AUTO_LAUNCH_ENV: Final[str] = "OPENSRE_AUTO_LAUNCH"
OPENSRE_UPDATE_EXECUTABLE_ENV: Final[str] = "OPENSRE_UPDATE_EXECUTABLE"
OPENSRE_UPDATE_PARENT_PID_ENV: Final[str] = "OPENSRE_UPDATE_PARENT_PID"
WINDOWS_APP_DIR_NAME: Final[str] = ".opensre-app"
WINDOWS_BINARY_FILENAME: Final[str] = "opensre.exe"
WINDOWS_CURRENT_POINTER_FILENAME: Final[str] = "current.txt"
WINDOWS_INSTALL_LOCK_FILENAME: Final[str] = ".opensre-app.install.lock"
WINDOWS_LAUNCHER_FILENAME: Final[str] = "opensre.cmd"
WINDOWS_LAUNCHER_MARKER: Final[str] = ":: OpenSRE Windows launcher v1"
WINDOWS_LAYOUT_MARKER_FILENAME: Final[str] = "layout-v1.marker"
WINDOWS_LAYOUT_MARKER_TEXT: Final[str] = "OpenSRE Windows bundle layout v1"
WINDOWS_VERSIONS_DIR_NAME: Final[str] = "versions"

__all__ = [
    "OPENSRE_AUTO_LAUNCH_ENV",
    "OPENSRE_UPDATE_EXECUTABLE_ENV",
    "OPENSRE_UPDATE_PARENT_PID_ENV",
    "WINDOWS_APP_DIR_NAME",
    "WINDOWS_BINARY_FILENAME",
    "WINDOWS_CURRENT_POINTER_FILENAME",
    "WINDOWS_INSTALL_LOCK_FILENAME",
    "WINDOWS_LAUNCHER_FILENAME",
    "WINDOWS_LAUNCHER_MARKER",
    "WINDOWS_LAYOUT_MARKER_FILENAME",
    "WINDOWS_LAYOUT_MARKER_TEXT",
    "WINDOWS_VERSIONS_DIR_NAME",
]
