"""Persistence for processor settings and repair jobs.

This module is the only part of ``json_processor`` that knows about SQLite.  It
contains no provider HTTP calls, no worker loop, and no admin API.  Every write
uses parameterized SQL inside ``Database.transaction()``; no transaction spans a
network or other external await.

Job lifecycle::

    queued --claim_due--> processing --complete_job--> succeeded
                                  \\--fail_job-------> failed
    queued/processing --cancel_job / cancel_jobs_for_run--> cancelled

A claim increments ``attempts`` and grants a time-limited lease owned by one
worker.  Completion/failure/cancellation use compare-and-set predicates on
status plus lease owner/expiry, so two workers can never both finalize a job.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any

from ..database import Database, utcnow
from .models import (
    AttemptStatus,
    ProcessorErrorCode,
    RepairJobStatus,
    RepairMethod,
    RepairSourceType,
)
from .schema import validate_schema
from .settings import (
    JsonProcessorSettings,
    SettingsValidationError,
    default_settings_json,
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PROMPT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TERMINAL = tuple(item.value for item in RepairJobStatus.terminal())
_SECRET_META_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "apikey",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "session_key",
        "set_cookie",
        "token",
        "x_api_key",
    }
)


class JsonProcessorRepositoryError(RuntimeError):
    """Base class for repository failures with a stable processor error code."""

    code = ProcessorErrorCode.INTERNAL_ERROR

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class SettingsVersionConflict(JsonProcessorRepositoryError):
    """``expected_version`` did not match the stored settings version."""

    code = ProcessorErrorCode.SETTINGS_VERSION_CONFLICT

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"settings version conflict: expected {expected}, current {actual}"
        )


class JsonRepairJobNotFound(JsonProcessorRepositoryError):
    code = ProcessorErrorCode.JOB_NOT_FOUND


class JsonRepairStateConflict(JsonProcessorRepositoryError):
    """The job is not in a state that permits the requested transition."""

    code = ProcessorErrorCode.JOB_STATE_CONFLICT


class JsonRepairLeaseLost(JsonProcessorRepositoryError):
    """The caller no longer owns an unexpired lease on the job."""

    code = ProcessorErrorCode.LEASE_LOST


class JsonRepairIdempotencyConflict(JsonProcessorRepositoryError):
    """An explicit idempotency key already belongs to a different request."""

    code = ProcessorErrorCode.IDEMPOTENCY_CONFLICT


def sanitize_metadata(value: Any) -> Any:
    """Recursively redact secret-shaped keys from attempt metadata.

    ``api_key_ref`` is intentionally allowed: it is an opaque reference, not a
    secret.  The function does not mutate its input.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("metadata keys must be strings")
            result[key] = (
                "[redacted]" if _is_secret_meta_key(key) else sanitize_metadata(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_metadata(item) for item in value]
    return value


def _is_secret_meta_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SECRET_META_KEYS:
        return True
    return normalized.endswith(
        (
            "_api_key",
            "_secret",
            "_password",
            "_access_token",
            "_refresh_token",
        )
    )


def _dump_json(value: Any, *, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON-serializable") from exc


def _safe_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        current = datetime.fromisoformat(value)
    else:
        raise TypeError("now must be a datetime, an ISO-8601 string, or None")
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _require_text(field: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field} must contain at most {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{field} must not contain control characters")
    return normalized


def _optional_text(field: str, value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_text(field, value, maximum=maximum)


def _enum_value(field: str, value: Any, enum_type: Any) -> Any:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    allowed = ", ".join(item.value for item in enum_type)
    raise ValueError(f"{field} must be one of: {allowed}")


def _positive_int(field: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _normalize_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("raw_hash must be a 64-character hex SHA-256 digest")
    normalized = value.strip().lower()
    if not _HEX64_RE.fullmatch(normalized):
        raise ValueError("raw_hash must be a 64-character hex SHA-256 digest")
    return normalized


def _canonical_idempotency_key(
    *,
    source_type: RepairSourceType,
    task_run_id: str | None,
    vflow_run_id: str | None,
    point_id: str | None,
    raw_hash: str,
    schema_hash: str,
    settings_version: int,
    prompt_version: str,
) -> str:
    payload = json.dumps(
        [
            source_type.value,
            task_run_id or "",
            vflow_run_id or "",
            point_id or "",
            raw_hash,
            schema_hash,
            settings_version,
            prompt_version,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JsonProcessorRepository:
    """Settings and repair-job repository.

    The caller owns the :class:`~opencode_web.database.Database` lifecycle.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_settings_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM json_processor_settings WHERE id = 1"
        ).fetchone()
        if row is not None:
            return row
        now = utcnow()
        connection.execute(
            """INSERT OR IGNORE INTO json_processor_settings
               (id, settings_json, version, updated_by, created_at, updated_at)
               VALUES (1, ?, 1, NULL, ?, ?)""",
            (default_settings_json(), now, now),
        )
        row = connection.execute(
            "SELECT * FROM json_processor_settings WHERE id = 1"
        ).fetchone()
        if row is None:  # pragma: no cover - schema guarantees the row
            raise JsonProcessorRepositoryError("processor settings are unavailable")
        return row

    @staticmethod
    def _settings_payload(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        raw = _safe_load(row["settings_json"], {})
        if not isinstance(raw, Mapping):
            raw = {}
        model = JsonProcessorSettings.from_dict(raw, reject_unknown=False)
        return {
            "id": 1,
            **model.to_dict(),
            "version": int(row["version"]),
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_settings(self) -> dict[str, Any]:
        """Return the singleton settings plus audit-safe metadata."""

        with self.database.transaction() as connection:
            row = self._ensure_settings_row(connection)
        return self._settings_payload(row)

    def get_settings_model(self) -> JsonProcessorSettings:
        return JsonProcessorSettings.from_dict(
            self.get_settings(), reject_unknown=False
        )

    def update_settings(
        self,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically replace settings using optimistic locking.

        ``expected_version`` must equal the current stored version.  A stale
        writer receives :class:`SettingsVersionConflict` and changes nothing.
        Unknown keys are rejected, so a secret such as ``api_key`` can never be
        persisted by accident.
        """

        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValueError("expected_version must be an integer")
        if expected_version < 1:
            raise ValueError("expected_version must be at least 1")
        if not isinstance(changes, Mapping):
            raise SettingsValidationError("settings changes must be a JSON object")
        known = {item.name for item in fields(JsonProcessorSettings)}
        unknown = sorted(set(changes) - known)
        if unknown:
            raise SettingsValidationError(
                [f"unknown setting: {name}" for name in unknown]
            )

        with self.database.transaction() as connection:
            row = self._ensure_settings_row(connection)
            current_version = int(row["version"])
            if current_version != expected_version:
                raise SettingsVersionConflict(expected_version, current_version)
            if not changes:
                return self._settings_payload(row)
            current = JsonProcessorSettings.from_dict(
                _safe_load(row["settings_json"], {}), reject_unknown=False
            )
            merged = current.to_dict()
            merged.update(changes)
            validated = JsonProcessorSettings.from_dict(merged)
            if updated_by is not None:
                updated_by = _require_text("updated_by", updated_by, maximum=200)
                exists = connection.execute(
                    "SELECT 1 FROM users WHERE id = ?", (updated_by,)
                ).fetchone()
                if exists is None:
                    raise ValueError("updated_by user does not exist")
            now = utcnow()
            cursor = connection.execute(
                """UPDATE json_processor_settings
                   SET settings_json = ?, version = version + 1,
                       updated_by = ?, updated_at = ?
                   WHERE id = 1 AND version = ?""",
                (validated.to_json(), updated_by, now, expected_version),
            )
            if cursor.rowcount != 1:
                latest = connection.execute(
                    "SELECT version FROM json_processor_settings WHERE id = 1"
                ).fetchone()
                raise SettingsVersionConflict(
                    expected_version, int(latest["version"]) if latest else 0
                )
        return self.get_settings()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    @staticmethod
    def _decode_job(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        job = dict(row)
        job["schema"] = _safe_load(job.get("schema_json"))
        job["result"] = _safe_load(job.get("result_json")) if job.get("result_json") else None
        job["has_result"] = job.get("result_json") is not None
        return job

    @staticmethod
    def _decode_attempt(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        attempt = dict(row)
        attempt["request_meta"] = _safe_load(attempt.get("request_meta_json"), {})
        attempt["token_usage"] = _safe_load(attempt.get("token_usage_json"))
        return attempt

    def _find_canonical_job(
        self,
        *,
        source_type: RepairSourceType,
        task_run_id: str | None,
        vflow_run_id: str | None,
        point_id: str | None,
        raw_hash: str,
        schema_hash: str,
        settings_version: int,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        row = self.database._one(
            """SELECT * FROM json_repair_jobs
               WHERE source_type = ? AND task_run_id IS ? AND vflow_run_id IS ?
                 AND point_id IS ? AND raw_hash = ? AND schema_hash = ?
                 AND settings_version = ? AND prompt_version = ?""",
            (
                source_type.value,
                task_run_id,
                vflow_run_id,
                point_id,
                raw_hash,
                schema_hash,
                settings_version,
                prompt_version,
            ),
        )
        return None if row is None else self._decode_job(row)

    def create_or_get_job(
        self,
        *,
        source_type: RepairSourceType | str,
        raw_output_ref: str,
        raw_hash: str,
        schema: Any,
        prompt_version: str,
        task_run_id: str | None = None,
        vflow_run_id: str | None = None,
        point_id: str | None = None,
        settings_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently create a queued repair job.

        Canonical identity is
        ``(source_type, task_run_id, vflow_run_id, point_id, raw_hash,
        schema_hash, settings_version, prompt_version)``.  Repeated calls with
        the same identity return the existing row even if the caller supplies a
        different explicit idempotency key.  Reusing an explicit key for a
        different identity is a conflict.

        The schema is validated and hashed here, so a stale caller-supplied
        hash can never diverge from the stored schema.
        """

        resolved_source = _enum_value(
            "source_type", source_type, RepairSourceType
        )
        ref = _require_text("raw_output_ref", raw_output_ref, maximum=2_048)
        normalized_raw_hash = _normalize_hash(raw_hash)
        task_run_id = _optional_text("task_run_id", task_run_id, maximum=200)
        vflow_run_id = _optional_text("vflow_run_id", vflow_run_id, maximum=200)
        point_id = _optional_text("point_id", point_id, maximum=200)
        prompt = _require_text("prompt_version", prompt_version, maximum=64)
        if not _PROMPT_VERSION_RE.fullmatch(prompt):
            raise ValueError(
                "prompt_version must match [A-Za-z0-9][A-Za-z0-9._:-]*"
            )
        if resolved_source is RepairSourceType.TASK and not task_run_id:
            raise ValueError("task_run_id is required for a task repair")
        if resolved_source is RepairSourceType.VFLOW and not vflow_run_id:
            raise ValueError("vflow_run_id is required for a vflow repair")
        if settings_version is not None:
            settings_version = _positive_int(
                "settings_version",
                settings_version,
                minimum=1,
                maximum=2_147_483_647,
            )
        explicit_key = None
        if idempotency_key is not None:
            explicit_key = _require_text(
                "idempotency_key", idempotency_key, maximum=200
            )

        validate_schema(schema)
        schema_json = _dump_json(schema, field="schema")
        schema_hash = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
        resolved_settings_version = settings_version

        try:
            with self.database.transaction() as connection:
                settings_row = self._ensure_settings_row(connection)
                resolved_settings_version = (
                    int(settings_row["version"])
                    if settings_version is None
                    else settings_version
                )
                canonical_key = _canonical_idempotency_key(
                    source_type=resolved_source,
                    task_run_id=task_run_id,
                    vflow_run_id=vflow_run_id,
                    point_id=point_id,
                    raw_hash=normalized_raw_hash,
                    schema_hash=schema_hash,
                    settings_version=resolved_settings_version,
                    prompt_version=prompt,
                )
                existing = connection.execute(
                    """SELECT * FROM json_repair_jobs
                       WHERE source_type = ? AND task_run_id IS ?
                         AND vflow_run_id IS ? AND point_id IS ?
                         AND raw_hash = ? AND schema_hash = ?
                         AND settings_version = ? AND prompt_version = ?""",
                    (
                        resolved_source.value,
                        task_run_id,
                        vflow_run_id,
                        point_id,
                        normalized_raw_hash,
                        schema_hash,
                        resolved_settings_version,
                        prompt,
                    ),
                ).fetchone()
                if existing is not None:
                    return self._decode_job(existing)

                if explicit_key is not None:
                    clash = connection.execute(
                        "SELECT id FROM json_repair_jobs WHERE idempotency_key = ?",
                        (explicit_key,),
                    ).fetchone()
                    if clash is not None:
                        raise JsonRepairIdempotencyConflict(
                            "idempotency key is already used by another repair job"
                        )

                job_id = str(uuid.uuid4())
                now = utcnow()
                connection.execute(
                    """INSERT INTO json_repair_jobs
                       (id, source_type, task_run_id, vflow_run_id, point_id,
                        raw_output_ref, raw_hash, schema_json, schema_hash,
                        settings_version, prompt_version, status, method, attempts,
                        result_json, error_code, error_message, idempotency_key,
                        lease_owner, lease_until, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'none', 0,
                               NULL, NULL, NULL, ?, NULL, NULL, ?, ?)""",
                    (
                        job_id,
                        resolved_source.value,
                        task_run_id,
                        vflow_run_id,
                        point_id,
                        ref,
                        normalized_raw_hash,
                        schema_json,
                        schema_hash,
                        resolved_settings_version,
                        prompt,
                        explicit_key or canonical_key,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:  # pragma: no cover - insert above succeeded
                    raise JsonProcessorRepositoryError("repair job could not be read")
                return self._decode_job(row)
        except sqlite3.IntegrityError as exc:
            # Another writer may have inserted the canonical row between our
            # lookup and insert.  Resolve the race deterministically.
            existing = self._find_canonical_job(
                source_type=resolved_source,
                task_run_id=task_run_id,
                vflow_run_id=vflow_run_id,
                point_id=point_id,
                raw_hash=normalized_raw_hash,
                schema_hash=schema_hash,
                settings_version=(
                    resolved_settings_version
                    if resolved_settings_version is not None
                    else self._current_settings_version()
                ),
                prompt_version=prompt,
            )
            if existing is not None:
                return existing
            if explicit_key is not None:
                clash = self.database._one(
                    "SELECT id FROM json_repair_jobs WHERE idempotency_key = ?",
                    (explicit_key,),
                )
                if clash is not None:
                    raise JsonRepairIdempotencyConflict(
                        "idempotency key is already used by another repair job"
                    ) from exc
            raise JsonProcessorRepositoryError(
                "repair job could not be persisted"
            ) from exc

    def _current_settings_version(self) -> int:
        return int(self.get_settings()["version"])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.database._one(
            "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
        )
        return None if row is None else self._decode_job(row)

    def list_jobs(
        self,
        *,
        status: RepairJobStatus | str | None = None,
        source_type: RepairSourceType | str | None = None,
        method: RepairMethod | str | None = None,
        task_run_id: str | None = None,
        vflow_run_id: str | None = None,
        point_id: str | None = None,
        idempotency_key: str | None = None,
        error_code: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a filtered, paginated job page (newest first)."""

        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(_enum_value("status", status, RepairJobStatus).value)
        if source_type is not None:
            conditions.append("source_type = ?")
            params.append(
                _enum_value("source_type", source_type, RepairSourceType).value
            )
        if method is not None:
            conditions.append("method = ?")
            params.append(_enum_value("method", method, RepairMethod).value)
        if task_run_id is not None:
            conditions.append("task_run_id = ?")
            params.append(_require_text("task_run_id", task_run_id, maximum=200))
        if vflow_run_id is not None:
            conditions.append("vflow_run_id = ?")
            params.append(_require_text("vflow_run_id", vflow_run_id, maximum=200))
        if point_id is not None:
            conditions.append("point_id = ?")
            params.append(_require_text("point_id", point_id, maximum=200))
        if idempotency_key is not None:
            conditions.append("idempotency_key = ?")
            params.append(
                _require_text("idempotency_key", idempotency_key, maximum=200)
            )
        if error_code is not None:
            conditions.append("error_code = ?")
            params.append(_require_text("error_code", error_code, maximum=64))
        if created_after is not None:
            conditions.append("created_at >= ?")
            params.append(_require_text("created_after", created_after, maximum=64))
        if created_before is not None:
            conditions.append("created_at <= ?")
            params.append(_require_text("created_before", created_before, maximum=64))
        page_size = _positive_int("limit", limit, minimum=1, maximum=200)
        page_offset = _positive_int("offset", offset, minimum=0, maximum=1_000_000)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total_row = self.database._one(
            f"SELECT COUNT(*) AS count FROM json_repair_jobs {where}",
            tuple(params),
        )
        total = int(total_row["count"]) if total_row else 0
        rows = self.database._all(
            f"""SELECT * FROM json_repair_jobs {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, page_offset),
        )
        return {
            "items": [self._decode_job(row) for row in rows],
            "total": total,
            "limit": page_size,
            "offset": page_offset,
        }

    def claim_due(
        self,
        limit: int = 1,
        lease_seconds: int = 60,
        *,
        owner: str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``limit`` queued jobs.

        Each claim sets ``status='processing'``, increments ``attempts``, and
        grants a lease.  ``attempts`` counts processing claims, not detailed
        repair attempts (those live in ``json_repair_attempts``).
        """

        claim_limit = _positive_int("limit", limit, minimum=1, maximum=1_000)
        lease = _positive_int(
            "lease_seconds", lease_seconds, minimum=1, maximum=86_400
        )
        if owner is None:
            owner = f"json-processor-{uuid.uuid4().hex}"
        else:
            owner = _require_text("owner", owner, maximum=200)
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease)).isoformat()

        claimed: list[dict[str, Any]] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM json_repair_jobs
                   WHERE status = 'queued'
                   ORDER BY created_at, id
                   LIMIT ?""",
                (claim_limit,),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """UPDATE json_repair_jobs
                       SET status = 'processing', lease_owner = ?, lease_until = ?,
                           attempts = attempts + 1,
                           started_at = COALESCE(started_at, ?),
                           updated_at = ?
                       WHERE id = ? AND status = 'queued'""",
                    (owner, lease_until, now, now, row["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                claimed_row = connection.execute(
                    "SELECT * FROM json_repair_jobs WHERE id = ?", (row["id"],)
                ).fetchone()
                if claimed_row is not None:
                    claimed.append(self._decode_job(claimed_row))
        return claimed

    @staticmethod
    def _lease_is_active(job: Mapping[str, Any], lease_owner: str, now: str) -> bool:
        lease_until = job.get("lease_until")
        return (
            job.get("status") == RepairJobStatus.PROCESSING.value
            and job.get("lease_owner") == lease_owner
            and isinstance(lease_until, str)
            and lease_until > now
        )

    def _guarded_finalize(
        self,
        job_id: str,
        *,
        lease_owner: str,
        new_status: RepairJobStatus,
        assignments: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner = _require_text("lease_owner", lease_owner, maximum=200)
        if new_status not in RepairJobStatus.terminal():
            raise ValueError("new_status must be terminal")
        now = utcnow()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JsonRepairJobNotFound("repair job not found")
            status = RepairJobStatus(row["status"])
            if status.is_terminal:
                raise JsonRepairStateConflict(
                    f"repair job is already {status.value}"
                )
            if status is not RepairJobStatus.PROCESSING:
                raise JsonRepairStateConflict(
                    "repair job is not processing and cannot be finalized"
                )
            if not self._lease_is_active(dict(row), owner, now):
                raise JsonRepairLeaseLost("repair job lease is not owned or has expired")

            values: dict[str, Any] = dict(assignments)
            values.update(
                {
                    "status": new_status.value,
                    "lease_owner": None,
                    "lease_until": None,
                    "updated_at": now,
                    "finished_at": now,
                }
            )
            columns = ", ".join(f"{name} = ?" for name in values)
            cursor = connection.execute(
                f"""UPDATE json_repair_jobs SET {columns}
                    WHERE id = ? AND status = 'processing'
                      AND lease_owner = ? AND lease_until IS NOT NULL
                      AND lease_until > ?""",
                (*values.values(), job_id, owner, now),
            )
            if cursor.rowcount != 1:
                latest = connection.execute(
                    "SELECT status FROM json_repair_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if latest is not None and RepairJobStatus(latest["status"]).is_terminal:
                    raise JsonRepairStateConflict(
                        f"repair job is already {latest['status']}"
                    )
                raise JsonRepairLeaseLost(
                    "repair job lease was lost during finalization"
                )
            finalized = connection.execute(
                "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if finalized is None:  # pragma: no cover - row existed above
                raise JsonRepairJobNotFound("repair job not found")
            return self._decode_job(finalized)

    def complete_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        result_json: Any,
        method: RepairMethod | str = RepairMethod.NONE,
    ) -> dict[str, Any]:
        """Finalize a processing job as succeeded using its active lease."""

        resolved_method = _enum_value("method", method, RepairMethod)
        result = _dump_json(result_json, field="result_json")
        return self._guarded_finalize(
            job_id,
            lease_owner=lease_owner,
            new_status=RepairJobStatus.SUCCEEDED,
            assignments={
                "method": resolved_method.value,
                "result_json": result,
                "error_code": None,
                "error_message": None,
            },
        )

    def fail_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        error_code: ProcessorErrorCode | str,
        error_message: str = "",
        method: RepairMethod | str | None = None,
    ) -> dict[str, Any]:
        """Finalize a processing job as failed using its active lease."""

        try:
            resolved_code = (
                error_code
                if isinstance(error_code, ProcessorErrorCode)
                else ProcessorErrorCode(error_code)
            )
        except ValueError as exc:
            raise ValueError("error_code is not a known processor error") from exc
        if not isinstance(error_message, str):
            raise ValueError("error_message must be a string")
        message = error_message.strip()
        if len(message) > 4_000:
            raise ValueError("error_message must contain at most 4000 characters")
        assignments: dict[str, Any] = {
            "result_json": None,
            "error_code": resolved_code.value,
            "error_message": message or None,
        }
        if method is not None:
            assignments["method"] = _enum_value("method", method, RepairMethod).value
        return self._guarded_finalize(
            job_id,
            lease_owner=lease_owner,
            new_status=RepairJobStatus.FAILED,
            assignments=assignments,
        )

    def cancel_job(
        self,
        job_id: str,
        *,
        lease_owner: str | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a queued job, or a processing job with its lease/force.

        Terminal jobs are never rewritten.  ``force=True`` is intended for an
        explicit administrative/run cancellation; without it a processing job
        can only be cancelled by its current lease owner.
        """

        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        message = "cancelled" if reason is None else _require_text(
            "reason", reason, maximum=500
        )
        owner = (
            None
            if lease_owner is None
            else _require_text("lease_owner", lease_owner, maximum=200)
        )
        now = utcnow()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JsonRepairJobNotFound("repair job not found")
            status = RepairJobStatus(row["status"])
            if status.is_terminal:
                raise JsonRepairStateConflict(
                    f"repair job is already {status.value}"
                )

            params: list[Any] = [
                RepairJobStatus.CANCELLED.value,
                ProcessorErrorCode.CANCELLED.value,
                message,
                now,
                now,
                job_id,
                status.value,
            ]
            lease_guard = ""
            if status is RepairJobStatus.PROCESSING and not force:
                if owner is None:
                    raise JsonRepairLeaseLost(
                        "processing job cancellation requires its lease owner"
                    )
                if not self._lease_is_active(dict(row), owner, now):
                    raise JsonRepairLeaseLost(
                        "repair job lease is not owned or has expired"
                    )
                lease_guard = (
                    " AND lease_owner = ? AND lease_until IS NOT NULL"
                    " AND lease_until > ?"
                )
                params.extend([owner, now])
            cursor = connection.execute(
                f"""UPDATE json_repair_jobs
                    SET status = ?, error_code = ?, error_message = ?,
                        result_json = NULL, lease_owner = NULL, lease_until = NULL,
                        updated_at = ?, finished_at = ?
                    WHERE id = ? AND status = ?{lease_guard}""",
                tuple(params),
            )
            if cursor.rowcount != 1:
                latest = connection.execute(
                    "SELECT status FROM json_repair_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if latest is not None and RepairJobStatus(latest["status"]).is_terminal:
                    raise JsonRepairStateConflict(
                        f"repair job is already {latest['status']}"
                    )
                raise JsonRepairLeaseLost(
                    "repair job lease was lost during cancellation"
                )
            cancelled = connection.execute(
                "SELECT * FROM json_repair_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if cancelled is None:  # pragma: no cover - row existed above
                raise JsonRepairJobNotFound("repair job not found")
            return self._decode_job(cancelled)

    def cancel_jobs_for_run(
        self,
        *,
        task_run_id: str | None = None,
        vflow_run_id: str | None = None,
        point_id: str | None = None,
        force: bool = True,
        reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cancel every non-terminal job correlated with a Task/VFlow run."""

        filters = [
            item
            for item in (
                _optional_text("task_run_id", task_run_id, maximum=200),
                _optional_text("vflow_run_id", vflow_run_id, maximum=200),
                _optional_text("point_id", point_id, maximum=200),
            )
            if item is not None
        ]
        if not filters:
            raise ValueError("task_run_id, vflow_run_id, or point_id is required")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        message = "cancelled by run" if reason is None else _require_text(
            "reason", reason, maximum=500
        )
        conditions: list[str] = []
        params: list[Any] = []
        if task_run_id is not None:
            conditions.append("task_run_id = ?")
            params.append(_require_text("task_run_id", task_run_id, maximum=200))
        if vflow_run_id is not None:
            conditions.append("vflow_run_id = ?")
            params.append(_require_text("vflow_run_id", vflow_run_id, maximum=200))
        if point_id is not None:
            conditions.append("point_id = ?")
            params.append(_require_text("point_id", point_id, maximum=200))
        where = " AND ".join(conditions)
        now = utcnow()
        cancelled_ids: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""SELECT id, status FROM json_repair_jobs
                    WHERE {where} AND status IN ('queued', 'processing')""",
                tuple(params),
            ).fetchall()
            for row in rows:
                if row["status"] == RepairJobStatus.PROCESSING.value and not force:
                    continue
                cursor = connection.execute(
                    """UPDATE json_repair_jobs
                       SET status = 'cancelled', error_code = ?,
                           error_message = ?, result_json = NULL,
                           lease_owner = NULL, lease_until = NULL,
                           updated_at = ?, finished_at = ?
                       WHERE id = ? AND status = ?""",
                    (
                        ProcessorErrorCode.CANCELLED.value,
                        message,
                        now,
                        now,
                        row["id"],
                        row["status"],
                    ),
                )
                if cursor.rowcount == 1:
                    cancelled_ids.append(str(row["id"]))
            if not cancelled_ids:
                return []
            placeholders = ", ".join("?" for _ in cancelled_ids)
            cancelled = connection.execute(
                f"SELECT * FROM json_repair_jobs WHERE id IN ({placeholders})",
                tuple(cancelled_ids),
            ).fetchall()
            by_id = {str(item["id"]): item for item in cancelled}
            return [
                self._decode_job(by_id[item_id])
                for item_id in cancelled_ids
                if item_id in by_id
            ]

    def recover_expired_leases(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 500,
    ) -> int:
        """Return processing jobs with expired/missing leases to the queue."""

        moment = _as_utc(now).isoformat()
        recovery_limit = _positive_int("limit", limit, minimum=1, maximum=10_000)
        recovered = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM json_repair_jobs
                   WHERE status = 'processing'
                     AND (lease_until IS NULL OR lease_until <= ?)
                   ORDER BY updated_at, id
                   LIMIT ?""",
                (moment, recovery_limit),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """UPDATE json_repair_jobs
                       SET status = 'queued', lease_owner = NULL, lease_until = NULL,
                           updated_at = ?
                       WHERE id = ? AND status = 'processing'
                         AND (lease_until IS NULL OR lease_until <= ?)""",
                    (moment, row["id"], moment),
                )
                recovered += cursor.rowcount
        return recovered

    # ------------------------------------------------------------------
    # Attempts
    # ------------------------------------------------------------------
    def add_attempt(
        self,
        job_id: str,
        *,
        method: RepairMethod | str,
        attempt_no: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        request_meta: Mapping[str, Any] | None = None,
        validation_error: str | None = None,
        status: AttemptStatus | str = AttemptStatus.SUCCEEDED,
        latency_ms: int | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one ordered attempt record.

        ``request_meta`` is recursively sanitized: secret-shaped keys such as
        ``api_key`` or ``authorization`` are replaced with ``[redacted]``.
        """

        resolved_method = _enum_value("method", method, RepairMethod)
        resolved_status = _enum_value("status", status, AttemptStatus)
        if attempt_no is not None:
            attempt_no = _positive_int(
                "attempt_no", attempt_no, minimum=1, maximum=2_147_483_647
            )
        provider = _optional_text("provider", provider, maximum=200)
        model = _optional_text("model", model, maximum=200)
        if validation_error is not None:
            if not isinstance(validation_error, str):
                raise ValueError("validation_error must be a string")
            if len(validation_error) > 4_000:
                raise ValueError(
                    "validation_error must contain at most 4000 characters"
                )
        if latency_ms is not None:
            latency_ms = _positive_int(
                "latency_ms", latency_ms, minimum=0, maximum=86_400_000
            )
        if request_meta is None:
            request_meta_json = "{}"
        else:
            if not isinstance(request_meta, Mapping):
                raise ValueError("request_meta must be a JSON object")
            request_meta_json = _dump_json(
                sanitize_metadata(request_meta), field="request_meta"
            )
        if token_usage is None:
            token_usage_json = None
        else:
            if not isinstance(token_usage, Mapping):
                raise ValueError("token_usage must be a JSON object")
            token_usage_json = _dump_json(
                sanitize_metadata(token_usage), field="token_usage"
            )
        now = utcnow()
        try:
            with self.database.transaction() as connection:
                job = connection.execute(
                    "SELECT id FROM json_repair_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise JsonRepairJobNotFound("repair job not found")
                if attempt_no is None:
                    next_row = connection.execute(
                        """SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
                           FROM json_repair_attempts WHERE job_id = ?""",
                        (job_id,),
                    ).fetchone()
                    attempt_no = int(next_row["next_no"]) if next_row else 1
                attempt_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO json_repair_attempts
                       (id, job_id, attempt_no, method, provider, model,
                        request_meta_json, validation_error, status, latency_ms,
                        token_usage_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attempt_id,
                        job_id,
                        attempt_no,
                        resolved_method.value,
                        provider,
                        model,
                        request_meta_json,
                        validation_error or None,
                        resolved_status.value,
                        latency_ms,
                        token_usage_json,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM json_repair_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - insert above succeeded
                    raise JsonProcessorRepositoryError("attempt could not be read")
                return self._decode_attempt(row)
        except sqlite3.IntegrityError as exc:
            raise JsonRepairStateConflict(
                "attempt number already exists for this repair job"
            ) from exc

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        if self.get_job(job_id) is None:
            raise JsonRepairJobNotFound("repair job not found")
        rows = self.database._all(
            """SELECT * FROM json_repair_attempts
               WHERE job_id = ? ORDER BY attempt_no, id""",
            (job_id,),
        )
        return [self._decode_attempt(row) for row in rows]

    # ------------------------------------------------------------------
    # Retention: non-destructive preview plus an explicit purge routine
    # ------------------------------------------------------------------
    @staticmethod
    def _retention_cutoffs(
        settings: Mapping[str, Any], moment: datetime
    ) -> dict[str, str]:
        return {
            "raw_output_cutoff": (
                moment - timedelta(days=int(settings["raw_output_retention_days"]))
            ).isoformat(),
            "attempt_cutoff": (
                moment - timedelta(days=int(settings["attempt_retention_days"]))
            ).isoformat(),
            "raw_output_retention_days": int(settings["raw_output_retention_days"]),
            "attempt_retention_days": int(settings["attempt_retention_days"]),
        }

    def retention_cutoffs(
        self, now: datetime | str | None = None
    ) -> dict[str, Any]:
        return self._retention_cutoffs(self.get_settings(), _as_utc(now))

    def retention_preview(
        self, now: datetime | str | None = None
    ) -> dict[str, Any]:
        """Count rows that a retention purge would change right now.

        This is deliberately non-destructive.  Only terminal jobs are ever
        considered: an active or processing job and its attempts are always
        preserved.  Repaired output defaults to 30 days after the job became
        terminal; attempt audit rows default to 90 days.  Both values come from
        the settings singleton and are measured from immutable timestamps.
        """

        settings = self.get_settings()
        cutoffs = self._retention_cutoffs(settings, _as_utc(now))
        terminal_placeholders = ", ".join("?" for _ in _TERMINAL)
        jobs = self.database._one(
            f"""SELECT COUNT(*) AS count FROM json_repair_jobs
                WHERE status IN ({terminal_placeholders})
                  AND result_json IS NOT NULL
                  AND COALESCE(finished_at, created_at) <= ?""",
            (*_TERMINAL, cutoffs["raw_output_cutoff"]),
        )
        attempts = self.database._one(
            f"""SELECT COUNT(*) AS count
                FROM json_repair_attempts AS attempts
                JOIN json_repair_jobs AS jobs ON jobs.id = attempts.job_id
                WHERE jobs.status IN ({terminal_placeholders})
                  AND attempts.created_at <= ?""",
            (*_TERMINAL, cutoffs["attempt_cutoff"]),
        )
        return {
            **cutoffs,
            "jobs_with_expired_raw_output": int(jobs["count"]) if jobs else 0,
            "expired_attempts": int(attempts["count"]) if attempts else 0,
        }

    def retention_purge(
        self,
        *,
        now: datetime | str | None = None,
        batch_size: int = 500,
    ) -> dict[str, Any]:
        """Remove only expired processor-owned retention data.

        * Repaired output (``result_json``) is nulled for terminal jobs older
          than ``raw_output_retention_days``.  The job row, schema, hashes,
          correlation ids, error and timing metadata are kept.
        * Attempt audit rows are deleted only when their job is terminal and
          the attempt is older than ``attempt_retention_days``.
        * Queued/processing jobs are never touched, even if their timestamps
          are old or their lease expired.  Recovery owns that lifecycle.
        * External Task/VFlow raw output is *not* deleted here; the processor
          does not own those rows.  This routine only removes processor-owned
          copies and audit metadata.

        ``batch_size`` bounds each table operation.  The routine is idempotent
        and safe to call repeatedly; the returned ``remaining`` preview shows
        whether another batch is needed.
        """

        limit = _positive_int("batch_size", batch_size, minimum=1, maximum=10_000)
        moment = _as_utc(now)
        settings = self.get_settings()
        cutoffs = self._retention_cutoffs(settings, moment)
        timestamp = moment.isoformat()
        terminal_placeholders = ", ".join("?" for _ in _TERMINAL)
        jobs_output_purged = 0
        attempts_deleted = 0
        with self.database.transaction() as connection:
            expired_jobs = connection.execute(
                f"""SELECT id FROM json_repair_jobs
                    WHERE status IN ({terminal_placeholders})
                      AND result_json IS NOT NULL
                      AND COALESCE(finished_at, created_at) <= ?
                    ORDER BY COALESCE(finished_at, created_at), id
                    LIMIT ?""",
                (*_TERMINAL, cutoffs["raw_output_cutoff"], limit),
            ).fetchall()
            job_ids = [str(row["id"]) for row in expired_jobs]
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                cursor = connection.execute(
                    f"""UPDATE json_repair_jobs
                        SET result_json = NULL, updated_at = ?
                        WHERE id IN ({placeholders})
                          AND status IN ({terminal_placeholders})""",
                    (timestamp, *job_ids, *_TERMINAL),
                )
                jobs_output_purged = cursor.rowcount

            expired_attempts = connection.execute(
                f"""SELECT attempts.id
                    FROM json_repair_attempts AS attempts
                    JOIN json_repair_jobs AS jobs ON jobs.id = attempts.job_id
                    WHERE jobs.status IN ({terminal_placeholders})
                      AND attempts.created_at <= ?
                    ORDER BY attempts.created_at, attempts.id
                    LIMIT ?""",
                (*_TERMINAL, cutoffs["attempt_cutoff"], limit),
            ).fetchall()
            attempt_ids = [str(row["id"]) for row in expired_attempts]
            if attempt_ids:
                placeholders = ", ".join("?" for _ in attempt_ids)
                cursor = connection.execute(
                    f"DELETE FROM json_repair_attempts WHERE id IN ({placeholders})",
                    tuple(attempt_ids),
                )
                attempts_deleted = cursor.rowcount

        remaining = self.retention_preview(moment)
        return {
            **cutoffs,
            "jobs_output_purged": jobs_output_purged,
            "attempts_deleted": attempts_deleted,
            "batch_size": limit,
            "has_more": bool(
                remaining["jobs_with_expired_raw_output"]
                or remaining["expired_attempts"]
            ),
            "remaining": remaining,
            "purged_at": timestamp,
        }

    # ------------------------------------------------------------------
    # Small read helpers
    # ------------------------------------------------------------------
    def queue_depth(self) -> dict[str, int]:
        rows = self.database._all(
            "SELECT status, COUNT(*) AS count FROM json_repair_jobs GROUP BY status"
        )
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            status.value: counts.get(status.value, 0)
            for status in RepairJobStatus
        }


__all__ = [
    "JsonProcessorRepository",
    "JsonProcessorRepositoryError",
    "JsonRepairIdempotencyConflict",
    "JsonRepairJobNotFound",
    "JsonRepairLeaseLost",
    "JsonRepairStateConflict",
    "SettingsVersionConflict",
    "sanitize_metadata",
]
