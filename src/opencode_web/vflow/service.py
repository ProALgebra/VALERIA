"""Definition lifecycle, validation and publication for VFlow.

The service is deliberately synchronous: it is called from short HTTP request
handlers and never waits for OpenCode. Runtime execution will be added as a
separate scheduler on top of the same repository.
"""

from __future__ import annotations

import json
from typing import Any

from ..database import Database
from .compiler import MISSING_MODES, compile_graph, compile_source
from .formatter import format_graph
from .limits import DEFAULT_LIMITS, VFlowLimits
from .logic import IF_OPERATORS, LogicError, apply_transform, evaluate_if
from .models import Diagnostic, Graph, ValidationError, VFlowError
from .policy import PolicyError, cancel_own, validate_policy
from .registry import DEFAULT_REGISTRY, TASK, NodeRegistry
from .repository import (
    VFlowBadRequest,
    VFlowConflict,
    VFlowNotFound,
    VFlowPreconditionFailed,
    VFlowRepository,
)

_UNSET = object()


class VFlowService:
    def __init__(
        self,
        database: Database,
        registry: NodeRegistry = DEFAULT_REGISTRY,
        limits: VFlowLimits = DEFAULT_LIMITS,
    ):
        self.database = database
        self.registry = registry
        self.limits = limits
        self.repository = VFlowRepository(database)

    # ------------------------------------------------------------------
    # Admin definitions and drafts
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
        if settings is not None:
            settings = self._validate_settings(settings)
        definition = self.repository.create_definition(
            actor_id,
            slug=slug,
            title=title,
            description=description,
            audience=audience,
            settings=settings,
        )
        return self._admin_definition_summary(definition)

    def list_admin_definitions(self) -> list[dict[str, Any]]:
        return [
            self._admin_definition_summary(item)
            for item in self.repository.list_definitions()
        ]

    def admin_definition(self, definition_id: str) -> dict[str, Any]:
        definition = self._require_definition(definition_id)
        versions = [
            self._admin_version_summary(item)
            for item in self.repository.list_versions(definition_id)
        ]
        draft = self.repository.get_draft(definition_id)
        published = self.repository.get_published(definition_id)
        result = self._admin_definition_summary(definition)
        result["versions"] = versions
        result["draft"] = self._admin_version_view(draft) if draft else None
        result["published"] = self._admin_version_view(published) if published else None
        return result

    def save_draft(
        self,
        definition_id: str,
        actor_id: str,
        *,
        source: str | None = None,
        graph: dict[str, Any] | None = None,
        input_schema: Any = _UNSET,
        public_output_schema: Any = _UNSET,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        definition = self._require_definition(definition_id)
        if definition["status"] != "active":
            raise VFlowConflict("Archived pipelines cannot be edited")
        if source is None and graph is None:
            raise VFlowBadRequest("Either source or graph is required")
        if input_schema is not _UNSET and input_schema is not None and not isinstance(
            input_schema, dict
        ):
            raise VFlowBadRequest("input_schema must be a JSON object")
        if (
            public_output_schema is not _UNSET
            and public_output_schema is not None
            and not isinstance(public_output_schema, dict)
        ):
            raise VFlowBadRequest("public_output_schema must be a JSON object or null")

        compiled = None
        if source is not None:
            compiled = compile_source(source, registry=self.registry)
        if graph is not None:
            graph_compiled = compile_graph(graph, registry=self.registry, source=source or "")
            if compiled is not None and compiled.graph.canonical_json() != (
                graph_compiled.graph.canonical_json()
            ):
                raise VFlowBadRequest("source and graph describe different workflows")
            compiled = graph_compiled
        if compiled is None:
            raise VFlowBadRequest("Either source or graph is required")
        if source is None:
            source = format_graph(compiled.graph)

        existing = self.repository.get_draft(definition_id)
        if existing is not None:
            if not expected_etag:
                raise VFlowPreconditionFailed("If-Match is required when updating a draft")
            if expected_etag != self.repository.version_etag(existing):
                raise VFlowPreconditionFailed("Draft ETag does not match")
            schema = (
                self._load_json(existing.get("input_schema_json"), {})
                if input_schema is _UNSET
                else (input_schema if input_schema is not None else {})
            )
            public_schema = (
                self._load_json(existing.get("public_output_schema_json"), None)
                if public_output_schema is _UNSET
                else public_output_schema
            )
            updated = self.repository.update_draft(
                definition_id,
                existing["id"],
                actor_id,
                graph_ir=compiled.graph.to_dict(),
                source=source,
                input_schema=schema,
                public_output_schema=public_schema,
                required_handlers=compiled.required_handlers,
                graph_hash=compiled.graph_hash,
            )
            return self._admin_version_view(updated)

        published = self.repository.get_published(definition_id)
        if input_schema is _UNSET:
            input_schema = (
                self._load_json(published.get("input_schema_json"), {})
                if published
                else {}
            )
        elif input_schema is None:
            input_schema = {}
        if public_output_schema is _UNSET:
            public_output_schema = (
                self._load_json(published.get("public_output_schema_json"), None)
                if published
                else None
            )
        created = self.repository.create_draft_version(
            definition_id,
            actor_id,
            graph_ir=compiled.graph.to_dict(),
            source=source,
            input_schema=input_schema,
            public_output_schema=public_output_schema,
            required_handlers=compiled.required_handlers,
            graph_hash=compiled.graph_hash,
        )
        return self._admin_version_view(created)

    def validate_definition(
        self,
        definition_id: str,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        version = self._version_for_validation(definition_id, version_id)
        graph_value = self._load_json(version["graph_ir_json"], {})
        diagnostics: list[Diagnostic] = []
        try:
            compiled = compile_graph(graph_value, registry=self.registry)
        except VFlowError as exc:
            diagnostics.extend(exc.diagnostics)
            compiled = None
        if compiled is not None:
            diagnostics.extend(self._validate_tasks(compiled.graph.nodes))
        return {
            "valid": not diagnostics,
            "diagnostics": [item.to_dict() for item in diagnostics],
            "graph_hash": compiled.graph_hash if compiled else None,
            "required_handlers": (
                list(compiled.required_handlers) if compiled else []
            ),
        }

    def publish(
        self,
        definition_id: str,
        actor_id: str,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        draft = self.repository.get_draft(definition_id)
        if draft is None:
            raise VFlowBadRequest("There is no draft to publish")
        if expected_etag and expected_etag != self.repository.version_etag(draft):
            raise VFlowPreconditionFailed("Draft ETag does not match")
        report = self.validate_definition(definition_id, version_id=draft["id"])
        if not report["valid"]:
            diagnostics = [
                Diagnostic(
                    code=item["code"],
                    message=item["message"],
                    line=item["line"],
                    column=item["column"],
                    node_id=item["node_id"],
                    edge_id=item["edge_id"],
                )
                for item in report["diagnostics"]
            ]
            raise ValidationError(diagnostics)
        published = self.repository.publish_version(definition_id, draft["id"], actor_id)
        return self._admin_version_view(published)

    def rollback(
        self, definition_id: str, version_id: str, actor_id: str
    ) -> dict[str, Any]:
        self._require_definition(definition_id)
        version = self.repository.rollback(definition_id, version_id, actor_id)
        return self._admin_version_view(version)

    def archive(self, definition_id: str, actor_id: str) -> dict[str, Any]:
        definition = self.repository.archive_definition(definition_id, actor_id)
        return self._admin_definition_summary(definition)

    def set_audience(
        self, definition_id: str, actor_id: str, audience: dict[str, Any]
    ) -> dict[str, Any]:
        if audience.get("mode") == "selected":
            for user_id in audience.get("user_ids", []):
                if self.database.get_user(user_id) is None:
                    raise VFlowBadRequest(f"Unknown user: {user_id}")
        definition = self.repository.set_audience(definition_id, actor_id, audience)
        return self._admin_definition_summary(definition)

    def set_settings(
        self,
        definition_id: str,
        actor_id: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = self._validate_settings(settings)
        definition = self.repository.set_settings(definition_id, actor_id, normalized)
        return self._admin_definition_summary(definition)

    def cancel_allowed(self, definition_id: str) -> bool:
        definition = self.repository.get_definition(definition_id)
        if definition is None:
            return True
        return cancel_own(definition.get("settings_json"), self.limits)

    # ------------------------------------------------------------------
    # User catalog
    # ------------------------------------------------------------------
    def user_catalog(self, user_id: str) -> list[dict[str, Any]]:
        return [
            self._user_version_view(item)
            for item in self.repository.catalog_for_user(user_id)
        ]

    def user_pipeline(self, identifier: str, user_id: str) -> dict[str, Any]:
        for item in self.repository.catalog_for_user(user_id):
            if identifier in {item["id"], item["slug"]}:
                return self._user_version_view(item)
        raise VFlowNotFound("Pipeline not found")

    def user_run_target(self, identifier: str, user_id: str) -> dict[str, Any]:
        """Return the internal catalog row needed to create a run."""
        for item in self.repository.catalog_for_user(user_id):
            if identifier in {item["id"], item["slug"]}:
                return item
        raise VFlowNotFound("Pipeline not found")

    # ------------------------------------------------------------------
    # Admin helper endpoints
    # ------------------------------------------------------------------
    def block_registry(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.registry.all()]

    def tasks_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": task["id"],
                "title": task["title"],
                "description": task["description"],
                "is_published": bool(task["is_published"]),
                "queue_id": task["queue_id"],
                "updated_at": task["updated_at"],
            }
            for task in self.database.list_tasks(published_only=False)
        ]

    def compile_preview(self, source: str) -> dict[str, Any]:
        compiled = compile_source(source, registry=self.registry)
        return {
            "graph": compiled.graph.to_dict(),
            "graph_hash": compiled.graph_hash,
            "required_handlers": list(compiled.required_handlers),
        }

    def format_graph(self, graph: dict[str, Any]) -> str:
        try:
            return format_graph(Graph.from_dict(graph))
        except (TypeError, ValueError) as exc:
            raise VFlowBadRequest(str(exc)) from exc

    def preview_transform(
        self,
        data: Any,
        operations: list[dict[str, Any]],
        *,
        create_missing: bool = False,
    ) -> Any:
        if not isinstance(operations, list) or not operations:
            raise VFlowBadRequest("operations must be a non-empty list")
        try:
            return apply_transform(
                data,
                {"operations": operations, "create_missing": create_missing},
            )
        except LogicError as exc:
            raise VFlowBadRequest(str(exc)) from exc

    def preview_if(
        self,
        input_data: Any,
        path: str,
        operator: str,
        value: Any = None,
        missing: str = "error",
    ) -> dict[str, Any]:
        """Evaluate one If condition without creating a run or writing state."""
        if not isinstance(operator, str) or operator not in IF_OPERATORS:
            raise VFlowBadRequest(f"Invalid if operator: {operator!r}")
        if not isinstance(missing, str) or missing not in MISSING_MODES:
            raise VFlowBadRequest(f"Invalid missing mode: {missing!r}")
        if not isinstance(path, str) or not path:
            raise VFlowBadRequest("if requires a path")
        try:
            result = evaluate_if(
                input_data,
                {
                    "path": path,
                    "operator": operator,
                    "value": value,
                    "missing": missing,
                },
            )
        except LogicError as exc:
            raise VFlowBadRequest(str(exc)) from exc
        return {"result": result, "port": "true" if result else "false"}

    # ------------------------------------------------------------------
    # Internal views and checks
    # ------------------------------------------------------------------
    def _validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_policy(settings, self.limits)
        except PolicyError as exc:
            raise VFlowBadRequest(str(exc)) from exc

    def _require_definition(self, definition_id: str) -> dict[str, Any]:
        definition = self.repository.find_definition(definition_id)
        if definition is None:
            raise VFlowNotFound("Pipeline not found")
        return definition

    def _version_for_validation(
        self, definition_id: str, version_id: str | None
    ) -> dict[str, Any]:
        if version_id:
            version = self.repository.get_version_for_definition(definition_id, version_id)
        else:
            version = self.repository.get_draft(definition_id)
            if version is None:
                version = self.repository.get_published(definition_id)
        if version is None:
            raise VFlowNotFound("No draft or published version to validate")
        return version

    def _validate_tasks(self, nodes: tuple[Any, ...]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for node in nodes:
            if node.type != TASK:
                continue
            task_id = node.config.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                diagnostics.append(
                    Diagnostic(
                        "TASK_ID_REQUIRED",
                        "Task node requires a task_id",
                        node_id=node.id,
                    )
                )
                continue
            if self.database.get_task(task_id) is None:
                diagnostics.append(
                    Diagnostic(
                        "TASK_NOT_FOUND",
                        f"Task {task_id!r} does not exist or was deleted",
                        node_id=node.id,
                    )
                )
        return diagnostics

    def _admin_definition_summary(self, definition: dict[str, Any]) -> dict[str, Any]:
        draft = (
            self.repository.get_version(definition["draft_version_id"])
            if definition.get("draft_version_id")
            else None
        )
        published = (
            self.repository.get_version(definition["published_version_id"])
            if definition.get("published_version_id")
            else None
        )
        return {
            "id": definition["id"],
            "slug": definition["slug"],
            "title": definition["title"],
            "description": definition["description"],
            "status": definition["status"],
            "created_by": definition["created_by"],
            "created_at": definition["created_at"],
            "updated_at": definition["updated_at"],
            "audience": self._load_json(definition["audience_json"], {"mode": "all"}),
            "settings": self._load_json(definition["settings_json"], {}),
            "draft_version_id": definition.get("draft_version_id"),
            "published_version_id": definition.get("published_version_id"),
            "draft_version_number": draft["version_number"] if draft else None,
            "published_version_number": (
                published["version_number"] if published else None
            ),
            "published_at": published.get("published_at") if published else None,
        }

    def _admin_version_summary(self, version: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": version["id"],
            "version_number": version["version_number"],
            "status": version["status"],
            "graph_hash": version["graph_hash"],
            "created_at": version["created_at"],
            "published_at": version["published_at"],
            "etag": self.repository.version_etag(version),
        }

    def _admin_version_view(self, version: dict[str, Any]) -> dict[str, Any]:
        result = self._admin_version_summary(version)
        result.update(
            {
                "definition_id": version["definition_id"],
                "graph": self._load_json(version["graph_ir_json"], {}),
                "source": version.get("vflow_source"),
                "input_schema": self._load_json(version["input_schema_json"], {}),
                "public_output_schema": self._load_json(
                    version.get("public_output_schema_json"), None
                ),
                "required_handlers": self._load_json(
                    version["required_handlers_json"], []
                ),
            }
        )
        return result

    def _user_version_view(self, row: dict[str, Any]) -> dict[str, Any]:
        public_schema = self._load_json(row.get("public_output_schema_json"), None)
        return {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "description": row["description"],
            "version": row["version_number"],
            "published_at": row["published_at"],
            "input_schema": self._load_json(row["input_schema_json"], {}),
            "public_result_enabled": isinstance(public_schema, dict),
        }

    @staticmethod
    def _load_json(value: str | None, default: Any) -> Any:
        if value is None:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
