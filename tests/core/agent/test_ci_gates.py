"""Behavior of the live-turn CI gates: fail in trusted CI, skip on fork PRs/local."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.core.agent._ci_gates import is_untrusted_fork_pr, skip_or_fail


def _write_pr_event(tmp_path: Path, head_full_name: str) -> str:
    event = {"pull_request": {"head": {"repo": {"full_name": head_full_name}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return str(event_path)


def test_fork_pr_detected_when_head_repo_differs_from_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Tracer-Cloud/opensre")
    monkeypatch.setenv("GITHUB_EVENT_PATH", _write_pr_event(tmp_path, "forker/opensre"))
    assert is_untrusted_fork_pr() is True


def test_same_repo_pr_is_trusted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Tracer-Cloud/opensre")
    monkeypatch.setenv("GITHUB_EVENT_PATH", _write_pr_event(tmp_path, "Tracer-Cloud/opensre"))
    assert is_untrusted_fork_pr() is False


def test_non_pull_request_event_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Tracer-Cloud/opensre")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert is_untrusted_fork_pr() is False


def test_missing_event_payload_is_treated_as_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Tracer-Cloud/opensre")
    monkeypatch.setenv("GITHUB_EVENT_PATH", "/nonexistent/event.json")
    assert is_untrusted_fork_pr() is False


def test_skip_or_fail_fails_in_trusted_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    with pytest.raises(pytest.fail.Exception):
        skip_or_fail("missing creds")


def test_skip_or_fail_skips_on_fork_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Tracer-Cloud/opensre")
    monkeypatch.setenv("GITHUB_EVENT_PATH", _write_pr_event(tmp_path, "forker/opensre"))
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail("missing creds")


def test_skip_or_fail_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail("missing creds")
