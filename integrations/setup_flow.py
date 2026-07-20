"""Surface-agnostic integration setup: merge, validate, then persist.

Setting up an integration is the same work regardless of where the values come
from — the onboarding wizard collects them with interactive prompts, while the
interactive-shell action tool receives them from a user message the agent
parsed. Only the *collection* differs, so this module owns everything after it:
merging over the stored record, running the vendor's verifier, persisting, and
reporting the advisory a vendor wants shown on success.

Keeping it here (``integrations/``) rather than in the wizard lets both the
``surfaces/`` wizard and the ``tools/`` action tool share one implementation
without either reaching across layers. Validation is delegated to the existing
per-vendor verifier registry, so a vendor that can already be verified needs
only a :class:`IntegrationSetupSpec` to become settable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from integrations.store import get_integration, upsert_integration
from integrations.verification import get_verifier

_REDACTED = "<redacted>"


@dataclass(frozen=True)
class SetupField:
    """One credential a vendor needs, and how it should be handled."""

    name: str
    description: str
    required: bool = False
    # Secret values are scrubbed from any text shown to the user or the model:
    # verifier failures often quote the request URL, which embeds the token.
    secret: bool = False
    env_var: str | None = None


@dataclass(frozen=True)
class IntegrationSetupSpec:
    """What a vendor needs to be configured, independent of how it's collected."""

    service: str
    fields: tuple[SetupField, ...]
    # Advisory shown after a successful save — e.g. the integration is valid but
    # cannot deliver yet. Returns None when there is nothing to say.
    warn: Callable[[dict[str, Any]], str | None] | None = None


@dataclass(frozen=True)
class SetupOutcome:
    """Result of a setup attempt. ``saved`` is False whenever ``ok`` is False."""

    ok: bool
    detail: str
    saved: bool = False
    warning: str | None = None


def _telegram_warning(values: dict[str, Any]) -> str | None:
    if values.get("default_chat_id"):
        return None
    return (
        "No default chat ID set — Hermes, watchdog, and scheduled deliveries "
        "need TELEGRAM_DEFAULT_CHAT_ID to send messages."
    )


_SPECS: dict[str, IntegrationSetupSpec] = {}


def register_setup_spec(spec: IntegrationSetupSpec) -> None:
    """Make ``spec.service`` configurable through :func:`apply_setup`."""
    _SPECS[spec.service] = spec


def get_setup_spec(service: str) -> IntegrationSetupSpec | None:
    return _SPECS.get(service)


def settable_services() -> tuple[str, ...]:
    """Services that can be configured from collected values, sorted."""
    return tuple(sorted(_SPECS))


register_setup_spec(
    IntegrationSetupSpec(
        service="telegram",
        fields=(
            SetupField(
                name="bot_token",
                description=(
                    "Bot HTTP API token from @BotFather, in the form <numeric-id>:<secret>."
                ),
                required=True,
                secret=True,
                env_var="TELEGRAM_BOT_TOKEN",
            ),
            SetupField(
                name="default_chat_id",
                description="Default delivery destination; required for delivery to work.",
                env_var="TELEGRAM_DEFAULT_CHAT_ID",
            ),
        ),
        warn=_telegram_warning,
    )
)


def _stored_credentials(service: str) -> dict[str, Any]:
    record = get_integration(service)
    if not record:
        return {}
    credentials = record.get("credentials")
    return dict(credentials) if isinstance(credentials, dict) else {}


def _merge(spec: IntegrationSetupSpec, values: dict[str, Any]) -> dict[str, Any]:
    """Overlay supplied values on the stored record, ignoring blanks.

    Merging (rather than replacing) means setting only one field — pasting a
    fresh token, say — keeps the rest of the record, so a previously configured
    ``default_chat_id`` is not silently dropped.
    """
    known = {field.name for field in spec.fields}
    supplied = {
        name: str(value).strip()
        for name, value in values.items()
        if name in known and str(value or "").strip()
    }
    return {**_stored_credentials(spec.service), **supplied}


def _redact(text: str, spec: IntegrationSetupSpec, credentials: dict[str, Any]) -> str:
    """Scrub secret values out of user- and model-facing text."""
    for field in spec.fields:
        if not field.secret:
            continue
        secret = str(credentials.get(field.name) or "")
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def apply_setup(service: str, values: dict[str, Any]) -> SetupOutcome:
    """Configure ``service`` from ``values``, verifying before anything is saved.

    Values are merged over the stored record, checked for required fields, and
    validated by the vendor's verifier. Nothing is persisted unless verification
    passes, so a typo cannot overwrite a working integration.
    """
    spec = get_setup_spec(service)
    if spec is None:
        return SetupOutcome(ok=False, detail=f"{service} cannot be configured this way.")

    # Check the inputs before priming the verifier registry: that walk imports
    # every vendor module, which is wasted work for an incomplete request.
    credentials = _merge(spec, values)
    missing = [f.name for f in spec.fields if f.required and not credentials.get(f.name)]
    if missing:
        return SetupOutcome(ok=False, detail=f"Missing required: {', '.join(missing)}.")

    # Verifiers register themselves on import, so prime the registry rather than
    # relying on the caller having imported the vendor module. Idempotent.
    from integrations._verifiers_loader import register_all_verifiers

    register_all_verifiers()
    verifier = get_verifier(service)
    if verifier is None:
        return SetupOutcome(ok=False, detail=f"No verifier is registered for {service}.")

    result = verifier("setup", credentials)
    detail = _redact(result.get("detail", ""), spec, credentials)
    if result.get("status") != "passed":
        return SetupOutcome(ok=False, detail=detail)

    upsert_integration(service, {"credentials": credentials})
    warning = spec.warn(credentials) if spec.warn else None
    return SetupOutcome(ok=True, detail=detail, saved=True, warning=warning)


__all__ = [
    "IntegrationSetupSpec",
    "SetupField",
    "SetupOutcome",
    "apply_setup",
    "get_setup_spec",
    "register_setup_spec",
    "settable_services",
]
