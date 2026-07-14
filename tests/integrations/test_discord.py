from __future__ import annotations

import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from integrations.discord import classify
from integrations.discord.verifier import verify_discord


class _FakeIntents:
    def __init__(self) -> None:
        self.guilds = False

    @classmethod
    def none(cls) -> _FakeIntents:
        return cls()


def _install_fake_discord(
    monkeypatch: Any,
    *,
    run_error_factory: Callable[[type[Exception]], Exception] | None = None,
) -> list[str]:
    tokens: list[str] = []

    class LoginFailure(Exception):
        pass

    class Client:
        def __init__(self, *, intents: _FakeIntents) -> None:
            assert intents.guilds is True

        def run(self, token: str) -> None:
            tokens.append(token)
            if run_error_factory is not None:
                raise run_error_factory(LoginFailure)

    fake_discord = SimpleNamespace(
        Intents=_FakeIntents,
        Client=Client,
        LoginFailure=LoginFailure,
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    return tokens


def test_verify_discord_missing_bot_token(monkeypatch: Any) -> None:
    _install_fake_discord(monkeypatch)

    result = verify_discord("local env", {})

    assert result["status"] == "missing"
    assert "bot_token" in result["detail"]


def test_verify_discord_reports_login_failure(monkeypatch: Any) -> None:
    _install_fake_discord(
        monkeypatch,
        run_error_factory=lambda login_failure_cls: login_failure_cls("bad token"),
    )

    result = verify_discord("local env", {"bot_token": "bad-token"})

    assert result["status"] == "failed"
    assert "Discord login failed" in result["detail"]


def test_verify_discord_reports_api_failure(monkeypatch: Any) -> None:
    _install_fake_discord(
        monkeypatch,
        run_error_factory=lambda _login_failure_cls: RuntimeError("gateway unavailable"),
    )

    result = verify_discord("local env", {"bot_token": "token"})

    assert result["status"] == "failed"
    assert "gateway unavailable" in result["detail"]


def test_verify_discord_accepts_running_event_loop_success(monkeypatch: Any) -> None:
    _install_fake_discord(
        monkeypatch,
        run_error_factory=lambda _login_failure_cls: RuntimeError(
            "run() cannot be called from a running event loop"
        ),
    )

    result = verify_discord("local env", {"bot_token": "token"})

    assert result["status"] == "passed"
    assert "Discord bot token accepted" in result["detail"]


def test_verify_discord_accepts_token_when_client_run_succeeds(monkeypatch: Any) -> None:
    tokens = _install_fake_discord(monkeypatch)

    result = verify_discord("local env", {"bot_token": " token "})

    assert tokens == ["token"]
    assert result["status"] == "passed"


def test_classify_validation_failure_reports_without_secret_value() -> None:
    """SM-18: a real ValidationError in Discord classify() returns (None, None)
    and the exception reaching Sentry carries no secret field value.

    Pydantic v2 embeds the failing field's ``input_value`` in the
    ValidationError string, so an invalid ``public_key`` here would leak its
    value; the shared reporter sanitizes it to a model-name-only ValueError.
    """
    secret_value = "leaked-non-hex-secret"

    with patch("integrations._validation_helpers.report_exception") as mock_report:
        result = classify(
            {"bot_token": "some-token", "public_key": secret_value},
            record_id="rec-discord",
        )

    assert result == (None, None)
    mock_report.assert_called_once()
    reported_exc = mock_report.call_args.args[0]
    assert secret_value not in str(reported_exc)
    assert "DiscordBotConfig validation failed" in str(reported_exc)
