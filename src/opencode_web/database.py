from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import hash_password, normalize_login, token_digest


def clean_input_hint(value: str | None) -> str | None:
    """The run-form placeholder; blank means none."""
    hint = (value or "").strip()
    return hint or None


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


UNSET = object()
DEFAULT_QUEUE_ID = "default"
ACTIVE_JOB_STATUSES = (
    "dispatching", "running", "waiting_permission", "waiting_question", "cancelling",
)
UNFINISHED_JOB_STATUSES = ("queued", *ACTIVE_JOB_STATUSES)
# One row per chat for every sidebar kind. The representative job is the active one,
# then the next queued one, then the latest; a task chat has exactly one job.
ITEM_QUERY = """SELECT c.id AS chat_id, c.kind AS chat_kind, c.consultation,
       c.title AS chat_title, c.workspace_snapshot, c.owner_user_id,
       u.login AS owner_login, c.created_at, c.updated_at, c.archived_at,
       c.archived_reason, c.snoozed_until, c.deleted_at, j.id AS job_id, j.status AS job_status,
       j.task_id, j.task_title, j.plan_first, j.phase2_message_id
FROM chats c
JOIN users u ON u.id = c.owner_user_id
LEFT JOIN queue_jobs j ON j.id = (
    SELECT q.id FROM queue_jobs q WHERE q.chat_id = c.id
    ORDER BY CASE
        WHEN q.status IN ('dispatching', 'running', 'waiting_permission',
                          'waiting_question', 'cancelling') THEN 0
        WHEN q.status = 'queued' THEN 1 ELSE 2 END,
        q.sequence DESC
    LIMIT 1
)"""
# Who last changed an admin-editable object, and whether it is gone. Deleting a task or
# archiving a queue is its last change, so `updated_by` then names who removed it.
EDIT_STATE_QUERIES = {
    "users": """SELECT o.updated_at, editor.login AS updated_by, 0 AS deleted
                FROM users o LEFT JOIN users editor ON editor.id = o.updated_by
                WHERE o.id = ?""",
    "tasks": """SELECT o.updated_at, editor.login AS updated_by,
                       o.deleted_at IS NOT NULL AS deleted
                FROM tasks o LEFT JOIN users editor ON editor.id = o.updated_by
                WHERE o.id = ?""",
    "work_queues": """SELECT o.updated_at, editor.login AS updated_by,
                             o.status = 'archived' AS deleted
                      FROM work_queues o LEFT JOIN users editor ON editor.id = o.updated_by
                      WHERE o.id = ?""",
}


class StaleWrite(Exception):
    """The row changed or disappeared after the caller read it."""


class ChatDeleted(LookupError):
    """The chat was deleted before the message could be queued."""


AUDIT_QUERY = """SELECT a.*, actor.login AS actor_login, effective.login AS effective_login
FROM audit_log a
LEFT JOIN users actor ON actor.id = a.actor_user_id
LEFT JOIN users effective ON effective.id = a.effective_user_id"""
ITEM_KIND_CONDITIONS = {
    "question": "(c.kind = 'chat' AND c.consultation = 1)",
    "folder": "(c.kind = 'chat' AND c.consultation = 0)",
    "task": "c.kind = 'task'",
}
MATTERMOST_USER_ID_RE = re.compile(r"^[a-z0-9]{26}$")
MATTERMOST_USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,21}$")


def normalize_mattermost_user_id(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if not MATTERMOST_USER_ID_RE.fullmatch(normalized):
        raise ValueError("Mattermost user ID must contain 26 lowercase letters or digits")
    return normalized


def normalize_mattermost_username(value: str | None) -> str | None:
    normalized = (value or "").strip().lower().removeprefix("@")
    if not normalized:
        return None
    if not MATTERMOST_USERNAME_RE.fullmatch(normalized):
        raise ValueError(
            "Mattermost username must start with a letter and contain 3 to 22 "
            "lowercase letters, digits, dots, dashes, or underscores"
        )
    return normalized


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    tasks_only INTEGER NOT NULL DEFAULT 0 CHECK (tasks_only IN (0, 1)),
    mattermost_username TEXT,
    mattermost_user_id TEXT,
    workspace_path TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    auth_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    is_published INTEGER NOT NULL DEFAULT 0,
    created_by_admin_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_queues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    max_workers INTEGER CHECK (max_workers IS NULL OR max_workers >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider_id TEXT,
    model_id TEXT,
    model_variant TEXT,
    queue_id TEXT NOT NULL DEFAULT 'default' REFERENCES work_queues(id),
    is_published INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'all',
    created_by_admin_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS task_audience (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_task_audience_user ON task_audience(user_id);

CREATE TABLE IF NOT EXISTS task_successors (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    next_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, next_task_id),
    CHECK (task_id != next_task_id)
);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    opencode_session_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    workspace_snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'task')),
    archived_reason TEXT,
    preferred_agent TEXT,
    preferred_provider_id TEXT,
    preferred_model_id TEXT,
    preferred_model_variant TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_jobs (
    id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    chat_id TEXT NOT NULL REFERENCES chats(id),
    session_id TEXT NOT NULL,
    effective_user_id TEXT NOT NULL REFERENCES users(id),
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    workspace_snapshot TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider_id TEXT,
    model_id TEXT,
    model_variant TEXT,
    queue_id TEXT NOT NULL DEFAULT 'default' REFERENCES work_queues(id),
    task_id TEXT REFERENCES tasks(id),
    task_title TEXT,
    task_initial_title TEXT,
    user_input TEXT,
    status TEXT NOT NULL CHECK (status IN (
      'queued', 'dispatching', 'running', 'waiting_permission', 'waiting_question',
      'completed', 'failed', 'cancelling', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status_sequence ON queue_jobs(status, sequence);
CREATE INDEX IF NOT EXISTS idx_queue_user ON queue_jobs(effective_user_id, sequence);
CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_run_results (
    job_id TEXT PRIMARY KEY REFERENCES queue_jobs(id) ON DELETE CASCADE,
    message_id TEXT,
    text TEXT,
    saved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('task_completed', 'consultation_answer_ready')),
    object_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    mattermost_user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    link_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    post_id TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notification_due
ON notification_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS submissions (
    actor_id TEXT NOT NULL REFERENCES users(id),
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    PRIMARY KEY (actor_id, request_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by TEXT REFERENCES users(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id TEXT REFERENCES users(id),
    effective_user_id TEXT REFERENCES users(id),
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path, session_secret: str):
        self.path = path
        self.session_secret = session_secret
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(SCHEMA)
            now = utcnow()
            self._connection.execute(
                """INSERT OR IGNORE INTO work_queues
                   (id, name, priority, max_workers, status, is_system, created_at, updated_at)
                   VALUES (?, 'Основная', 0, NULL, 'active', 1, ?, ?)""",
                (DEFAULT_QUEUE_ID, now, now),
            )
            user_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "tasks_only" not in user_columns:
                self._connection.execute(
                    "ALTER TABLE users ADD COLUMN tasks_only INTEGER NOT NULL DEFAULT 0"
                )
            if "max_active_jobs" not in user_columns:
                self._connection.execute("ALTER TABLE users ADD COLUMN max_active_jobs INTEGER")
            if "mattermost_user_id" not in user_columns:
                self._connection.execute("ALTER TABLE users ADD COLUMN mattermost_user_id TEXT")
            if "mattermost_username" not in user_columns:
                self._connection.execute("ALTER TABLE users ADD COLUMN mattermost_username TEXT")
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_mattermost_user_id "
                "ON users(mattermost_user_id) WHERE mattermost_user_id IS NOT NULL"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_mattermost_username "
                "ON users(mattermost_username) WHERE mattermost_username IS NOT NULL"
            )
            # A process may stop after claiming an outbox row and before reporting
            # success. Retrying with the same Mattermost pending_post_id is safe.
            self._connection.execute(
                "UPDATE notification_outbox SET status = 'pending' WHERE status = 'sending'"
            )
            task_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "visibility" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN visibility TEXT NOT NULL DEFAULT 'all'"
                )
            if "plan_first" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN plan_first INTEGER NOT NULL DEFAULT 0"
                )
            if "input_hint" not in task_columns:
                self._connection.execute("ALTER TABLE tasks ADD COLUMN input_hint TEXT")
            if "queue_id" not in task_columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN queue_id TEXT REFERENCES work_queues(id)"
                )
                self._connection.execute(
                    "UPDATE tasks SET queue_id = ? WHERE queue_id IS NULL", (DEFAULT_QUEUE_ID,)
                )
            for table in ("users", "tasks", "work_queues"):
                columns = {
                    row[1]
                    for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "updated_by" not in columns:
                    self._connection.execute(f"ALTER TABLE {table} ADD COLUMN updated_by TEXT")
            chat_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "consultation" not in chat_columns:
                self._connection.execute(
                    "ALTER TABLE chats ADD COLUMN consultation INTEGER NOT NULL DEFAULT 0"
                )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS consultation_answers (
                    job_id TEXT PRIMARY KEY REFERENCES queue_jobs(id),
                    content TEXT NOT NULL
                )"""
            )
            if "kind" not in chat_columns:
                self._connection.execute(
                    "ALTER TABLE chats ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'"
                )
            for column in (
                "preferred_agent",
                "preferred_provider_id",
                "preferred_model_id",
                "preferred_model_variant",
            ):
                if column not in chat_columns:
                    self._connection.execute(f"ALTER TABLE chats ADD COLUMN {column} TEXT")
            for column in ("archived_at", "snoozed_until", "deleted_at"):
                if column not in chat_columns:
                    self._connection.execute(f"ALTER TABLE chats ADD COLUMN {column} TEXT")
            # Chats archived automatically before archived_at existed stay archived.
            self._connection.execute(
                """UPDATE chats SET archived_at = updated_at
                   WHERE archived_reason IS NOT NULL AND archived_at IS NULL"""
            )
            job_columns = {
                row[1]
                for row in self._connection.execute("PRAGMA table_info(queue_jobs)").fetchall()
            }
            for column, definition in (
                ("task_id", "TEXT REFERENCES tasks(id)"),
                ("task_title", "TEXT"),
                ("user_input", "TEXT"),
                ("pipeline_json", "TEXT"),
                ("parent_job_id", "TEXT REFERENCES queue_jobs(id)"),
                ("session_pending", "INTEGER NOT NULL DEFAULT 0"),
                ("plan_first", "INTEGER NOT NULL DEFAULT 0"),
                ("phase2_message_id", "TEXT"),
                ("resume_content", "TEXT"),
                ("resume_from_message_id", "TEXT"),
                ("resume_monitor", "INTEGER NOT NULL DEFAULT 0"),
                ("queue_id", "TEXT REFERENCES work_queues(id)"),
            ):
                if column not in job_columns:
                    self._connection.execute(
                        f"ALTER TABLE queue_jobs ADD COLUMN {column} {definition}"
                    )
            self._connection.execute(
                "UPDATE queue_jobs SET queue_id = ? WHERE queue_id IS NULL", (DEFAULT_QUEUE_ID,)
            )
            if "task_initial_title" not in job_columns:
                with self.transaction() as connection:
                    connection.execute("ALTER TABLE queue_jobs ADD COLUMN task_initial_title TEXT")
                    connection.execute(
                        """UPDATE queue_jobs SET task_initial_title = (
                             SELECT title FROM chats WHERE chats.id = queue_jobs.chat_id
                           ) WHERE task_id IS NOT NULL"""
                    )
            self._migrate_queue_job_status_constraint()
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_status_sequence "
                "ON queue_jobs(status, sequence)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_user "
                "ON queue_jobs(effective_user_id, sequence)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_route "
                "ON queue_jobs(queue_id, status, sequence)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_task ON queue_jobs(task_id, sequence)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_chat ON queue_jobs(chat_id, sequence)"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_child "
                "ON queue_jobs(parent_job_id, task_id) WHERE parent_job_id IS NOT NULL"
            )
            self._connection.execute(
                """UPDATE chats SET
                     preferred_agent = COALESCE(preferred_agent, (
                       SELECT agent FROM queue_jobs
                       WHERE queue_jobs.chat_id = chats.id ORDER BY sequence LIMIT 1
                     )),
                     preferred_provider_id = COALESCE(preferred_provider_id, (
                       SELECT provider_id FROM queue_jobs
                       WHERE queue_jobs.chat_id = chats.id ORDER BY sequence LIMIT 1
                     )),
                     preferred_model_id = COALESCE(preferred_model_id, (
                       SELECT model_id FROM queue_jobs
                       WHERE queue_jobs.chat_id = chats.id ORDER BY sequence LIMIT 1
                     )),
                     preferred_model_variant = COALESCE(preferred_model_variant, (
                       SELECT model_variant FROM queue_jobs
                       WHERE queue_jobs.chat_id = chats.id ORDER BY sequence LIMIT 1
                     ))"""
            )
            self._connection.execute(
                """UPDATE queue_jobs
                   SET message_id = 'msg_' || replace(message_id, '-', '')
                   WHERE status IN ('queued', 'dispatching') AND message_id NOT LIKE 'msg%'"""
            )

    def _migrate_queue_job_status_constraint(self) -> None:
        """Add waiting_question to SQLite's immutable status CHECK constraint."""
        schema = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'queue_jobs'"
        ).fetchone()[0]
        if "waiting_question" in schema:
            return
        columns = (
            "id", "sequence", "chat_id", "session_id", "effective_user_id",
            "actor_user_id", "workspace_snapshot", "message_id", "content", "agent",
            "provider_id", "model_id", "model_variant", "task_id", "task_title",
            "task_initial_title", "user_input", "status", "created_at", "started_at",
            "finished_at", "error_code", "pipeline_json", "parent_job_id",
            "session_pending", "plan_first", "phase2_message_id", "resume_content",
            "resume_from_message_id", "resume_monitor",
            "queue_id",
        )
        names = ", ".join(columns)
        self._connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.transaction() as connection:
                connection.execute("DROP TABLE IF EXISTS queue_jobs_new")
                connection.execute(
                    """CREATE TABLE queue_jobs_new (
                        id TEXT PRIMARY KEY,
                        sequence INTEGER NOT NULL UNIQUE,
                        chat_id TEXT NOT NULL REFERENCES chats(id),
                        session_id TEXT NOT NULL,
                        effective_user_id TEXT NOT NULL REFERENCES users(id),
                        actor_user_id TEXT NOT NULL REFERENCES users(id),
                        workspace_snapshot TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        content TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        provider_id TEXT,
                        model_id TEXT,
                        model_variant TEXT,
                        task_id TEXT REFERENCES tasks(id),
                        task_title TEXT,
                        task_initial_title TEXT,
                        user_input TEXT,
                        status TEXT NOT NULL CHECK (status IN (
                          'queued', 'dispatching', 'running', 'waiting_permission',
                          'waiting_question', 'completed', 'failed', 'cancelling', 'cancelled'
                        )),
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        error_code TEXT,
                        pipeline_json TEXT,
                        parent_job_id TEXT REFERENCES queue_jobs(id),
                        session_pending INTEGER NOT NULL DEFAULT 0,
                        plan_first INTEGER NOT NULL DEFAULT 0,
                        phase2_message_id TEXT,
                        resume_content TEXT,
                        resume_from_message_id TEXT,
                        resume_monitor INTEGER NOT NULL DEFAULT 0,
                        queue_id TEXT NOT NULL DEFAULT 'default' REFERENCES work_queues(id)
                    )"""
                )
                connection.execute(
                    f"INSERT INTO queue_jobs_new ({names}) SELECT {names} FROM queue_jobs"
                )
                connection.execute("DROP TABLE queue_jobs")
                connection.execute("ALTER TABLE queue_jobs_new RENAME TO queue_jobs")
        finally:
            self._connection.execute("PRAGMA foreign_keys=ON")
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("queue_jobs status migration broke foreign keys")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection.in_transaction:
                savepoint = "nested_" + uuid.uuid4().hex
                self._connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield self._connection
                except BaseException:
                    self._connection.execute(f"ROLLBACK TO {savepoint}")
                    raise
                finally:
                    self._connection.execute(f"RELEASE {savepoint}")
                return
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def _all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_users(self) -> int:
        row = self._one("SELECT COUNT(*) AS count FROM users")
        return int(row["count"]) if row else 0

    def create_user(
        self,
        login: str,
        password: str,
        role: str,
        workspace_path: str | None,
        *,
        tasks_only: bool = False,
        mattermost_username: str | None = None,
        mattermost_user_id: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        now = utcnow()
        normalized = normalize_login(login)
        if not normalized or len(normalized) > 80:
            raise ValueError("Login must contain 1 to 80 characters")
        if role not in {"user", "admin"}:
            raise ValueError("Invalid role")
        if role == "admin" and tasks_only:
            raise ValueError("An administrator cannot be restricted to tasks")
        if role == "user" and not workspace_path:
            raise ValueError("A user requires a workspace")
        mattermost_username = normalize_mattermost_username(mattermost_username)
        mattermost_user_id = normalize_mattermost_user_id(mattermost_user_id)
        if mattermost_username and not mattermost_user_id:
            raise ValueError("Mattermost username must be resolved before saving")
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO users
                   (id, login, password_hash, role, workspace_path, tasks_only,
                    mattermost_username, mattermost_user_id, created_at, updated_at,
                    updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    normalized,
                    hash_password(password),
                    role,
                    workspace_path,
                    int(tasks_only),
                    mattermost_username,
                    mattermost_user_id,
                    now,
                    now,
                    actor_id,
                ),
            )
        return self.get_user(user_id)  # type: ignore[return-value]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE login = ?", (normalize_login(login),))

    def list_users(self) -> list[dict[str, Any]]:
        return self._all(
            """SELECT id, login, role, tasks_only, mattermost_username,
                      mattermost_user_id, workspace_path,
                      status, max_active_jobs,
                      created_at, updated_at, last_login_at FROM users ORDER BY login"""
        )

    def workspace_in_use(self, workspace: str, exclude_user_id: str | None = None) -> bool:
        query = "SELECT 1 FROM users WHERE workspace_path = ? AND status = 'active'"
        params: tuple[Any, ...] = (workspace,)
        if exclude_user_id:
            query += " AND id != ?"
            params += (exclude_user_id,)
        return self._one(query, params) is not None

    def update_user(
        self,
        user_id: str,
        *,
        role: str | None = None,
        workspace_path: str | None = None,
        status: str | None = None,
        tasks_only: bool | None = None,
        max_active_jobs: Any = UNSET,
        mattermost_username: Any = UNSET,
        mattermost_user_id: Any = UNSET,
        actor_id: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """None when the user is missing or no longer at `expected_updated_at`."""
        current = self.get_user(user_id)
        if not current:
            return None
        new_role = role or current["role"]
        new_workspace = workspace_path if workspace_path is not None else current["workspace_path"]
        new_status = status or current["status"]
        new_tasks_only = bool(current["tasks_only"]) if tasks_only is None else tasks_only
        new_max_active_jobs = (
            current["max_active_jobs"] if max_active_jobs is UNSET else max_active_jobs
        )
        new_mattermost_username = (
            current.get("mattermost_username")
            if mattermost_username is UNSET
            else normalize_mattermost_username(mattermost_username)
        )
        new_mattermost_user_id = (
            current["mattermost_user_id"]
            if mattermost_user_id is UNSET
            else normalize_mattermost_user_id(mattermost_user_id)
        )
        if mattermost_username is not UNSET and new_mattermost_username is None:
            new_mattermost_user_id = None
        if new_mattermost_username and not new_mattermost_user_id:
            raise ValueError("Mattermost username must be resolved before saving")
        if new_max_active_jobs is not None:
            new_max_active_jobs = int(new_max_active_jobs)
            if new_max_active_jobs < 1:
                raise ValueError("max_active_jobs must be at least 1")
        if new_role == "admin" and new_tasks_only:
            raise ValueError("An administrator cannot be restricted to tasks")
        if new_role == "user" and not new_workspace:
            raise ValueError("A user requires a workspace")
        if new_role not in {"user", "admin"} or new_status not in {"active", "disabled"}:
            raise ValueError("Invalid user fields")
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE users SET role = ?, workspace_path = ?, status = ?, tasks_only = ?,
                   max_active_jobs = ?, mattermost_username = ?, mattermost_user_id = ?,
                   updated_at = ?, updated_by = ?,
                   auth_version = auth_version + CASE WHEN status != ? THEN 1 ELSE 0 END
                   WHERE id = ? AND (? IS NULL OR updated_at = ?)""",
                (
                    new_role,
                    new_workspace,
                    new_status,
                    int(new_tasks_only),
                    new_max_active_jobs,
                    new_mattermost_username,
                    new_mattermost_user_id,
                    utcnow(),
                    actor_id,
                    new_status,
                    user_id,
                    expected_updated_at,
                    expected_updated_at,
                ),
            )
        return self.get_user(user_id) if cursor.rowcount else None

    def reset_password(
        self, user_id: str, password: str, actor_id: str | None = None
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE users SET password_hash = ?, auth_version = auth_version + 1,
                   updated_at = ?, updated_by = ? WHERE id = ?""",
                (hash_password(password), utcnow(), actor_id, user_id),
            )
            connection.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    def create_auth_session(
        self, user: dict[str, Any], token: str, csrf: str, ttl_hours: int
    ) -> None:
        now = datetime.now(UTC)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO auth_sessions
                   (id, user_id, token_hash, csrf_token, auth_version, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    user["id"],
                    token_digest(token, self.session_secret),
                    csrf,
                    user["auth_version"],
                    (now + timedelta(hours=ttl_hours)).isoformat(),
                    now.isoformat(),
                ),
            )
            # Not an edit: leave updated_at alone so open admin forms do not conflict.
            connection.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), user["id"])
            )

    def get_user_for_token(self, token: str) -> tuple[dict[str, Any], str] | None:
        row = self._one(
            """SELECT u.*, s.csrf_token FROM auth_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND s.auth_version = u.auth_version
                 AND u.status = 'active'""",
            (token_digest(token, self.session_secret), utcnow()),
        )
        if not row:
            return None
        csrf = row.pop("csrf_token")
        return row, csrf

    def delete_auth_session(self, token: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (token_digest(token, self.session_secret),),
            )

    def create_template(
        self, admin_id: str, title: str, description: str | None, content: str, published: bool
    ) -> dict[str, Any]:
        if not title.strip() or not content.strip():
            raise ValueError("Title and content are required")
        template_id = str(uuid.uuid4())
        now = utcnow()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO templates
                   (id, title, description, content, is_published, created_by_admin_id,
                    created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    template_id,
                    title.strip(),
                    description.strip() if description else None,
                    content,
                    int(published),
                    admin_id,
                    now,
                    now,
                ),
            )
        return self.get_template(template_id)  # type: ignore[return-value]

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM templates WHERE id = ?", (template_id,))

    def list_templates(self, *, published_only: bool) -> list[dict[str, Any]]:
        where = "WHERE is_published = 1" if published_only else ""
        return self._all(f"SELECT * FROM templates {where} ORDER BY updated_at DESC")

    def update_template(self, template_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_template(template_id)
        if not current:
            return None
        title = fields.get("title", current["title"])
        content = fields.get("content", current["content"])
        if not str(title).strip() or not str(content).strip():
            raise ValueError("Title and content are required")
        with self.transaction() as connection:
            connection.execute(
                """UPDATE templates SET title = ?, description = ?, content = ?,
                   is_published = ?, updated_at = ? WHERE id = ?""",
                (
                    str(title).strip(),
                    fields.get("description", current["description"]),
                    content,
                    int(fields.get("is_published", bool(current["is_published"]))),
                    utcnow(),
                    template_id,
                ),
            )
        return self.get_template(template_id)

    def delete_template(self, template_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        return cursor.rowcount > 0

    def ensure_default_queue_limit(self, max_workers: int) -> None:
        """Give upgraded installations a useful default without reducing old capacity."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE work_queues SET max_workers = ?, updated_at = ? "
                "WHERE id = ? AND max_workers IS NULL",
                (max_workers, utcnow(), DEFAULT_QUEUE_ID),
            )

    def get_work_queue(self, queue_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM work_queues WHERE id = ?", (queue_id,))

    def list_work_queues(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE q.status = 'active'"
        return self._all(
            f"""SELECT q.*,
                       (SELECT COUNT(DISTINCT j.session_id) FROM queue_jobs j
                        WHERE j.queue_id = q.id AND j.status IN
                          ('dispatching', 'running', 'waiting_permission', 'cancelling')) AS active,
                       (SELECT COUNT(*) FROM queue_jobs j
                        WHERE j.queue_id = q.id AND j.status = 'queued') AS queued
                FROM work_queues q {where}
                ORDER BY q.priority DESC, q.created_at, q.id"""
        )

    def create_work_queue(
        self, name: str, priority: int, max_workers: int, actor_id: str | None = None
    ) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("Queue name must contain 1 to 120 characters")
        if max_workers < 1:
            raise ValueError("Queue worker limit must be positive")
        queue_id, now = str(uuid.uuid4()), utcnow()
        try:
            with self.transaction() as connection:
                connection.execute(
                    """INSERT INTO work_queues
                       (id, name, priority, max_workers, status, is_system, created_at, updated_at,
                        updated_by)
                       VALUES (?, ?, ?, ?, 'active', 0, ?, ?, ?)""",
                    (queue_id, normalized, int(priority), int(max_workers), now, now, actor_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Queue name already exists") from exc
        return self.get_work_queue(queue_id)  # type: ignore[return-value]

    def update_work_queue(
        self, queue_id: str, *, name: str, priority: int, max_workers: int,
        actor_id: str | None = None, expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """None when the queue is missing, archived or no longer at `expected_updated_at`."""
        current = self.get_work_queue(queue_id)
        if not current or current["status"] != "active":
            return None
        normalized = name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("Queue name must contain 1 to 120 characters")
        if max_workers < 1:
            raise ValueError("Queue worker limit must be positive")
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE work_queues SET name = ?, priority = ?, max_workers = ?,
                       updated_at = ?, updated_by = ?
                       WHERE id = ? AND status = 'active' AND (? IS NULL OR updated_at = ?)""",
                    (normalized, int(priority), int(max_workers), utcnow(), actor_id, queue_id,
                     expected_updated_at, expected_updated_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Queue name already exists") from exc
        return self.get_work_queue(queue_id) if cursor.rowcount else None

    def archive_work_queue(self, queue_id: str, actor_id: str | None = None) -> bool:
        queue = self.get_work_queue(queue_id)
        if not queue:
            return False
        if queue["is_system"]:
            raise ValueError("The system queue cannot be archived")
        references = self._one(
            """SELECT
                 EXISTS(SELECT 1 FROM tasks WHERE queue_id = ? AND deleted_at IS NULL) AS task_ref,
                 EXISTS(SELECT 1 FROM settings WHERE key IN
                   ('consultation_queue_id', 'default_chat_queue_id') AND value = ?) AS setting_ref,
                 EXISTS(SELECT 1 FROM queue_jobs WHERE queue_id = ? AND status NOT IN
                   ('completed', 'failed', 'cancelled')) AS job_ref,
                 EXISTS(SELECT 1 FROM queue_jobs WHERE pipeline_json LIKE '%' || ? || '%'
                   AND status NOT IN ('completed', 'failed', 'cancelled')) AS pipeline_ref""",
            (queue_id, queue_id, queue_id, queue_id),
        )
        if references and any(references.values()):
            raise ValueError("Queue is still assigned or contains unfinished jobs")
        with self.transaction() as connection:
            connection.execute(
                # Archiving twice keeps the first archiver as `updated_by`.
                "UPDATE work_queues SET status = 'archived', updated_at = ?, updated_by = ? "
                "WHERE id = ? AND status = 'active'",
                (utcnow(), actor_id, queue_id),
            )
        return True

    def _active_queue(self, queue_id: str) -> dict[str, Any]:
        queue = self.get_work_queue(queue_id)
        if not queue or queue["status"] != "active":
            raise ValueError("Queue is unavailable")
        return queue

    def create_task(
        self,
        admin_id: str,
        title: str,
        description: str | None,
        content: str,
        agent: str,
        provider_id: str | None,
        model_id: str | None,
        model_variant: str | None,
        published: bool,
        next_task_ids: list[str] | None = None,
        plan_first: bool = False,
        queue_id: str = DEFAULT_QUEUE_ID,
        input_hint: str | None = None,
    ) -> dict[str, Any]:
        """`admin_id` creates the task and is its first `updated_by`."""
        if not title.strip() or not content.strip() or not agent.strip():
            raise ValueError("Title, content and agent are required")
        if bool(provider_id) != bool(model_id):
            raise ValueError("Provider and model must be specified together")
        if plan_first and agent.strip() != "build":
            raise ValueError("Plan-first tasks require the build agent")
        self._active_queue(queue_id)
        task_id = str(uuid.uuid4())
        now = utcnow()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO tasks
                   (id, title, description, content, agent, provider_id, model_id,
                    model_variant, queue_id, is_published, created_by_admin_id,
                    created_at, updated_at, input_hint, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    title.strip(),
                    description.strip() if description else None,
                    content,
                    agent.strip(),
                    provider_id,
                    model_id,
                    model_variant,
                    queue_id,
                    int(published),
                    admin_id,
                    now,
                    now,
                    clean_input_hint(input_hint),
                    admin_id,
                ),
            )
            connection.execute(
                "UPDATE tasks SET plan_first = ? WHERE id = ?", (int(plan_first), task_id)
            )
            self._set_task_successors(connection, task_id, next_task_ids or [])
        return self.get_task(task_id)  # type: ignore[return-value]

    def list_task_successors(self, task_id: str) -> list[str]:
        return [row["next_task_id"] for row in self._all(
            """SELECT s.next_task_id FROM task_successors s
               JOIN tasks t ON t.id = s.next_task_id
               WHERE s.task_id = ? AND t.deleted_at IS NULL ORDER BY s.next_task_id""",
            (task_id,),
        )]

    def _set_task_successors(
        self, connection: sqlite3.Connection, task_id: str, next_task_ids: list[str]
    ) -> None:
        for next_id in set(next_task_ids):
            if not self.get_task(next_id):
                raise ValueError("Unknown pipeline task")
            cycle = connection.execute(
                """WITH RECURSIVE reachable(id) AS (
                     SELECT ? UNION
                     SELECT s.next_task_id FROM task_successors s
                     JOIN reachable r ON r.id = s.task_id
                   ) SELECT 1 FROM reachable WHERE id = ? LIMIT 1""",
                (next_id, task_id),
            ).fetchone()
            if cycle:
                raise ValueError("Pipeline cannot contain cycles")
        connection.execute("DELETE FROM task_successors WHERE task_id = ?", (task_id,))
        connection.executemany(
            "INSERT INTO task_successors (task_id, next_task_id) VALUES (?, ?)",
            [(task_id, next_id) for next_id in dict.fromkeys(next_task_ids)],
        )

    def _pipeline_snapshot(self, task_id: str) -> str:
        """Freeze definitions and edges for this run, including private/draft tasks."""
        graph: dict[str, Any] = {}
        pending = [task_id]
        while pending:
            current_id = pending.pop()
            if current_id in graph:
                continue
            task = self.get_task(current_id)
            if task is None:
                continue
            successors = self.list_task_successors(current_id)
            graph[current_id] = {"task": task, "next_task_ids": successors}
            pending.extend(successors)
        return json.dumps(graph, ensure_ascii=False)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,))

    def edit_state(self, table: str, object_id: str) -> dict[str, Any] | None:
        """`updated_at`, editor login and a `deleted` flag for a users, tasks or
        work_queues row; None when the row never existed."""
        return self._one(EDIT_STATE_QUERIES[table], (object_id,))

    def list_tasks(self, *, published_only: bool) -> list[dict[str, Any]]:
        where = "deleted_at IS NULL"
        if published_only:
            where += " AND is_published = 1"
        return self._all(f"SELECT * FROM tasks WHERE {where} ORDER BY updated_at DESC")

    def list_tasks_for_user(
        self, user_id: str, *, published_only: bool = True
    ) -> list[dict[str, Any]]:
        conditions = ["t.deleted_at IS NULL"]
        params: list[Any] = []
        if published_only:
            conditions.append("t.is_published = 1")
        conditions.append(
            """(t.visibility = 'all' OR EXISTS (
                 SELECT 1 FROM task_audience a
                 WHERE a.task_id = t.id AND a.user_id = ?))"""
        )
        params.append(user_id)
        return self._all(
            f"SELECT t.* FROM tasks t WHERE {' AND '.join(conditions)}"
            " ORDER BY t.updated_at DESC",
            tuple(params),
        )

    def task_visible_to(self, task_id: str, user_id: str) -> bool:
        return (
            self._one(
                """SELECT 1 FROM tasks t WHERE t.id = ? AND t.deleted_at IS NULL
                     AND t.is_published = 1 AND (t.visibility = 'all' OR EXISTS (
                       SELECT 1 FROM task_audience a
                       WHERE a.task_id = t.id AND a.user_id = ?)) LIMIT 1""",
                (task_id, user_id),
            )
            is not None
        )

    def list_task_audience(self, task_id: str) -> list[str]:
        return [
            row["user_id"]
            for row in self._all(
                "SELECT user_id FROM task_audience WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            )
        ]

    def set_task_audience(
        self, task_id: str, visibility: str, user_ids: list[str] | None,
        actor_id: str | None = None,
    ) -> dict[str, Any] | None:
        if visibility not in ("all", "selected"):
            raise ValueError("Unknown visibility")
        if not self.get_task(task_id):
            return None
        with self.transaction() as connection:
            self._write_task_audience(
                connection, task_id, visibility, user_ids, actor_id, utcnow()
            )
        return self.get_task(task_id)

    @staticmethod
    def _write_task_audience(
        connection: sqlite3.Connection, task_id: str, visibility: str,
        user_ids: list[str] | None, actor_id: str | None, now: str,
    ) -> None:
        connection.execute(
            "UPDATE tasks SET visibility = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (visibility, now, actor_id, task_id),
        )
        connection.execute("DELETE FROM task_audience WHERE task_id = ?", (task_id,))
        if visibility == "selected":
            for user_id in dict.fromkeys(user_ids or []):
                connection.execute(
                    """INSERT OR IGNORE INTO task_audience (task_id, user_id, created_at)
                       VALUES (?, ?, ?)""",
                    (task_id, user_id, now),
                )

    def list_user_task_access(self, user_id: str) -> list[dict[str, Any]]:
        return self._all(
            """SELECT t.id, t.title, t.description, t.is_published, t.visibility,
                      CASE WHEN t.visibility = 'all' OR EXISTS (
                        SELECT 1 FROM task_audience a
                        WHERE a.task_id = t.id AND a.user_id = ?) THEN 1 ELSE 0 END AS assigned
               FROM tasks t WHERE t.deleted_at IS NULL ORDER BY t.updated_at DESC""",
            (user_id,),
        )

    def set_user_task_access(
        self, user_id: str, task_ids: list[str]
    ) -> list[dict[str, Any]]:
        wanted = set(task_ids)
        now = utcnow()
        with self.transaction() as connection:
            tasks = connection.execute(
                "SELECT id, visibility FROM tasks WHERE deleted_at IS NULL"
            ).fetchall()
            for task in tasks:
                if task["visibility"] == "all":
                    continue
                if task["id"] in wanted:
                    connection.execute(
                        """INSERT OR IGNORE INTO task_audience (task_id, user_id, created_at)
                           VALUES (?, ?, ?)""",
                        (task["id"], user_id, now),
                    )
                else:
                    connection.execute(
                        "DELETE FROM task_audience WHERE task_id = ? AND user_id = ?",
                        (task["id"], user_id),
                    )
        return self.list_user_task_access(user_id)

    def update_task(
        self, task_id: str, fields: dict[str, Any], *, actor_id: str | None = None,
        expected_updated_at: str | None = None, visibility: str | None = None,
        user_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """None when the task is missing, deleted or no longer at `expected_updated_at`.
        `visibility` and `user_ids` replace the audience in the same write; the one left
        out keeps its stored value."""
        if visibility not in (None, "all", "selected"):
            raise ValueError("Unknown visibility")
        current = self.get_task(task_id)
        if not current:
            return None
        updated = current | fields
        if updated.get("plan_first") and str(updated["agent"]).strip() != "build":
            raise ValueError("Plan-first tasks require the build agent")
        for field in ("title", "content", "agent"):
            if not isinstance(updated[field], str) or not updated[field].strip():
                raise ValueError("Title, content and agent are required")
        if bool(updated["provider_id"]) != bool(updated["model_id"]):
            raise ValueError("Provider and model must be specified together")
        self._active_queue(updated.get("queue_id") or DEFAULT_QUEUE_ID)
        now = utcnow()
        try:
            with self.transaction() as connection:
                if "next_task_ids" in fields:
                    self._set_task_successors(connection, task_id, fields["next_task_ids"])
                cursor = connection.execute(
                    """UPDATE tasks SET title = ?, description = ?, content = ?, agent = ?,
                       provider_id = ?, model_id = ?, model_variant = ?, queue_id = ?,
                       is_published = ?, plan_first = ?, input_hint = ?, updated_at = ?,
                       updated_by = ?
                       WHERE id = ? AND deleted_at IS NULL AND (? IS NULL OR updated_at = ?)""",
                    (
                        updated["title"].strip(),
                        updated["description"],
                        updated["content"],
                        updated["agent"].strip(),
                        updated["provider_id"],
                        updated["model_id"],
                        updated["model_variant"],
                        updated.get("queue_id") or DEFAULT_QUEUE_ID,
                        int(updated["is_published"]),
                        int(updated["plan_first"]),
                        clean_input_hint(updated.get("input_hint")),
                        now,
                        actor_id,
                        task_id,
                        expected_updated_at,
                        expected_updated_at,
                    ),
                )
                if not cursor.rowcount:
                    raise StaleWrite(task_id)  # roll back the successor changes too
                if visibility is not None or user_ids is not None:
                    stored = connection.execute(
                        "SELECT visibility FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()["visibility"]
                    if user_ids is None:
                        user_ids = [row["user_id"] for row in connection.execute(
                            """SELECT user_id FROM task_audience WHERE task_id = ?
                               ORDER BY created_at""", (task_id,),
                        )]
                    self._write_task_audience(
                        connection, task_id, visibility or stored, user_ids, actor_id, now
                    )
        except StaleWrite:
            return None
        return self.get_task(task_id)

    def delete_task(self, task_id: str, actor_id: str | None = None) -> bool:
        now = utcnow()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM task_successors WHERE task_id = ? OR next_task_id = ?",
                (task_id, task_id),
            )
            cursor = connection.execute(
                """UPDATE tasks SET deleted_at = ?, updated_at = ?, updated_by = ?, is_published = 0
                   WHERE id = ? AND deleted_at IS NULL""",
                (now, now, actor_id, task_id),
            )
        return cursor.rowcount > 0

    def create_chat(
        self,
        owner_id: str,
        session_id: str | None,
        title: str,
        workspace: str,
        *,
        preferred_agent: str | None = None,
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
        preferred_model_variant: str | None = None,
        consultation: bool = False,
    ) -> dict[str, Any]:
        chat_id = str(uuid.uuid4())
        session_id = session_id or f"pending_{chat_id}"
        now = utcnow()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO chats
                   (id, owner_user_id, opencode_session_id, title, workspace_snapshot,
                    preferred_agent, preferred_provider_id, preferred_model_id,
                    preferred_model_variant, created_at, updated_at, consultation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    owner_id,
                    session_id,
                    title,
                    workspace,
                    preferred_agent,
                    preferred_provider_id,
                    preferred_model_id,
                    preferred_model_variant,
                    now,
                    now,
                    int(consultation),
                ),
            )
        return self.get_chat(chat_id)  # type: ignore[return-value]

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def chat_submission_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return [
            {
                "info": {"id": job["message_id"], "role": "user"},
                "parts": [{"type": "text", "text": job["content"]}],
                "submission_status": job["status"],
            }
            for job in self._all(
                "SELECT * FROM queue_jobs WHERE chat_id = ? ORDER BY sequence", (chat_id,)
            )
        ]

    def set_chat_title(self, chat_id: str, title: str) -> dict[str, Any] | None:
        normalized = title.strip()
        if not normalized:
            return self.get_chat(chat_id)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE chats SET title = ? WHERE id = ? AND title != ?",
                (normalized, chat_id, normalized),
            )
        return self.get_chat(chat_id)

    def user_owns_session(self, user_id: str, workspace: str | None, session_id: str) -> bool:
        if not workspace:
            return False
        return (
            self._one(
                """SELECT 1 FROM chats WHERE owner_user_id = ? AND workspace_snapshot = ?
                   AND opencode_session_id = ? AND kind = 'chat'
                   AND consultation = 0 AND deleted_at IS NULL LIMIT 1""",
                (user_id, workspace, session_id),
            )
            is not None
        )

    def list_chats(
        self, owner_id: str | None = None, *, workspace: str | None = None,
        consultation: bool = False, include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        query = """SELECT c.*, u.login AS owner_login FROM chats c
                   JOIN users u ON u.id = c.owner_user_id"""
        conditions: list[str] = ["c.kind = 'chat'", "c.consultation = ?"]
        params: list[Any] = [int(consultation)]
        if not include_deleted:
            conditions.append("c.deleted_at IS NULL")
        if owner_id:
            conditions.append("c.owner_user_id = ?")
            params.append(owner_id)
        if workspace:
            conditions.append("c.workspace_snapshot = ?")
            params.append(workspace)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.updated_at DESC"
        return self._all(query, tuple(params))

    def apply_workspace_change(self, owner_id: str, workspace: str) -> list[str]:
        """Archive the owner's folder chats and questions from other workspaces and
        bring back the ones archived earlier for leaving `workspace`. Task runs follow
        the user across workspaces and stay as they are. Chats the user archived by hand
        stay archived either way. Returns the changed chat ids."""
        now = utcnow()
        outside = """owner_user_id = ? AND workspace_snapshot != ? AND kind != 'task'
                     AND archived_at IS NULL AND archived_reason IS NULL"""
        back = """owner_user_id = ? AND workspace_snapshot = ?
                  AND archived_reason = 'workspace_changed'"""
        with self.transaction() as connection:
            chat_ids = [
                row["id"] for row in connection.execute(
                    f"SELECT id FROM chats WHERE ({outside}) OR ({back})",
                    (owner_id, workspace, owner_id, workspace),
                )
            ]
            connection.execute(
                f"""UPDATE chats SET archived_reason = 'workspace_changed', updated_at = ?,
                           archived_at = ?, snoozed_until = NULL
                    WHERE {outside}""",
                (now, now, owner_id, workspace),
            )
            connection.execute(
                f"""UPDATE chats SET archived_reason = NULL, archived_at = NULL, updated_at = ?
                    WHERE {back}""",
                (now, owner_id, workspace),
            )
        return chat_ids

    def clear_chat_snooze(self, chat_id: str) -> bool:
        with self.transaction() as connection:
            return connection.execute(
                "UPDATE chats SET snoozed_until = NULL WHERE id = ? AND snoozed_until IS NOT NULL",
                (chat_id,),
            ).rowcount > 0

    def chat_for_session(self, workspace: str | None, session_id: str) -> dict[str, Any] | None:
        """The folder or task chat behind an OpenCode session (consultations keep their title)."""
        return self._one(
            """SELECT * FROM chats WHERE workspace_snapshot = ? AND opencode_session_id = ?
               AND consultation = 0 LIMIT 1""",
            (workspace, session_id),
        )

    def list_items(
        self,
        *,
        owner_id: str | None = None,
        owner_logins: list[str] | None = None,
        workspace: str | None = None,
        kinds: tuple[str, ...] | list[str] | None = None,
        task_id: str | None = None,
        chat_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """Sidebar rows for every chat kind; `workspace` scopes non-task chats."""
        conditions: list[str] = []
        params: list[Any] = []
        if not include_deleted:
            conditions.append("c.deleted_at IS NULL")
        if owner_id:
            conditions.append("c.owner_user_id = ?")
            params.append(owner_id)
        if owner_logins:
            conditions.append(f"u.login IN ({', '.join('?' for _ in owner_logins)})")
            params.extend(owner_logins)
        if workspace:
            conditions.append("(c.kind = 'task' OR c.workspace_snapshot = ?)")
            params.append(workspace)
        if kinds is not None:
            selected = [ITEM_KIND_CONDITIONS[kind] for kind in kinds]
            conditions.append(f"({' OR '.join(selected)})" if selected else "0")
        if task_id:
            conditions.append("j.task_id = ?")
            params.append(task_id)
        if chat_id:
            conditions.append("c.id = ?")
            params.append(chat_id)
        if created_from:
            conditions.append("julianday(c.created_at) >= julianday(?)")
            params.append(created_from)
        if created_to:
            conditions.append("julianday(c.created_at) < julianday(?)")
            params.append(created_to)
        query = ITEM_QUERY
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        return self._all(query + " ORDER BY c.updated_at DESC, c.id", tuple(params))

    def archive_chat(self, chat_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE chats SET archived_at = ?, snoozed_until = NULL
                   WHERE id = ? AND archived_at IS NULL AND deleted_at IS NULL""",
                (utcnow(), chat_id),
            )
        return cursor.rowcount > 0

    def unarchive_chat(self, chat_id: str) -> bool:
        # Task runs were once auto-archived on a workspace change by mistake; let them out.
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE chats SET archived_at = NULL,
                          archived_reason = CASE WHEN kind = 'task' THEN NULL
                                                 ELSE archived_reason END
                   WHERE id = ? AND archived_at IS NOT NULL
                     AND (archived_reason IS NULL OR kind = 'task') AND deleted_at IS NULL""",
                (chat_id,),
            )
        return cursor.rowcount > 0

    def snooze_chat(self, chat_id: str, until: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE chats SET snoozed_until = ?
                   WHERE id = ? AND archived_at IS NULL AND deleted_at IS NULL""",
                (until, chat_id),
            )
        return cursor.rowcount > 0

    def unsnooze_chat(self, chat_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE chats SET snoozed_until = NULL
                   WHERE id = ? AND snoozed_until IS NOT NULL AND deleted_at IS NULL""",
                (chat_id,),
            )
        return cursor.rowcount > 0

    def delete_chat(self, chat_id: str) -> bool:
        """Soft delete: user lists hide the chat, admin views and job history keep it."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE chats SET deleted_at = ?, snoozed_until = NULL
                   WHERE id = ? AND deleted_at IS NULL""",
                (utcnow(), chat_id),
            )
            if cursor.rowcount:
                # Nobody is pinged about a chat that is gone; sent ones stay as history.
                connection.execute(
                    """DELETE FROM notification_outbox
                       WHERE status IN ('pending', 'sending')
                         AND (object_id = ?
                              OR object_id IN (SELECT id FROM queue_jobs WHERE chat_id = ?))""",
                    (chat_id, chat_id),
                )
        return cursor.rowcount > 0

    def unfinished_chat_jobs(self, chat_id: str) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in UNFINISHED_JOB_STATUSES)
        return self._all(
            f"""SELECT * FROM queue_jobs WHERE chat_id = ? AND status IN ({placeholders})
                ORDER BY sequence""",
            (chat_id, *UNFINISHED_JOB_STATUSES),
        )

    def has_pending_jobs(self, effective_user_id: str) -> bool:
        return (
            self._one(
                """SELECT 1 FROM queue_jobs WHERE effective_user_id = ? AND status IN
                   ('queued', 'dispatching', 'running', 'waiting_permission',
                    'waiting_question', 'cancelling')
                   LIMIT 1""",
                (effective_user_id,),
            )
            is not None
        )

    def set_chat_status(self, chat_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE chats SET status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow(), chat_id),
            )

    def queued_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS count FROM queue_jobs WHERE status = 'queued'")
        return int(row["count"]) if row else 0

    def create_job(
        self,
        *,
        chat: dict[str, Any],
        effective_user_id: str,
        actor_user_id: str,
        content: str,
        agent: str,
        provider_id: str | None,
        model_id: str | None,
        model_variant: str | None,
        max_queued: int | None = None,
        queue_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        message_id = f"msg_{uuid.uuid4().hex}"
        now = utcnow()
        queue_id = queue_id or self.default_chat_queue_id()
        self._active_queue(queue_id)
        with self.transaction() as connection:
            # The caller's chat may predate session attachment by the dispatcher,
            # or a delete that raced this message.
            chat = self.get_chat(chat["id"])
            if not chat or chat["deleted_at"]:
                raise ChatDeleted
            if max_queued is not None and connection.execute(
                "SELECT COUNT(*) FROM queue_jobs WHERE status = 'queued'"
            ).fetchone()[0] >= max_queued:
                raise ValueError("Queue is full")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM queue_jobs"
            ).fetchone()
            sequence = int(row[0])
            connection.execute(
                """INSERT INTO queue_jobs
                   (id, sequence, chat_id, session_id, effective_user_id, actor_user_id,
                    workspace_snapshot, message_id, content, agent, provider_id, model_id,
                    model_variant, queue_id, status, created_at, session_pending)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    job_id,
                    sequence,
                    chat["id"],
                    chat["opencode_session_id"],
                    effective_user_id,
                    actor_user_id,
                    chat["workspace_snapshot"],
                    message_id,
                    content,
                    agent,
                    provider_id,
                    model_id,
                    model_variant,
                    queue_id,
                    now,
                    int(chat["opencode_session_id"].startswith("pending_")),
                ),
            )
            connection.execute(
                """UPDATE chats SET status = CASE WHEN status IN
                   ('dispatching', 'running', 'waiting_permission',
                    'waiting_question', 'cancelling')
                   THEN status ELSE 'queued' END, updated_at = ? WHERE id = ?""",
                (now, chat["id"]),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def has_pending_task(self, task_id: str, user_input: str) -> bool:
        return self._one(
            """SELECT 1 FROM queue_jobs WHERE task_id = ? AND user_input = ?
               AND status IN ('queued', 'dispatching', 'running',
                              'waiting_permission', 'waiting_question', 'cancelling') LIMIT 1""",
            (task_id, user_input.strip()),
        ) is not None

    def create_task_job(
        self,
        *,
        task: dict[str, Any],
        user: dict[str, Any],
        actor_user_id: str,
        session_id: str | None,
        user_input: str,
        max_queued: int,
        session_title: str | None = None,
    ) -> dict[str, Any]:
        if not user.get("workspace_path"):
            raise ValueError("A task requires a workspace")
        chat_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        session_pending = session_id is None
        session_id = session_id or f"pending_{chat_id}"
        now = utcnow()
        initial_title = session_title if session_title is not None else task["title"]
        user_input = user_input.strip()
        content = task["content"]
        if user_input.strip():
            content += "\n\n" + user_input
        with self.transaction() as connection:
            current_user = connection.execute(
                "SELECT status, workspace_path, auth_version FROM users WHERE id = ?",
                (user["id"],),
            ).fetchone()
            if (
                not current_user
                or current_user["status"] != "active"
                or current_user["workspace_path"] != user["workspace_path"]
                or current_user["auth_version"] != user["auth_version"]
            ):
                raise ValueError("User or workspace changed")
            available = connection.execute(
                """SELECT updated_at FROM tasks
                   WHERE id = ? AND is_published = 1 AND deleted_at IS NULL""",
                (task["id"],),
            ).fetchone()
            if not available:
                raise ValueError("Task is unavailable")
            if available["updated_at"] != task["updated_at"]:
                raise ValueError("Task changed")
            # The check and insertion share BEGIN IMMEDIATE so simultaneous
            # submissions cannot both enter the dispatcher's queue.
            duplicate = self.has_pending_task(task["id"], user_input)
            queued = connection.execute(
                "SELECT COUNT(*) FROM queue_jobs WHERE status = 'queued'"
            ).fetchone()[0]
            if not duplicate and queued >= max_queued:
                raise ValueError("The queue is full")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM queue_jobs"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO chats
                   (id, owner_user_id, opencode_session_id, title, workspace_snapshot,
                    kind, status, preferred_agent, preferred_provider_id,
                    preferred_model_id, preferred_model_variant, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'task', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    user["id"],
                    session_id,
                    initial_title,
                    user["workspace_path"],
                    "idle" if duplicate else "queued",
                    task["agent"],
                    task["provider_id"],
                    task["model_id"],
                    task["model_variant"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO queue_jobs
                   (id, sequence, chat_id, session_id, effective_user_id, actor_user_id,
                    workspace_snapshot, message_id, content, agent, provider_id, model_id,
                    model_variant, queue_id, task_id, task_title, task_initial_title, user_input,
                    status, created_at, finished_at, error_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    sequence,
                    chat_id,
                    session_id,
                    user["id"],
                    actor_user_id,
                    user["workspace_path"],
                    f"msg_{uuid.uuid4().hex}",
                    content,
                    task["agent"],
                    task["provider_id"],
                    task["model_id"],
                    task["model_variant"],
                    task.get("queue_id") or DEFAULT_QUEUE_ID,
                    task["id"],
                    task["title"],
                    initial_title,
                    user_input,
                    "cancelled" if duplicate else "queued",
                    now,
                    now if duplicate else None,
                    "duplicate_task" if duplicate else None,
                ),
            )
            connection.execute(
                "UPDATE queue_jobs SET pipeline_json = ?, session_pending = ?, plan_first = ? "
                "WHERE id = ?",
                (self._pipeline_snapshot(task["id"]), int(session_pending),
                 int(task.get("plan_first", False)), job_id),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def reserve_build_phase(self, job_id: str) -> str | None:
        """Persist the second prompt ID once, before making the external request."""
        message_id = f"msg_{uuid.uuid4().hex}"
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE queue_jobs SET phase2_message_id = ?
                   WHERE id = ? AND task_id IS NOT NULL AND plan_first = 1
                     AND phase2_message_id IS NULL AND status = 'running'""",
                (message_id, job_id),
            )
        return message_id if cursor.rowcount else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._one(
            """SELECT q.*, owner.login AS effective_login, actor.login AS actor_login,
                      c.title AS chat_title, c.consultation AS consultation,
                      c.deleted_at AS chat_deleted_at,
                      w.name AS queue_name, w.priority AS queue_priority
               FROM queue_jobs q
               JOIN users owner ON owner.id = q.effective_user_id
               JOIN users actor ON actor.id = q.actor_user_id
               JOIN chats c ON c.id = q.chat_id
               JOIN work_queues w ON w.id = q.queue_id WHERE q.id = ?""",
            (job_id,),
        )

    def list_jobs(self, effective_user_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT q.*, owner.login AS effective_login, actor.login AS actor_login,
                          c.title AS chat_title, c.consultation AS consultation,
                          c.deleted_at AS chat_deleted_at,
                          w.name AS queue_name, w.priority AS queue_priority
                   FROM queue_jobs q
                   JOIN users owner ON owner.id = q.effective_user_id
                   JOIN users actor ON actor.id = q.actor_user_id
                   JOIN chats c ON c.id = q.chat_id
                   JOIN work_queues w ON w.id = q.queue_id"""
        params: tuple[Any, ...] = ()
        if effective_user_id:
            query += " WHERE q.effective_user_id = ?"
            params = (effective_user_id,)
        query += " ORDER BY q.sequence DESC LIMIT 500"
        return self._all(query, params)

    def queue_positions(self) -> dict[str, int]:
        """1-based place of every queued job, in the order `claim_next_job` prefers."""
        rows = self._all(
            """SELECT q.id FROM queue_jobs q JOIN work_queues w ON w.id = q.queue_id
               WHERE q.status = 'queued'
               ORDER BY (q.resume_content IS NOT NULL) DESC, w.priority DESC, q.sequence"""
        )
        return {row["id"]: position for position, row in enumerate(rows, start=1)}

    def list_queue_overview_jobs(self) -> list[dict[str, Any]]:
        """Return the bounded projection used by the global queue screen."""
        return self._all(
            """SELECT q.id, q.sequence, q.chat_id, q.session_id,
                      q.effective_user_id, q.actor_user_id, q.message_id,
                      substr(q.content, 1, 90) AS content,
                      q.agent, q.provider_id, q.model_id, q.model_variant,
                      q.queue_id, q.task_id, q.task_title, q.task_initial_title,
                      q.user_input, q.status, q.created_at, q.started_at,
                      q.finished_at, q.error_code, q.parent_job_id,
                      q.session_pending, q.plan_first, q.phase2_message_id,
                      q.resume_from_message_id, q.resume_monitor,
                      owner.login AS effective_login, actor.login AS actor_login,
                      c.title AS chat_title, c.consultation AS consultation,
                      w.name AS queue_name, w.priority AS queue_priority
               FROM queue_jobs q
               JOIN users owner ON owner.id = q.effective_user_id
               JOIN users actor ON actor.id = q.actor_user_id
               JOIN chats c ON c.id = q.chat_id
               JOIN work_queues w ON w.id = q.queue_id
               ORDER BY q.sequence DESC LIMIT 500"""
        )

    def list_task_runs(
        self, owner_id: str | None = None, *, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        query = """SELECT q.*, owner.login AS effective_login, actor.login AS actor_login,
                          c.title AS chat_title, c.consultation AS consultation,
                          c.deleted_at AS chat_deleted_at,
                          w.name AS queue_name, w.priority AS queue_priority
                   FROM queue_jobs q
                   JOIN users owner ON owner.id = q.effective_user_id
                   JOIN users actor ON actor.id = q.actor_user_id
                   JOIN chats c ON c.id = q.chat_id
                   JOIN work_queues w ON w.id = q.queue_id
                   WHERE q.task_id IS NOT NULL"""
        params: tuple[Any, ...] = ()
        if not include_deleted:
            query += " AND c.deleted_at IS NULL"
        if owner_id:
            query += " AND q.effective_user_id = ?"
            params = (owner_id,)
        query += " ORDER BY q.sequence DESC LIMIT 500"
        return self._all(query, params)

    def task_statistics(self, start: str, end: str) -> dict[str, Any]:
        # Aggregate the full history; list_task_runs is capped at 500 rows.
        return self._one(
            """WITH samples AS (
                SELECT julianday(finished_at) >= julianday(?)
                       AND julianday(finished_at) < julianday(?) AS in_day,
                       (julianday(finished_at) - julianday(started_at)) * 86400 AS execution,
                       (julianday(finished_at) - julianday(created_at)) * 86400 AS total
                FROM queue_jobs WHERE task_id IS NOT NULL AND status = 'completed'
            )
            SELECT COUNT(*) AS completed_all_time,
                   COUNT(CASE WHEN in_day THEN 1 END) AS completed_day,
                   AVG(CASE WHEN in_day AND execution >= 0 THEN execution END)
                       AS average_execution_seconds,
                   AVG(CASE WHEN in_day AND total >= 0 THEN total END)
                       AS average_total_seconds,
                   COUNT(CASE WHEN in_day AND execution >= 0 THEN 1 END)
                       AS execution_samples,
                   COUNT(CASE WHEN in_day AND total >= 0 THEN 1 END) AS total_samples
            FROM samples""",
            (start, end),
        ) or {}

    def list_statistics_runs(
        self,
        *,
        kind: str | None = None,
        task_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every question and task run, deleted chats included; deduplicated launches
        that never ran are left out."""
        conditions = [
            "(q.task_id IS NOT NULL OR (c.kind = 'chat' AND c.consultation = 1))",
            "COALESCE(q.error_code, '') != 'duplicate_task'",
        ]
        params: list[Any] = []
        if kind == "question":
            conditions.append("q.task_id IS NULL")
        elif kind == "task":
            conditions.append("q.task_id IS NOT NULL")
        if task_id:
            conditions.append("q.task_id = ?")
            params.append(task_id)
        if created_from:
            conditions.append("julianday(q.created_at) >= julianday(?)")
            params.append(created_from)
        if created_to:
            conditions.append("julianday(q.created_at) < julianday(?)")
            params.append(created_to)
        return self._all(
            f"""SELECT q.id, q.task_id, q.task_title, q.status, q.created_at, q.started_at,
                       q.finished_at, u.login
                FROM queue_jobs q JOIN chats c ON c.id = q.chat_id
                LEFT JOIN users u ON u.id = c.owner_user_id
                WHERE {' AND '.join(conditions)} ORDER BY q.sequence""",
            tuple(params),
        )

    def get_task_run(self, run_id: str) -> dict[str, Any] | None:
        job = self.get_job(run_id)
        return job if job and job["task_id"] is not None else None

    def get_task_run_for_session(self, workspace: str, session_id: str) -> dict[str, Any] | None:
        job = self._one(
            """SELECT q.id FROM queue_jobs q JOIN chats c ON c.id = q.chat_id
               WHERE q.workspace_snapshot = ? AND q.session_id = ?
                 AND c.workspace_snapshot = ? AND c.opencode_session_id = ?
                 AND c.kind = 'task' AND q.task_id IS NOT NULL
               ORDER BY q.sequence DESC LIMIT 1""",
            (workspace, session_id, workspace, session_id),
        )
        return self.get_job(job["id"]) if job else None

    def workspace_has_pending_jobs(self, workspace: str) -> bool:
        # Include queued work and unknown nonterminal states (fail closed).
        return bool(self._one(
            """SELECT 1 FROM queue_jobs WHERE workspace_snapshot = ?
               AND status NOT IN ('completed', 'failed', 'cancelled') LIMIT 1""",
            (workspace,),
        ))

    def active_jobs(self) -> list[dict[str, Any]]:
        return self._all(
            """SELECT * FROM queue_jobs WHERE status IN
               ('dispatching', 'running', 'waiting_permission', 'waiting_question', 'cancelling')
               ORDER BY sequence"""
        )

    def active_slot_count(self) -> int:
        row = self._one(
            """SELECT COUNT(DISTINCT session_id) AS count FROM queue_jobs WHERE status IN
               ('dispatching', 'running', 'waiting_permission', 'cancelling')"""
        )
        return int(row["count"]) if row else 0

    def pipeline_children(self, job_id: str) -> list[dict[str, Any]]:
        return self._all(
            """SELECT q.*, c.title AS chat_title, c.consultation AS consultation FROM queue_jobs q
               JOIN chats c ON c.id = q.chat_id
               WHERE q.parent_job_id = ? ORDER BY q.sequence""", (job_id,)
        )

    def claim_next_job(
        self, limit: int, per_user_default: int | None = None
    ) -> dict[str, Any] | None:
        with self.transaction() as connection:
            # A message can slip in while its chat is being deleted; never run it.
            for orphan in connection.execute(
                """SELECT q.id FROM queue_jobs q JOIN chats c ON c.id = q.chat_id
                   WHERE q.status = 'queued' AND c.deleted_at IS NOT NULL"""
            ).fetchall():
                self.cancel_queued_job(orphan["id"])
            active = connection.execute(
                """SELECT COUNT(DISTINCT session_id) FROM queue_jobs WHERE status IN
                   ('dispatching', 'running', 'waiting_permission', 'cancelling')"""
            ).fetchone()[0]
            if int(active) >= limit:
                return None
            row = connection.execute(
                """SELECT q.* FROM queue_jobs q
                   JOIN users u ON u.id = q.effective_user_id
                   JOIN work_queues w ON w.id = q.queue_id
                   WHERE q.status = 'queued'
                     AND (
                       w.max_workers IS NULL OR (
                         SELECT COUNT(DISTINCT qa.session_id) FROM queue_jobs qa
                         WHERE qa.queue_id = q.queue_id AND qa.status IN
                           ('dispatching', 'running', 'waiting_permission', 'cancelling')
                       ) < w.max_workers
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM queue_jobs active
                       WHERE active.session_id = q.session_id AND active.status IN
                         ('dispatching', 'running', 'waiting_permission',
                          'waiting_question', 'cancelling')
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM queue_jobs prior
                       WHERE prior.session_id = q.session_id AND prior.sequence < q.sequence
                         AND prior.status IN ('queued', 'dispatching', 'running',
                                              'waiting_permission', 'waiting_question',
                                              'cancelling')
                     )
                     AND (
                       COALESCE(u.max_active_jobs, ?) IS NULL
                       OR (
                         SELECT COUNT(DISTINCT a.session_id) FROM queue_jobs a
                         WHERE a.effective_user_id = q.effective_user_id
                           AND a.status IN ('dispatching', 'running',
                                            'waiting_permission', 'cancelling')
                       ) < COALESCE(u.max_active_jobs, ?)
                     )
                   -- A continuation prepared after a controlled restart restores
                   -- work which already owned a slot. Let it regain capacity before
                   -- newly queued work, then preserve normal queue priority/FIFO.
                   ORDER BY (q.resume_content IS NOT NULL) DESC,
                            w.priority DESC, q.sequence LIMIT 1""",
                (per_user_default, per_user_default),
            ).fetchone()
            if not row:
                return None
            now = utcnow()
            connection.execute(
                "UPDATE queue_jobs SET status = 'dispatching', "
                "started_at = COALESCE(started_at, ?) WHERE id = ?",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE chats SET status = 'dispatching', updated_at = ? WHERE id = ?",
                (now, row["chat_id"]),
            )
        return self.get_job(row["id"])

    def resume_interrupted_job(self, job_id: str, expected_message_id: str, content: str) -> bool:
        """Operator-only deployment recovery after checking the stopped session history.

        Call only while the dispatcher is stopped. Preserve the user's prompt,
        session, pipeline and phase; an ordinary claim submits the continuation once.
        """
        if not content.strip():
            raise ValueError("Continuation instructions are required")
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] not in {"running", "waiting_permission"}:
                return False
            previous = row["phase2_message_id"] or row["message_id"]
            if previous != expected_message_id or row["session_pending"]:
                return False
            column = "phase2_message_id" if row["phase2_message_id"] else "message_id"
            connection.execute(
                f"""UPDATE queue_jobs SET status = 'queued', {column} = ?,
                    resume_from_message_id = ?, resume_content = ?,
                    finished_at = NULL, error_code = NULL WHERE id = ?""",
                (f"msg_{uuid.uuid4().hex}", previous, content, job_id),
            )
            connection.execute(
                "UPDATE chats SET status = 'queued', updated_at = ? WHERE id = ?",
                (utcnow(), row["chat_id"]),
            )
        return True

    def get_task_result(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM task_run_results WHERE job_id = ?", (job_id,))

    def save_task_result(self, job_id: str, answer: dict[str, Any] | None) -> None:
        # A row with NULL text records a completed run without a visible answer.
        # Keep results separate so queue listings and events stay small.
        with self.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO task_run_results (job_id, message_id, text, saved_at)
                   SELECT id, ?, ?, ? FROM queue_jobs
                   WHERE id = ? AND task_id IS NOT NULL AND status = 'completed'""",
                ((answer or {}).get("message_id"), (answer or {}).get("text"), utcnow(), job_id),
            )

    def complete_task_run(self, job_id: str, answer: dict[str, Any] | None) -> None:
        # Status, pipeline fan-out, result and notification outbox commit together.
        with self.transaction() as connection:
            self.set_job_status(job_id, "completed")
            self.save_task_result(job_id, answer)
            self._maybe_enqueue_task_notification(connection, job_id)

    def _pipeline_root(
        self, connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row | None:
        row = connection.execute("SELECT * FROM queue_jobs WHERE id = ?", (job_id,)).fetchone()
        while row and row["parent_job_id"]:
            row = connection.execute(
                "SELECT * FROM queue_jobs WHERE id = ?", (row["parent_job_id"],)
            ).fetchone()
        return row

    def _pipeline_tree(
        self, connection: sqlite3.Connection, root_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """WITH RECURSIVE tree(id) AS (
                   SELECT id FROM queue_jobs WHERE id = ?
                   UNION ALL
                   SELECT child.id FROM queue_jobs child
                   JOIN tree parent ON child.parent_job_id = parent.id
               )
               SELECT q.* FROM queue_jobs q JOIN tree ON tree.id = q.id""",
            (root_id,),
        ).fetchall()

    def _pipeline_completed(
        self, connection: sqlite3.Connection, root: sqlite3.Row
    ) -> bool:
        rows = self._pipeline_tree(connection, root["id"])
        if not rows or any(row["status"] != "completed" for row in rows):
            return False
        try:
            graph = json.loads(root["pipeline_json"]) if root["pipeline_json"] else {}
        except (TypeError, ValueError):
            return False
        children: dict[str, set[str]] = {}
        for row in rows:
            if row["parent_job_id"]:
                children.setdefault(row["parent_job_id"], set()).add(row["task_id"])
        for row in rows:
            expected = set((graph.get(row["task_id"]) or {}).get("next_task_ids") or [])
            if children.get(row["id"], set()) != expected:
                return False
        return True

    def _enqueue_notification(
        self,
        connection: sqlite3.Connection,
        *,
        dedupe_key: str,
        kind: str,
        object_id: str,
        chat_id: str,
        user_id: str,
        title: str,
        link_path: str,
    ) -> None:
        deleted = connection.execute(
            "SELECT 1 FROM chats WHERE id = ? AND deleted_at IS NOT NULL", (chat_id,)
        ).fetchone()
        if deleted:
            return
        user = connection.execute(
            "SELECT mattermost_user_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user or not user["mattermost_user_id"]:
            return
        now = utcnow()
        connection.execute(
            """INSERT OR IGNORE INTO notification_outbox
               (id, dedupe_key, kind, object_id, user_id, mattermost_user_id,
                title, link_path, status, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                str(uuid.uuid4()), dedupe_key, kind, object_id, user_id,
                user["mattermost_user_id"], title[:120], link_path, now, now,
            ),
        )

    def _maybe_enqueue_task_notification(
        self, connection: sqlite3.Connection, job_id: str
    ) -> None:
        root = self._pipeline_root(connection, job_id)
        if not root or not root["task_id"] or not self._pipeline_completed(connection, root):
            return
        self._enqueue_notification(
            connection,
            dedupe_key=f"task-chain:{root['id']}",
            kind="task_completed",
            object_id=root["id"],
            chat_id=root["chat_id"],
            user_id=root["effective_user_id"],
            title=root["task_title"] or "Таска",
            link_path=f"/tasks/runs/{root['id']}",
        )

    def complete_consultation_run(self, job_id: str, content: str) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT q.*, c.title AS chat_title, c.consultation AS consultation
                   FROM queue_jobs q JOIN chats c ON c.id = q.chat_id WHERE q.id = ?""",
                (job_id,),
            ).fetchone()
            if not row or not row["consultation"] or row["status"] in {
                "completed", "failed", "cancelled", "cancelling",
            }:
                return False
            connection.execute(
                """INSERT INTO consultation_answers(job_id, content) VALUES (?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET content = excluded.content""",
                (job_id, content),
            )
            self.set_job_status(job_id, "completed")
            current = connection.execute(
                "SELECT status FROM queue_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not current or current["status"] != "completed":
                return False
            self._enqueue_notification(
                connection,
                dedupe_key=f"consultation-answer:{job_id}",
                kind="consultation_answer_ready",
                object_id=row["chat_id"],
                chat_id=row["chat_id"],
                user_id=row["effective_user_id"],
                title=row["chat_title"] or "Консультация",
                link_path=f"/questions/{row['chat_id']}",
            )
            return True

    def claim_notification(self) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM notification_outbox
                   WHERE status = 'pending' AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT 1""",
                (utcnow(),),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """UPDATE notification_outbox SET status = 'sending', attempts = attempts + 1
                   WHERE id = ? AND status = 'pending'""",
                (row["id"],),
            )
            return dict(connection.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (row["id"],)
            ).fetchone())

    def mark_notification_sent(self, notification_id: str, post_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE notification_outbox
                   SET status = 'sent', post_id = ?, sent_at = ?, last_error = NULL
                   WHERE id = ? AND status = 'sending'""",
                (post_id, utcnow(), notification_id),
            )

    def mark_notification_failed(
        self,
        notification_id: str,
        error_code: str,
        *,
        permanent: bool,
        retry_after_seconds: float = 0,
        max_attempts: int = 8,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM notification_outbox WHERE id = ?", (notification_id,)
            ).fetchone()
            if not row:
                return
            dead = permanent or int(row["attempts"]) >= max_attempts
            delay = retry_after_seconds or min(3600, 5 * (2 ** max(0, int(row["attempts"]) - 1)))
            next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """UPDATE notification_outbox
                   SET status = ?, next_attempt_at = ?, last_error = ? WHERE id = ?""",
                ("dead" if dead else "pending", next_attempt, error_code, notification_id),
            )

    def list_notifications(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notification_outbox ORDER BY created_at")

    def set_job_status(self, job_id: str, status: str, error_code: str | None = None) -> bool:
        """Persist a job status; True when it also cleared the chat's snooze."""
        terminal = status in {"completed", "failed", "cancelled"}
        snooze_cleared = False
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM queue_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return False
            # Completion and fan-out are one durable operation. A repeated terminal
            # event must neither restart a branch nor turn a cancellation into success.
            if row["status"] in {"completed", "failed", "cancelled"}:
                return False
            if row["status"] == "cancelling" and status not in {"cancelling", "cancelled"}:
                return False
            connection.execute(
                """UPDATE queue_jobs SET status = ?, error_code = ?,
                   finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE id = ?""",
                (status, error_code, terminal, utcnow(), job_id),
            )
            next_status = status
            if terminal:
                pending = connection.execute(
                    """SELECT status FROM queue_jobs WHERE chat_id = ? AND status IN
                       ('queued', 'dispatching', 'running', 'waiting_permission',
                        'waiting_question', 'cancelling')
                       ORDER BY (status = 'queued'), sequence LIMIT 1""",
                    (row["chat_id"],),
                ).fetchone()
                next_status = pending["status"] if pending else (
                    "failed" if status == "failed" else "idle"
                )
            connection.execute(
                "UPDATE chats SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, utcnow(), row["chat_id"]),
            )
            if status in {"waiting_question", "waiting_permission"}:
                # A snoozed chat that needs the user comes back immediately.
                snooze_cleared = connection.execute(
                    """UPDATE chats SET snoozed_until = NULL
                       WHERE id = ? AND snoozed_until IS NOT NULL""",
                    (row["chat_id"],),
                ).rowcount > 0
            if status == "completed" and row["pipeline_json"]:
                self._enqueue_pipeline_children(connection, dict(row))
        return snooze_cleared

    def _enqueue_pipeline_children(
        self, connection: sqlite3.Connection, parent: dict[str, Any]
    ) -> None:
        graph = json.loads(parent["pipeline_json"])
        for task_id in graph[parent["task_id"]]["next_task_ids"]:
            task = graph[task_id]["task"]
            job_id, chat_id = str(uuid.uuid4()), str(uuid.uuid4())
            # An actual OpenCode session is created only when a worker claims this job.
            session_id = f"pending_{job_id}"
            user_input = parent["user_input"]
            title = " ".join(user_input.split())[:120] or task["title"]
            now = utcnow()
            connection.execute(
                """INSERT INTO chats
                   (id, owner_user_id, opencode_session_id, title, workspace_snapshot,
                    kind, status, preferred_agent, preferred_provider_id,
                    preferred_model_id, preferred_model_variant, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'task', 'queued', ?, ?, ?, ?, ?, ?)""",
                (chat_id, parent["effective_user_id"], session_id, title,
                 parent["workspace_snapshot"], task["agent"], task["provider_id"],
                 task["model_id"], task["model_variant"], now, now),
            )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM queue_jobs"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO queue_jobs
                   (id, sequence, chat_id, session_id, effective_user_id, actor_user_id,
                    workspace_snapshot, message_id, content, agent, provider_id, model_id,
                    model_variant, queue_id, task_id, task_title, task_initial_title, user_input,
                    status, created_at, pipeline_json, parent_job_id, session_pending, plan_first)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'queued', ?, ?, ?, 1, ?)""",
                (job_id, sequence, chat_id, session_id, parent["effective_user_id"],
                 parent["effective_user_id"], parent["workspace_snapshot"],
                 f"msg_{uuid.uuid4().hex}",
                 task["content"] + ("\n\n" + user_input if user_input else ""),
                 task["agent"], task["provider_id"], task["model_id"], task["model_variant"],
                 task.get("queue_id") or DEFAULT_QUEUE_ID,
                 task_id, task["title"], title, user_input, now, parent["pipeline_json"],
                 parent["id"], int(task.get("plan_first", False))),
            )
            connection.execute(
                """INSERT INTO audit_log
                   (actor_user_id, effective_user_id, action, object_type, object_id,
                    metadata_json, created_at)
                   VALUES (?, ?, 'task.pipeline.run', 'task_run', ?, ?, ?)""",
                (parent["effective_user_id"], parent["effective_user_id"], job_id,
                 json.dumps({"task_id": task_id, "parent_job_id": parent["id"]}), now),
            )

    def attach_task_session(self, job_id: str, session_id: str) -> bool:
        """Attach one session to the chat and all turns queued before it existed."""
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT chat_id FROM queue_jobs
                   WHERE id = ? AND session_pending = 1 AND status = 'dispatching'""",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                """UPDATE queue_jobs SET session_id = ?, session_pending = 0
                   WHERE chat_id = ? AND session_pending = 1""",
                (session_id, row["chat_id"]),
            )
            connection.execute(
                "UPDATE chats SET opencode_session_id = ? WHERE id = ?",
                (session_id, row["chat_id"]),
            )
        return True

    def requeue_interrupted_job(self, job_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT chat_id FROM queue_jobs WHERE id = ? AND status = 'dispatching'",
                (job_id,),
            ).fetchone()
            if not row:
                return
            connection.execute(
                """UPDATE queue_jobs SET status = 'queued', started_at = NULL,
                   finished_at = NULL, error_code = NULL WHERE id = ?""",
                (job_id,),
            )
            connection.execute(
                "UPDATE chats SET status = 'queued', updated_at = ? WHERE id = ?",
                (utcnow(), row["chat_id"]),
            )

    def cancel_queued_job(self, job_id: str, effective_user_id: str | None = None) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT chat_id FROM queue_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return False
            query = (
                "UPDATE queue_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE id = ? AND status = 'queued'"
            )
            params: tuple[Any, ...] = (utcnow(), job_id)
            if effective_user_id:
                query += " AND effective_user_id = ?"
                params += (effective_user_id,)
            cursor = connection.execute(query, params)
            if cursor.rowcount:
                pending = connection.execute(
                    """SELECT 1 FROM queue_jobs WHERE chat_id = ? AND status IN
                       ('queued', 'dispatching', 'running', 'waiting_permission',
                        'waiting_question', 'cancelling')
                       LIMIT 1""",
                    (row["chat_id"],),
                ).fetchone()
                if not pending:
                    connection.execute(
                        "UPDATE chats SET status = 'idle', updated_at = ? WHERE id = ?",
                        (utcnow(), row["chat_id"]),
                    )
        return cursor.rowcount > 0

    def consultation_for_session(self, session_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM chats WHERE opencode_session_id = ? AND consultation = 1",
            (session_id,),
        )

    def consultation_jobs(self, chat_id: str) -> list[dict[str, Any]]:
        return self._all(
            """SELECT q.*, a.content AS answer, w.name AS queue_name FROM queue_jobs q
               LEFT JOIN consultation_answers a ON a.job_id = q.id
               JOIN work_queues w ON w.id = q.queue_id
               WHERE q.chat_id = ? ORDER BY q.sequence""", (chat_id,),
        )

    def consultation_status(self, chat_id: str) -> str:
        row = self._one(
            """SELECT status FROM queue_jobs WHERE chat_id = ?
               ORDER BY CASE
                 WHEN status IN ('dispatching', 'running', 'waiting_permission',
                                 'waiting_question', 'cancelling')
                   THEN 0
                 WHEN status = 'queued' THEN 1 ELSE 2 END,
                 sequence DESC LIMIT 1""", (chat_id,),
        )
        return row["status"] if row else "idle"

    def waiting_consultation_job(self, chat_id: str) -> dict[str, Any] | None:
        row = self._one(
            """SELECT id FROM queue_jobs
               WHERE chat_id = ? AND status = 'waiting_question'
               ORDER BY sequence DESC LIMIT 1""",
            (chat_id,),
        )
        return self.get_job(row["id"]) if row else None

    def queue_consultation_resume(self, job_id: str) -> bool:
        """Queue monitoring after a question reply without resending the prompt."""
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT q.chat_id FROM queue_jobs q JOIN chats c ON c.id = q.chat_id
                   WHERE q.id = ? AND q.status = 'waiting_question'
                     AND c.consultation = 1""",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                """UPDATE queue_jobs SET status = 'queued', resume_monitor = 1,
                   finished_at = NULL, error_code = NULL WHERE id = ?""",
                (job_id,),
            )
            connection.execute(
                "UPDATE chats SET status = 'queued', updated_at = ? WHERE id = ?",
                (utcnow(), row["chat_id"]),
            )
        return True

    def delete_empty_consultation(self, chat_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """DELETE FROM chats WHERE id = ? AND consultation = 1
                   AND NOT EXISTS (SELECT 1 FROM queue_jobs WHERE chat_id = chats.id)""",
                (chat_id,),
            )

    def save_consultation_answer(self, job_id: str, content: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO consultation_answers(job_id, content) VALUES (?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET content = excluded.content""",
                (job_id, content),
            )

    def consultation_model(self) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = 'consultation_model'")
        return row["value"] if row else None

    def set_consultation_model(self, model: str, admin_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO settings(key, value, updated_by, updated_at)
                   VALUES ('consultation_model', ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (model, admin_id, utcnow()),
            )

    def set_consultation_settings(
        self, model: str, queue_id: str, admin_id: str
    ) -> None:
        self._active_queue(queue_id)
        now = utcnow()
        with self.transaction() as connection:
            connection.executemany(
                """INSERT INTO settings(key, value, updated_by, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (
                    ("consultation_model", model, admin_id, now),
                    ("consultation_queue_id", queue_id, admin_id, now),
                ),
            )

    def _queue_setting(self, key: str) -> str:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        queue_id = row["value"] if row else DEFAULT_QUEUE_ID
        queue = self.get_work_queue(queue_id)
        return queue_id if queue and queue["status"] == "active" else DEFAULT_QUEUE_ID

    def consultation_queue_id(self) -> str:
        return self._queue_setting("consultation_queue_id")

    def default_chat_queue_id(self) -> str:
        return self._queue_setting("default_chat_queue_id")

    def set_queue_setting(self, key: str, queue_id: str, admin_id: str) -> None:
        if key not in {"consultation_queue_id", "default_chat_queue_id"}:
            raise ValueError("Unknown queue setting")
        self._active_queue(queue_id)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO settings(key, value, updated_by, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (key, queue_id, admin_id, utcnow()),
            )

    def concurrency_override(self) -> int | None:
        row = self._one("SELECT value FROM settings WHERE key = 'max_active_sessions'")
        return int(row["value"]) if row else None

    def set_concurrency_override(self, value: int, admin_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO settings(key, value, updated_by, updated_at)
                   VALUES ('max_active_sessions', ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (str(value), admin_id, utcnow()),
            )

    def clear_concurrency_override(self) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM settings WHERE key = 'max_active_sessions'")

    def audit(
        self,
        actor_id: str | None,
        action: str,
        *,
        effective_user_id: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO audit_log
                   (actor_user_id, effective_user_id, action, object_type, object_id,
                    metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    actor_id,
                    effective_user_id,
                    action,
                    object_type,
                    object_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utcnow(),
                ),
            )

    def list_audit(self) -> list[dict[str, Any]]:
        return self._all(AUDIT_QUERY + " ORDER BY a.id DESC LIMIT 500")

    def iter_audit(
        self,
        *,
        logins: list[str] | None = None,
        actions: list[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        before_id: int | None = None,
        batch: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """Newest first, fetched lazily so callers can filter further in Python."""
        conditions: list[str] = []
        params: list[Any] = []
        if logins:
            marks = ", ".join("?" for _ in logins)
            conditions.append(f"(actor.login IN ({marks}) OR effective.login IN ({marks}))")
            params.extend([*logins, *logins])
        if actions:
            conditions.append(f"a.action IN ({', '.join('?' for _ in actions)})")
            params.extend(actions)
        if created_from:
            conditions.append("julianday(a.created_at) >= julianday(?)")
            params.append(created_from)
        if created_to:
            conditions.append("julianday(a.created_at) < julianday(?)")
            params.append(created_to)
        while True:
            where = [*conditions, "a.id < ?"] if before_id is not None else conditions
            rows = self._all(
                AUDIT_QUERY
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY a.id DESC LIMIT ?",
                (*params, *([before_id] if before_id is not None else []), batch),
            )
            yield from rows
            if len(rows) < batch:
                return
            before_id = rows[-1]["id"]
