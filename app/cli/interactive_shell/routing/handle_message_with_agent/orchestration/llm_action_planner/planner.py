"""Top-level planner orchestration for LLM-driven action plans."""

from __future__ import annotations

from typing import Any

from app.cli.interactive_shell.routing.handle_message_with_agent.orchestration.interaction_models import (
    PlannedAction,
)

from .llm_client import _call_llm
from .parsing import _parse_tool_plan
from .postprocessing import _fail_closed_vague_local_model, _finalize_planner_result
from .prompting import _sanitise_text


def plan_actions_with_llm(
    message: str,
    *,
    session: Any | None = None,
) -> tuple[list[PlannedAction], bool] | None:
    """Plan actions from *message* using native tool-calling."""
    sanitised = _sanitise_text(message.strip())
    early = _fail_closed_vague_local_model(sanitised)
    if early is not None:
        return early

    raw = _call_llm(sanitised, session)
    if raw is None:
        return None

    parsed = _parse_tool_plan(raw, session=session)
    if parsed is None:
        return None
    actions, has_unhandled = parsed
    return _finalize_planner_result(sanitised, actions, has_unhandled, session=session)
