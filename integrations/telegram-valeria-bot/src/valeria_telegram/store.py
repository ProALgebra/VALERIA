from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CALLBACK_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Session:
    telegram_id: int
    chat_id: int
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    user: dict[str, Any]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _private_parent(path: Path) -> None:
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


class Store:
    """Private, durable state for Telegram authentication and replay protection."""

    def __init__(self, path: Path | str):
        path = Path(path).expanduser()
        _private_parent(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.connection = sqlite3.connect(path)
        try:
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self) -> None:
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                telegram_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                session_token TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                user TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flows (
                telegram_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS callbacks (
                token TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS callbacks_user ON callbacks(telegram_id);
            CREATE TABLE IF NOT EXISTS watches (
                telegram_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY(telegram_id, kind, resource_id)
            );
            CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS submissions (
                telegram_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                valeria_user_id TEXT NOT NULL,
                path TEXT NOT NULL,
                body TEXT NOT NULL,
                expected_status INTEGER NOT NULL,
                key TEXT NOT NULL,
                kind TEXT NOT NULL,
                context TEXT NOT NULL,
                result TEXT,
                PRIMARY KEY(telegram_id, update_id)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_session(self, uid: int) -> Session | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE telegram_id = ?", (uid,)
        ).fetchone()
        if row is None:
            return None
        return Session(
            telegram_id=row["telegram_id"],
            chat_id=row["chat_id"],
            session_token=row["session_token"],
            csrf_token=row["csrf_token"],
            user=json.loads(row["user"]),
        )

    def save_session(self, session: Session) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "chat_id=excluded.chat_id, session_token=excluded.session_token, "
                "csrf_token=excluded.csrf_token, user=excluded.user",
                (
                    session.telegram_id,
                    session.chat_id,
                    session.session_token,
                    session.csrf_token,
                    _json(session.user),
                ),
            )

    def delete_session(self, uid: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM sessions WHERE telegram_id = ?", (uid,))

    def clear_user(self, uid: int) -> None:
        with self.connection:
            for table in ("sessions", "flows", "callbacks", "watches", "submissions"):
                self.connection.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (uid,))

    def get_flow(self, uid: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT kind, data FROM flows WHERE telegram_id = ?", (uid,)
        ).fetchone()
        return {"kind": row["kind"], "data": json.loads(row["data"])} if row else {}

    def set_flow(self, uid: int, kind: str, data: dict[str, Any] | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO flows VALUES (?, ?, ?) ON CONFLICT(telegram_id) "
                "DO UPDATE SET kind=excluded.kind, data=excluded.data",
                (uid, kind, _json(data if data is not None else {})),
            )

    def clear_flow(self, uid: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM flows WHERE telegram_id = ?", (uid,))

    def create_callback(self, uid: int, action: str, data: dict[str, Any] | None = None) -> str:
        token = secrets.token_urlsafe(18)
        with self.connection:
            self.connection.execute(
                "INSERT INTO callbacks VALUES (?, ?, ?, ?, ?)",
                (token, uid, action, _json(data if data is not None else {}), time.time()),
            )
            self.connection.execute(
                "DELETE FROM callbacks WHERE created_at <= ?", (time.time() - CALLBACK_TTL_SECONDS,)
            )
        return token

    def get_callback(self, uid: int, token: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT action, data FROM callbacks "
            "WHERE token = ? AND telegram_id = ? AND created_at > ?",
            (token, uid, time.time() - CALLBACK_TTL_SECONDS),
        ).fetchone()
        return {"action": row["action"], "data": json.loads(row["data"])} if row else None

    def clear_callbacks(self, uid: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM callbacks WHERE telegram_id = ?", (uid,))

    def upsert_watch(self, uid: int, kind: str, resource_id: str, data: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO watches VALUES (?, ?, ?, ?) "
                "ON CONFLICT(telegram_id, kind, resource_id) DO UPDATE SET data=excluded.data",
                (uid, kind, resource_id, _json(data)),
            )

    def get_watch(self, uid: int, kind: str, resource_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data FROM watches WHERE telegram_id = ? AND kind = ? AND resource_id = ?",
            (uid, kind, resource_id),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_watches(self, uid: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM watches"
        params: tuple[int, ...] = ()
        if uid is not None:
            query += " WHERE telegram_id = ?"
            params = (uid,)
        query += " ORDER BY telegram_id, kind, resource_id"
        return [
            {
                "telegram_id": row["telegram_id"],
                "kind": row["kind"],
                "resource_id": row["resource_id"],
                "data": json.loads(row["data"]),
            }
            for row in self.connection.execute(query, params)
        ]

    def remove_watch(self, uid: int, kind: str, resource_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM watches WHERE telegram_id = ? AND kind = ? AND resource_id = ?",
                (uid, kind, resource_id),
            )

    def is_processed(self, update_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM processed_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
            is not None
        )

    def mark_processed(self, update_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_updates VALUES (?)", (update_id,)
            )

    def get_offset(self) -> int:
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'offset'").fetchone()
        return int(row["value"]) if row else 0

    def set_offset(self, offset: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO meta VALUES ('offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(offset),),
            )

    def get_submission(self, uid: int, update_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM submissions WHERE telegram_id = ? AND update_id = ?", (uid, update_id)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field_name in ("body", "context", "result"):
            result[field_name] = (
                json.loads(row[field_name]) if row[field_name] is not None else None
            )
        return result

    def save_submission(
        self,
        uid: int,
        update_id: int,
        *,
        valeria_user_id: str,
        path: str,
        body: dict[str, Any],
        expected_status: int,
        key: str,
        kind: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO submissions "
                "(telegram_id, update_id, valeria_user_id, path, body, expected_status, "
                "key, kind, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid,
                    update_id,
                    valeria_user_id,
                    path,
                    _json(body),
                    expected_status,
                    key,
                    kind,
                    _json(context),
                ),
            )
            record = self.get_submission(uid, update_id)
        assert record is not None
        return record

    def complete_submission(self, uid: int, update_id: int, result: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE submissions SET result = ? "
                "WHERE telegram_id = ? AND update_id = ? AND result IS NULL",
                (_json(result), uid, update_id),
            )
