"""Tests for the interactive-shell 'configure telegram' action tool.

Covers: token validation before persistence, that a valid token is saved (and
the secret is never echoed, even on the failure path), and optional chat id. The
action LLM's decision to call this tool with a pasted token is a live concern
exercised via ReplDriver, not here.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from rich.console import Console

import tools.interactive_shell.actions.configure_telegram as configure_telegram
from core.agent_harness.tools.tool_context import ActionToolContext

_VERIFY = "integrations.telegram.verifier.verify_telegram"
_UPSERT = "integrations.store.upsert_integration"
_TOKEN = "123456789:AAExampleSecretTokenValue"


def _ctx() -> tuple[ActionToolContext, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)
    return ActionToolContext(session=MagicMock(), console=console), buf


def test_missing_token_is_not_handled() -> None:
    ctx, _ = _ctx()
    with patch(_UPSERT) as upsert:
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": "  "}, ctx)
    assert result["ok"] is False
    assert result["error"] == "missing_bot_token"
    upsert.assert_not_called()


def test_invalid_token_reports_error_and_does_not_save() -> None:
    ctx, buf = _ctx()
    with (
        patch(
            _VERIFY,
            # The verifier's failure detail can embed the token (via the request URL);
            # the tool must redact it before showing the user.
            return_value={
                "status": "failed",
                "detail": f"Telegram API check failed: 401 for https://api.telegram.org/bot{_TOKEN}/getMe",
            },
        ),
        patch(_UPSERT) as upsert,
    ):
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": _TOKEN}, ctx)
    assert result["ok"] is False
    upsert.assert_not_called()
    assert "failed" in buf.getvalue().lower()
    # The token must never be surfaced, even on the failure path.
    assert _TOKEN not in buf.getvalue()
    assert _TOKEN not in result["error"]


def test_valid_token_verifies_then_saves_without_echoing_secret() -> None:
    ctx, buf = _ctx()
    with (
        patch(
            _VERIFY,
            return_value={"status": "passed", "detail": "Connected to Telegram bot @acme_bot."},
        ),
        patch(_UPSERT) as upsert,
    ):
        result = configure_telegram.execute_configure_telegram_tool({"bot_token": _TOKEN}, ctx)

    assert result["ok"] is True
    upsert.assert_called_once_with("telegram", {"credentials": {"bot_token": _TOKEN}})
    out = buf.getvalue()
    assert "@acme_bot" in out
    # The secret must never be printed back to the user.
    assert _TOKEN not in out


def test_optional_chat_id_is_persisted() -> None:
    ctx, _ = _ctx()
    with (
        patch(
            _VERIFY,
            return_value={"status": "passed", "detail": "Connected to Telegram bot @b."},
        ),
        patch(_UPSERT) as upsert,
    ):
        configure_telegram.execute_configure_telegram_tool(
            {"bot_token": _TOKEN, "chat_id": "-1001234567890"}, ctx
        )
    upsert.assert_called_once_with(
        "telegram",
        {"credentials": {"bot_token": _TOKEN, "default_chat_id": "-1001234567890"}},
    )
