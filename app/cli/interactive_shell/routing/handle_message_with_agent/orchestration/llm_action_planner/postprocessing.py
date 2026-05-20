"""Post-parsing policy transforms for planner action results."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.cli.interactive_shell.routing.handle_message_with_agent.orchestration.intent_parser import (
    split_prompt_clauses,
)
from app.cli.interactive_shell.routing.handle_message_with_agent.orchestration.interaction_models import (
    PlannedAction,
)
from app.cli.interactive_shell.routing.handle_message_with_agent.orchestration.slash_commands.deterministic_action_mapper import (
    map_actions_with_unhandled,
)

from .constants import (
    _HTTP_INCIDENT_PASTE_RE,
    _INCIDENT_UPGRADE_SYMPTOM_RE,
    _LOCAL_LLAMA_CONNECT_RE,
    is_rich_pasted_incident,
)


def _as_llm_sourced(actions: list[PlannedAction]) -> list[PlannedAction]:
    return [replace(action, source="llm") for action in actions]


def _fail_closed_vague_local_model(message: str) -> tuple[list[PlannedAction], bool] | None:
    if _LOCAL_LLAMA_CONNECT_RE.search(message):
        return [], True
    return None


def _reconcile_compound_actions(
    message: str,
    actions: list[PlannedAction],
    has_unhandled: bool,
) -> tuple[list[PlannedAction], bool]:
    if len(split_prompt_clauses(message)) <= 1:
        return actions, has_unhandled
    if actions and all(action.kind == "assistant_handoff" for action in actions):
        return actions, has_unhandled

    det_actions, det_unhandled = map_actions_with_unhandled(message)
    if not det_actions or len(det_actions) <= len(actions):
        return actions, has_unhandled
    return _as_llm_sourced(det_actions), det_unhandled


def _upgrade_handoff_to_incident(
    message: str,
    actions: list[PlannedAction],
    has_unhandled: bool,
) -> tuple[list[PlannedAction], bool]:
    if len(split_prompt_clauses(message)) != 1:
        return actions, has_unhandled
    if not actions or not all(action.kind == "assistant_handoff" for action in actions):
        return actions, has_unhandled
    if "?" in message or re.search(r"\bhow\s+(?:do|to)\b", message, re.IGNORECASE):
        return actions, has_unhandled
    if not _INCIDENT_UPGRADE_SYMPTOM_RE.search(message):
        return actions, has_unhandled

    alert_text = message.strip()
    return [
        PlannedAction(
            kind="investigation",
            content=alert_text,
            position=0,
            source="llm",
            target_surface="investigation",
            args={"alert_text": alert_text},
        )
    ], False


def _fail_closed_unconfigured_integration_detail(
    message: str,
    session: Any | None,
    actions: list[PlannedAction],
    has_unhandled: bool,
) -> tuple[list[PlannedAction], bool]:
    if session is None or not bool(getattr(session, "configured_integrations_known", False)):
        return actions, has_unhandled

    configured = set(getattr(session, "configured_integrations", ()) or ())
    lowered = message.lower()
    for service in ("datadog", "grafana", "sentry", "posthog", "clickhouse"):
        if (
            service in lowered
            and service not in configured
            and re.search(r"\b(show|details|verify|remove|integration)\b", lowered)
        ):
            return [
                PlannedAction(
                    kind="assistant_handoff",
                    content=f"integration_details:{service}_unconfigured",
                    position=0,
                    source="llm",
                )
            ], False
    return actions, has_unhandled


def _coerce_incident_paste_handoff(
    message: str,
    actions: list[PlannedAction],
    has_unhandled: bool,
) -> tuple[list[PlannedAction], bool]:
    if not actions or not all(action.kind == "investigation" for action in actions):
        return actions, has_unhandled
    if _INCIDENT_UPGRADE_SYMPTOM_RE.search(message):
        return actions, has_unhandled
    if re.search(r"\bhow\s+(?:do|to)\b", message, re.IGNORECASE):
        return actions, has_unhandled

    is_rich_paste = is_rich_pasted_incident(message)
    is_http_incident = _HTTP_INCIDENT_PASTE_RE.search(message) is not None
    if not is_rich_paste and not is_http_incident:
        return actions, has_unhandled

    content = (
        "incident_description:rich_context"
        if is_rich_paste
        else "incident_description:http_incident"
    )
    return [
        PlannedAction(
            kind="assistant_handoff",
            content=content,
            position=0,
            source="llm",
        )
    ], True


def _finalize_planner_result(
    message: str,
    actions: list[PlannedAction],
    has_unhandled: bool,
    *,
    session: Any | None = None,
) -> tuple[list[PlannedAction], bool]:
    early = _fail_closed_vague_local_model(message)
    if early is not None:
        return early

    actions, has_unhandled = _fail_closed_unconfigured_integration_detail(
        message,
        session,
        actions,
        has_unhandled,
    )
    if not actions and has_unhandled:
        return actions, has_unhandled

    actions, has_unhandled = _reconcile_compound_actions(message, actions, has_unhandled)
    actions, has_unhandled = _upgrade_handoff_to_incident(message, actions, has_unhandled)
    return _coerce_incident_paste_handoff(message, actions, has_unhandled)
