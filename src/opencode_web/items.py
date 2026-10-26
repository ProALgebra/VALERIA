"""One projection of consultations, folder chats and task runs for the new UI."""

from __future__ import annotations

import re
from typing import Any

from .database import UNFINISHED_JOB_STATUSES

ITEM_KINDS = ("question", "task", "folder")
ITEM_STATUSES = frozenset(
    {*UNFINISHED_JOB_STATUSES, "completed", "idle", "failed", "cancelled"}
)
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def public_error_code(code: str | None) -> str | None:
    """Machine codes only; anything free-form stays server-side."""
    if code is None:
        return None
    return code if ERROR_CODE.fullmatch(code) else "internal_error"


def job_status(status: str | None) -> str:
    return status if status in ITEM_STATUSES else "running"


def task_run_display_title(job: dict[str, Any]) -> str:
    template_title = job["task_title"]
    title = job.get("chat_title") or template_title
    if title == template_title or title.startswith(f"{template_title}: "):
        return title
    return f"{template_title}: {title}"


def task_phase(job: dict[str, Any]) -> str:
    if not job.get("plan_first"):
        return "build"
    return "build" if job.get("phase2_message_id") else "plan"


def item_kind(row: dict[str, Any]) -> str:
    kind = row.get("chat_kind", row.get("kind"))
    if kind == "task":
        return "task"
    return "question" if row.get("consultation") else "folder"


def item_status(kind: str, status: str | None) -> str:
    if kind == "folder":
        # A folder chat is "idle" whenever its agent has nothing unfinished.
        return status if status in UNFINISHED_JOB_STATUSES else "idle"
    return "idle" if status is None else job_status(status)


def public_item(row: dict[str, Any]) -> dict[str, Any]:
    kind = item_kind(row)
    is_task = kind == "task" and row.get("task_id") is not None
    return {
        "kind": kind,
        "id": row["job_id"] if is_task else row["chat_id"],
        "chat_id": row["chat_id"],
        "title": task_run_display_title(row) if is_task else row["chat_title"],
        "status": item_status(kind, row.get("job_status")),
        "phase": task_phase(row) if is_task else None,
        "task_id": row.get("task_id") if is_task else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row.get("archived_at"),
        "snoozed_until": row.get("snoozed_until"),
    }


def public_admin_request(row: dict[str, Any]) -> dict[str, Any]:
    """`/api/admin/requests` row; soft-deleted chats stay listed with `deleted_at`."""
    item = public_item(row)
    return {
        **{key: item[key] for key in ("kind", "id", "chat_id", "title", "status")},
        "user": {"id": row["owner_user_id"], "login": row["owner_login"]},
        "task_id": item["task_id"],
        "task_title": row.get("task_title") if item["task_id"] else None,
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "deleted_at": row.get("deleted_at"),
    }


def allowed_item_kinds(user: dict[str, Any]) -> tuple[str, ...]:
    return ("question", "task") if user.get("tasks_only") else ITEM_KINDS


CHAT_EVENT_TYPES = frozenset({"chat.updated", "chat.deleted", "task.title.updated"})


def chat_item_row(database: Any, chat_id: str) -> dict[str, Any] | None:
    rows = database.list_items(chat_id=chat_id, include_deleted=True)
    return rows[0] if rows else None


async def publish_chat_updated(
    database: Any, events: Any, chat_id: str, *, title_changed: bool = False
) -> None:
    """Tell the shell that a chat row changed (archive, snooze, title)."""
    row = chat_item_row(database, chat_id)
    if row is None:
        return
    await events.publish({"type": "chat.updated", "item": row})
    if title_changed and item_kind(row) == "task" and row.get("task_id") is not None:
        await events.publish({"type": "task.title.updated", "item": row})


async def publish_chat_deleted(database: Any, events: Any, chat_id: str) -> None:
    if row := chat_item_row(database, chat_id):
        await events.publish({"type": "chat.deleted", "item": row})


def chat_event_for_user(user: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    """Only the owner (in the scope where they can open the chat) and admins see these."""
    row = event.get("item") or {}
    kind = item_kind(row)
    admin = user["role"] == "admin"
    if not admin:
        if row.get("owner_user_id") != user["id"] or kind not in allowed_item_kinds(user):
            return None
        workspace = user.get("workspace_path")
        if kind != "task" and workspace and row.get("workspace_snapshot") != workspace:
            if (
                event["type"] == "chat.updated"
                and row.get("archived_reason") == "workspace_changed"
            ):
                # Archived because the owner moved to another workspace: the chat is now
                # out of the owner's reach (like after a reload), so drop it from the list.
                return {"type": "chat.deleted", "chat_id": row.get("chat_id")}
            return None
        if row.get("deleted_at") and event["type"] != "chat.deleted":
            return None
    item = public_item(row)
    if event["type"] == "chat.deleted":
        visible: dict[str, Any] = {"type": "chat.deleted", "chat_id": item["chat_id"]}
    elif event["type"] == "task.title.updated":
        visible = {
            "type": "task.title.updated",
            "run_id": item["id"],
            "chat_id": item["chat_id"],
            "title": item["title"],
            # The legacy UI patches titles from `task`.
            "task": {"id": item["id"], "title": item["title"]},
        }
    else:
        visible = {"type": "chat.updated", **item}
    if admin:
        visible["owner_user_id"] = row.get("owner_user_id")
    return visible


QUEUE_JOB_FIELDS = (
    "id", "chat_id", "queue_id", "queue_name", "created_at", "started_at", "finished_at",
)


def job_kind(job: dict[str, Any]) -> str:
    if job.get("task_id"):
        return "task"
    return "question" if job.get("consultation") else "folder"


def public_queue_job(job: dict[str, Any], position: int | None) -> dict[str, Any]:
    """`/api/queue/me` row; questions and tasks never expose prompts or sessions."""
    kind = job_kind(job)
    if kind == "folder":
        return {**job, "kind": kind, "position": position}
    return {
        **{key: job.get(key) for key in QUEUE_JOB_FIELDS},
        "kind": kind,
        "status": job_status(job["status"]),
        "title": task_run_display_title(job) if kind == "task" else job.get("chat_title"),
        "task_id": job.get("task_id"),
        "error_code": public_error_code(job.get("error_code")),
        "position": position,
    }
