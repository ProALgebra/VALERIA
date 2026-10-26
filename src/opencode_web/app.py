import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field
from starlette.convertors import Convertor, register_url_convertor

from .config import ConfigError, Settings
from .consultations import public_job as public_consultation_job
from .consultations import register_consultations
from .database import (
    DEFAULT_QUEUE_ID,
    ChatDeleted,
    Database,
    normalize_mattermost_user_id,
    normalize_mattermost_username,
)
from .dispatcher import QueueDispatcher
from .events import EventBroker
from .filters import matches_search, search_tokens, time_range
from .items import (
    CHAT_EVENT_TYPES,
    ITEM_KINDS,
    allowed_item_kinds,
    chat_event_for_user,
    item_kind,
    job_kind,
    job_status,
    public_admin_request,
    public_error_code,
    public_item,
    public_queue_job,
    publish_chat_deleted,
    publish_chat_updated,
    task_phase,
    task_run_display_title,
)
from .notifications import (
    MattermostNotificationError,
    NotificationDispatcher,
    resolve_mattermost_username,
)
from .opencode_client import OpenCodeError, OpenCodeManager
from .questions import question_event, session_questions, validate_answers
from .security import new_token, normalize_login, verify_password
from .security_headers import ContentSecurityPolicyMiddleware
from .submissions import submission
from .task_results import task_final_answer

logger = logging.getLogger(__name__)
COOKIE_NAME = "ocw_session"
PACKAGE_DIR = Path(__file__).parent


class LoginBody(BaseModel):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class UserCreateBody(BaseModel):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=1024)
    role: str = "user"
    workspace_path: str | None = None
    tasks_only: bool = False
    mattermost_username: str | None = Field(default=None, max_length=23)


class UserUpdateBody(BaseModel):
    role: str | None = None
    workspace_path: str | None = None
    status: str | None = None
    tasks_only: bool | None = None
    max_active_jobs: int | None = Field(default=None, ge=1)
    mattermost_username: str | None = Field(default=None, max_length=23)
    # The `updated_at` the edit started from; a newer stored value is a conflict (409).
    updated_at: AwareDatetime | None = None


class UserProfileUpdateBody(BaseModel):
    model_config = {"extra": "forbid"}
    mattermost_username: str | None = Field(default=None, max_length=23)


TERMINAL_OUTCOMES = frozenset({"completed", "failed", "cancelled"})


class UserBatchBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    status: Literal["active", "blocked"]


class PasswordResetBody(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class TemplateBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)
    is_published: bool = False


class TemplatePatchBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    is_published: bool | None = None


class ChatCreateBody(BaseModel):
    message: str | None = Field(default=None, max_length=100_000)
    agent: str | None = None
    model: str | None = None
    model_variant: str | None = None
    template_id: str | None = None


class TaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)
    agent: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    model_variant: str | None = Field(default=None, max_length=100)
    is_published: bool = False
    plan_first: bool = False
    input_hint: str | None = Field(default=None, max_length=500)
    visibility: str = "all"
    user_ids: list[str] | None = None
    next_task_ids: list[str] = Field(default_factory=list)
    queue_id: str = DEFAULT_QUEUE_ID


class TaskPatchBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    agent: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=300)
    model_variant: str | None = Field(default=None, max_length=100)
    is_published: bool | None = None
    plan_first: bool | None = None
    input_hint: str | None = Field(default=None, max_length=500)
    visibility: str | None = None
    user_ids: list[str] | None = None
    next_task_ids: list[str] = Field(default_factory=list)
    queue_id: str | None = None
    # The `updated_at` the edit started from; a newer stored value is a conflict (409).
    updated_at: AwareDatetime | None = None


class TaskAudienceBody(BaseModel):
    visibility: str
    user_ids: list[str] = Field(default_factory=list)


class UserTaskAccessBody(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


class TaskRunBody(BaseModel):
    model_config = {"extra": "forbid"}
    user_input: str = Field(default="", max_length=100_000)


class MessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    agent: str | None = None
    model: str | None = None
    model_variant: str | None = None


class ConcurrencyBody(BaseModel):
    value: int = Field(ge=1)


class WorkQueueBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(ge=-1_000_000, le=1_000_000)
    max_workers: int = Field(ge=1)


class WorkQueuePatchBody(WorkQueueBody):
    # The `updated_at` the edit started from; a newer stored value is a conflict (409).
    updated_at: AwareDatetime | None = None


class QueueRoutingBody(BaseModel):
    queue_id: str = Field(min_length=1, max_length=100)


class SnoozeBody(BaseModel):
    model_config = {"extra": "forbid"}
    until: AwareDatetime


class PermissionBody(BaseModel):
    response: str
    remember: bool = False


class QuestionReplyBody(BaseModel):
    model_config = {"extra": "forbid"}
    answers: list[list[str]] = Field(min_length=1, max_length=100)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "login",
        "role",
        "tasks_only",
        "mattermost_username",
        "mattermost_user_id",
        "workspace_path",
        "status",
        "max_active_jobs",
        "created_at",
        "updated_at",
        "last_login_at",
    )
    result = {key: user.get(key) for key in fields}
    result["tasks_only"] = bool(user.get("tasks_only"))
    return result


async def mattermost_identity(
    settings: Settings, raw_username: str | None,
) -> tuple[str | None, str | None]:
    try:
        username = normalize_mattermost_username(raw_username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not username:
        return None, None
    try:
        identity = await resolve_mattermost_username(settings, username)
    except MattermostNotificationError as exc:
        if exc.code == "http_404":
            raise HTTPException(422, "Mattermost user was not found") from exc
        raise HTTPException(503, "Mattermost is unavailable") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Mattermost integration is not configured") from exc
    try:
        canonical_username = normalize_mattermost_username(identity.get("username"))
        user_id = normalize_mattermost_user_id(identity.get("id"))
    except ValueError as exc:
        raise HTTPException(503, "Mattermost returned an invalid user") from exc
    if not canonical_username or not user_id:
        raise HTTPException(503, "Mattermost returned an invalid user")
    return canonical_username, user_id


def public_task_run(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "task_id": job["task_id"],
        "plan_first": bool(job.get("plan_first")),
        "phase": task_phase(job),
        "title": task_run_display_title(job),
        "status": job_status(job["status"]),
        "user_input": job.get("user_input") or None,
        "error_code": public_error_code(job.get("error_code")),
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job["finished_at"],
    }


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: task.get(key) for key in ("id", "title", "description", "plan_first", "input_hint")
    }


def admin_task(task: dict[str, Any]) -> dict[str, Any]:
    return {**task, "model": f"{task['provider_id']}/{task['model_id']}"}


def task_run_title(task: dict[str, Any], user_input: str) -> str:
    normalized = " ".join(user_input.split())
    if not normalized:
        return task["title"]
    return normalized[:120]


def checked_visibility(value: str) -> str:
    if value not in ("all", "selected"):
        raise HTTPException(422, "Visibility must be 'all' or 'selected'")
    return value


def checked_user_ids(db: Database, user_ids: list[str] | None) -> list[str]:
    result: list[str] = []
    for user_id in dict.fromkeys(user_ids or []):
        if not db.get_user(user_id):
            raise HTTPException(422, f"Unknown user: {user_id}")
        result.append(user_id)
    return result


def edit_error(state: dict[str, Any] | None) -> HTTPException:
    """404 when the object is gone, otherwise 409 naming who changed it last."""
    if not state or state["deleted"]:
        deleted_by = state["updated_by"] if state else None
        return HTTPException(404, {"code": "deleted", "deleted_by": deleted_by})
    return HTTPException(
        409,
        {"code": "conflict", "updated_by": state["updated_by"], "updated_at": state["updated_at"]},
    )


def edit_base(
    db: Database, table: str, object_id: str, sent: datetime | None
) -> str | None:
    """Stored `updated_at` to compare-and-set against, or None when the client sent
    none (last write wins). Raises the 404/409 from `edit_error`."""
    state = db.edit_state(table, object_id)
    if (
        not state
        or state["deleted"]
        or (sent is not None and datetime.fromisoformat(state["updated_at"]) != sent)
    ):
        raise edit_error(state)
    return state["updated_at"] if sent is not None else None


def admin_task_payload(db: Database, task: dict[str, Any]) -> dict[str, Any]:
    return {
        **admin_task(task),
        "audience_user_ids": db.list_task_audience(task["id"]),
        "next_task_ids": db.list_task_successors(task["id"]),
    }


def parse_model(model: str | None) -> tuple[str | None, str | None]:
    if not model:
        return None, None
    if "/" not in model or any(not part.strip() for part in model.split("/", 1)):
        raise HTTPException(422, "Model must use provider/model format")
    return tuple(model.split("/", 1))  # type: ignore[return-value]


def safe_model_catalog(payload: dict[str, Any], allowed: tuple[str, ...]) -> list[dict[str, str]]:
    source = payload.get("all", [])
    providers = source.values() if isinstance(source, dict) else source
    connected_raw = payload.get("connected")
    connected = (
        {str(provider_id) for provider_id in connected_raw}
        if isinstance(connected_raw, list)
        else None
    )
    result: list[dict[str, str]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id") or provider.get("providerID")
        if not provider_id:
            continue
        provider_id = str(provider_id)
        if connected is not None and provider_id not in connected:
            continue
        models = provider.get("models") or {}
        model_items = (
            models.items()
            if isinstance(models, dict)
            else (
                (model.get("id") or model.get("modelID"), model)
                for model in models
                if isinstance(model, dict)
            )
        )
        for model_id, model in model_items:
            if not model_id:
                continue
            full_id = f"{provider_id}/{model_id}"
            if allowed != ("*",) and full_id not in allowed:
                continue
            name = model.get("name", model_id) if isinstance(model, dict) else model_id
            result.append({"id": full_id, "name": str(name), "provider": provider_id})
    return sorted(result, key=lambda item: (item["provider"], item["name"]))


def session_id_from(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("sessionID", "session_id"):
            if isinstance(value.get(key), str):
                return value[key]
        for item in value.values():
            if result := session_id_from(item):
                return result
    elif isinstance(value, list):
        for item in value:
            if result := session_id_from(item):
                return result
    return None


def safe_opencode_event(event: dict[str, Any]) -> dict[str, Any]:
    envelope = event.get("event", {})
    return {
        "type": "opencode.event",
        "workspace": event.get("workspace"),
        "session_id": session_id_from(envelope),
        "event": envelope,
    }


def session_title_update(envelope: Any) -> tuple[str, str] | None:
    """(session id, title) from an OpenCode `session.updated` event."""
    payload = envelope.get("payload", envelope) if isinstance(envelope, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "session.updated":
        return None
    info = (payload.get("properties") or {}).get("info") or {}
    session_id, title = info.get("id"), info.get("title")
    if isinstance(session_id, str) and isinstance(title, str) and title.strip():
        return session_id, title
    return None


def safe_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return messages


def safe_mcp_statuses(payload: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name, raw_status in payload.items():
        status_value = (
            raw_status.get("status", "unknown") if isinstance(raw_status, dict) else raw_status
        )
        result.append({"name": str(name), "status": str(status_value or "unknown")})
    return sorted(result, key=lambda item: item["name"].lower())


CHAT_ACTIONS = frozenset({"archive", "unarchive", "snooze", "unsnooze"})


def is_chat_action(method: str, path: str) -> bool:
    """Chat menu routes that every role may call, including tasks_only accounts."""
    parts = path.split("/")
    if len(parts) < 4 or parts[:3] != ["", "api", "chats"] or not parts[3]:
        return False
    if len(parts) == 4:
        return method == "DELETE"
    return len(parts) == 5 and method == "POST" and parts[4] in CHAT_ACTIONS


def can_receive_event(database: Database, user: dict[str, Any], event: dict[str, Any]) -> bool:
    if user["role"] == "admin":
        return True
    if event.get("type") == "job.updated":
        job = event.get("job") or {}
        return job.get("effective_user_id") == user["id"] and (
            not user.get("tasks_only") or bool(job.get("task_id"))
        )
    if user.get("tasks_only"):
        return False
    if event.get("type") not in {
        "opencode.event", "opencode.disconnected", "chat.questions.updated"
    }:
        return False
    if event.get("workspace") != user["workspace_path"]:
        return False
    if session_id := event.get("session_id"):
        return database.user_owns_session(user["id"], user["workspace_path"], session_id)
    payload = (event.get("event") or {}).get("payload") or {}
    return payload.get("type") == "server.connected" or event.get("type") == (
        "opencode.disconnected"
    )


def event_for_user(
    database: Database, user: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any] | None:
    if event.get("type") in CHAT_EVENT_TYPES:
        return chat_event_for_user(user, event)
    job = event.get("job") or {}
    if event.get("type") == "job.updated" and job.get("chat_id"):
        chat = database.get_chat(job["chat_id"])
        if chat and chat.get("deleted_at") and user["role"] != "admin":
            return None
        if chat and chat.get("consultation"):
            if user["role"] != "admin" and (
                chat["owner_user_id"] != user["id"]
                or chat["workspace_snapshot"] != user.get("workspace_path")
            ):
                return None
            return {"type": "consultation.updated", "job": public_consultation_job(job)}
    if event.get("session_id") and database.consultation_for_session(event["session_id"]):
        return None
    if not can_receive_event(database, user, event):
        return None
    if event.get("type") == "job.updated" and (event.get("job") or {}).get("task_id"):
        visible = {"type": "task.updated", "task": public_task_run(event["job"])}
        if user["role"] == "admin":
            visible["owner_user_id"] = event["job"]["effective_user_id"]
        return visible
    return event


def bootstrap_admin(database: Database, settings: Settings) -> None:
    if database.count_users() > 0:
        return
    password_file = settings.bootstrap_admin_password_file
    if not settings.bootstrap_admin_login or not password_file or not password_file.is_file():
        logger.warning(
            "Database has no users; create BOOTSTRAP_ADMIN_PASSWORD_FILE before first start"
        )
        return
    password = password_file.read_text(encoding="utf-8").strip()
    admin = database.create_user(settings.bootstrap_admin_login, password, "admin", None)
    database.audit(admin["id"], "admin.bootstrap", object_type="user", object_id=admin["id"])


class SpaPathConvertor(Convertor[str]):
    """Any path outside /api and /static, so unknown API routes stay JSON 404s.
    A last segment with a dot names a file (/favicon.ico, /main.js): a plain 404.
    A trailing slash does not hide that dot, or the slash redirect would serve it."""

    regex = r"(?!(?:api|static)(?:/|$))(?:(?:[^/]*/)*[^/.]+/?)?"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("spa_path", SpaPathConvertor())


def create_app(settings: Settings | None = None, *, start_dispatcher: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    question_reply_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.database_path, settings.session_secret)
        database.initialize()
        database.ensure_default_queue_limit(settings.max_active_sessions_hard_limit)
        bootstrap_admin(database, settings)
        events = EventBroker()

        async def publish_live_title(event: dict[str, Any]) -> None:
            update = session_title_update(event.get("event"))
            if not update:
                return
            try:
                chat = database.chat_for_session(event.get("workspace"), update[0])
                if not chat or update[1].strip() == chat["title"]:
                    return
                database.set_chat_title(chat["id"], update[1])
                await publish_chat_updated(database, events, chat["id"], title_changed=True)
            except Exception:
                # Title sync is cosmetic; it must never break the event relay.
                logger.exception("Live chat title update failed")

        async def publish_opencode(event: dict[str, Any]) -> None:
            if event.get("type") == "opencode.disconnected":
                await events.publish(event)
            else:
                await events.publish(safe_opencode_event(event))
                await publish_live_title(event)

        manager = OpenCodeManager(settings, publish_opencode)
        dispatcher = QueueDispatcher(
            database,
            manager,
            events,
            settings.max_active_sessions,
            settings.max_active_sessions_per_user or None,
        )
        notification_dispatcher = NotificationDispatcher(database, settings)
        app.state.settings = settings
        app.state.database = database
        app.state.events = events
        app.state.manager = manager
        app.state.dispatcher = dispatcher
        app.state.notification_dispatcher = notification_dispatcher
        manager.start_idle_reaper(database.workspace_has_pending_jobs)
        try:
            if start_dispatcher:
                dispatcher.start()
                notification_dispatcher.start()
            yield
        finally:
            try:
                await notification_dispatcher.stop()
            finally:
                try:
                    await dispatcher.stop()
                finally:
                    try:
                        await manager.close()
                    finally:
                        database.close()

    docs = settings.enable_docs
    app = FastAPI(
        title="OpenCode Web",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.add_middleware(
        ContentSecurityPolicyMiddleware,
        exempt_paths=(
            frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"}) if docs else frozenset()
        ),
    )

    def database(request: Request) -> Database:
        return request.app.state.database

    def settings_dep(request: Request) -> Settings:
        return request.app.state.settings

    def current_principal(
        request: Request, db: Annotated[Database, Depends(database)]
    ) -> dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
        result = db.get_user_for_token(token)
        if not result:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
        user, csrf = result
        if user.get("tasks_only") and not (
            request.url.path.startswith("/api/auth/")
            or request.url.path in {
                "/api/config/public", "/api/events", "/api/profile", "/api/tasks",
                "/api/consultations", "/api/me/items", "/api/queue/me",
            }
            or request.url.path.startswith((
                "/api/tasks/", "/api/task-runs", "/api/consultations/",
            ))
            or is_chat_action(request.method, request.url.path)
        ):
            raise HTTPException(403, "This account can only use tasks and consultations")
        return {"user": user, "csrf": csrf, "token": token}

    def csrf_principal(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
    ) -> dict[str, Any]:
        if request.headers.get("x-csrf-token") != principal["csrf"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF check failed")
        return principal

    def admin_principal(
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
    ) -> dict[str, Any]:
        if principal["user"]["role"] != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
        return principal

    def admin_read_principal(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
    ) -> dict[str, Any]:
        if principal["user"]["role"] != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
        return principal

    def owned_chat(db: Database, principal: dict[str, Any], chat_id: str) -> dict[str, Any]:
        chat = db.get_chat(chat_id)
        if not chat or chat.get("kind") == "task" or chat.get("consultation"):
            raise HTTPException(404, "Chat not found")
        if principal["user"]["role"] != "admin" and (
            chat["owner_user_id"] != principal["user"]["id"]
            or chat["workspace_snapshot"] != principal["user"]["workspace_path"]
            or chat.get("deleted_at")
        ):
            raise HTTPException(404, "Chat not found")
        return chat

    def visible_item(db: Database, user: dict[str, Any], chat_id: str) -> dict[str, Any]:
        """The caller's own chat of any kind, exactly as /api/me/items lists it."""
        rows = db.list_items(
            owner_id=user["id"],
            workspace=user.get("workspace_path"),
            kinds=allowed_item_kinds(user),
            chat_id=chat_id,
        )
        if not rows:
            raise HTTPException(404, "Chat not found")
        return rows[0]

    async def stop_job_audited(
        request: Request, db: Database, actor_id: str, job: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        db.audit(
            actor_id, "job.stop.request", effective_user_id=job["effective_user_id"],
            object_type="job", object_id=job["id"],
            metadata={
                "previous_status": job["status"], "task_id": job.get("task_id"),
                **(metadata or {}),
            },
        )
        result = await request.app.state.dispatcher.stop_job(job["id"])
        if result and result["status"] == "cancelled" and job["status"] == "queued":
            db.audit(
                actor_id, "job.stop.completed", effective_user_id=job["effective_user_id"],
                object_type="job", object_id=job["id"],
            )
        return result

    def apply_opencode_title(
        db: Database, chat: dict[str, Any], session: dict[str, Any] | None
    ) -> dict[str, Any]:
        title = session.get("title") if session else None
        if not isinstance(title, str) or not title.strip() or title.strip() == chat["title"]:
            return chat
        return db.set_chat_title(chat["id"], title) or chat

    async def refresh_chat_title(
        request: Request, db: Database, chat: dict[str, Any]
    ) -> dict[str, Any]:
        if chat["opencode_session_id"].startswith("pending_"):
            return chat
        try:
            client = await request.app.state.manager.ensure(chat["workspace_snapshot"])
            session = await client.session(chat["opencode_session_id"])
        except OpenCodeError:
            return chat
        updated = apply_opencode_title(db, chat, session)
        if updated["title"] != chat["title"]:
            await publish_chat_updated(
                db, request.app.state.events, chat["id"], title_changed=True
            )
        return updated

    async def sync_chat_titles(
        request: Request, db: Database, chats: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for chat in chats:
            by_workspace.setdefault(chat["workspace_snapshot"], []).append(chat)

        async def refresh_workspace(
            workspace: str, workspace_chats: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            try:
                async with request.app.state.manager.use(
                    workspace, wake=False, activity=False
                ) as client:
                    if client is None:
                        return []
                    sessions = await client.sessions()
            except (OpenCodeError, ConfigError):
                return []
            by_id = {
                session.get("id") or session.get("sessionID"): session
                for session in sessions
                if isinstance(session, dict)
            }
            changes: list[dict[str, Any]] = []
            for chat in workspace_chats:
                updated = apply_opencode_title(
                    db, chat, by_id.get(chat["opencode_session_id"])
                )
                if updated["title"] != chat["title"]:
                    changes.append({"id": updated["id"], "title": updated["title"]})
            return changes

        groups = await asyncio.gather(
            *(refresh_workspace(workspace, items) for workspace, items in by_workspace.items())
        )
        changes = [change for group in groups for change in group]
        for change in changes:
            await publish_chat_updated(
                db, request.app.state.events, change["id"], title_changed=True
            )
        return changes

    async def sync_task_titles(
        request: Request, db: Database, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        run_by_chat = {job["chat_id"]: job for job in jobs}
        chats = [
            {
                "id": job["chat_id"],
                "title": job["chat_title"],
                "workspace_snapshot": job["workspace_snapshot"],
                "opencode_session_id": job["session_id"],
            }
            for job in jobs
        ]
        changes = await sync_chat_titles(request, db, chats)
        return [
            {
                "id": run_by_chat[change["id"]]["id"],
                "title": task_run_display_title(
                    {**run_by_chat[change["id"]], "chat_title": change["title"]}
                ),
            }
            for change in changes
            if change["id"] in run_by_chat
        ]

    def checked_workspace(
        db: Database,
        config: Settings,
        path: str | None,
        *,
        role: str,
        exclude_user_id: str | None = None,
    ) -> str | None:
        if role == "admin" and not path:
            return None
        if not path:
            raise HTTPException(422, "Workspace is required for a user")
        try:
            workspace = str(config.resolve_workspace_path(path))
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not config.allow_shared_workspaces and db.workspace_in_use(
            workspace, exclude_user_id=exclude_user_id
        ):
            raise HTTPException(409, "Workspace is already assigned")
        return workspace

    def validate_agent_model(
        config: Settings, agent: str | None, model: str | None
    ) -> tuple[str, str | None, str | None]:
        selected_agent = (agent or config.default_agent).lower()
        if selected_agent not in config.allowed_agents:
            raise HTTPException(422, "Agent is not allowed")
        selected_model = model or config.default_model
        if (
            selected_model
            and config.allowed_models != ("*",)
            and selected_model not in config.allowed_models
        ):
            raise HTTPException(422, "Model is not allowed")
        provider_id, model_id = parse_model(selected_model)
        return selected_agent, provider_id, model_id

    def enqueue(
        db: Database,
        config: Settings,
        chat: dict[str, Any],
        actor_id: str,
        body: MessageBody | ChatCreateBody,
    ) -> dict[str, Any] | None:
        content = body.message.strip() if body.message else ""
        if not content:
            return None
        if chat.get("deleted_at"):
            raise HTTPException(404, "Chat not found")
        if db.queued_count() >= config.max_queued_messages:
            raise HTTPException(429, "Queue is full")
        pinned_model = None
        if chat.get("preferred_provider_id") and chat.get("preferred_model_id"):
            pinned_model = f"{chat['preferred_provider_id']}/{chat['preferred_model_id']}"
        requested_model = body.model or pinned_model
        requested_agent = body.agent or chat.get("preferred_agent")
        agent, provider_id, model_id = validate_agent_model(
            config, requested_agent, requested_model
        )
        model_variant = body.model_variant
        if not model_variant and requested_model == pinned_model:
            model_variant = chat.get("preferred_model_variant")
        try:
            job = db.create_job(
                chat=chat,
                effective_user_id=chat["owner_user_id"],
                actor_user_id=actor_id,
                content=content,
                agent=agent,
                provider_id=provider_id,
                model_id=model_id,
                model_variant=model_variant,
            )
        except ChatDeleted as exc:
            raise HTTPException(404, "Chat not found") from exc
        app.state.dispatcher.wake()
        return job

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(PACKAGE_DIR / "static" / "index.html")

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": app.version,
            "dispatcher_limit": request.app.state.dispatcher.limit,
        }

    @app.post("/api/auth/login")
    async def login(
        body: LoginBody, response: Response, db: Annotated[Database, Depends(database)]
    ) -> dict[str, Any]:
        user = db.get_user_by_login(body.login)
        if (
            not user
            or user["status"] != "active"
            or not verify_password(user["password_hash"], body.password)
        ):
            await asyncio.sleep(0.2)
            raise HTTPException(401, "Неверный логин или пароль")
        token, csrf = new_token(), new_token()
        db.create_auth_session(user, token, csrf, settings.auth_session_ttl_hours)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.auth_session_ttl_hours * 3600,
            path="/",
        )
        db.audit(user["id"], "auth.login", object_type="user", object_id=user["id"])
        return {"user": public_user(user), "csrf_token": csrf}

    @app.post("/api/auth/logout", status_code=204)
    async def logout(
        response: Response,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        db.delete_auth_session(principal["token"])
        response.delete_cookie(COOKIE_NAME, path="/")

    @app.get("/api/auth/me")
    async def me(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
    ) -> dict[str, Any]:
        return {"user": public_user(principal["user"]), "csrf_token": principal["csrf"]}

    @app.patch("/api/profile")
    async def update_profile(
        body: UserProfileUpdateBody,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
        config: Annotated[Settings, Depends(settings_dep)],
    ) -> dict[str, Any]:
        if "mattermost_username" not in body.model_fields_set:
            return public_user(principal["user"])
        username, user_id = await mattermost_identity(config, body.mattermost_username)
        try:
            updated = db.update_user(
                principal["user"]["id"],
                mattermost_username=username,
                mattermost_user_id=user_id,
                actor_id=principal["user"]["id"],
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Mattermost user is already assigned") from exc
        db.audit(
            principal["user"]["id"],
            "user.profile_update",
            object_type="user",
            object_id=principal["user"]["id"],
            metadata={"mattermost_notifications": bool(username)},
        )
        return public_user(updated)  # type: ignore[arg-type]

    @app.get("/api/config/public")
    async def public_config(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
    ) -> dict[str, Any]:
        user = principal["user"]
        result = {
            "workspace": user["workspace_path"],
            "role": user["role"],
            "max_active_sessions": request.app.state.dispatcher.limit,
            "agents": list(settings.allowed_agents),
            "default_agent": settings.default_agent,
            "default_model": settings.default_model,
        }
        if user["role"] == "admin":
            result["max_active_jobs_hard_limit"] = (
                settings.max_active_sessions_per_user_hard_limit
            )
        return result

    @app.get("/api/options")
    async def options(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        config: Settings = request.app.state.settings
        result: dict[str, Any] = {
            "agents": list(config.allowed_agents),
            "default_agent": config.default_agent,
            "default_model": config.default_model,
            "models": [],
        }
        workspace = principal["user"]["workspace_path"]
        if chat_id:
            chat = owned_chat(db, principal, chat_id)
            workspace = chat["workspace_snapshot"]
            result["selected_agent"] = chat.get("preferred_agent") or config.default_agent
            if chat.get("preferred_provider_id") and chat.get("preferred_model_id"):
                result["selected_model"] = (
                    f"{chat['preferred_provider_id']}/{chat['preferred_model_id']}"
                )
            else:
                result["selected_model"] = config.default_model
            result["selected_model_variant"] = chat.get("preferred_model_variant")
            if chat["opencode_session_id"].startswith("pending_"):
                return result
        if not workspace and principal["user"]["role"] == "admin":
            # A bootstrap administrator can configure tasks before any users or
            # chats exist. Load the same OpenCode provider catalog without making
            # a chat or assigning a personal workspace to the administrator.
            try:
                workspace = str(
                    config.ensure_workspace(
                        config.allowed_workspace_roots[0] / ".opencode-web-catalog"
                    )
                )
            except ConfigError:
                result["opencode_unavailable"] = True
                return result
        if workspace:
            try:
                client = await request.app.state.manager.ensure(workspace)
                providers = await client.providers()
                result["models"] = safe_model_catalog(providers, config.allowed_models)
            except (OpenCodeError, ConfigError):
                result["opencode_unavailable"] = True
        return result

    @app.get("/api/tasks")
    async def user_tasks(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        user = principal["user"]
        tasks = db.list_tasks_for_user(user["id"], published_only=True)
        return [public_task(task) for task in tasks]

    @app.post("/api/tasks/{task_id}/runs", status_code=202)
    async def run_task(
        task_id: str,
        body: TaskRunBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        task = db.get_task(task_id)
        user = principal["user"]
        if not task or not db.task_visible_to(task_id, user["id"]):
            raise HTTPException(404, "Task not found")
        if not user["workspace_path"]:
            raise HTTPException(422, "No workspace assigned")
        validate_agent_model(
            settings, task["agent"], f"{task['provider_id']}/{task['model_id']}"
        )
        user_input = body.user_input.strip()
        with submission(db, request, user, body) as result:
            if result:
                return result
            try:
                job = db.create_task_job(
                    task=task, user=user, actor_user_id=user["id"], session_id=None,
                    user_input=user_input, session_title=task_run_title(task, user_input),
                    max_queued=settings.max_queued_messages,
                )
            except ValueError as exc:
                if str(exc) == "The queue is full":
                    raise HTTPException(429, "Queue is full") from exc
                if str(exc) == "Task is unavailable":
                    raise HTTPException(404, "Task not found") from exc
                raise HTTPException(409, "Task or user settings changed; try again") from exc
            db.audit(
                user["id"], "task.run", object_type="task_run", object_id=job["id"],
                metadata={"task_id": task_id},
            )
            result.update(public_task_run(job))
        if job["status"] == "queued":
            request.app.state.dispatcher.wake()
        await request.app.state.events.publish({"type": "job.updated", "job": job})
        return public_task_run(job)

    @app.get("/api/task-runs")
    async def task_runs(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return [public_task_run(job) for job in db.list_task_runs(principal["user"]["id"])]

    @app.post("/api/task-runs/title-sync")
    async def sync_task_run_titles_from_opencode(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return await sync_task_titles(
            request, db, db.list_task_runs(principal["user"]["id"])
        )

    @app.get("/api/task-runs/{run_id}")
    async def task_run(
        run_id: str,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        job = db.get_task_run(run_id)
        if (
            not job or job["effective_user_id"] != principal["user"]["id"]
            or job.get("chat_deleted_at")
        ):
            raise HTTPException(404, "Task run not found")
        return public_task_run(job)

    @app.get("/api/task-runs/{run_id}/result")
    async def task_run_result(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        job = db.get_task_run(run_id)
        user = principal["user"]
        if not job or (
            user["role"] != "admin"
            and (job["effective_user_id"] != user["id"] or job.get("chat_deleted_at"))
        ):
            raise HTTPException(404, "Task run not found")
        if job["status"] != "completed":
            return {"run_id": run_id, "status": "not_completed", "text": None}
        result = db.get_task_result(run_id)
        if result is None:
            answer = None
            if not job.get("session_pending") and not (
                job.get("plan_first") and not job.get("phase2_message_id")
            ):
                try:
                    # Only legacy runs need OpenCode. Bound startup/history latency;
                    # transient failures must remain retryable, not be cached as empty.
                    async with asyncio.timeout(20):
                        client = await request.app.state.manager.ensure(job["workspace_snapshot"])
                        messages = await client.messages(job["session_id"])
                    answer = task_final_answer(
                        messages, job.get("phase2_message_id") or job["message_id"]
                    )
                except (OpenCodeError, ConfigError, TimeoutError) as exc:
                    raise HTTPException(503, "Итоговое сообщение временно недоступно") from exc
            db.save_task_result(run_id, answer)
            result = db.get_task_result(run_id)
        if user["role"] == "admin" and job["effective_user_id"] != user["id"]:
            db.audit(
                user["id"], "task_run.result", effective_user_id=job["effective_user_id"],
                object_type="task_run", object_id=run_id,
            )
        text = result["text"] if result else None
        return {"run_id": run_id, "status": "available" if text else "unavailable", "text": text}

    @app.post("/api/task-runs/{run_id}/stop", status_code=202)
    async def stop_task_run(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        job = db.get_task_run(run_id)
        user_id = principal["user"]["id"]
        if not job or job["effective_user_id"] != user_id or job.get("chat_deleted_at"):
            raise HTTPException(404, "Task run not found")
        result = await stop_job_audited(request, db, user_id, job)
        if result is None:
            raise HTTPException(404, "Task run not found")
        return public_task_run(result)

    @app.get("/api/admin/tasks")
    async def admin_tasks(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return [admin_task_payload(db, task) for task in db.list_tasks(published_only=False)]

    @app.post("/api/admin/tasks", status_code=201)
    async def admin_create_task(
        body: TaskBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        agent, provider_id, model_id = validate_agent_model(settings, body.agent, body.model)
        visibility = checked_visibility(body.visibility)
        user_ids = checked_user_ids(db, body.user_ids)
        try:
            task = db.create_task(
                principal["user"]["id"], body.title, body.description, body.content,
                agent, provider_id, model_id, body.model_variant, body.is_published,
                next_task_ids=body.next_task_ids, plan_first=body.plan_first,
                queue_id=body.queue_id, input_hint=body.input_hint,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if visibility != "all" or user_ids:
            db.set_task_audience(task["id"], visibility, user_ids, principal["user"]["id"])
        db.audit(
            principal["user"]["id"], "task.create", object_type="task", object_id=task["id"]
        )
        return admin_task_payload(db, db.get_task(task["id"]))  # type: ignore[arg-type]

    @app.patch("/api/admin/tasks/{task_id}")
    async def admin_update_task(
        task_id: str,
        body: TaskPatchBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        expected = edit_base(db, "tasks", task_id, body.updated_at)
        task = db.get_task(task_id)
        if not task:
            raise edit_error(db.edit_state("tasks", task_id))
        fields = body.model_dump(exclude_unset=True, exclude={"updated_at"})
        for key in (
            "title", "content", "agent", "model", "is_published", "plan_first", "queue_id"
        ):
            if key in fields and fields[key] is None:
                raise HTTPException(422, f"{key} cannot be null")
        visibility = fields.pop("visibility", None)
        user_ids = fields.pop("user_ids", None)
        if visibility is not None:
            checked_visibility(visibility)
        if user_ids is not None:
            checked_user_ids(db, user_ids)
        agent, provider_id, model_id = validate_agent_model(
            settings, fields.get("agent", task["agent"]),
            fields.pop("model", f"{task['provider_id']}/{task['model_id']}"),
        )
        fields.update(agent=agent, provider_id=provider_id, model_id=model_id)
        try:
            updated = db.update_task(
                task_id, fields, actor_id=principal["user"]["id"], expected_updated_at=expected,
                visibility=visibility, user_ids=user_ids,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not updated:
            raise edit_error(db.edit_state("tasks", task_id))
        db.audit(
            principal["user"]["id"], "task.update", object_type="task", object_id=task_id
        )
        return admin_task_payload(db, updated)  # type: ignore[arg-type]

    @app.put("/api/admin/tasks/{task_id}/audience")
    async def admin_set_task_audience(
        task_id: str,
        body: TaskAudienceBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        if not db.get_task(task_id):
            raise HTTPException(404, "Task not found")
        visibility = checked_visibility(body.visibility)
        user_ids = checked_user_ids(db, body.user_ids)
        updated = db.set_task_audience(task_id, visibility, user_ids, principal["user"]["id"])
        db.audit(
            principal["user"]["id"],
            "task.audience.update",
            object_type="task",
            object_id=task_id,
            metadata={"visibility": visibility, "user_ids": user_ids},
        )
        return admin_task_payload(db, updated)  # type: ignore[arg-type]

    @app.delete("/api/admin/tasks/{task_id}", status_code=204)
    async def admin_delete_task(
        task_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        if not db.delete_task(task_id, principal["user"]["id"]):
            raise HTTPException(404, "Task not found")
        db.audit(
            principal["user"]["id"], "task.delete", object_type="task", object_id=task_id
        )

    @app.get("/api/admin/task-runs")
    async def admin_task_runs(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return [
            {**public_task_run(job), "owner_login": job["effective_login"]}
            for job in db.list_task_runs(include_deleted=True)
        ]

    @app.post("/api/admin/task-runs/title-sync")
    async def admin_sync_task_run_titles_from_opencode(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return await sync_task_titles(request, db, db.list_task_runs(include_deleted=True))

    @app.get("/api/admin/task-runs/{run_id}/messages")
    async def admin_task_run_messages(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        job = db.get_task_run(run_id)
        if not job:
            raise HTTPException(404, "Task run not found")
        if job.get("session_pending"):
            messages = db.chat_submission_messages(job["chat_id"])
        else:
            try:
                client = await request.app.state.manager.ensure(job["workspace_snapshot"])
                messages = await client.messages(job["session_id"])
            except OpenCodeError as exc:
                raise HTTPException(503, "OpenCode is unavailable") from exc
        db.audit(
            principal["user"]["id"],
            "task_run.messages",
            effective_user_id=job["effective_user_id"],
            object_type="task_run",
            object_id=run_id,
        )
        return safe_messages(messages)

    @app.get("/api/templates")
    async def user_templates(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return db.list_templates(published_only=True)

    @app.get("/api/chats")
    async def chats(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        user = principal["user"]
        return db.list_chats(user["id"], workspace=user["workspace_path"])

    @app.post("/api/chats/title-sync")
    async def sync_chat_titles_from_opencode(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        user = principal["user"]
        chats = db.list_chats(user["id"], workspace=user["workspace_path"])
        return await sync_chat_titles(request, db, chats)

    @app.post("/api/chats", status_code=201)
    async def create_chat(
        body: ChatCreateBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        if not user["workspace_path"]:
            raise HTTPException(422, "No workspace assigned")
        agent, provider_id, model_id = validate_agent_model(settings, body.agent, body.model)
        with submission(db, request, user, body) as result:
            if result:
                return result
            chat = db.create_chat(
                user["id"], None, "Новый диалог", user["workspace_path"],
                preferred_agent=agent,
                preferred_provider_id=provider_id,
                preferred_model_id=model_id,
                preferred_model_variant=body.model_variant,
            )
            job = enqueue(db, settings, chat, user["id"], body)
            result.update({"chat": db.get_chat(chat["id"]), "job": job})
        return result

    @app.get("/api/chats/{chat_id}")
    async def get_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        chat = owned_chat(db, principal, chat_id)
        return await refresh_chat_title(request, db, chat)

    @app.get("/api/chats/{chat_id}/messages")
    async def chat_messages(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        chat = owned_chat(db, principal, chat_id)
        pending = db.chat_submission_messages(chat_id)
        if chat["opencode_session_id"].startswith("pending_"):
            return pending
        try:
            client = await request.app.state.manager.ensure(chat["workspace_snapshot"])
            messages = safe_messages(await client.messages(chat["opencode_session_id"]))
        except OpenCodeError as exc:
            raise HTTPException(503, "OpenCode is unavailable") from exc
        ids = {message.get("info", message).get("id") for message in messages}
        return messages + [message for message in pending if message["info"]["id"] not in ids]

    @app.get("/api/chats/{chat_id}/questions")
    async def chat_questions(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        chat = owned_chat(db, principal, chat_id)
        if chat["opencode_session_id"].startswith("pending_"):
            return []
        try:
            async with request.app.state.manager.use(
                chat["workspace_snapshot"], wake=False, activity=False
            ) as client:
                if client is None:
                    return []
                return await session_questions(
                    client, chat["opencode_session_id"], chat["workspace_snapshot"]
                )
        except OpenCodeError as exc:
            raise HTTPException(503, "Не удалось получить вопросы OpenCode") from exc

    @app.post("/api/chats/{chat_id}/questions/{question_id}/reply")
    async def reply_chat_question(
        chat_id: str,
        question_id: str,
        body: QuestionReplyBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, bool]:
        chat = owned_chat(db, principal, chat_id)
        try:
            # Recheck pending state under the same lock as sending.
            # A retry/second tab must never send a second answer to a resolved request.
            async with question_reply_lock:
                client = await request.app.state.manager.ensure(chat["workspace_snapshot"])
                pending = await session_questions(
                    client, chat["opencode_session_id"], chat["workspace_snapshot"]
                )
                question = next((item for item in pending if item.get("id") == question_id), None)
                if question is None:
                    raise HTTPException(409, "Вопрос уже закрыт или недоступен в этом диалоге")
                try:
                    validate_answers(question, body.answers)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                try:
                    accepted = await client.respond_question(question_id, body.answers)
                    if accepted is False:
                        raise OpenCodeError("Question reply was not accepted")
                except OpenCodeError:
                    remaining = await session_questions(
                        client, chat["opencode_session_id"], chat["workspace_snapshot"]
                    )
                    if any(item.get("id") == question_id for item in remaining):
                        raise
                    raise HTTPException(
                        409, "Вопрос уже закрыт; состояние диалога обновлено"
                    ) from None
        except OpenCodeError as exc:
            raise HTTPException(
                503, "Ответ не подтверждён. Обновите вопрос и повторите попытку"
            ) from exc
        db.audit(
            principal["user"]["id"], "question.reply", effective_user_id=chat["owner_user_id"],
            object_type="chat", object_id=chat_id, metadata={"question_id": question_id},
        )
        await request.app.state.events.publish(
            question_event(chat["workspace_snapshot"], chat["opencode_session_id"])
        )
        return {"accepted": True}

    @app.get("/api/chats/{chat_id}/permissions")
    async def chat_permissions(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        chat = owned_chat(db, principal, chat_id)
        if chat["opencode_session_id"].startswith("pending_"):
            return []
        try:
            async with request.app.state.manager.use(
                chat["workspace_snapshot"], wake=False, activity=False
            ) as client:
                if client is None:
                    return []
                permissions = await client.permissions()
        except OpenCodeError as exc:
            raise HTTPException(503, "OpenCode is unavailable") from exc
        return [
            item for item in permissions if item.get("sessionID") == chat["opencode_session_id"]
        ]

    @app.get("/api/chats/{chat_id}/mcp")
    async def chat_mcp(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        chat = owned_chat(db, principal, chat_id)
        if chat["opencode_session_id"].startswith("pending_"):
            return {"servers": [], "pending": True}
        try:
            client = await request.app.state.manager.ensure(chat["workspace_snapshot"])
            return {"servers": safe_mcp_statuses(await client.mcp_statuses())}
        except OpenCodeError as exc:
            raise HTTPException(503, "MCP status is unavailable") from exc

    @app.post("/api/chats/{chat_id}/messages", status_code=202)
    async def send_message(
        chat_id: str,
        body: MessageBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        chat = owned_chat(db, principal, chat_id)
        with submission(db, request, principal["user"], body) as result:
            if not result:
                result.update(enqueue(db, settings, chat, principal["user"]["id"], body) or {})
        return result

    @app.get("/api/queue/me")
    async def my_queue(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        user = principal["user"]
        kinds = allowed_item_kinds(user)
        workspace = user.get("workspace_path")
        positions = db.queue_positions()
        return [
            public_queue_job(job, positions.get(job["id"]))
            for job in db.list_jobs(user["id"])
            if not job.get("chat_deleted_at") and job_kind(job) in kinds
            # Like /api/me/items: folder chats and questions belong to one workspace,
            # task runs follow the user.
            and (job_kind(job) == "task" or job["workspace_snapshot"] == workspace)
        ]

    @app.get("/api/jobs/{job_id}")
    async def get_job(
        job_id: str,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        job = db.get_job(job_id)
        if not job or (
            principal["user"]["role"] != "admin"
            and (
                job["effective_user_id"] != principal["user"]["id"] or job.get("task_id")
                or job.get("chat_deleted_at")
            )
        ):
            raise HTTPException(404, "Job not found")
        if job.get("consultation") and principal["user"]["role"] != "admin":
            chat = db.get_chat(job["chat_id"])
            if chat["workspace_snapshot"] != principal["user"]["workspace_path"]:
                raise HTTPException(404, "Job not found")
            return public_consultation_job(job)
        return job

    @app.delete("/api/jobs/{job_id}", status_code=204)
    async def cancel_job(
        job_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        owner = None if principal["user"]["role"] == "admin" else principal["user"]["id"]
        job = db.get_job(job_id)
        if not job or (owner and (job["effective_user_id"] != owner or job.get("task_id"))):
            raise HTTPException(404, "Job not found")
        if not db.cancel_queued_job(job_id, owner):
            raise HTTPException(409, "Only queued jobs can be cancelled")
        await request.app.state.events.publish({"type": "job.updated", "job": db.get_job(job_id)})

    @app.post("/api/chats/{chat_id}/abort", status_code=202)
    async def abort_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, bool]:
        owned_chat(db, principal, chat_id)
        active = next((job for job in db.active_jobs() if job["chat_id"] == chat_id), None)
        if not active:
            raise HTTPException(409, "Chat is not running")
        job = await request.app.state.dispatcher.stop_job(active["id"])
        return {"aborted": bool(job and job["status"] == "cancelled")}

    @app.get("/api/me/items")
    async def my_items(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        user = principal["user"]
        rows = db.list_items(
            owner_id=user["id"],
            workspace=user.get("workspace_path"),
            kinds=allowed_item_kinds(user),
        )
        return [public_item(row) for row in rows]

    @app.get("/api/admin/requests")
    async def admin_requests(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        user: str = "",
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        kind: Annotated[str, Query(alias="type")] = "",
        task: str | None = None,
        q: str = "",
        timezone: str = "UTC",
    ) -> list[dict[str, Any]]:
        kinds = list(dict.fromkeys(part.strip() for part in kind.split(",") if part.strip()))
        if unknown := [value for value in kinds if value not in ITEM_KINDS]:
            raise HTTPException(422, f"Unknown type: {', '.join(unknown)}")
        try:
            created_from, created_to = time_range(start, end, timezone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        logins = [normalize_login(part) for part in user.split(",") if part.strip()]
        rows = db.list_items(
            owner_logins=logins or None,
            kinds=kinds or None,
            task_id=task or None,
            created_from=created_from,
            created_to=created_to,
            include_deleted=True,
        )
        tokens = search_tokens(q)
        result = []
        for row in rows:
            request_row = public_admin_request(row)
            if matches_search(
                tokens, request_row["title"], request_row["user"]["login"],
                request_row["task_title"],
            ):
                result.append(request_row)
        return result

    def chat_action_audit(
        db: Database, user: dict[str, Any], action: str, item: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db.audit(
            user["id"], action, effective_user_id=user["id"], object_type="chat",
            object_id=item["chat_id"],
            metadata={"kind": public_item(item)["kind"], **(metadata or {})},
        )

    @app.post("/api/chats/{chat_id}/archive")
    async def archive_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        item = visible_item(db, user, chat_id)
        # Archiving never stops work; it only moves the chat out of the main lists.
        if db.archive_chat(chat_id):
            chat_action_audit(
                db, user, "chat.archive", item,
                {"snoozed_until": item["snoozed_until"]} if item["snoozed_until"] else None,
            )
            await publish_chat_updated(db, request.app.state.events, chat_id)
            item = visible_item(db, user, chat_id)
        return public_item(item)

    @app.post("/api/chats/{chat_id}/unarchive")
    async def unarchive_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        item = visible_item(db, user, chat_id)
        if (
            item["archived_at"] and item_kind(item) != "task"
            and db.get_chat(chat_id).get("archived_reason")
        ):
            raise HTTPException(409, "Chat was archived after a workspace change")
        if db.unarchive_chat(chat_id):
            chat_action_audit(db, user, "chat.unarchive", item)
            await publish_chat_updated(db, request.app.state.events, chat_id)
            item = visible_item(db, user, chat_id)
        return public_item(item)

    @app.post("/api/chats/{chat_id}/snooze")
    async def snooze_chat(
        chat_id: str,
        body: SnoozeBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        item = visible_item(db, user, chat_id)
        if body.until <= datetime.now(UTC):
            raise HTTPException(422, "Snooze time must be in the future")
        if item["archived_at"]:
            raise HTTPException(409, "Archived chat cannot be snoozed")
        until = body.until.astimezone(UTC).isoformat()
        if not db.snooze_chat(chat_id, until):
            raise HTTPException(409, "Archived chat cannot be snoozed")
        chat_action_audit(db, user, "chat.snooze", item, {"until": until})
        await publish_chat_updated(db, request.app.state.events, chat_id)
        return public_item(visible_item(db, user, chat_id))

    @app.post("/api/chats/{chat_id}/unsnooze")
    async def unsnooze_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        item = visible_item(db, user, chat_id)
        if db.unsnooze_chat(chat_id):
            chat_action_audit(db, user, "chat.unsnooze", item)
            await publish_chat_updated(db, request.app.state.events, chat_id)
            item = visible_item(db, user, chat_id)
        return public_item(item)

    @app.delete("/api/chats/{chat_id}", status_code=204)
    async def delete_chat(
        chat_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> Response:
        user = principal["user"]
        item = visible_item(db, user, chat_id)
        stopped: list[str] = []
        # Unfinished work goes through the regular stop path before the chat disappears.
        for job in db.unfinished_chat_jobs(chat_id):
            await stop_job_audited(request, db, user["id"], job, {"reason": "chat.delete"})
            stopped.append(job["id"])
        if db.delete_chat(chat_id):
            chat_action_audit(db, user, "chat.delete", item, {"stopped_job_ids": stopped})
            await publish_chat_deleted(db, request.app.state.events, chat_id)
        return Response(status_code=204)

    @app.post("/api/chats/{chat_id}/permissions/{permission_id}")
    async def respond_permission(
        chat_id: str,
        permission_id: str,
        body: PermissionBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, bool]:
        if body.response not in {"once", "always", "reject"}:
            raise HTTPException(422, "Invalid permission response")
        chat = owned_chat(db, principal, chat_id)
        try:
            client = await request.app.state.manager.ensure(chat["workspace_snapshot"])
            accepted = await client.respond_permission(
                chat["opencode_session_id"],
                permission_id,
                body.response,
                body.remember and body.response == "always",
            )
        except OpenCodeError as exc:
            raise HTTPException(503, "OpenCode is unavailable") from exc
        if principal["user"]["role"] == "admin":
            db.audit(
                principal["user"]["id"],
                "permission.admin_response",
                effective_user_id=chat["owner_user_id"],
                object_type="chat",
                object_id=chat_id,
                metadata={"response": body.response},
            )
        return {"accepted": accepted}

    @app.get("/api/admin/users")
    async def admin_users(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return db.list_users()

    @app.post("/api/admin/users", status_code=201)
    async def admin_create_user(
        body: UserCreateBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
        config: Annotated[Settings, Depends(settings_dep)],
    ) -> dict[str, Any]:
        workspace = checked_workspace(db, config, body.workspace_path, role=body.role)
        if workspace:
            try:
                workspace = str(config.ensure_workspace(workspace))
            except ConfigError as exc:
                raise HTTPException(422, str(exc)) from exc
        mattermost_username, mattermost_user_id = await mattermost_identity(
            config, body.mattermost_username
        )
        try:
            user = db.create_user(
                body.login, body.password, body.role, workspace,
                tasks_only=body.tasks_only,
                mattermost_username=mattermost_username,
                mattermost_user_id=mattermost_user_id,
                actor_id=principal["user"]["id"],
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Login or Mattermost user already exists") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.audit(principal["user"]["id"], "user.create", object_type="user", object_id=user["id"])
        return public_user(user)

    @app.patch("/api/admin/users/{user_id}")
    async def admin_update_user(
        user_id: str,
        body: UserUpdateBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
        config: Annotated[Settings, Depends(settings_dep)],
    ) -> dict[str, Any]:
        expected = edit_base(db, "users", user_id, body.updated_at)
        current = db.get_user(user_id)
        if not current:
            raise edit_error(None)
        role = body.role or current["role"]
        path_candidate = (
            body.workspace_path if body.workspace_path is not None else current["workspace_path"]
        )
        workspace = checked_workspace(
            db, config, path_candidate, role=role, exclude_user_id=user_id
        )
        workspace_changed = workspace != current["workspace_path"]
        if workspace_changed and db.has_pending_jobs(user_id):
            raise HTTPException(409, "Workspace cannot change while jobs are queued or active")
        if workspace:
            try:
                workspace = str(config.ensure_workspace(workspace))
            except ConfigError as exc:
                raise HTTPException(422, str(exc)) from exc
        updates: dict[str, Any] = {}
        if "max_active_jobs" in body.model_fields_set:
            if (
                body.max_active_jobs is not None
                and body.max_active_jobs > config.max_active_sessions_per_user_hard_limit
            ):
                raise HTTPException(
                    422,
                    "Value exceeds MAX_ACTIVE_SESSIONS_PER_USER_HARD_LIMIT"
                    f" ({config.max_active_sessions_per_user_hard_limit})",
                )
            updates["max_active_jobs"] = body.max_active_jobs
        if "mattermost_username" in body.model_fields_set:
            mattermost_username, mattermost_user_id = await mattermost_identity(
                config, body.mattermost_username
            )
            updates["mattermost_username"] = mattermost_username
            updates["mattermost_user_id"] = mattermost_user_id
        try:
            updated = db.update_user(
                user_id, role=role, workspace_path=workspace, status=body.status,
                tasks_only=body.tasks_only, actor_id=principal["user"]["id"],
                expected_updated_at=expected, **updates,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Mattermost user is already assigned") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not updated:
            raise edit_error(db.edit_state("users", user_id))
        changed_chat_ids = (
            db.apply_workspace_change(user_id, workspace)
            if workspace_changed and workspace else []
        )
        db.audit(principal["user"]["id"], "user.update", object_type="user", object_id=user_id)
        for chat_id in changed_chat_ids:
            await publish_chat_updated(db, request.app.state.events, chat_id)
        return public_user(updated)  # type: ignore[arg-type]

    @app.post("/api/admin/users/batch")
    async def admin_batch_users(
        body: UserBatchBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
        config: Annotated[Settings, Depends(settings_dep)],
    ) -> dict[str, Any]:
        """Block or unblock several users; each one succeeds or fails on its own."""
        admin_id = principal["user"]["id"]
        target = "disabled" if body.status == "blocked" else "active"
        results: list[dict[str, Any]] = []
        for user_id in dict.fromkeys(body.ids):
            user = db.get_user(user_id)
            result = {
                "id": user_id, "login": user["login"] if user else None,
                "ok": False, "skipped": False, "reason": None,
            }
            results.append(result)
            if not user:
                result["reason"] = "not_found"
            elif user_id == admin_id:
                result.update(skipped=True, reason="self")
            elif user["status"] == target:
                result.update(ok=True, skipped=True, reason="already")
            elif (
                target == "active"
                and user.get("workspace_path")
                and not config.allow_shared_workspaces
                and db.workspace_in_use(user["workspace_path"], exclude_user_id=user_id)
            ):
                # Same exclusivity rule as the single PATCH (checked_workspace):
                # an active user may not share a workspace with another active user.
                result["reason"] = "workspace_in_use"
            else:
                try:
                    db.update_user(user_id, status=target, actor_id=admin_id)
                except ValueError:
                    result["reason"] = "invalid"
                    continue
                db.audit(
                    admin_id, "user.update", object_type="user", object_id=user_id,
                    metadata={"status": target, "batch": True},
                )
                result["ok"] = True
        return {"results": results}

    @app.post("/api/admin/users/{user_id}/reset-password", status_code=204)
    async def admin_reset_password(
        user_id: str,
        body: PasswordResetBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        try:
            db.reset_password(user_id, body.password, principal["user"]["id"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.audit(
            principal["user"]["id"], "user.password_reset", object_type="user", object_id=user_id
        )

    @app.get("/api/admin/users/{user_id}/chats")
    async def admin_user_chats(
        user_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        db.audit(
            principal["user"]["id"],
            "chats.admin_list",
            effective_user_id=user_id,
            object_type="user",
            object_id=user_id,
        )
        return db.list_chats(user_id, include_deleted=True)

    @app.post("/api/admin/users/{user_id}/chats/title-sync")
    async def admin_sync_chat_titles_from_opencode(
        user_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        return await sync_chat_titles(request, db, db.list_chats(user_id, include_deleted=True))

    @app.get("/api/admin/users/{user_id}/task-runs")
    async def admin_user_task_runs(
        user_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        db.audit(
            principal["user"]["id"],
            "task_runs.admin_list",
            effective_user_id=user_id,
            object_type="user",
            object_id=user_id,
        )
        return [
            {**public_task_run(job), "owner_login": job["effective_login"]}
            for job in db.list_task_runs(user_id, include_deleted=True)
        ]

    @app.post("/api/admin/users/{user_id}/task-runs/title-sync")
    async def admin_sync_user_task_run_titles_from_opencode(
        user_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        return await sync_task_titles(request, db, db.list_task_runs(user_id, include_deleted=True))

    @app.get("/api/admin/users/{user_id}/tasks")
    async def admin_user_tasks(
        user_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        db.audit(
            principal["user"]["id"],
            "task_access.admin_list",
            effective_user_id=user_id,
            object_type="user",
            object_id=user_id,
        )
        return [
            {**task, "assigned": bool(task["assigned"])}
            for task in db.list_user_task_access(user_id)
        ]

    @app.put("/api/admin/users/{user_id}/task-access")
    async def admin_set_user_task_access(
        user_id: str,
        body: UserTaskAccessBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        if not db.get_user(user_id):
            raise HTTPException(404, "User not found")
        for task_id in dict.fromkeys(body.task_ids):
            if not db.get_task(task_id):
                raise HTTPException(422, f"Unknown task: {task_id}")
        tasks = db.set_user_task_access(user_id, body.task_ids)
        db.audit(
            principal["user"]["id"],
            "task_access.update",
            effective_user_id=user_id,
            object_type="user",
            object_id=user_id,
            metadata={"task_ids": sorted(set(body.task_ids))},
        )
        return [{**task, "assigned": bool(task["assigned"])} for task in tasks]

    @app.post("/api/admin/chats/{chat_id}/messages", status_code=202)
    async def admin_impersonated_message(
        chat_id: str,
        body: MessageBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        chat = owned_chat(db, principal, chat_id)
        with submission(db, request, principal["user"], body) as result:
            if result:
                return result
            job = enqueue(db, settings, chat, principal["user"]["id"], body)
            db.audit(
                principal["user"]["id"], "message.impersonated",
                effective_user_id=chat["owner_user_id"], object_type="job",
                object_id=job["id"] if job else None, metadata={"chat_id": chat_id},
            )
            result.update(job or {})
        return result

    @app.post("/api/admin/jobs/{job_id}/stop", status_code=202)
    async def admin_stop_job(
        job_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        db.audit(
            principal["user"]["id"], "job.stop.request",
            effective_user_id=job["effective_user_id"], object_type="job", object_id=job_id,
            metadata={"previous_status": job["status"], "task_id": job.get("task_id")},
        )
        result = await request.app.state.dispatcher.stop_job(job_id)
        if result and result["status"] == "cancelled" and job["status"] == "queued":
            db.audit(
                principal["user"]["id"], "job.stop.completed",
                effective_user_id=job["effective_user_id"], object_type="job", object_id=job_id,
            )
        return result  # type: ignore[return-value]

    @app.get("/api/admin/statistics")
    async def admin_statistics(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        day: date | None = None,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, "Unknown timezone") from None
        selected_day = day or datetime.now(zone).date()
        if selected_day in {date.min, date.max}:
            raise HTTPException(422, "Date is out of range")
        start = datetime.combine(selected_day, time.min, zone).astimezone(UTC)
        end = datetime.combine(selected_day + timedelta(days=1), time.min, zone).astimezone(UTC)
        return {
            "day": selected_day.isoformat(),
            "timezone": timezone,
            **db.task_statistics(start.isoformat(), end.isoformat()),
        }

    @app.get("/api/admin/statistics/runs")
    async def admin_statistics_runs(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        kind: Annotated[str, Query(alias="type")] = "",
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        try:
            created_from, created_to = time_range(start, end, timezone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        kind = kind.strip()
        rows = db.list_statistics_runs(
            kind=kind if kind in {"question", "task"} else None,
            task_id=kind if kind not in {"", "question", "task"} else None,
            created_from=created_from,
            created_to=created_to,
        )
        return {
            "items": [
                {
                    "id": row["id"],
                    "kind": "task" if row["task_id"] else "question",
                    "task_id": row["task_id"],
                    "task_title": row["task_title"],
                    "login": row["login"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "outcome": row["status"] if row["status"] in TERMINAL_OUTCOMES else None,
                }
                for row in rows
            ],
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/admin/queue")
    async def admin_queue(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        jobs = db.list_queue_overview_jobs()
        return {
            "limit": request.app.state.dispatcher.limit,
            "env_limit": settings.max_active_sessions,
            "hard_limit": settings.max_active_sessions_hard_limit,
            "active": db.active_slot_count(),
            "queued": sum(job["status"] == "queued" for job in jobs),
            "default_chat_queue_id": db.default_chat_queue_id(),
            "queues": db.list_work_queues(include_archived=True),
            "jobs": [
                {**job, "chat_title": task_run_display_title(job)} if job.get("task_id") else job
                for job in jobs
            ],
        }

    @app.get("/api/admin/queues")
    async def list_queues(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return db.list_work_queues(include_archived=True)

    @app.post("/api/admin/queues", status_code=201)
    async def create_queue(
        body: WorkQueueBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        if body.max_workers > settings.max_active_sessions_hard_limit:
            raise HTTPException(422, "Value exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT")
        try:
            queue = db.create_work_queue(
                body.name, body.priority, body.max_workers, principal["user"]["id"]
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.audit(
            principal["user"]["id"], "queue.create", object_type="queue",
            object_id=queue["id"],
        )
        request.app.state.dispatcher.wake()
        return queue

    @app.patch("/api/admin/queues/{queue_id}")
    async def update_queue(
        queue_id: str,
        body: WorkQueuePatchBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        if body.max_workers > settings.max_active_sessions_hard_limit:
            raise HTTPException(422, "Value exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT")
        expected = edit_base(db, "work_queues", queue_id, body.updated_at)
        try:
            queue = db.update_work_queue(
                queue_id, name=body.name, priority=body.priority,
                max_workers=body.max_workers, actor_id=principal["user"]["id"],
                expected_updated_at=expected,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not queue:
            raise edit_error(db.edit_state("work_queues", queue_id))
        db.audit(
            principal["user"]["id"], "queue.update", object_type="queue",
            object_id=queue_id,
        )
        request.app.state.dispatcher.wake()
        return queue

    @app.delete("/api/admin/queues/{queue_id}", status_code=204)
    async def archive_queue(
        queue_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        try:
            archived = db.archive_work_queue(queue_id, principal["user"]["id"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not archived:
            raise HTTPException(404, "Queue not found")
        db.audit(
            principal["user"]["id"], "queue.archive", object_type="queue",
            object_id=queue_id,
        )
        request.app.state.dispatcher.wake()

    @app.put("/api/admin/settings/default-chat-queue")
    async def set_default_chat_queue(
        body: QueueRoutingBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, str]:
        try:
            db.set_queue_setting(
                "default_chat_queue_id", body.queue_id, principal["user"]["id"]
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.audit(
            principal["user"]["id"], "queue.chat-default.update",
            object_type="queue", object_id=body.queue_id,
        )
        return {"queue_id": body.queue_id}

    @app.get("/api/admin/mcp")
    async def admin_mcp(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
    ) -> list[dict[str, Any]]:
        async def inspect(instance: Any) -> dict[str, Any]:
            try:
                async with request.app.state.manager.use(
                    str(instance.workspace), wake=False, activity=False
                ) as client:
                    if client is None:
                        return {"workspace": str(instance.workspace), "servers": [], "idle": True}
                    servers = safe_mcp_statuses(await client.mcp_statuses())
                return {"workspace": str(instance.workspace), "servers": servers}
            except OpenCodeError:
                return {"workspace": str(instance.workspace), "servers": [], "unavailable": True}

        return await asyncio.gather(
            *(inspect(instance) for instance in request.app.state.manager.instances())
        )

    @app.patch("/api/admin/settings/concurrency")
    async def set_concurrency(
        body: ConcurrencyBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, int]:
        if body.value > settings.max_active_sessions_hard_limit:
            raise HTTPException(422, "Value exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT")
        db.set_concurrency_override(body.value, principal["user"]["id"])
        db.audit(principal["user"]["id"], "concurrency.update", metadata={"value": body.value})
        request.app.state.dispatcher.wake()
        return {"value": body.value}

    @app.delete("/api/admin/settings/concurrency/override", status_code=204)
    async def reset_concurrency(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        db.clear_concurrency_override()
        db.audit(principal["user"]["id"], "concurrency.reset")
        request.app.state.dispatcher.wake()

    @app.get("/api/admin/templates")
    async def admin_templates(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return db.list_templates(published_only=False)

    @app.post("/api/admin/templates", status_code=201)
    async def admin_create_template(
        body: TemplateBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        template = db.create_template(
            principal["user"]["id"], body.title, body.description, body.content, body.is_published
        )
        db.audit(
            principal["user"]["id"],
            "template.create",
            object_type="template",
            object_id=template["id"],
        )
        return template

    @app.patch("/api/admin/templates/{template_id}")
    async def admin_update_template(
        template_id: str,
        body: TemplatePatchBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        template = db.update_template(template_id, fields)
        if not template:
            raise HTTPException(404, "Template not found")
        db.audit(
            principal["user"]["id"],
            "template.update",
            object_type="template",
            object_id=template_id,
        )
        return template

    @app.delete("/api/admin/templates/{template_id}", status_code=204)
    async def admin_delete_template(
        template_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> None:
        if not db.delete_template(template_id):
            raise HTTPException(404, "Template not found")
        db.audit(
            principal["user"]["id"],
            "template.delete",
            object_type="template",
            object_id=template_id,
        )

    @app.get("/api/admin/audit")
    async def admin_audit(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        user: str = "",
        action: str = "",
        q: str = "",
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        try:
            created_from, created_to = time_range(start, end, timezone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        before_id = None
        if cursor:
            try:
                before_id = int(cursor)
            except ValueError:
                raise HTTPException(422, "Invalid cursor") from None
        rows = db.iter_audit(
            logins=[normalize_login(part) for part in user.split(",") if part.strip()],
            actions=[part.strip() for part in action.split(",") if part.strip()],
            created_from=created_from,
            created_to=created_to,
            before_id=before_id,
        )
        tokens = search_tokens(q)
        items: list[dict[str, Any]] = []
        for row in rows:
            if matches_search(
                tokens, row["action"], row["object_type"], row["object_id"],
                row["actor_login"], row["effective_login"], row["metadata_json"],
            ):
                items.append(row)
                if len(items) > limit:
                    break
        more = len(items) > limit
        items = items[:limit]
        return {"items": items, "next_cursor": str(items[-1]["id"]) if more else None}

    @app.get("/api/events")
    async def events(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> StreamingResponse:
        async def filtered_stream():
            async for event in request.app.state.events.subscribe():
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                refreshed = db.get_user_for_token(principal["token"])
                if not refreshed:
                    return
                user = refreshed[0]
                visible = event_for_user(db, user, event)
                if visible is not None:
                    yield f"data: {json.dumps(visible, ensure_ascii=False)}\n\n"

        return StreamingResponse(filtered_stream(), media_type="text/event-stream")

    register_consultations(
        app, settings, database, current_principal, csrf_principal,
        admin_read_principal, admin_principal, validate_agent_model,
    )

    # Registered last: the client-side router owns every other page URL.
    @app.get("/{path:spa_path}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        return FileResponse(PACKAGE_DIR / "static" / "index.html")

    return app
