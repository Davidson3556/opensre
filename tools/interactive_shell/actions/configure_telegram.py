"""Configure the Telegram integration from a bot token provided in chat.

The action agent selects this tool when a user pastes a Telegram bot token and
asks to set up / connect / enable Telegram, and extracts the token (and an
optional chat id) into the tool arguments — so the user does not have to
re-enter the token in the interactive `/integrations setup telegram` wizard.

Everything after collecting those values (merging over the stored record,
verifying, persisting, and the "configured but cannot deliver" advisory) is
delegated to :func:`integrations.setup_flow.apply_setup`, which the onboarding
wizard drives with prompt-collected values. This module is only the adapter
between the agent's arguments and that shared flow.

Selection is the action agent's job via normal tool-calling; there is no
keyword/regex detection of tokens here (see ``tools/interactive_shell/AGENTS.md``).
The tool stays UI-agnostic — it validates and persists, leaving any confirmation
UX to the surface — matching the other action tools (e.g. ``sentry_fix``).
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from core.agent_harness.tools.tool_context import (
    ActionToolContext,
    execute_with_action_context,
    object_schema,
    string_property,
)
from core.tool_framework.registered_tool import RegisteredTool


def execute_configure_telegram_tool(args: dict[str, Any], ctx: ActionToolContext) -> dict[str, Any]:
    bot_token = str(args.get("bot_token", "")).strip()
    chat_id = str(args.get("chat_id", "")).strip()
    if not bot_token:
        ctx.console.print("[yellow]No Telegram bot token provided.[/]")
        return {"ok": False, "error": "missing_bot_token"}

    # The shared setup flow merges over the stored record, verifies, and only
    # then persists — so a token-only paste keeps an existing default_chat_id, a
    # bad token cannot overwrite a working integration, and the secret is
    # redacted out of anything shown back.
    from integrations.setup_flow import apply_setup

    ctx.console.print("[dim]Validating Telegram bot token…[/]")
    outcome = apply_setup("telegram", {"bot_token": bot_token, "default_chat_id": chat_id})
    if not outcome.ok:
        ctx.console.print(f"[red]Telegram setup failed: {escape(outcome.detail)}[/]")
        return {"ok": False, "error": outcome.detail}

    ctx.console.print(f"[green]✓ {escape(outcome.detail)} Saved to the integration store.[/]")
    if outcome.warning:
        ctx.console.print(f"[yellow]{escape(outcome.warning)}[/]")
    ctx.session.record("configure_telegram", "telegram", ok=True)
    return {
        "ok": True,
        "detail": outcome.detail,
        "warning": outcome.warning,
        "chat_id_set": bool(chat_id),
    }


def run_configure_telegram(*, bot_token: str, chat_id: str = "", context: Any) -> dict[str, Any]:
    return execute_with_action_context(
        {"bot_token": bot_token, "chat_id": chat_id},
        context,
        execute_configure_telegram_tool,
    )


configure_telegram_tool = RegisteredTool(
    name="configure_telegram",
    description=(
        "Set up the Telegram integration from a bot token the user pasted in chat: "
        "validate the token against the Telegram API and, if valid, save it (plus an "
        "optional default chat id). Use when the user provides a Telegram bot token and "
        "asks to set up, connect, or enable Telegram. Do not use for sending messages, "
        "and do not invent a token — only call this with a token the user supplied."
    ),
    input_schema=object_schema(
        properties={
            "bot_token": string_property(
                description=(
                    "Telegram bot HTTP API token from BotFather, in the form "
                    "<numeric-id>:<secret>. Only pass a value the user actually provided."
                ),
                min_length=1,
            ),
            "chat_id": string_property(
                description="Optional default chat id to deliver messages to.",
            ),
        },
        required=("bot_token",),
    ),
    source="interactive_shell",
    surfaces=("action",),
    parallel_safe=False,
    accepts_runtime_context=True,
    run=run_configure_telegram,
)


__all__ = ["configure_telegram_tool", "execute_configure_telegram_tool"]
