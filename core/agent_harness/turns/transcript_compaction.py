"""Runtime/session compaction helpers for long REPL conversations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_AUTO_COMPACTION_CHARS = 48_000
_KEEP_RECENT_MESSAGES = 8
_SUMMARY_MAX_CHARS = 6_000
_SUMMARY_PREFIX = "Session summary:\n"


@dataclass(frozen=True)
class CompactionResult:
    summary: str
    before_chars: int
    after_chars: int
    first_kept_entry_id: str


def _message_chars(messages: list[tuple[str, str]]) -> int:
    return sum(len(role) + len(text) + 2 for role, text in messages)


def should_compact(
    session: Any,
    *,
    threshold_chars: int | None = None,
) -> bool:
    # Headless / in-memory sessions do not have a persisted ``session.agent``;
    # they can never grow past a threshold worth compacting, so treat missing
    # attributes as "no compaction needed" rather than raising.
    agent = getattr(session, "agent", None)
    messages = getattr(agent, "messages", None) if agent is not None else None
    if messages is None:
        return False
    threshold = threshold_chars or _auto_threshold()
    return _message_chars(list(messages)) > threshold


def compact_session_branch(
    session: Any,
    *,
    summary: str | None = None,
    first_kept_entry_id: str = "",
) -> CompactionResult | None:
    """Compact the live session branch and persist a compaction entry.

    The LLM-summary path is intentionally optional at this layer. When callers
    do not provide a summary, compaction uses a deterministic fallback so the
    shell can always recover space without depending on another provider call.
    """

    messages = list(session.agent.messages)
    if len(messages) <= _KEEP_RECENT_MESSAGES:
        return None

    before_chars = _message_chars(messages)
    kept = messages[-_KEEP_RECENT_MESSAGES:]
    compacted = messages[:-_KEEP_RECENT_MESSAGES]
    final_summary = summary or deterministic_summary(compacted)
    session.agent.messages = [("assistant", f"{_SUMMARY_PREFIX}{final_summary}"), *kept]
    after_chars = _message_chars(list(session.agent.messages))
    session.storage.append_compaction(
        session.session_id,
        summary=final_summary,
        first_kept_entry_id=first_kept_entry_id,
        before_chars=before_chars,
        after_chars=after_chars,
        before_tokens=_estimate_tokens(before_chars),
        after_tokens=_estimate_tokens(after_chars),
    )
    return CompactionResult(
        summary=final_summary,
        before_chars=before_chars,
        after_chars=after_chars,
        first_kept_entry_id=first_kept_entry_id,
    )


def auto_compact_if_needed(
    session: Any,
    *,
    threshold_chars: int | None = None,
) -> CompactionResult | None:
    if not should_compact(session, threshold_chars=threshold_chars):
        return None
    return compact_session_branch(session)


def fold_overflow_into_summary(
    messages: list[tuple[str, str]],
    *,
    max_messages: int,
) -> list[tuple[str, str]]:
    """Fold turns beyond the window into a leading running summary.

    The end-of-turn trim used to drop the oldest turns outright, so anything past
    the window vanished with no trace. Instead, summarize the overflow into a
    single leading ``Session summary:`` message and keep the most recent turns
    verbatim, so early context survives inside the window.

    Works on a bare message list (not a persisted session) so both the live
    ``Session`` and headless stores share one behavior. Extends an existing
    leading summary rather than nesting a new one, keeping it a running summary.
    """
    if max_messages < 1 or len(messages) <= max_messages:
        return messages

    head_is_summary = messages[0][0] == "assistant" and messages[0][1].startswith(_SUMMARY_PREFIX)
    prior_summary = messages[0][1][len(_SUMMARY_PREFIX) :] if head_is_summary else ""
    body = messages[1:] if head_is_summary else list(messages)

    # Reserve one slot for the summary, and keep an even count so the retained
    # tail always starts on a user turn rather than orphaning an assistant reply.
    keep = max((max_messages - 1) // 2 * 2, 2)
    if len(body) <= keep:
        return messages
    overflow = body[:-keep]
    recent = body[-keep:]

    folded = deterministic_summary(overflow)[:_SUMMARY_MAX_CHARS]
    if prior_summary:
        # Keep the newest fold intact, then fill the rest of the budget with the
        # earliest summarized context. Plain head-truncation would freeze once
        # full and silently drop fresh overflow; plain tail-truncation would
        # drop the original anchor context the summary exists to preserve.
        head_room = _SUMMARY_MAX_CHARS - len(folded) - 1
        head = prior_summary[:head_room].rstrip() if head_room > 0 else ""
        combined = f"{head}\n{folded}".strip() if head else folded
    else:
        combined = folded
    return [("assistant", f"{_SUMMARY_PREFIX}{combined}"), *recent]


def deterministic_summary(messages: list[tuple[str, str]]) -> str:
    if not messages:
        return ""
    first = _render_message_excerpt(messages[:4])
    recent = _render_message_excerpt(messages[-4:]) if len(messages) > 4 else ""
    parts = [
        f"Compacted {len(messages)} earlier conversation messages.",
        "Earlier context:",
        first,
    ]
    if recent:
        parts.extend(["Most recent compacted context:", recent])
    return "\n".join(part for part in parts if part).strip()[:_SUMMARY_MAX_CHARS]


def _render_message_excerpt(messages: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for role, text in messages:
        compact = " ".join(str(text).split())
        if len(compact) > 700:
            compact = compact[:697] + "..."
        lines.append(f"- {role}: {compact}")
    return "\n".join(lines)


def _estimate_tokens(chars: int) -> int:
    return max(1, chars // 4) if chars else 0


def _auto_threshold() -> int:
    raw = os.getenv("OPENSRE_SESSION_COMPACTION_CHARS", "").strip()
    if raw.isdigit():
        return max(1_000, int(raw))
    return DEFAULT_AUTO_COMPACTION_CHARS


__all__ = [
    "CompactionResult",
    "DEFAULT_AUTO_COMPACTION_CHARS",
    "auto_compact_if_needed",
    "compact_session_branch",
    "deterministic_summary",
    "fold_overflow_into_summary",
    "should_compact",
]
