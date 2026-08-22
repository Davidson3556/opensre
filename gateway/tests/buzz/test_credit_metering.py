"""Buzz turns are metered, and a denied turn never reaches the agent.

Twin of ``gateway/tests/telegram/test_credit_metering.py``. Buzz was born as a
copy of Telegram, and both shipped without the credit gate Slack and Discord
carry — every turn ran for free. The charge is billed to the silo organization,
not the sender's pubkey: only an explicit 402 blocks a turn, so a charge posted
against the wrong account would fail open and never surface.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

import pytest

from config.constants.gateway import CREDITS_DENIED_MESSAGE
from core.agent_harness.session import SessionCore
from core.agent_harness.session.persistence.memory import InMemorySessionStore
from gateway.core.billing.credits_client import CreditsOutcome
from gateway.core.middleware.active_turns import ActiveTurnRegistry
from gateway.core.middleware.approvals import ApprovalBroker
from gateway.transports.buzz import inbound_handler
from gateway.transports.buzz.inbound_handler import handle_polled_inbound_buzz_message
from gateway.transports.buzz.inbound_security import InboundDecision
from gateway.transports.buzz.pending_approvals import PendingApprovals
from gateway.transports.buzz.settings import BuzzInboundMessage, GatewaySettings

TEST_ORG_ID = "org_buzz_credits"


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, *, channel: str, content: str, **_kwargs: Any) -> dict[str, Any]:
        _ = channel
        self.sent.append(content)
        return {"success": True, "error": "", "event_id": f"ev-{len(self.sent)}"}

    def edit_message(self, *, event_id: str, content: str) -> dict[str, Any]:
        _ = (event_id, content)
        return {"success": True}


class _FakeSessionResolver:
    def __init__(self, session: SessionCore) -> None:
        self._session = session

    def resolve(self, **_kwargs: object) -> SessionCore:
        return self._session

    def rotate(self, **_kwargs: object) -> SessionCore:
        return self._session


@pytest.fixture(autouse=True)
def _authorized_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGANIZATION_ID", TEST_ORG_ID)
    monkeypatch.setattr(
        inbound_handler,
        "enforce_inbound_buzz_message_security",
        lambda **_kwargs: InboundDecision(allowed=True),
    )


def _run_turn(client: _FakeClient, callback: MagicMock) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        asyncio.run(
            handle_polled_inbound_buzz_message(
                BuzzInboundMessage(
                    event_id="in-1",
                    pubkey="npub-1",
                    channel_id="chan-1",
                    content="@bot hello",
                    created_at=1,
                    reply_event_ids=frozenset(),
                ),
                client=client,  # type: ignore[arg-type]
                session_resolver=_FakeSessionResolver(  # type: ignore[arg-type]
                    SessionCore(store=InMemorySessionStore())
                ),
                settings=GatewaySettings(private_key="k", allowed_pubkeys=["npub-1"]),
                executor=executor,
                chat_locks={},
                turn_semaphore=asyncio.Semaphore(1),
                approvals=ApprovalBroker(),
                pending_approvals=PendingApprovals(),
                active_cancels=ActiveTurnRegistry(),
                handle_callback_to_gateway_agent=callback,
            )
        )
    finally:
        executor.shutdown(wait=True)


def test_denied_credits_stop_the_turn_and_bill_the_owning_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    charges: list[tuple[str, str]] = []

    def deny(organization_id: str, *, reason: str, **_kwargs: object) -> CreditsOutcome:
        charges.append((organization_id, reason))
        return CreditsOutcome.DENIED

    monkeypatch.setattr(inbound_handler, "consume_credits", deny)
    client = _FakeClient()
    callback = MagicMock()

    _run_turn(client, callback)

    assert charges == [(TEST_ORG_ID, "buzz_turn")]
    callback.assert_not_called()
    assert client.sent == [CREDITS_DENIED_MESSAGE]


def test_unconfigured_metering_still_runs_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open: a dev box without metering env is not a billing denial."""
    monkeypatch.setattr(
        inbound_handler,
        "consume_credits",
        lambda *_a, **_kw: CreditsOutcome.UNCONFIGURED,
    )
    callback = MagicMock()

    _run_turn(_FakeClient(), callback)

    callback.assert_called_once()
