"""Typed, fixed administrator API operations exposed by the MCP server.

The registry is the complete request allowlist: tools cannot choose HTTP methods or
URLs. Optional update fields are serialized only when supplied, preserving the
API's distinction between omission, an empty list, and an explicit null.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str, StringConstraints(strict=True, min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
]
Title = Annotated[str, Field(min_length=1, max_length=120)]
Content = Annotated[str, Field(min_length=1, max_length=100_000)]
Description = Annotated[str, Field(max_length=1000)]
Agent = Annotated[str, Field(min_length=1, max_length=100)]
Model = Annotated[str, Field(min_length=1, max_length=300)]
ModelVariant = Annotated[str, Field(max_length=100)]
MattermostUsername = Annotated[str, Field(max_length=23)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Password = Annotated[
    str, Field(min_length=12, max_length=1024, repr=False, json_schema_extra={"writeOnly": True})
]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArguments(Arguments):
    pass


class UserId(Arguments):
    user_id: Identifier


class TaskId(Arguments):
    task_id: Identifier


class RunId(Arguments):
    run_id: Identifier


class JobId(Arguments):
    job_id: Identifier


class QueueId(Arguments):
    queue_id: Identifier


class TemplateId(Arguments):
    template_id: Identifier


class ChatId(Arguments):
    chat_id: Identifier


class CreateUser(Arguments):
    login: str = Field(min_length=1, max_length=80)
    password: Password
    role: Literal["user", "admin"] = "user"
    workspace_path: str | None = Field(
        default=None, description="Required for a regular user; optional for an administrator."
    )
    tasks_only: bool = False
    mattermost_username: MattermostUsername | None = None


class UpdateUser(UserId):
    # Non-null annotations with None defaults make fields optional to supply but
    # reject explicit null. Unset defaults never leave Operation.prepare().
    role: Literal["user", "admin"] = Field(default=None)
    workspace_path: str | None = Field(
        default=None, description="Omit or pass null to preserve the current workspace."
    )
    status: Literal["active", "disabled"] = Field(default=None)
    tasks_only: bool = Field(default=None)
    max_active_jobs: PositiveInt | None = Field(
        default=None, description="Explicit null removes the per-user concurrency override."
    )
    mattermost_username: MattermostUsername | None = Field(
        default=None, description="Explicit null or an empty string disconnects Mattermost."
    )


class ResetUserPassword(UserId):
    password: Password


class SetUserTaskAccess(UserId):
    task_ids: list[Identifier] = Field(
        description="Replace the selected-task memberships; [] removes all selected memberships."
    )


class CreateTask(Arguments):
    title: Title
    content: Content
    agent: Agent
    model: Model = Field(description="Provider/model identifier from valeria_admin_get_options.")
    description: Description | None = None
    model_variant: ModelVariant | None = None
    is_published: bool = False
    plan_first: bool = Field(default=False, description="Requires the build agent when enabled.")
    visibility: Literal["all", "selected"] = "all"
    user_ids: list[Identifier] | None = None
    next_task_ids: list[Identifier] = Field(default_factory=list)
    queue_id: Identifier = "default"


class UpdateTask(TaskId):
    title: Title = Field(default=None)
    content: Content = Field(default=None)
    agent: Agent = Field(default=None)
    model: Model = Field(default=None)
    description: Description | None = None
    model_variant: ModelVariant | None = None
    is_published: bool = Field(default=None)
    plan_first: bool = Field(default=None)
    visibility: Literal["all", "selected"] | None = None
    user_ids: list[Identifier] | None = None
    next_task_ids: list[Identifier] = Field(
        default_factory=list, description="Omit to preserve pipeline links; [] clears them."
    )
    queue_id: Identifier = Field(default=None)


class SetTaskAudience(TaskId):
    visibility: Literal["all", "selected"]
    user_ids: list[Identifier] = Field(
        default_factory=list, description="Complete audience when visibility is selected."
    )


class WorkQueue(Arguments):
    name: Title
    priority: int = Field(strict=True, ge=-1_000_000, le=1_000_000)
    max_workers: PositiveInt = Field(description="Must not exceed Valeria's configured hard limit.")


class UpdateQueue(WorkQueue, QueueId):
    pass


class SetDefaultChatQueue(Arguments):
    queue_id: Identifier


class SetConcurrency(Arguments):
    value: PositiveInt = Field(description="Must not exceed Valeria's configured hard limit.")


class CreateTemplate(Arguments):
    title: Title
    content: Content
    description: Description | None = None
    is_published: bool = False


class UpdateTemplate(TemplateId):
    title: Title = Field(default=None)
    content: Content = Field(default=None)
    description: Description | None = None
    is_published: bool = Field(default=None)


class SetConsultationSettings(Arguments):
    model: Model = Field(description="Provider/model identifier for consultation sessions.")
    queue_id: Identifier | None = Field(
        default=None, description="Omit or pass null to retain the current consultation queue."
    )


class ListConsultations(Arguments):
    owner_id: Identifier | None = None
    q: str = Field(default="", description="Case-insensitive title or owner-login search.")


class Statistics(Arguments):
    day: date | None = Field(default=None, description="Calendar day in YYYY-MM-DD format.")
    timezone: str = Field(default="UTC", min_length=1, description="IANA time zone, e.g. UTC.")

    @field_validator("day")
    @classmethod
    def supported_day(cls, value: date | None) -> date | None:
        if value in {date.min, date.max}:
            raise ValueError("Date is out of range")
        return value


class Options(Arguments):
    chat_id: Identifier | None = Field(
        default=None, description="Optional regular chat to inspect available agents/models."
    )


class ReplyChatQuestion(ChatId):
    question_id: Identifier
    answers: list[list[str]] = Field(min_length=1, max_length=100)


class RespondChatPermission(ChatId):
    permission_id: Identifier
    response: Literal["once", "always", "reject"]
    remember: bool = False


class IdempotentSubmission(Arguments):
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[\x20-\x7e]+$",
        description="Unique printable ASCII key. Reuse it only to retry this exact request.",
    )

    @field_validator("idempotency_key")
    @classmethod
    def nonblank_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Idempotency key cannot be blank")
        return value


class SendChatMessage(ChatId, IdempotentSubmission):
    message: Content
    agent: str | None = None
    model: str | None = None
    model_variant: str | None = None


class CreateTaskRun(TaskId, IdempotentSubmission):
    user_input: str = Field(default="", max_length=100_000)


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    method: str
    path: str
    input_model: type[BaseModel]
    expected_status: int = 200
    result_key: str | None = None
    destructive: bool = False
    idempotent: bool = True

    @property
    def read_only(self) -> bool:
        return self.method == "GET"

    def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        values = self.input_model.model_validate(arguments).model_dump(
            mode="json", exclude_unset=True
        )
        path_values = {
            field: values.pop(field) for field in re.findall(r"\{([a-z_]+)\}", self.path)
        }
        path = self.path.format(**path_values)
        idempotency_key = values.pop("idempotency_key", None)
        params = {key: value for key, value in values.items() if value is not None}
        return {
            "method": self.method,
            "path": path,
            "expected_status": self.expected_status,
            "body": None if self.read_only else values,
            "params": (params or None) if self.read_only else None,
            "idempotency_key": idempotency_key,
        }


def _operation(
    suffix: str,
    description: str,
    method: str,
    path: str,
    input_model: type[BaseModel] = NoArguments,
    *,
    status: int = 200,
    result_key: str | None = None,
    destructive: bool = False,
    idempotent: bool = True,
) -> Operation:
    return Operation(
        name=f"valeria_admin_{suffix}",
        description=description,
        method=method,
        path=f"api/{path}",
        input_model=input_model,
        expected_status=status,
        result_key=result_key,
        destructive=destructive,
        idempotent=idempotent,
    )


_OPERATION_LIST = (
    _operation(
        "list_users",
        "List all Valeria users and their administrator-visible settings.",
        "GET",
        "admin/users",
        result_key="users",
    ),
    _operation(
        "create_user",
        "Create a user or administrator with an initial password.",
        "POST",
        "admin/users",
        CreateUser,
        status=201,
        idempotent=False,
    ),
    _operation(
        "update_user",
        "Update role, account status, workspace, limits, or Mattermost identity.",
        "PATCH",
        "admin/users/{user_id}",
        UpdateUser,
        destructive=True,
    ),
    _operation(
        "reset_user_password",
        "Reset a user's password; existing sessions are revoked.",
        "POST",
        "admin/users/{user_id}/reset-password",
        ResetUserPassword,
        status=204,
        destructive=True,
        idempotent=False,
    ),
    _operation(
        "list_user_chats",
        "List a user's regular chats.",
        "GET",
        "admin/users/{user_id}/chats",
        UserId,
        result_key="chats",
    ),
    _operation(
        "sync_user_chat_titles",
        "Refresh a user's chat titles from OpenCode.",
        "POST",
        "admin/users/{user_id}/chats/title-sync",
        UserId,
        result_key="chats",
    ),
    _operation(
        "list_user_task_runs",
        "List a user's task runs, states, and current plan/build phases.",
        "GET",
        "admin/users/{user_id}/task-runs",
        UserId,
        result_key="runs",
    ),
    _operation(
        "sync_user_task_run_titles",
        "Refresh a user's task-run titles from OpenCode.",
        "POST",
        "admin/users/{user_id}/task-runs/title-sync",
        UserId,
        result_key="runs",
    ),
    _operation(
        "list_user_tasks",
        "Inspect a user's task access, including selected memberships.",
        "GET",
        "admin/users/{user_id}/tasks",
        UserId,
        result_key="tasks",
    ),
    _operation(
        "set_user_task_access",
        "Replace a user's selected-task memberships.",
        "PUT",
        "admin/users/{user_id}/task-access",
        SetUserTaskAccess,
        result_key="tasks",
        destructive=True,
    ),
    _operation(
        "list_tasks",
        "List all task definitions, including unpublished tasks and audiences.",
        "GET",
        "admin/tasks",
        result_key="tasks",
    ),
    _operation(
        "create_task",
        "Create a task definition with agent, model, audience, queue, and pipeline.",
        "POST",
        "admin/tasks",
        CreateTask,
        status=201,
        idempotent=False,
    ),
    _operation(
        "update_task",
        "Patch a task definition; omitted fields and pipeline links are preserved.",
        "PATCH",
        "admin/tasks/{task_id}",
        UpdateTask,
        destructive=True,
    ),
    _operation(
        "set_task_audience",
        "Replace a task's visibility and complete selected-user audience.",
        "PUT",
        "admin/tasks/{task_id}/audience",
        SetTaskAudience,
        destructive=True,
    ),
    _operation(
        "delete_task",
        "Delete a task definition; existing runs retain their saved parameters.",
        "DELETE",
        "admin/tasks/{task_id}",
        TaskId,
        status=204,
        destructive=True,
    ),
    _operation(
        "list_task_runs",
        "List task runs across all users with current states and owners.",
        "GET",
        "admin/task-runs",
        result_key="runs",
    ),
    _operation(
        "sync_task_run_titles",
        "Refresh task-run titles across all users from OpenCode.",
        "POST",
        "admin/task-runs/title-sync",
        result_key="runs",
    ),
    _operation(
        "get_task_run_messages",
        "Read the conversation messages for any user's task run.",
        "GET",
        "admin/task-runs/{run_id}/messages",
        RunId,
        result_key="messages",
    ),
    _operation(
        "get_queue",
        "Inspect global limits, active and queued jobs, and all work queues.",
        "GET",
        "admin/queue",
    ),
    _operation(
        "list_queues",
        "List work queues, including archived queues.",
        "GET",
        "admin/queues",
        result_key="queues",
    ),
    _operation(
        "create_queue",
        "Create a work queue with priority and worker limit.",
        "POST",
        "admin/queues",
        WorkQueue,
        status=201,
        idempotent=False,
    ),
    _operation(
        "update_queue",
        "Set all queue settings: name, priority, and worker limit.",
        "PATCH",
        "admin/queues/{queue_id}",
        UpdateQueue,
        destructive=True,
    ),
    _operation(
        "archive_queue",
        "Archive an unused queue; system or referenced queues cannot be archived.",
        "DELETE",
        "admin/queues/{queue_id}",
        QueueId,
        status=204,
        destructive=True,
    ),
    _operation(
        "set_default_chat_queue",
        "Choose the work queue used by new regular chat jobs.",
        "PUT",
        "admin/settings/default-chat-queue",
        SetDefaultChatQueue,
        destructive=True,
    ),
    _operation(
        "stop_job",
        "Stop any user's queued or running chat, task, or consultation job.",
        "POST",
        "admin/jobs/{job_id}/stop",
        JobId,
        status=202,
        destructive=True,
    ),
    _operation(
        "get_statistics",
        "Get task statistics for one calendar day in the supplied time zone.",
        "GET",
        "admin/statistics",
        Statistics,
    ),
    _operation(
        "get_mcp_status",
        "Inspect MCP server status for running OpenCode workspaces.",
        "GET",
        "admin/mcp",
        result_key="workspaces",
    ),
    _operation(
        "set_concurrency",
        "Set the global active-session concurrency override.",
        "PATCH",
        "admin/settings/concurrency",
        SetConcurrency,
        destructive=True,
    ),
    _operation(
        "reset_concurrency",
        "Remove the global concurrency override and use the server setting.",
        "DELETE",
        "admin/settings/concurrency/override",
        status=204,
        destructive=True,
    ),
    _operation(
        "list_templates",
        "List all chat templates, including unpublished templates.",
        "GET",
        "admin/templates",
        result_key="templates",
    ),
    _operation(
        "create_template",
        "Create a reusable chat prompt template.",
        "POST",
        "admin/templates",
        CreateTemplate,
        status=201,
        idempotent=False,
    ),
    _operation(
        "update_template",
        "Patch a chat template; explicit null clears only its description.",
        "PATCH",
        "admin/templates/{template_id}",
        UpdateTemplate,
        destructive=True,
    ),
    _operation(
        "delete_template",
        "Delete a chat template.",
        "DELETE",
        "admin/templates/{template_id}",
        TemplateId,
        status=204,
        destructive=True,
    ),
    _operation(
        "get_audit", "Read the administrator audit log.", "GET", "admin/audit", result_key="events"
    ),
    _operation(
        "get_consultation_settings",
        "Inspect the consultation model and queue settings.",
        "GET",
        "admin/settings/consultations",
    ),
    _operation(
        "set_consultation_settings",
        "Configure the consultation model and optional work queue.",
        "PUT",
        "admin/settings/consultations",
        SetConsultationSettings,
        destructive=True,
    ),
    _operation(
        "list_consultations",
        "List consultations, optionally filtering by owner or search text.",
        "GET",
        "admin/consultations",
        ListConsultations,
        result_key="consultations",
    ),
    _operation(
        "get_consultation",
        "Read a consultation's administrator detail and messages.",
        "GET",
        "admin/consultations/{chat_id}",
        ChatId,
    ),
    _operation(
        "get_options",
        "Get available agents and provider/model identifiers.",
        "GET",
        "options",
        Options,
    ),
    _operation(
        "get_public_config",
        "Inspect Valeria's public configuration and feature settings.",
        "GET",
        "config/public",
    ),
    _operation(
        "get_job",
        "Read any user's job status, including task and consultation jobs.",
        "GET",
        "jobs/{job_id}",
        JobId,
    ),
    _operation(
        "cancel_queued_job",
        "Cancel a queued job; running jobs require valeria_admin_stop_job.",
        "DELETE",
        "jobs/{job_id}",
        JobId,
        status=204,
        destructive=True,
    ),
    _operation(
        "get_task_result",
        "Read the final answer/result of any user's task run.",
        "GET",
        "task-runs/{run_id}/result",
        RunId,
    ),
    _operation(
        "get_chat",
        "Inspect any user's regular chat; task and consultation chats are excluded.",
        "GET",
        "chats/{chat_id}",
        ChatId,
    ),
    _operation(
        "get_chat_messages",
        "Read messages from any user's regular chat.",
        "GET",
        "chats/{chat_id}/messages",
        ChatId,
        result_key="messages",
    ),
    _operation(
        "get_chat_questions",
        "Read pending questions in a regular chat.",
        "GET",
        "chats/{chat_id}/questions",
        ChatId,
        result_key="questions",
    ),
    _operation(
        "get_chat_permissions",
        "Read pending tool-permission requests in a regular chat.",
        "GET",
        "chats/{chat_id}/permissions",
        ChatId,
        result_key="permissions",
    ),
    _operation(
        "get_chat_mcp",
        "Inspect the MCP servers attached to a regular chat workspace.",
        "GET",
        "chats/{chat_id}/mcp",
        ChatId,
    ),
    _operation(
        "reply_chat_question",
        "Answer a pending question in a user's regular chat.",
        "POST",
        "chats/{chat_id}/questions/{question_id}/reply",
        ReplyChatQuestion,
        destructive=True,
        idempotent=False,
    ),
    _operation(
        "respond_chat_permission",
        "Approve or reject a permission request in a regular chat.",
        "POST",
        "chats/{chat_id}/permissions/{permission_id}",
        RespondChatPermission,
        destructive=True,
        idempotent=False,
    ),
    _operation(
        "abort_chat",
        "Stop the active job in a regular chat.",
        "POST",
        "chats/{chat_id}/abort",
        ChatId,
        status=202,
        destructive=True,
        idempotent=False,
    ),
    _operation(
        "send_chat_message",
        "Send a message into an existing regular chat as its owner, audited "
        "as an administrator action. Requires an explicit idempotency key.",
        "POST",
        "admin/chats/{chat_id}/messages",
        SendChatMessage,
        status=202,
        destructive=True,
        idempotent=False,
    ),
    _operation(
        "create_task_run",
        "Start an available task for the configured administrator account "
        "only. Does not run as another user; the account needs a workspace. "
        "Requires an explicit idempotency key.",
        "POST",
        "tasks/{task_id}/runs",
        CreateTaskRun,
        status=202,
        idempotent=False,
    ),
)

OPERATIONS: dict[str, Operation] = {operation.name: operation for operation in _OPERATION_LIST}
