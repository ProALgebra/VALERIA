"""Managed files published by consultation agents.

The agent writes deliverables into a per-job staging outbox.  The MCP server
publishes them through an authenticated internal endpoint; this service copies
each file into an immutable store and records only metadata in SQLite.  Nothing
in a user's workspace is ever served directly.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import secrets
import shutil
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings
from .database import Database
from .opencode_client import OpenCodeError, OpenCodeManager

logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = {"dispatching", "running", "waiting_question", "waiting_permission"}
_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_BLOCKED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/javascript",
    "application/javascript",
    "application/ecmascript",
    "text/ecmascript",
}
_SAFE_FALLBACK_CONTENT_TYPE = "application/octet-stream"
_MAX_NAME = 200
_MAX_DESCRIPTION = 2000
_MAX_SESSION_ANCESTRY = 64

CONSULTATION_ARTIFACT_INSTRUCTIONS = (
    "You can deliver files to the user as consultation artifacts. "
    "Write every final deliverable as a regular file at the top level of this "
    "outbox directory: {outbox} . Use {work} for intermediate files. "
    "Then call the artifact_publish tool from the consultation-artifacts MCP "
    "server with the file's absolute path, a short name and, when useful, a "
    "description. For a short text deliverable you may instead call the "
    "artifact_publish_text tool from the same server. "
    "Do not write into the repository, the user's workspace or any other directory. "
    "Do not paste file contents or base64 into the final answer. Mention the "
    "published file names in the final answer. If publishing fails, report the "
    "failure honestly; never claim that a file was delivered when the tool did "
    "not return an artifact id."
)


class ArtifactError(ValueError):
    """A publication or download cannot be completed safely."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class StagingPaths:
    work: Path
    outbox: Path
    inbox: Path


def public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Expose only browser-safe metadata; never storage paths or hashes."""
    return {
        "id": artifact["id"],
        "name": artifact["original_name"],
        "content_type": artifact["content_type"],
        "size_bytes": artifact["size_bytes"],
        "description": artifact.get("description"),
        "status": artifact["status"],
        "download_url": (
            f"/api/consultations/{artifact['chat_id']}/artifacts/{artifact['id']}"
        ),
    }


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ArtifactError(f"invalid_{label}", f"Invalid {label}")
    return value


def _safe_name(value: str | None, fallback: str | None = None) -> str:
    candidate = (value if value is not None else fallback or "").strip()
    if not candidate:
        raise ArtifactError("invalid_name", "Artifact name is required")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ArtifactError("invalid_name", "Artifact name must not contain a path")
    candidate = "".join(ch if ch.isprintable() else "_" for ch in candidate)
    candidate = candidate.replace("\r", "_").replace("\n", "_").replace("\t", "_").strip()
    if candidate in {"", ".", ".."}:
        raise ArtifactError("invalid_name", "Artifact name is invalid")
    if len(candidate) > _MAX_NAME:
        candidate = candidate[:_MAX_NAME].rstrip(". ")
    if not candidate:
        raise ArtifactError("invalid_name", "Artifact name is invalid")
    return candidate


def _safe_content_type(value: str | None, name: str) -> str:
    if value is not None:
        candidate = value.split(";", 1)[0].strip().lower()
    else:
        candidate = (mimetypes.guess_type(name)[0] or _SAFE_FALLBACK_CONTENT_TYPE).lower()
    if not _CONTENT_TYPE.fullmatch(candidate) or candidate in _BLOCKED_CONTENT_TYPES:
        return _SAFE_FALLBACK_CONTENT_TYPE
    return candidate


class ConsultationArtifacts:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        manager: OpenCodeManager | None = None,
    ):
        base = settings.database_path.parent
        self.settings = settings
        self.database = database
        self.manager = manager
        self.artifact_root = (
            settings.consultation_artifact_root or base / "consultation-artifacts"
        ).expanduser()
        self.staging_root = (
            settings.consultation_staging_root or base / "consultation-staging"
        ).expanduser()
        self.token_file = (
            settings.consultation_artifact_token_file or base / "consultation-artifacts.token"
        ).expanduser()
        self.max_bytes = settings.consultation_artifact_max_bytes
        self.max_files = settings.consultation_artifact_max_files
        self.max_total_bytes = settings.consultation_artifact_max_total_bytes
        self.max_text_bytes = settings.consultation_artifact_text_max_bytes
        self._token: str | None = None
        self._initialized = False

    @property
    def token(self) -> str:
        if not self._token:
            raise ArtifactError("not_initialized", "Artifact service is not initialized")
        return self._token

    def authorized(self, token: str | None) -> bool:
        return bool(token and self._token and secrets.compare_digest(token, self._token))

    def initialize(self) -> None:
        """Create private roots and a stable service token for the MCP server."""
        if self._initialized:
            return
        for root, label in (
            (self.artifact_root, "artifact"),
            (self.staging_root, "staging"),
        ):
            path = root.expanduser()
            if path.is_symlink():
                raise ConfigError(f"{label} root must not be a symlink")
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)
            except OSError as exc:
                raise ConfigError(f"Cannot create {label} root: {exc}") from exc
            if not path.is_dir():
                raise ConfigError(f"{label} root is not a directory")

        explicit = (self.settings.consultation_artifact_token or "").strip()
        token_file = self.token_file
        if token_file.is_symlink():
            raise ConfigError("Artifact token file must not be a symlink")
        file_token = ""
        if token_file.is_file():
            try:
                file_token = token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError("Cannot read artifact token file") from exc
        token = explicit or file_token
        if len(token) < 32:
            if token:
                raise ConfigError("CONSULTATION_ARTIFACT_TOKEN must contain at least 32 characters")
            token = secrets.token_urlsafe(48)
        if token != file_token:
            temporary: Path | None = None
            try:
                token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = token_file.with_name(f".{token_file.name}.{uuid.uuid4().hex}.tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    payload = token.encode("utf-8")
                    offset = 0
                    while offset < len(payload):
                        offset += os.write(fd, payload[offset:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, token_file)
                token_file.chmod(0o600)
            except OSError as exc:
                if temporary is not None:
                    with suppress(OSError):
                        temporary.unlink(missing_ok=True)
                raise ConfigError("Cannot write artifact token file") from exc
        self._token = token
        self._initialized = True

    def _staging_job_dir(self, chat_id: str, job_id: str) -> Path:
        chat = _safe_component(chat_id, "chat_id")
        job = _safe_component(job_id, "job_id")
        return self.staging_root / chat / job

    def _storage_path(self, storage_key: str) -> Path:
        if not storage_key or storage_key.startswith(("/", "\\")):
            raise ArtifactError("invalid_storage", "Invalid artifact storage key")
        parts = Path(storage_key).parts
        if len(parts) != 2:
            raise ArtifactError("invalid_storage", "Invalid artifact storage key")
        _safe_component(parts[0], "chat_id")
        _safe_component(parts[1], "artifact_id")
        candidate = (self.artifact_root / parts[0] / parts[1]).resolve()
        root = self.artifact_root.resolve()
        if not candidate.is_relative_to(root):
            raise ArtifactError("invalid_storage", "Artifact storage is outside its root")
        return candidate

    def prepare_job(self, chat_id: str, job_id: str) -> StagingPaths:
        paths = self._staging_job_dir(chat_id, job_id)
        work = paths / "work"
        outbox = paths / "outbox"
        inbox = paths / "inbox"
        try:
            for candidate in (paths.parent, paths, work, outbox, inbox):
                if candidate.is_symlink():
                    raise ArtifactError("unsafe_staging", "Staging path must not be a symlink")
            paths.mkdir(parents=True, exist_ok=True, mode=0o700)
            paths.chmod(0o700)
            for directory in (work, outbox, inbox):
                if directory.is_symlink():
                    raise ArtifactError("unsafe_staging", "Staging path must not be a symlink")
                directory.mkdir(exist_ok=True, mode=0o700)
                directory.chmod(0o700)
                if not directory.is_dir():
                    raise ArtifactError("unsafe_staging", "Staging path is not a directory")
            staging_root = self.staging_root.resolve()
            if not paths.resolve().is_relative_to(staging_root):
                raise ArtifactError("unsafe_staging", "Staging path is outside its root")
        except OSError as exc:
            raise ArtifactError("staging_unavailable", "Cannot prepare artifact staging") from exc
        return StagingPaths(
            work=work.resolve(), outbox=outbox.resolve(), inbox=inbox.resolve()
        )

    async def publish_from_session(
        self,
        session_id: str,
        *,
        path: str | None = None,
        text: str | None = None,
        name: str | None = None,
        description: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        chat = await self._resolve_session(session_id)
        if not chat:
            raise ArtifactError("session_not_found", "Consultation session was not found")
        job = self.database.active_consultation_job(chat["id"])
        if not job:
            raise ArtifactError("inactive_job", "Consultation has no active question")
        if (path is None) == (text is None):
            raise ArtifactError("invalid_source", "Provide exactly one of path or text")
        if text is not None:
            return self.publish_text_for_job(
                chat["id"],
                job["id"],
                name=name,
                text=text,
                description=description,
                content_type=content_type,
            )
        return self.publish_file_for_job(
            chat["id"],
            job["id"],
            path=path or "",
            name=name,
            description=description,
            content_type=content_type,
        )

    async def _resolve_session(self, session_id: str) -> dict[str, Any] | None:
        direct = self.database.consultation_for_session(session_id)
        if direct:
            return direct
        if not session_id or self.manager is None:
            return None
        active = self.database.active_consultation_jobs()
        by_session = {
            row["opencode_session_id"]: row for row in active if row.get("opencode_session_id")
        }
        if session_id in by_session:
            return self.database.get_chat(by_session[session_id]["chat_id"])
        workspaces = {
            row["workspace_snapshot"] for row in active if row.get("workspace_snapshot")
        }
        for workspace in workspaces:
            try:
                async with self.manager.use(
                    workspace, wake=False, activity=False
                ) as client:
                    if client is None:
                        continue
                    current = session_id
                    seen: set[str] = set()
                    for _ in range(_MAX_SESSION_ANCESTRY):
                        if not current or current in seen:
                            break
                        seen.add(current)
                        try:
                            info = await client.session(current)
                        except OpenCodeError:
                            break
                        if not info:
                            break
                        directory = info.get("directory")
                        if directory:
                            try:
                                if Path(directory).resolve() != Path(workspace).resolve():
                                    break
                            except (OSError, RuntimeError):
                                break
                        candidate = by_session.get(current)
                        if candidate:
                            return self.database.get_chat(candidate["chat_id"])
                        parent = info.get("parentID") or info.get("parentId")
                        if not parent:
                            break
                        current = parent
            except OpenCodeError:
                continue
        return None

    def publish_file_for_job(
        self,
        chat_id: str,
        job_id: str,
        *,
        path: str | Path,
        name: str | None = None,
        description: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        job = self._active_job(chat_id, job_id)
        staging = self.prepare_job(chat_id, job_id)
        source = Path(path)
        if not source.is_absolute():
            raise ArtifactError("invalid_path", "Artifact path must be absolute")
        try:
            if source.is_symlink():
                raise ArtifactError("unsafe_path", "Symbolic links are not allowed")
            real_source = source.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactError("not_found", "Artifact file was not found") from exc
        except (OSError, RuntimeError) as exc:
            raise ArtifactError("invalid_path", "Artifact path cannot be resolved") from exc
        if real_source.parent != staging.outbox.resolve():
            raise ArtifactError(
                "outside_outbox", "Artifact file must be at the top level of the job outbox"
            )
        if not real_source.is_file():
            raise ArtifactError("not_regular", "Artifact must be a regular file")

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(real_source, flags)
        except OSError as exc:
            raise ArtifactError("unsafe_path", "Artifact cannot be opened safely") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactError("not_regular", "Artifact must be a regular file")
            if info.st_nlink != 1:
                raise ArtifactError("hardlink", "Hard-linked artifacts are not allowed")
            if info.st_size > self.max_bytes:
                raise ArtifactError("too_large", "Artifact exceeds the size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise ArtifactError("too_large", "Artifact exceeds the size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
        except OSError as exc:
            raise ArtifactError("invalid_path", "Artifact cannot be read") from exc
        finally:
            os.close(fd)
        filename = _safe_name(name, real_source.name)
        return self._store(
            chat_id,
            job["id"],
            data,
            name=filename,
            description=description,
            content_type=_safe_content_type(content_type, filename),
        )

    def publish_text_for_job(
        self,
        chat_id: str,
        job_id: str,
        *,
        name: str | None,
        text: str | None,
        description: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        job = self._active_job(chat_id, job_id)
        if not isinstance(text, str) or not text:
            raise ArtifactError("empty_text", "Text artifact must not be empty")
        filename = _safe_name(name, "artifact.txt")
        data = text.encode("utf-8")
        if len(data) > self.max_text_bytes:
            raise ArtifactError("too_large", "Text artifact exceeds the size limit")
        return self._store(
            chat_id,
            job["id"],
            data,
            name=filename,
            description=description,
            content_type=_safe_content_type(content_type or "text/plain", filename),
        )

    def _active_job(self, chat_id: str, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if (
            not job
            or job["chat_id"] != chat_id
            or not job.get("consultation")
            or job["status"] not in ACTIVE_JOB_STATUSES
        ):
            raise ArtifactError("inactive_job", "Consultation has no active question")
        return job

    def _store(
        self,
        chat_id: str,
        job_id: str,
        data: bytes,
        *,
        name: str,
        description: str | None,
        content_type: str,
    ) -> dict[str, Any]:
        if description is not None and len(description) > _MAX_DESCRIPTION:
            raise ArtifactError("invalid_description", "Artifact description is too long")
        _safe_component(chat_id, "chat_id")
        _safe_component(job_id, "job_id")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = uuid.uuid4().hex
        storage_key = f"{chat_id}/{artifact_id}"
        destination = self._storage_path(storage_key)
        temporary = destination.with_name(f".{artifact_id}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.parent.chmod(0o700)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise OSError("short write")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, destination)
            destination.chmod(0o600)
            try:
                row = self.database.create_consultation_artifact(
                    artifact_id=artifact_id,
                    chat_id=chat_id,
                    job_id=job_id,
                    original_name=name,
                    content_type=content_type,
                    size_bytes=len(data),
                    sha256=digest,
                    description=description,
                    storage_key=storage_key,
                    max_files=self.max_files,
                    max_total_bytes=self.max_total_bytes,
                )
            except ValueError as exc:
                raise ArtifactError(str(exc), "Artifact was rejected") from exc
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            with suppress(OSError):
                destination.unlink(missing_ok=True)
            raise ArtifactError("storage_unavailable", "Cannot store artifact") from exc
        except BaseException:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            with suppress(OSError):
                destination.unlink(missing_ok=True)
            raise
        if row["id"] != artifact_id:
            with suppress(OSError):
                destination.unlink(missing_ok=True)
        return row

    def publish_outbox(self, chat_id: str, job_id: str) -> list[dict[str, Any]]:
        """Fallback for files the model wrote but did not publish explicitly."""
        staging = self.prepare_job(chat_id, job_id)
        published = []
        try:
            entries = sorted(staging.outbox.iterdir(), key=lambda item: item.name)
        except OSError:
            return published
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_file():
                continue
            try:
                published.append(
                    self.publish_file_for_job(chat_id, job_id, path=entry)
                )
            except ArtifactError as exc:
                logger.warning(
                    "Skipping consultation outbox file",
                    extra={"job_id": job_id, "path": str(entry), "reason": exc.code},
                )
        return published

    def cleanup_job(self, job_id: str) -> None:
        job = self.database.get_job(job_id)
        chat_id = job["chat_id"] if job else None
        for storage_key in self.database.discard_pending_consultation_artifacts(job_id):
            with suppress(ArtifactError, OSError):
                self._storage_path(storage_key).unlink(missing_ok=True)
        if chat_id:
            self.cleanup_staging(chat_id, job_id)

    def cleanup_staging(self, chat_id: str, job_id: str) -> None:
        path = self._staging_job_dir(chat_id, job_id)
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.staging_root.resolve()):
                return
            shutil.rmtree(resolved, ignore_errors=True)
        except (OSError, RuntimeError):
            logger.warning("Cannot remove artifact staging", extra={"job_id": job_id})

    def download_path(self, chat_id: str, artifact_id: str) -> tuple[Path, dict[str, Any]]:
        artifact = self.database.consultation_artifact(chat_id, artifact_id)
        if not artifact:
            raise ArtifactError("not_found", "Artifact not found")
        if artifact["status"] != "ready":
            raise ArtifactError("not_ready", "Artifact is not available")
        path = self._storage_path(artifact["storage_key"])
        if not path.is_file() or path.is_symlink():
            self.database.mark_consultation_artifact_missing(artifact_id)
            raise ArtifactError("missing", "Artifact file is missing")
        return path, artifact
