"""Lightweight GitHub identity helpers for UI and analytics.

Kept separate from :mod:`integrations.github.login` so callers like the welcome
banner can read the saved handle without importing the heavy GitHub MCP stack.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def workspace_public_repository_source(
    runtime_metadata: Mapping[str, Any],
) -> dict[str, dict[str, str | bool]]:
    """Expose a valid workspace GitHub repository for public read-only tools."""
    workspace_repo = str(runtime_metadata.get("workspace_repo") or "").strip()
    owner, separator, repo = workspace_repo.partition("/")
    if not separator or not owner or not repo or "/" in repo:
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
