"""Persistence layer for VFlow definitions, versions and publication state.

All writes go through ``Database.transaction()`` so they participate in the
same SQLite write lock as the rest of VALERIA. Runtime tables will use the same
repository pattern later; this module intentionally contains no scheduler or
Task Gateway logic.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from ..database import Database, utcnow
from .redaction import redact_metadata

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class VFlowNotFound(Exception):
    pass


class VFlowBadRequest(Exception):
    pass


class VFlowConflict(Exception):
    pass


class VFlowPreconditionFailed(Exception):
    pass


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso_now() -> str:
    return utcnow()


class VFlowRepository:
    def __init__(self, database: Database):
        self.database = database

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------
    def create_definition(
        self,
        actor_id: str,
        *,
        slug: str,
        title: str,
        description: str | None = None,
        audience: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        slug = (slug or "").strip().lower()
        title = (title or "").strip()
        if not SLUG_RE.fullmatch(slug):
            raise VFlowBadRequest("slug must match [a-z0-9]+(-[a-z0-9]+)*")
        if not title:
            raise VFlowBadRequest("title is required")
        definition_id = str(uuid.uuid4())
        now = _iso_now()
        audience_json = _dump(audience or {"mode": "all"})
        settings_json = _dump(settings or {})
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO vflow_definitions
                       (id, slug, title, description, created_by, status,
                        audience_json, settings_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                    (
                        definition_id,
                        slug,
                        title,
                        description.strip() if description else None,
                        actor_id,
                        audience_json,
                        settings_json,
                        now,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    actor_id,
                    "definition.create",
                    "definition",
                    definition_id,
                    {"slug": slug},
                )
        except sqlite3.IntegrityError as exc:
            raise VFlowConflict("A pipeline with this slug already exists") from exc
        return self.get_definition(definition_id)  # type: ignore[return-value]

    def get_definition(self, definition_id: str) -> dict[str, Any] | None:
        return self.database._one(
            "SELECT * FROM vflow_definitions WHERE id = ?", (definition_id,)
        )

    def get_definition_by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.database._one(
            "SELECT * FROM vflow_definitions WHERE slug = ?", (slug,)
        )

    def find_definition(self, identifier: str) -> dict[str, Any] | None:
        return self.get_definition(identifier) or self.get_definition_by_slug(identifier)

    def list_definitions(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE status = 'active'"
        return self.database._all(
            f"SELECT * FROM vflow_definitions {where} ORDER BY updated_at DESC"
        )

    def update_definition_metadata(
        self,
        definition_id: str,
        actor_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_definition(definition_id)
        if current is None:
            raise VFlowNotFound("Pipeline not found")
        updates: list[str] = []
        params: list[Any] = []
        if title is not None:
            title = title.strip()
            if not title:
                raise VFlowBadRequest("title is required")
            updates.append("title = ?")
            params.append(title)
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip() or None)
        if not updates:
            return current
        updates.append("updated_at = ?")
        params.append(_iso_now())
        params.append(definition_id)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE vflow_definitions SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            self._audit(
                connection,
                actor_id,
                "definition.update",
                "definition",
                definition_id,
                {},
            )
        return self.get_definition(definition_id)  # type: ignore[return-value]

    def set_audience(
        self,
        definition_id: str,
        actor_id: str,
        audience: dict[str, Any],
    ) -> dict[str, Any]:
        if audience.get("mode") not in {"all", "selected"}:
            raise VFlowBadRequest("audience.mode must be 'all' or 'selected'")
        if audience.get("mode") == "selected":
            user_ids = audience.get("user_ids")
            if not isinstance(user_ids, list) or any(
                not isinstance(item, str) or not item for item in user_ids
            ):
                raise VFlowBadRequest("audience.user_ids must be a list of strings")
            audience = {"mode": "selected", "user_ids": list(dict.fromkeys(user_ids))}
        else:
            audience = {"mode": "all"}
        if self.get_definition(definition_id) is None:
            raise VFlowNotFound("Pipeline not found")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_definitions SET audience_json = ?, updated_at = ? WHERE id = ?",
                (_dump(audience), _iso_now(), definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "definition.audience",
                "definition",
                definition_id,
                {"mode": audience["mode"]},
            )
        return self.get_definition(definition_id)  # type: ignore[return-value]

    def set_settings(
        self,
        definition_id: str,
        actor_id: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        if self.get_definition(definition_id) is None:
            raise VFlowNotFound("Pipeline not found")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_definitions SET settings_json = ?, updated_at = ? WHERE id = ?",
                (_dump(settings), _iso_now(), definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "definition.settings",
                "definition",
                definition_id,
                {
                    "cancel_own": settings.get("cancel_own", True),
                    "limits": sorted((settings.get("limits") or {}).keys()),
                },
            )
        return self.get_definition(definition_id)  # type: ignore[return-value]

    def archive_definition(self, definition_id: str, actor_id: str) -> dict[str, Any]:
        current = self.get_definition(definition_id)
        if current is None:
            raise VFlowNotFound("Pipeline not found")
        if current["status"] == "archived":
            return current
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE vflow_definitions SET status = 'archived', updated_at = ? WHERE id = ?",
                (_iso_now(), definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "definition.archive",
                "definition",
                definition_id,
                {},
            )
        return self.get_definition(definition_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Versions and drafts
    # ------------------------------------------------------------------
    def get_version(self, version_id: str) -> dict[str, Any] | None:
        return self.database._one(
            "SELECT * FROM vflow_versions WHERE id = ?", (version_id,)
        )

    def get_version_for_definition(
        self, definition_id: str, version_id: str
    ) -> dict[str, Any] | None:
        return self.database._one(
            "SELECT * FROM vflow_versions WHERE id = ? AND definition_id = ?",
            (version_id, definition_id),
        )

    def list_versions(self, definition_id: str) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT * FROM vflow_versions
               WHERE definition_id = ? ORDER BY version_number DESC""",
            (definition_id,),
        )

    def get_draft(self, definition_id: str) -> dict[str, Any] | None:
        definition = self.get_definition(definition_id)
        if not definition or not definition.get("draft_version_id"):
            return None
        return self.get_version(definition["draft_version_id"])

    def get_published(self, definition_id: str) -> dict[str, Any] | None:
        definition = self.get_definition(definition_id)
        if not definition or not definition.get("published_version_id"):
            return None
        return self.get_version(definition["published_version_id"])

    def create_draft_version(
        self,
        definition_id: str,
        actor_id: str,
        *,
        graph_ir: dict[str, Any],
        source: str | None,
        input_schema: dict[str, Any],
        public_output_schema: dict[str, Any] | None,
        required_handlers: tuple[str, ...] | list[str],
        graph_hash: str,
    ) -> dict[str, Any]:
        definition = self.get_definition(definition_id)
        if definition is None:
            raise VFlowNotFound("Pipeline not found")
        if definition["status"] != "active":
            raise VFlowConflict("Archived pipelines cannot be edited")
        version_id = str(uuid.uuid4())
        now = _iso_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number
                   FROM vflow_versions WHERE definition_id = ?""",
                (definition_id,),
            ).fetchone()
            version_number = int(row["next_number"])
            connection.execute(
                """INSERT INTO vflow_versions
                   (id, definition_id, version_number, status, graph_ir_json,
                    vflow_source, input_schema_json, public_output_schema_json,
                    required_handlers_json, graph_hash, created_by, created_at)
                   VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    definition_id,
                    version_number,
                    _dump(graph_ir),
                    source,
                    _dump(input_schema),
                    _dump(public_output_schema) if public_output_schema is not None else None,
                    _dump(list(required_handlers)),
                    graph_hash,
                    actor_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE vflow_definitions SET draft_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "draft.create",
                "version",
                version_id,
                {"definition_id": definition_id, "version_number": version_number},
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    def update_draft(
        self,
        definition_id: str,
        version_id: str,
        actor_id: str,
        *,
        graph_ir: dict[str, Any],
        source: str | None,
        input_schema: dict[str, Any],
        public_output_schema: dict[str, Any] | None,
        required_handlers: tuple[str, ...] | list[str],
        graph_hash: str,
    ) -> dict[str, Any]:
        version = self.get_version_for_definition(definition_id, version_id)
        if version is None:
            raise VFlowNotFound("Draft not found")
        if version["status"] != "draft":
            raise VFlowConflict("Only draft versions can be edited")
        now = _iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_versions SET
                     graph_ir_json = ?, vflow_source = ?, input_schema_json = ?,
                     public_output_schema_json = ?, required_handlers_json = ?,
                     graph_hash = ?
                   WHERE id = ? AND status = 'draft'""",
                (
                    _dump(graph_ir),
                    source,
                    _dump(input_schema),
                    _dump(public_output_schema) if public_output_schema is not None else None,
                    _dump(list(required_handlers)),
                    graph_hash,
                    version_id,
                ),
            )
            connection.execute(
                "UPDATE vflow_definitions SET updated_at = ? WHERE id = ?",
                (now, definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "draft.update",
                "version",
                version_id,
                {"definition_id": definition_id},
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    def publish_version(
        self, definition_id: str, version_id: str, actor_id: str
    ) -> dict[str, Any]:
        definition = self.get_definition(definition_id)
        if definition is None:
            raise VFlowNotFound("Pipeline not found")
        version = self.get_version_for_definition(definition_id, version_id)
        if version is None:
            raise VFlowNotFound("Version not found")
        if version["status"] == "published" and definition["published_version_id"] == version_id:
            return version
        if version["status"] != "draft":
            raise VFlowConflict("Only a draft can be published")
        now = _iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_versions SET status = 'superseded'
                   WHERE definition_id = ? AND status = 'published'""",
                (definition_id,),
            )
            connection.execute(
                """UPDATE vflow_versions
                   SET status = 'published', published_at = ?
                   WHERE id = ? AND status = 'draft'""",
                (now, version_id),
            )
            connection.execute(
                """UPDATE vflow_definitions
                   SET published_version_id = ?, draft_version_id = NULL, updated_at = ?
                   WHERE id = ?""",
                (version_id, now, definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "version.publish",
                "version",
                version_id,
                {"definition_id": definition_id},
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    def rollback(
        self, definition_id: str, version_id: str, actor_id: str
    ) -> dict[str, Any]:
        definition = self.get_definition(definition_id)
        if definition is None:
            raise VFlowNotFound("Pipeline not found")
        version = self.get_version_for_definition(definition_id, version_id)
        if version is None:
            raise VFlowNotFound("Version not found")
        if version["status"] == "draft":
            raise VFlowConflict("A draft cannot be used as a rollback target")
        if definition["published_version_id"] == version_id:
            return version
        now = _iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE vflow_versions SET status = 'superseded'
                   WHERE definition_id = ? AND status = 'published'""",
                (definition_id,),
            )
            connection.execute(
                """UPDATE vflow_versions
                   SET status = 'published', published_at = COALESCE(published_at, ?)
                   WHERE id = ?""",
                (now, version_id),
            )
            connection.execute(
                """UPDATE vflow_definitions
                   SET published_version_id = ?, updated_at = ? WHERE id = ?""",
                (version_id, now, definition_id),
            )
            self._audit(
                connection,
                actor_id,
                "version.rollback",
                "version",
                version_id,
                {"definition_id": definition_id},
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # User-facing catalog
    # ------------------------------------------------------------------
    def catalog_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.database._all(
            """SELECT d.id, d.slug, d.title, d.description, d.audience_json,
                      d.settings_json, d.published_version_id,
                      v.id AS version_id, v.version_number, v.input_schema_json,
                      v.public_output_schema_json, v.published_at
               FROM vflow_definitions d
               JOIN vflow_versions v ON v.id = d.published_version_id
               WHERE d.status = 'active' AND v.status = 'published'
               ORDER BY v.published_at DESC, d.title COLLATE NOCASE"""
        )
        visible: list[dict[str, Any]] = []
        for row in rows:
            audience = _load(row["audience_json"], {"mode": "all"})
            mode = audience.get("mode", "all")
            if mode == "selected" and user_id not in audience.get("user_ids", []):
                continue
            if mode not in {"all", "selected"}:
                continue
            visible.append(row)
        return visible

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def version_etag(version: dict[str, Any]) -> str:
        payload = {
            "graph_ir_json": version.get("graph_ir_json"),
            "vflow_source": version.get("vflow_source"),
            "input_schema_json": version.get("input_schema_json"),
            "public_output_schema_json": version.get("public_output_schema_json"),
            "required_handlers_json": version.get("required_handlers_json"),
            "graph_hash": version.get("graph_hash"),
        }
        digest = hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest()
        return f'"{digest}"'

    def _audit(
        self,
        connection: sqlite3.Connection,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        metadata: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO vflow_audit_events
               (actor_user_id, action, object_type, object_id, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                actor_id,
                action,
                object_type,
                object_id,
                _dump(redact_metadata(metadata)),
                _iso_now(),
            ),
        )
