"""Tests for the shared integration setup flow.

This is the logic both the onboarding wizard and the interactive-shell action
tool rely on, so the invariants here are the ones that matter regardless of how
values were collected: nothing is persisted unless the vendor's verifier passes,
supplying one field does not discard the rest of the stored record, and secrets
never survive into text shown to a user or a model.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from integrations.setup_flow import (
    apply_setup,
    get_setup_spec,
    settable_services,
)

_STORED = "integrations.setup_flow._stored_credentials"
_VERIFIER = "integrations.setup_flow.get_verifier"
_UPSERT = "integrations.setup_flow.upsert_integration"
_TOKEN = "123456789:AAExampleSecretTokenValue"


def _verifier(status: str, detail: str = "") -> Any:
    return lambda _source, _config: {"status": status, "detail": detail}


def test_telegram_is_settable_and_declares_its_fields() -> None:
    assert "telegram" in settable_services()
    spec = get_setup_spec("telegram")
    assert spec is not None
    fields = {f.name: f for f in spec.fields}
    assert fields["bot_token"].required is True
    assert fields["bot_token"].secret is True
    # Optional, but delivery does not work without it — hence the advisory.
    assert fields["default_chat_id"].required is False


def test_unknown_service_is_rejected() -> None:
    outcome = apply_setup("not_a_service", {"token": "x"})
    assert outcome.ok is False
    assert outcome.saved is False


def test_missing_required_field_is_reported_before_any_verification() -> None:
    with (
        patch(_STORED, return_value={}),
        patch(_VERIFIER) as verifier,
        patch(_UPSERT) as upsert,
    ):
        outcome = apply_setup("telegram", {"default_chat_id": "-100"})
    assert outcome.ok is False
    assert "bot_token" in outcome.detail
    verifier.assert_not_called()
    upsert.assert_not_called()


def test_nothing_is_persisted_when_verification_fails() -> None:
    with (
        patch(_STORED, return_value={}),
        patch(_VERIFIER, return_value=_verifier("failed", "Unauthorized.")),
        patch(_UPSERT) as upsert,
    ):
        outcome = apply_setup("telegram", {"bot_token": _TOKEN})
    assert outcome.ok is False
    assert outcome.saved is False
    upsert.assert_not_called()


def test_secret_is_redacted_from_verifier_detail() -> None:
    """Verifier failures quote the request URL, which embeds the bot token."""
    detail = f"Telegram API check failed: 401 for https://api.telegram.org/bot{_TOKEN}/getMe"
    with (
        patch(_STORED, return_value={}),
        patch(_VERIFIER, return_value=_verifier("failed", detail)),
        patch(_UPSERT),
    ):
        outcome = apply_setup("telegram", {"bot_token": _TOKEN})
    assert _TOKEN not in outcome.detail
    assert "<redacted>" in outcome.detail


def test_supplying_one_field_keeps_the_rest_of_the_stored_record() -> None:
    """Pasting a fresh token must not drop an already-configured chat id."""
    stored = {"bot_token": "old-token", "default_chat_id": "-1001234567890"}
    with (
        patch(_STORED, return_value=stored),
        patch(_VERIFIER, return_value=_verifier("passed", "Connected.")),
        patch(_UPSERT) as upsert,
    ):
        outcome = apply_setup("telegram", {"bot_token": "new-token"})
    assert outcome.ok is True
    upsert.assert_called_once_with(
        "telegram",
        {"credentials": {"bot_token": "new-token", "default_chat_id": "-1001234567890"}},
    )


def test_blank_values_do_not_overwrite_stored_credentials() -> None:
    stored = {"bot_token": "kept", "default_chat_id": "-100"}
    with (
        patch(_STORED, return_value=stored),
        patch(_VERIFIER, return_value=_verifier("passed", "Connected.")),
        patch(_UPSERT) as upsert,
    ):
        apply_setup("telegram", {"bot_token": "kept", "default_chat_id": "   "})
    assert upsert.call_args[0][1]["credentials"]["default_chat_id"] == "-100"


def test_setup_without_a_chat_id_warns_that_delivery_will_not_work() -> None:
    with (
        patch(_STORED, return_value={}),
        patch(_VERIFIER, return_value=_verifier("passed", "Connected.")),
        patch(_UPSERT),
    ):
        outcome = apply_setup("telegram", {"bot_token": _TOKEN})
    assert outcome.ok is True
    assert outcome.warning is not None
    assert "TELEGRAM_DEFAULT_CHAT_ID" in outcome.warning


def test_no_warning_once_a_chat_id_is_configured() -> None:
    with (
        patch(_STORED, return_value={}),
        patch(_VERIFIER, return_value=_verifier("passed", "Connected.")),
        patch(_UPSERT),
    ):
        outcome = apply_setup(
            "telegram", {"bot_token": _TOKEN, "default_chat_id": "-1001234567890"}
        )
    assert outcome.warning is None
