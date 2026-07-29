"""Synthetic scenario for GitHub MCP config validation.

Validates that the single-session path runs list_tools, get_me, and the repo
probe on one shared connection, and that the result correctly surfaces
identity, repo count, and samples.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types as mcp_types

import integrations.github.mcp as github_mcp_module

pytestmark = pytest.mark.synthetic

_REPO_LIST_TOOLS: list[dict[str, Any]] = [
    {
        "name": n,
        "description": "",
        "input_schema": {"type": "object", "properties": {}},
    }
    for n in (
        "get_file_contents",
        "get_me",
        "get_repository_tree",
        "list_commits",
        "search_code",
        "list_repositories",
    )
]

_SEARCH_ONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": n,
        "description": "",
        "input_schema": {"type": "object", "properties": {}},
    }
    for n in (
        "get_file_contents",
        "get_me",
        "get_repository_tree",
        "list_commits",
        "search_code",
    )
] + [
    {
        "name": "search_repositories",
        "description": "",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def _make_session_patcher(
    tools: list[dict[str, Any]],
    call_fn: Any,
    session_open_count: list[int] | None = None,
) -> Any:
    """Return a fake _open_github_mcp_session context manager for validation tests."""

    @asynccontextmanager
    async def _fake_open(config: Any):  # type: ignore[return]
        if session_open_count is not None:
            session_open_count[0] += 1

        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        list_result = MagicMock()
        list_result.tools = [
            mcp_types.Tool(
                name=str(t["name"]),
                inputSchema=t.get("input_schema") or {},
            )
            for t in tools
        ]
        mock_session.list_tools = AsyncMock(return_value=list_result)

        async def _call_tool(name: str, args: dict) -> Any:
            payload: dict[str, Any] = call_fn(name, args or {})
            raw = MagicMock()
            raw.isError = payload.get("is_error", False)
            raw.content = [
                mcp_types.TextContent(type="text", text=payload.get("text", ""))
            ]
            raw.structuredContent = payload.get("structured_content")
            return raw

        mock_session.call_tool = _call_tool
        yield mock_session

    return _fake_open


def _hosted_config(token: str = "ghp_synthetic_token") -> Any:
    return github_mcp_module.build_github_mcp_config(
        {
            "url": "https://api.githubcopilot.com/mcp/",
            "mode": "streamable-http",
            "auth_token": token,
            "toolsets": ["repos"],
        }
    )


def test_single_session_list_repositories_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_tools, get_me, and list_repositories all run on a single MCP session."""

    session_open_count: list[int] = [0]

    def fake_call(name: str, args: dict) -> dict[str, Any]:
        if name == "get_me":
            return {
                "is_error": False,
                "structured_content": {"login": "synthetic-user"},
                "text": "",
            }
        if name == "list_repositories":
            return {
                "is_error": False,
                "structured_content": [
                    {"full_name": "synthetic-user/repo-alpha", "private": False, "fork": False},
                    {"full_name": "synthetic-user/repo-beta", "private": True, "fork": False},
                ],
                "text": "",
            }
        raise AssertionError(f"unexpected tool call: {name!r}")

    monkeypatch.setattr(
        "integrations.github.mcp._open_github_mcp_session",
        _make_session_patcher(_REPO_LIST_TOOLS, fake_call, session_open_count),
    )

    result = github_mcp_module.validate_github_mcp_config(_hosted_config())

    assert session_open_count[0] == 1, (
        f"Expected exactly 1 MCP session; got {session_open_count[0]}."
    )
    assert result.ok is True
    assert result.authenticated_user == "synthetic-user"
    assert result.repo_access_count == 2
    assert result.repo_access_probe_tool == "list_repositories"
    assert "synthetic-user/repo-alpha" in result.repo_access_samples
    assert result.repo_access_scope_owners == ("synthetic-user",)


def test_single_session_search_repositories_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only search_repositories is exposed the session-level probe still
    dispatches the correct user-scoped query and surfaces the result."""

    session_open_count: list[int] = [0]

    def fake_call(name: str, args: dict) -> dict[str, Any]:
        if name == "get_me":
            return {
                "is_error": False,
                "structured_content": {"login": "hosted-user"},
                "text": "",
            }
        if name == "search_repositories":
            assert args == {"query": "user:hosted-user"}
            return {
                "is_error": False,
                "structured_content": {
                    "items": [
                        {"full_name": "hosted-user/service-a", "private": False},
                        {"full_name": "hosted-user/service-b", "private": True},
                    ]
                },
                "text": "",
            }
        raise AssertionError(f"unexpected tool call: {name!r}")

    monkeypatch.setattr(
        "integrations.github.mcp._open_github_mcp_session",
        _make_session_patcher(_SEARCH_ONLY_TOOLS, fake_call, session_open_count),
    )

    result = github_mcp_module.validate_github_mcp_config(_hosted_config())

    assert session_open_count[0] == 1
    assert result.ok is True
    assert result.authenticated_user == "hosted-user"
    assert result.repo_access_probe_tool == "search_repositories"
    assert "hosted-user/service-a" in result.repo_access_samples


def test_session_open_failure_is_connectivity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure to open the MCP session surfaces as failure_category='connectivity'."""

    @asynccontextmanager
    async def _failing_open(config: Any):  # type: ignore[return]
        raise ConnectionRefusedError("synthetic: MCP server unreachable")
        yield  # make it a generator

    monkeypatch.setattr("integrations.github.mcp._open_github_mcp_session", _failing_open)

    result = github_mcp_module.validate_github_mcp_config(_hosted_config())

    assert result.ok is False
    assert result.failure_category == "connectivity"
    assert result.tool_names == ()
