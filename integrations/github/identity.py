"""Lightweight GitHub identity helpers for UI, analytics, and public sources.

Kept separate from :mod:`integrations.github.login` so callers can read saved
identity data or derive public repository scope without importing GitHub MCP.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_GITHUB_REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def workspace_public_repository_source(
    runtime_metadata: Mapping[str, Any],
) -> dict[str, dict[str, str | bool]]:
    """Expose a valid workspace GitHub repository for public read-only tools."""
    workspace_repo = str(runtime_metadata.get("workspace_repo") or "").strip()
    owner, separator, repo = workspace_repo.partition("/")
    if not separator or not _GITHUB_REPOSITORY_PART_RE.fullmatch(owner):
        return {}
    if not _GITHUB_REPOSITORY_PART_RE.fullmatch(repo):
        return {}
    return {
        "github": {
            "connection_verified": False,
            "public_repository": True,
            "owner": owner,
            "repo": repo,
        }
    }


def saved_github_username() -> str:
    """Return the persisted GitHub login from the integration store, or "".

    Best-effort and never raises: callers like the welcome banner and analytics
    re-identify must work even when the store is unreadable.
    """
    try:
        from integrations.store import get_integration

        record = get_integration("github")
        if not record:
            return ""
        credentials = record.get("credentials") or {}
        return str(credentials.get("username") or "").strip()
    except Exception:
        return ""
