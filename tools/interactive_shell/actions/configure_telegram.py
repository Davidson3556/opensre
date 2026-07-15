"""Configure the Telegram integration from a bot token provided in chat.

The action agent selects this tool when a user pastes a Telegram bot token and
asks to set up / connect / enable Telegram, and extracts the token (and an
optional chat id) into the tool arguments. It validates the token against the
Telegram Bot API and, on success, saves the integration — so the user does not
have to re-enter the token in the interactive `/integrations setup telegram`
wizard.

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

    # Validate the token against the Telegram Bot API before persisting anything,
    # so a typo or revoked token fails with a reason instead of saving junk. Uses
    # the integration-layer verifier (tools may depend on integrations, not on
    # surfaces).
    from integrations.telegram.verifier import verify_telegram

    ctx.console.print("[dim]Validating Telegram bot token…[/]")
    outcome = verify_telegram("setup", {"bot_token": bot_token})
    # The verifier's failure detail can embed the token (it includes the request
    # URL, which contains ``/bot<token>/``). Redact it so the secret is never
    # surfaced back to the user or the model.
    detail = outcome["detail"].replace(bot_token, "<token>")
    if outcome["status"] != "passed":
        ctx.console.print(f"[red]Telegram setup failed: {escape(detail)}[/]")
        return {"ok": False, "error": detail}

    from integrations.store import upsert_integration

    credentials: dict[str, Any] = {"bot_token": bot_token}
    if chat_id:
        credentials["default_chat_id"] = chat_id
    upsert_integration("telegram", {"credentials": credentials})

    # Never echo the token back; the verifier's detail names the bot, not the secret.
    ctx.console.print(f"[green]✓ {escape(detail)} Saved to the integration store.[/]")
    ctx.session.record("configure_telegram", "telegram", ok=True)
    return {"ok": True, "detail": detail, "chat_id_set": bool(chat_id)}


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
