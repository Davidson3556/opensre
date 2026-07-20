"""Tests for the interactive-shell 'configure telegram' action tool.

The tool is an adapter: it turns the agent's arguments into a call to the shared
:func:`integrations.setup_flow.apply_setup` and renders the outcome. These tests
cover that adapter contract — argument pass-through, the failure and success
renderings, the delivery advisory, and that the token is never echoed. Merging,
verification, and redaction belong to the shared flow and are tested in
``tests/integrations/test_setup_flow.py``.

The action LLM's decision to call this tool with a pasted token is a live
concern exercised via ReplDriver, not here.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from rich.console import Console

import tools.interactive_shell.actions.configure_telegram as configure_telegram
from core.agent_harness.tools.tool_context import ActionToolContext
from integrations.setup_flow import SetupOutcome

_APPLY = "integrations.setup_flow.apply_setup"
_TOKEN = "123456789:AAExampleSecretTokenValue"


def _ctx() -> tuple[ActionToolContext, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)
    return ActionToolContext(session=MagicMock(), console=console), buf


def test_missing_token_is_not_handled() -> None:
    ctx, _ = _ctx()
    with patch(_APPLY) as apply_setup:
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": "  "}, ctx)
    assert result["ok"] is False
    assert result["error"] == "missing_bot_token"
    apply_setup.assert_not_called()


def test_supplied_values_are_forwarded_to_the_shared_flow() -> None:
    ctx, _ = _ctx()
    with patch(
        _APPLY, return_value=SetupOutcome(ok=True, detail="Connected.", saved=True)
    ) as apply_setup:
        configure_telegram.execute_configure_telegram_tool(
            {"bot_token": _TOKEN, "chat_id": "-1001234567890"}, ctx
        )
    apply_setup.assert_called_once_with(
        "telegram", {"bot_token": _TOKEN, "default_chat_id": "-1001234567890"}
    )


def test_failed_setup_reports_the_reason_without_echoing_the_token() -> None:
    ctx, buf = _ctx()
    with patch(_APPLY, return_value=SetupOutcome(ok=False, detail="Unauthorized.")):
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": _TOKEN}, ctx)
    assert result["ok"] is False
    assert result["error"] == "Unauthorized."
    out = buf.getvalue()
    assert "failed" in out.lower()
    assert _TOKEN not in out


def test_successful_setup_reports_the_bot_without_echoing_the_token() -> None:
    ctx, buf = _ctx()
    outcome = SetupOutcome(ok=True, detail="Connected to Telegram bot @acme_bot.", saved=True)
    with patch(_APPLY, return_value=outcome):
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": _TOKEN}, ctx)
    assert result["ok"] is True
    out = buf.getvalue()
    assert "@acme_bot" in out
    assert _TOKEN not in out


def test_delivery_advisory_is_surfaced_on_success() -> None:
    """A token-only setup verifies, but cannot deliver until a chat id is set."""
    ctx, buf = _ctx()
    outcome = SetupOutcome(
        ok=True, detail="Connected.", saved=True, warning="No default chat ID set."
    )
    with patch(_APPLY, return_value=outcome):
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": _TOKEN}, ctx)
    assert result["warning"] == "No default chat ID set."
    assert "No default chat ID set." in buf.getvalue()
