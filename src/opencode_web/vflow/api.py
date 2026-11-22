"""HTTP API for VFlow definitions, drafts, publication and previews."""

import asyncio
import hashlib
import json
import re
import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..database import Database
from .anomalies import VFlowAnomalies
from .config import VFlowConfig
from .health import VFlowHealth
from .metrics import VFlowMetrics
from .models import VFlowError
from .public_result import PublicResultError, project_public_result
from .rate_limit import VFlowRateLimiter
from .redaction import redact_metadata
from .repository import (
    VFlowBadRequest,
    VFlowConflict,
    VFlowNotFound,
    VFlowPreconditionFailed,
)
from .retention import VFlowRetention
from .runtime import VFlowRuntimeError
from .service import VFlowService


class DefinitionCreateBody(BaseModel):
    slug: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    audience: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class DraftBody(BaseModel):
    source: str | None = Field(default=None, max_length=1_000_000)
    graph: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    public_output_schema: dict[str, Any] | None = None


class RunCreateBody(BaseModel):
    input: Any = None


PIPELINE_RUN_STATUSES = frozenset(
    {"created", "running", "cancel_requested", "succeeded", "failed", "cancelled"}
)
TASK_SNAPSHOT_FIELDS = (
    "task_id",
    "title",
    "updated_at",
    "agent",
    "provider_id",
    "model_id",
    "model_variant",
    "queue_id",
)


class AudienceBody(BaseModel):
    mode: str = "all"
    user_ids: list[str] = Field(default_factory=list, max_length=10_000)


class SettingsBody(BaseModel):
    settings: dict[str, Any]


class RollbackBody(BaseModel):
    version_id: str = Field(min_length=1, max_length=80)


class CompileBody(BaseModel):
    source: str = Field(min_length=1, max_length=1_000_000)


class FormatBody(BaseModel):
    graph: dict[str, Any]


class PreviewTransformBody(BaseModel):
    input: Any
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    create_missing: bool = False


class PreviewIfBody(BaseModel):
    input: Any
    path: str
    operator: str
    value: Any = None
    missing: str = "error"


class RetentionRunBody(BaseModel):
    dry_run: bool = False


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or uuid.uuid4().hex


def _error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": _request_id(request),
            }
        },
        headers=headers,
    )


def _validate_input(schema: Any, value: Any) -> None:
    if not isinstance(schema, dict) or not schema:
        return
    choices = schema.get("enum")
    if isinstance(choices, list) and not any(value == choice for choice in choices):
        raise VFlowBadRequest("Input field is not one of the allowed values")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise VFlowBadRequest("Input must be a JSON object")
        for key in schema.get("required", []):
            if key not in value:
                raise VFlowBadRequest(f"Missing required input field: {key}")
        for key, child in (schema.get("properties") or {}).items():
            if key in value:
                _validate_input(child, value[key])
    elif expected == "array":
        if not isinstance(value, list):
            raise VFlowBadRequest("Input must be a JSON array")
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise VFlowBadRequest("Input array has too few items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise VFlowBadRequest("Input array has too many items")
        items = schema.get("items")
        if isinstance(items, dict):
            for item in value:
                _validate_input(items, item)
    elif expected == "string":
        if not isinstance(value, str):
            raise VFlowBadRequest("Input field must be a string")
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise VFlowBadRequest("Input string is too short")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise VFlowBadRequest("Input string is too long")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matches = re.search(pattern, value) is not None
            except re.error as exc:
                raise VFlowBadRequest("Input schema contains an invalid pattern") from exc
            if not matches:
                raise VFlowBadRequest("Input string does not match the pattern")
    elif expected == "boolean" and not isinstance(value, bool):
        raise VFlowBadRequest("Input field must be a boolean")
    elif expected == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise VFlowBadRequest("Input field must be an integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise VFlowBadRequest("Input field must be a number")
    elif expected == "null" and value is not None:
        raise VFlowBadRequest("Input field must be null")
    if expected in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise VFlowBadRequest("Input number is below the minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise VFlowBadRequest("Input number is above the maximum")


def _run_view(db: Database, run: dict[str, Any]) -> dict[str, Any]:
    counts = db._one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('consumed', 'failed', 'cancelled')
                           THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN status = 'waiting_task' THEN 1 ELSE 0 END)
                      AS waiting_tasks,
                  SUM(CASE WHEN status = 'waiting_merge' THEN 1 ELSE 0 END)
                      AS waiting_merges
           FROM vflow_points WHERE run_id = ?""",
        (run["id"],),
    ) or {}
    version = db._one(
        "SELECT public_output_schema_json FROM vflow_versions WHERE id = ?",
        (run["version_id"],),
    )
    public_schema: Any = None
    if version:
        try:
            public_schema = json.loads(version.get("public_output_schema_json") or "null")
        except (TypeError, ValueError):
            public_schema = None
    return {
        "id": run["id"],
        "public_result_enabled": isinstance(public_schema, dict) and bool(public_schema),
        "pipeline": {
            "id": run["definition_id"],
            "slug": run["slug"],
            "title": run["title"],
            "version": run["version_number"],
        },
        "status": run["status"],
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "progress": {
            "points_total": int(counts.get("total") or 0),
            "points_done": int(counts.get("done") or 0),
            "waiting_tasks": int(counts.get("waiting_tasks") or 0),
            "waiting_merges": int(counts.get("waiting_merges") or 0),
        },
        "error": (
            {
                "code": run.get("error_code"),
                "message": run.get("safe_error_message"),
            }
            if run.get("status") == "failed"
            else None
        ),
    }


def _admin_run_view(
    db: Database, run: dict[str, Any], *, details: bool = False
) -> dict[str, Any]:
    user = db.get_user(run["started_by_user_id"]) or {}
    view = {
        "id": run["id"],
        "pipeline": {
            "id": run["definition_id"],
            "slug": run["slug"],
            "title": run["title"],
            "version": run["version_number"],
        },
        "version_id": run["version_id"],
        "started_by": {
            "id": run["started_by_user_id"],
            "login": user.get("login"),
        },
        "workspace_id": run["workspace_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "error": (
            {
                "code": run.get("error_code"),
                "message": run.get("safe_error_message"),
            }
            if run.get("error_code")
            else None
        ),
    }
    if details:
        points = []
        for point in db._all(
            """SELECT * FROM vflow_points WHERE run_id = ?
               ORDER BY created_at, id""",
            (run["id"],),
        ):
            stack = json.loads(point.get("fork_stack_json") or "[]")
            points.append(
                {
                    "id": point["id"],
                    "node_id": point["node_id"],
                    "status": point["status"],
                    "visit_seq": point["visit_seq"],
                    "attempt": point["attempt"],
                    "json_repair_attempt": int(
                        point.get("json_repair_attempt") or 0
                    ),
                    "task_run_id": point.get("task_run_id"),
                    "branch_index": stack[-1].get("branch_index")
                    if stack
                    else None,
                    "fork_stack": stack,
                    "data": json.loads(point.get("data_json") or "{}"),
                    "next_check_at": point.get("next_check_at"),
                    "created_at": point["created_at"],
                    "updated_at": point["updated_at"],
                }
            )
        events = [
            {
                "id": event["id"],
                "event_type": event["event_type"],
                "node_id": event["node_id"],
                "point_id": event["point_id"],
                "payload": redact_metadata(
                    json.loads(event.get("payload_json") or "{}")
                ),
                "created_at": event["created_at"],
            }
            for event in db._all(
                "SELECT * FROM vflow_events WHERE run_id = ? ORDER BY id",
                (run["id"],),
            )
        ]
        view["points"] = points
        view["events"] = events
        try:
            snapshots = json.loads(run.get("task_snapshots_json") or "{}")
        except (TypeError, ValueError):
            snapshots = {}
        if isinstance(snapshots, dict):
            # Admin diagnostics only: safe metadata, never prompts or content.
            view["task_snapshots"] = {
                str(node_id): {
                    key: snapshot.get(key)
                    for key in TASK_SNAPSHOT_FIELDS
                    if key in snapshot
                }
                for node_id, snapshot in snapshots.items()
                if isinstance(snapshot, dict)
            }
        correlations = []
        for link in db._all(
            """SELECT * FROM vflow_task_links
               WHERE run_id = ? ORDER BY queued_at, id""",
            (run["id"],),
        ):
            entry = {
                "point_id": link["point_id"],
                "node_id": link["node_id"],
                "origin_node_id": link["node_id"],
                "task_id": link["task_id"],
                "task_run_id": link["task_run_id"],
                "visit_seq": link["visit_seq"],
                "attempt": link["attempt"],
                "status": link["status"],
                "queued_at": link["queued_at"],
                "started_at": link.get("started_at"),
                "finished_at": link.get("finished_at"),
            }
            repair_attempt = int(link.get("repair_attempt") or 0)
            if repair_attempt:
                entry.update(
                    {
                        "repair_attempt": repair_attempt,
                        "parent_task_run_id": link.get("parent_task_run_id"),
                        "repair_error_code": link.get("repair_error_code"),
                        "repair_error_message": link.get("repair_error_message"),
                    }
                )
            correlations.append(entry)
        view["task_correlations"] = correlations
        # Admin diagnostics only.  user-facing run views never include links,
        # errors or repair jobs.
        view["task_repairs"] = [
            link for link in correlations if int(link.get("repair_attempt") or 0) > 0
        ]
        view["json_repair_attempts"] = max(
            (int(point.get("json_repair_attempt") or 0) for point in points),
            default=0,
        )
    return view


def register_vflow(
    app: FastAPI,
    database,
    current_principal,
    csrf_principal,
    admin_principal,
    admin_read_principal,
    *,
    config: VFlowConfig | None = None,
) -> None:
    run_limiter = (
        VFlowRateLimiter(
            limit=config.runs_per_minute,
            window_seconds=config.rate_window_seconds,
        )
        if config is not None
        else VFlowRateLimiter()
    )
    app.state.vflow_rate_limiter = run_limiter
    retention_days = (
        config.retention_days if config is not None else VFlowConfig().retention_days
    )

    @app.exception_handler(VFlowError)
    async def vflow_error_handler(request: Request, exc: VFlowError):
        return _error(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VFLOW_VALIDATION_ERROR",
            str(exc),
            exc.to_dict(),
        )

    @app.exception_handler(VFlowNotFound)
    async def vflow_not_found_handler(request: Request, exc: VFlowNotFound):
        return _error(request, status.HTTP_404_NOT_FOUND, "VFLOW_NOT_FOUND", str(exc))

    @app.exception_handler(VFlowConflict)
    async def vflow_conflict_handler(request: Request, exc: VFlowConflict):
        return _error(request, status.HTTP_409_CONFLICT, "VFLOW_CONFLICT", str(exc))

    @app.exception_handler(VFlowPreconditionFailed)
    async def vflow_precondition_handler(
        request: Request, exc: VFlowPreconditionFailed
    ):
        return _error(
            request,
            status.HTTP_412_PRECONDITION_FAILED,
            "VFLOW_PRECONDITION_FAILED",
            str(exc),
        )

    @app.exception_handler(VFlowBadRequest)
    async def vflow_bad_request_handler(request: Request, exc: VFlowBadRequest):
        return _error(request, status.HTTP_400_BAD_REQUEST, "VFLOW_BAD_REQUEST", str(exc))

    @app.exception_handler(VFlowRuntimeError)
    async def vflow_runtime_error_handler(request: Request, exc: VFlowRuntimeError):
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.code == "IDEMPOTENCY_CONFLICT"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _error(request, status_code, exc.code, exc.message)

    def service(db: Database) -> VFlowService:
        return VFlowService(db, limits=config.limits if config else VFlowConfig().limits)

    @app.get("/api/admin/vflow/health")
    async def admin_vflow_health(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return VFlowHealth(db).snapshot()

    @app.get("/api/admin/vflow/anomalies")
    async def admin_vflow_anomalies(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return VFlowAnomalies(db).scan()

    @app.get("/api/pipelines")
    async def list_pipelines(
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return service(db).user_catalog(principal["user"]["id"])

    @app.get("/api/pipelines/{identifier}")
    async def get_pipeline(
        identifier: str,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).user_pipeline(identifier, principal["user"]["id"])

    @app.post(
        "/api/pipelines/{identifier}/runs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_pipeline_run(
        identifier: str,
        body: RunCreateBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        user = principal["user"]
        full_user = db.get_user(user["id"]) or {}
        workspace_id = full_user.get("workspace_path")
        if not workspace_id:
            raise VFlowBadRequest("Workspace is not configured")
        if not run_limiter.allow(user["id"]):
            retry_after = run_limiter.retry_after(user["id"])
            return _error(
                request,
                status.HTTP_429_TOO_MANY_REQUESTS,
                "VFLOW_RATE_LIMITED",
                "Too many pipeline runs",
                headers={"Retry-After": str(retry_after)},
            )
        target = service(db).user_run_target(identifier, user["id"])
        schema = json.loads(target.get("input_schema_json") or "{}")
        _validate_input(schema, body.input)

        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key and len(idempotency_key) > 200:
            raise VFlowBadRequest("Idempotency-Key is too long")
        canonical = json.dumps(
            {"identifier": identifier, "input": body.input},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        runtime = request.app.state.vflow_runtime
        run = runtime.create_run(
            definition_id=target["id"],
            version_id=target["version_id"],
            user_id=user["id"],
            workspace_id=workspace_id,
            input_data=body.input,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        return _run_view(db, run)

    @app.get("/api/pipeline-runs")
    async def list_pipeline_runs(
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
        run_status: Annotated[str | None, Query(alias="status")] = None,
        pipeline_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        normalized_status = (run_status or "").strip().lower() or None
        if (
            normalized_status is not None
            and normalized_status not in PIPELINE_RUN_STATUSES
        ):
            raise VFlowBadRequest("Unknown pipeline run status")
        if offset < 0:
            raise VFlowBadRequest("Offset must be non-negative")
        runtime = request.app.state.vflow_runtime
        runs = runtime.list_runs(
            principal["user"]["id"],
            limit=max(1, min(limit, 200)),
            offset=offset,
            status=normalized_status,
            definition_id=(pipeline_id or "").strip() or None,
        )
        return [_run_view(db, run) for run in runs]

    @app.get("/api/pipeline-runs/{run_id}")
    async def get_pipeline_run(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        run = request.app.state.vflow_runtime.get_run(run_id)
        if not run or run["started_by_user_id"] != principal["user"]["id"]:
            raise VFlowNotFound("Run not found")
        return _run_view(db, run)

    @app.get("/api/pipeline-runs/{run_id}/result")
    async def get_pipeline_run_result(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(current_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        run = request.app.state.vflow_runtime.get_run(run_id)
        if not run or run["started_by_user_id"] != principal["user"]["id"]:
            raise VFlowNotFound("Run not found")
        if run["status"] != "succeeded":
            raise VFlowBadRequest("Run has not succeeded")
        version = db._one(
            "SELECT public_output_schema_json FROM vflow_versions WHERE id = ?",
            (run["version_id"],),
        )
        try:
            schema = json.loads(version.get("public_output_schema_json") or "null")
        except (TypeError, ValueError):
            schema = None
        if not isinstance(schema, dict) or not schema:
            raise VFlowNotFound("Public result is not available")
        try:
            final = json.loads(run.get("final_result_json") or "null")
            result = project_public_result(final, schema)
        except (TypeError, ValueError, PublicResultError) as exc:
            raise VFlowBadRequest(f"Public result does not match schema: {exc}") from exc
        return {"result": result}

    @app.post("/api/pipeline-runs/{run_id}/cancel")
    async def cancel_pipeline_run(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(csrf_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        runtime = request.app.state.vflow_runtime
        run = runtime.get_run(run_id)
        if not run or run["started_by_user_id"] != principal["user"]["id"]:
            raise VFlowNotFound("Run not found")
        if not service(db).cancel_allowed(run["definition_id"]):
            return _error(
                request,
                status.HTTP_403_FORBIDDEN,
                "VFLOW_CANCEL_FORBIDDEN",
                "Pipeline policy does not allow cancellation by users",
            )
        runtime.cancel_run(run_id)
        return _run_view(db, runtime.get_run(run_id))

    @app.get("/api/admin/pipelines")
    async def admin_list_pipelines(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return service(db).list_admin_definitions()

    @app.post(
        "/api/admin/pipelines",
        status_code=status.HTTP_201_CREATED,
    )
    async def admin_create_pipeline(
        body: DefinitionCreateBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).create_definition(
            principal["user"]["id"],
            slug=body.slug,
            title=body.title,
            description=body.description,
            audience=body.audience,
            settings=body.settings,
        )

    @app.get("/api/admin/pipelines/{definition_id}")
    async def admin_get_pipeline(
        definition_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).admin_definition(definition_id)

    @app.put("/api/admin/pipelines/{definition_id}/draft")
    async def admin_save_draft(
        definition_id: str,
        body: DraftBody,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        schemas: dict[str, Any] = {}
        if "input_schema" in body.model_fields_set:
            schemas["input_schema"] = body.input_schema
        if "public_output_schema" in body.model_fields_set:
            schemas["public_output_schema"] = body.public_output_schema
        return service(db).save_draft(
            definition_id,
            principal["user"]["id"],
            source=body.source,
            graph=body.graph,
            expected_etag=request.headers.get("if-match"),
            **schemas,
        )

    @app.post("/api/admin/pipelines/{definition_id}/validate")
    async def admin_validate_pipeline(
        definition_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).validate_definition(definition_id)

    @app.post("/api/admin/pipelines/{definition_id}/publish")
    async def admin_publish_pipeline(
        definition_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).publish(
            definition_id,
            principal["user"]["id"],
            expected_etag=request.headers.get("if-match"),
        )

    @app.post("/api/admin/pipelines/{definition_id}/rollback")
    async def admin_rollback_pipeline(
        definition_id: str,
        body: RollbackBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).rollback(
            definition_id, body.version_id, principal["user"]["id"]
        )

    @app.post("/api/admin/pipelines/{definition_id}/archive")
    async def admin_archive_pipeline(
        definition_id: str,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).archive(definition_id, principal["user"]["id"])

    @app.put("/api/admin/pipelines/{definition_id}/audience")
    async def admin_set_audience(
        definition_id: str,
        body: AudienceBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).set_audience(
            definition_id,
            principal["user"]["id"],
            body.model_dump(),
        )

    @app.put("/api/admin/pipelines/{definition_id}/settings")
    async def admin_set_pipeline_settings(
        definition_id: str,
        body: SettingsBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).set_settings(
            definition_id,
            principal["user"]["id"],
            body.settings,
        )

    @app.get("/api/admin/vflow/runs")
    async def admin_list_runs(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
        definition_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if definition_id:
            runs = db._all(
                """SELECT r.*, d.slug, d.title, v.version_number
                   FROM vflow_runs r
                   JOIN vflow_definitions d ON d.id = r.definition_id
                   JOIN vflow_versions v ON v.id = r.version_id
                   WHERE r.definition_id = ?
                   ORDER BY r.created_at DESC LIMIT ?""",
                (definition_id, max(1, min(limit, 200))),
            )
        else:
            runs = db._all(
                """SELECT r.*, d.slug, d.title, v.version_number
                   FROM vflow_runs r
                   JOIN vflow_definitions d ON d.id = r.definition_id
                   JOIN vflow_versions v ON v.id = r.version_id
                   ORDER BY r.created_at DESC LIMIT ?""",
                (max(1, min(limit, 200)),),
            )
        return [_admin_run_view(db, run) for run in runs]

    @app.get("/api/admin/vflow/runs/{run_id}")
    async def admin_get_run(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        run = request.app.state.vflow_runtime.get_run(run_id)
        if not run:
            raise VFlowNotFound("Run not found")
        return _admin_run_view(db, run, details=True)

    @app.post("/api/admin/vflow/runs/{run_id}/cancel")
    async def admin_cancel_run(
        run_id: str,
        request: Request,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        runtime = request.app.state.vflow_runtime
        run = runtime.get_run(run_id)
        if not run:
            raise VFlowNotFound("Run not found")
        runtime.cancel_run(run_id)
        return _admin_run_view(db, runtime.get_run(run_id))

    @app.get("/api/admin/vflow/metrics")
    async def admin_metrics(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return VFlowMetrics(db).snapshot()

    @app.get("/api/admin/vflow/retention")
    async def admin_retention_stats(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        retention = VFlowRetention(db, retention_days)
        return await asyncio.to_thread(retention.stats)

    @app.post("/api/admin/vflow/retention/run")
    async def admin_run_retention(
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
        body: RetentionRunBody | None = None,
    ) -> dict[str, int]:
        retention = VFlowRetention(db, retention_days)
        return await asyncio.to_thread(
            retention.run_once, dry_run=bool(body.dry_run) if body else False
        )

    @app.get("/api/admin/vflow/block-registry")
    async def admin_block_registry(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return service(db).block_registry()

    @app.get("/api/admin/vflow/tasks-catalog")
    async def admin_tasks_catalog(
        principal: Annotated[dict[str, Any], Depends(admin_read_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> list[dict[str, Any]]:
        return service(db).tasks_catalog()

    @app.post("/api/admin/vflow/compile")
    async def admin_compile(
        body: CompileBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).compile_preview(body.source)

    @app.post("/api/admin/vflow/format")
    async def admin_format(
        body: FormatBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return {"source": service(db).format_graph(body.graph)}

    @app.post("/api/admin/vflow/preview-transform")
    async def admin_preview_transform(
        body: PreviewTransformBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return {
            "result": service(db).preview_transform(
                body.input,
                body.operations,
                create_missing=body.create_missing,
            )
        }

    @app.post("/api/admin/vflow/preview-if")
    async def admin_preview_if(
        body: PreviewIfBody,
        principal: Annotated[dict[str, Any], Depends(admin_principal)],
        db: Annotated[Database, Depends(database)],
    ) -> dict[str, Any]:
        return service(db).preview_if(
            body.input,
            body.path,
            body.operator,
            body.value,
            body.missing,
        )
