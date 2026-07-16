"""Tests for folding conversation overflow into a running summary."""

from __future__ import annotations

from core.agent_harness.turns.transcript_compaction import (
    _SUMMARY_MAX_CHARS,
    _SUMMARY_PREFIX,
    fold_overflow_into_summary,
)


def _turns(count: int, *, start: int = 1) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for i in range(start, start + count):
        messages.append(("user", f"Turn {i} question"))
        messages.append(("assistant", f"Turn {i} answer"))
    return messages


def test_noop_when_within_window() -> None:
    messages = _turns(3)  # 6 messages
    assert fold_overflow_into_summary(messages, max_messages=24) == messages


def test_early_fact_survives_in_leading_summary() -> None:
    messages = [("user", "My cluster is named prod-eu-42."), ("assistant", "Noted.")]
    messages += _turns(13, start=2)  # push well past the window

    folded = fold_overflow_into_summary(messages, max_messages=24)

    assert len(folded) <= 24
    # The oldest turn is gone from the verbatim tail but preserved in the summary.
    assert folded[0][0] == "assistant"
    assert folded[0][1].startswith(_SUMMARY_PREFIX)
    assert "prod-eu-42" in folded[0][1]


def test_extends_existing_summary_without_nesting() -> None:
    messages = [("assistant", f"{_SUMMARY_PREFIX}older context here")]
    messages += _turns(20)

    folded = fold_overflow_into_summary(messages, max_messages=24)

    # Still exactly one leading summary, and prior summary text is retained.
    assert folded[0][1].startswith(_SUMMARY_PREFIX)
    assert folded[0][1].count(_SUMMARY_PREFIX) == 1
    assert "older context here" in folded[0][1]
    assert sum(1 for role, text in folded if text.startswith(_SUMMARY_PREFIX)) == 1


def test_full_summary_keeps_newest_overflow_and_anchor() -> None:
    # A prior summary already at the char budget must not freeze: fresh overflow
    # still lands, and the earliest anchor context is not fully evicted.
    anchor = "ANCHOR-cluster-prod-eu-42"
    prior = anchor + " " + ("x" * _SUMMARY_MAX_CHARS)
    messages = [("assistant", f"{_SUMMARY_PREFIX}{prior}")]
    messages += [("user", "NEWEST-overflow-marker question"), ("assistant", "answer")]
    messages += _turns(24)

    folded = fold_overflow_into_summary(messages, max_messages=24)

    summary = folded[0][1]
    assert summary.startswith(_SUMMARY_PREFIX)
    assert len(summary) <= len(_SUMMARY_PREFIX) + _SUMMARY_MAX_CHARS
    assert "NEWEST-overflow-marker" in summary  # fresh overflow retained
    assert anchor in summary  # earliest anchor retained
