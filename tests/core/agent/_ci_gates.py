"""CI vs local behavior for turn tests that may skip when prerequisites are missing."""

from __future__ import annotations

import json
import os

import pytest


def running_in_github_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"


def is_untrusted_fork_pr() -> bool:
    """True when the run is a pull request opened from a forked repository.

    GitHub withholds repository secrets (LLM keys, integration credentials) from
    ``pull_request`` runs whose head lives in a fork. Live gates that would
    otherwise fail on missing credentials must skip in that case — the absence is
    expected, not a misconfiguration. Same-repo PRs and branch/``main`` pushes are
    trusted and keep failing. ``pull_request_target`` runs in the base repo with
    secrets available, so it is treated as trusted too.
    """
    if os.getenv("GITHUB_EVENT_NAME", "").strip() != "pull_request":
        return False
    base_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    event_path = os.getenv("GITHUB_EVENT_PATH", "").strip()
    if not base_repo or not event_path:
        return False
    try:
        with open(event_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    head_repo = payload.get("pull_request", {}).get("head", {}).get("repo") or {}
    head_full_name = str(head_repo.get("full_name", "")).strip()
    return bool(head_full_name) and head_full_name != base_repo


def skip_or_fail(message: str) -> None:
    """Fail in trusted CI (required gate); skip locally and on fork PRs."""
    if running_in_github_actions() and not is_untrusted_fork_pr():
        pytest.fail(message)
    pytest.skip(message)
