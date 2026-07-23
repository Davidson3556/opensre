"""Configurator handlers for chat-bot notification channels."""

from __future__ import annotations

from config.env_file import sync_env_secret, sync_env_values
from integrations.discord.setup import DISCORD_SETUP
from integrations.rocketchat.setup import ROCKETCHAT_SETUP
from integrations.store import upsert_integration
from integrations.telegram.setup import TELEGRAM_SETUP
from platform.terminal.theme import ERROR, GLYPH_ERROR, SECONDARY
from surfaces.cli.wizard._ui import (
    Choice,
    _choose,
    _console,
    _integration_defaults,
    _prompt_value,
    _render_integration_result,
    _string_value,
)
from surfaces.cli.wizard.configurators.spec_configurator import configure_from_spec
from surfaces.cli.wizard.integration_health import (
    validate_slack_webhook,
)


def _configure_slack() -> tuple[str, str]:
    _, credentials = _integration_defaults("slack")
    mode = _choose(
        "Slack setup:",
        [
            Choice(value="webhook", label="Incoming webhook (outbound delivery)"),
            Choice(value="socket", label="Socket Mode bot (two-way gateway chat)"),
            Choice(value="both", label="Both webhook and Socket Mode"),
        ],
        default="webhook",
    )
    creds = dict(credentials)

    if mode in {"webhook", "both"}:
        while True:
            webhook_url = _prompt_value(
                "Slack webhook URL",
                default=_string_value(creds.get("webhook_url")),
                secret=True,
            )
            with _console.status("Validating Slack webhook...", spinner="dots"):
                result = validate_slack_webhook(webhook_url=webhook_url)
            _render_integration_result("Slack webhook", result)
            if result.ok:
                creds["webhook_url"] = webhook_url
                break
            _console.print(f"[{SECONDARY}]Try again or press Ctrl+C to cancel.[/]")

    if mode in {"socket", "both"}:
        bot_token = _prompt_value(
            "Slack bot token (xoxb-…)",
            default=_string_value(creds.get("bot_token")),
            secret=True,
        )
        app_token = _prompt_value(
            "Slack app-level token (xapp-…)",
            default=_string_value(creds.get("app_token")),
            secret=True,
        )
        if not bot_token.startswith("xoxb-") or not app_token.startswith("xapp-"):
            _console.print(
                f"[{ERROR}]{GLYPH_ERROR} Socket Mode needs xoxb- bot token and xapp- app token.[/]"
            )
            raise SystemExit(1)
        creds["bot_token"] = bot_token
        creds["app_token"] = app_token
        sync_env_secret("SLACK_BOT_TOKEN", bot_token)
        sync_env_secret("SLACK_APP_TOKEN", app_token)

    upsert_integration("slack", {"credentials": creds})
    env_path = sync_env_values({})
    return "Slack", str(env_path)


def _configure_discord() -> tuple[str, str]:
    return configure_from_spec(
        DISCORD_SETUP,
        title="Discord",
        intro=(
            "\n[bold]Discord Integration[/bold]\n"
            f"[{SECONDARY}]Get your credentials from https://discord.com/developers/applications. "
            "Only the bot token is required; the application ID (needed to register the "
            "/investigate slash command), public key, and a default channel ID are optional.[/]\n"
        ),
    )


def _configure_rocketchat() -> tuple[str, str]:
    return configure_from_spec(
        ROCKETCHAT_SETUP,
        title="Rocket.Chat",
        intro=(
            "\n[bold]Rocket.Chat Integration[/bold]\n"
            f"[{SECONDARY}]Set it up one of two ways: a personal access token (server URL + "
            "token + user ID, for dynamic channel targeting) or an incoming webhook (a fixed "
            "destination). Leave the fields for the path you are not using blank.\n"
            "Personal Access Token: My Account > Personal Access Tokens (the token page also "
            "shows your user ID). Incoming webhook: Administration > Integrations > Incoming.[/]\n"
        ),
    )


def _configure_telegram() -> tuple[str, str]:
    return configure_from_spec(
        TELEGRAM_SETUP,
        title="Telegram",
        intro=(
            "\n[bold]Telegram Integration[/bold]\n"
            f"[{SECONDARY}]Create a bot with @BotFather, then add it to the chat it should post "
            "in. For a public channel the @name is enough; otherwise find the numeric chat id "
            "via getUpdates. See docs/messaging/telegram for details.\n"
            "Both answers are required — Telegram cannot deliver without a chat. Press Ctrl+C to "
            "skip Telegram and continue onboarding; `opensre integrations setup telegram` picks "
            "it up later.[/]\n"
        ),
    )
