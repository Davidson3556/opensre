"""Scheduled reminder helpers for work items."""

from __future__ import annotations

import json

from core.domain.work_items import (
    WorkItem,
    WorkItemChannelTarget,
    cron_from_datetime,
    parse_work_item_datetime,
    work_items_path,
)
from infrastructure.scheduling.scheduler.storage import add_task as add_scheduled_task
from infrastructure.scheduling.scheduler.storage import list_tasks, update_task
from infrastructure.scheduling.scheduler.types import Provider, ScheduledTask, TaskKind
from tools.system.work_items.validation import validate_provider


def disable_existing_item_reminders(item_id: str, *, keep_task_id: str = "") -> int:
    """Disable enabled one-shot reminders for ``item_id`` so updates replace them.

    ``keep_task_id`` spares the replacement when this runs after it is persisted.
    """
    disabled = 0
    for task in list_tasks():
        if task.kind is not TaskKind.WORK_ITEM_REMINDER:
            continue
        if not task.enabled:
            continue
        if keep_task_id and task.id == keep_task_id:
            continue
        if task.params.get("work_item_id", "").strip() != item_id:
            continue
        task.enabled = False
        if update_task(task):
            disabled += 1
    return disabled


def schedule_item_reminder(
    item: WorkItem,
    *,
    targets: list[WorkItemChannelTarget],
    timezone: str,
) -> ScheduledTask | None:
    """Schedule or replace a one-shot work item reminder task."""
    if not item.remind_at:
        return None
    remind_at = parse_work_item_datetime(item.remind_at)
    if remind_at is None:
        return None
    valid_targets = [target for target in targets if validate_provider(target.provider) is not None]
    if not valid_targets:
        return None
    primary = valid_targets[0]
    parsed_provider = Provider(primary.provider)
    schedule_timezone = "UTC" if remind_at.tzinfo is not None else timezone
    task = ScheduledTask(
        kind=TaskKind.WORK_ITEM_REMINDER,
        cron=cron_from_datetime(remind_at),
        timezone=schedule_timezone,
        provider=parsed_provider,
        chat_id=primary.chat_id,
        params={
            "work_item_id": item.id,
            "store_path": str(work_items_path()),
            "disable_after_success": "true",
            "delivery_targets": json.dumps(
                [target.to_dict() for target in valid_targets], separators=(",", ":")
            ),
        },
    )
    created = add_scheduled_task(task)
    # Retire the prior reminder only once its replacement is durable. Disabling
    # first left the user with no reminder at all when the store write failed.
    disable_existing_item_reminders(item.id, keep_task_id=created.id)
    return created


_disable_existing_item_reminders = disable_existing_item_reminders
_schedule_item_reminder = schedule_item_reminder

__all__ = [
    "_disable_existing_item_reminders",
    "_schedule_item_reminder",
    "disable_existing_item_reminders",
    "schedule_item_reminder",
]
