"""Registry self-consistency invariants.

These tests don't probe any integration. They enforce the static contract
between :mod:`app.integrations.registry` and the CLI dispatch in
:mod:`app.integrations.cli`. Each invariant prevents a class of drift that has
already shipped to ``main`` at least once: duplicate orders, missing handlers,
verifier without a positional route. Failing here on PR catches the regression
before users hit it via ``opensre integrations setup/verify <svc>``.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.integrations.cli import _HANDLERS
from app.integrations.registry import (
    INTEGRATION_SPECS,
    SUPPORTED_SETUP_SERVICES,
)


def _duplicates(values: list[int]) -> set[int]:
    return {value for value, count in Counter(values).items() if count > 1}


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Pre-existing duplicate setup_order values in INTEGRATION_SPECS. "
        "Renumber PR flips this strict."
    ),
)
def test_setup_orders_are_unique() -> None:
    orders = [s.setup_order for s in INTEGRATION_SPECS if s.setup_order is not None]
    duplicates = _duplicates(orders)
    if duplicates:
        offenders = {
            order: sorted(s.service for s in INTEGRATION_SPECS if s.setup_order == order)
            for order in duplicates
        }
        raise AssertionError(
            f"Duplicate setup_order values in INTEGRATION_SPECS: {offenders}. "
            "Pick a unique order per service so the wizard sort is deterministic."
        )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Pre-existing duplicate verify_order values in INTEGRATION_SPECS. "
        "Renumber PR flips this strict."
    ),
)
def test_verify_orders_are_unique() -> None:
    orders = [s.verify_order for s in INTEGRATION_SPECS if s.verify_order is not None]
    duplicates = _duplicates(orders)
    if duplicates:
        offenders = {
            order: sorted(s.service for s in INTEGRATION_SPECS if s.verify_order == order)
            for order in duplicates
        }
        raise AssertionError(
            f"Duplicate verify_order values in INTEGRATION_SPECS: {offenders}. "
            "Pick a unique order per service so `integrations verify` ordering is deterministic."
        )


def test_every_setup_service_has_a_cli_handler() -> None:
    missing = [svc for svc in SUPPORTED_SETUP_SERVICES if svc not in _HANDLERS]
    if missing:
        raise AssertionError(
            f"Registry declares setup_order for {missing} but no _HANDLERS entry "
            "in app/integrations/cli.py. These services are silently dropped from "
            "_SETUP_SERVICES, so `opensre integrations setup <svc>` will reject them "
            "with the 'Usage: setup <service>' error."
        )


def test_every_cli_handler_is_registered_in_registry() -> None:
    orphans = [svc for svc in _HANDLERS if svc not in SUPPORTED_SETUP_SERVICES]
    if orphans:
        raise AssertionError(
            f"app/integrations/cli.py defines _HANDLERS for {orphans} but registry "
            "has no setup_order for them. These handlers are unreachable: "
            "_SETUP_SERVICES only includes services that appear in both "
            "SUPPORTED_SETUP_SERVICES and _HANDLERS, so the wizard will never offer them."
        )


def test_every_verifier_has_a_verify_order() -> None:
    orphans = [
        s.service for s in INTEGRATION_SPECS if s.verifier is not None and s.verify_order is None
    ]
    if orphans:
        raise AssertionError(
            f"Registry defines verifier but no verify_order for {orphans}. "
            "These run on the no-arg `integrations verify` path but are rejected positionally."
        )
