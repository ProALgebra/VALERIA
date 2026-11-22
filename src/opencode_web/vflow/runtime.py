"""Deterministic VFlow runtime.

The runtime owns persistence and transitions. Node handlers never receive a
database connection: they only produce an action, which the runtime applies
inside a short SQLite transaction. Task execution is delegated to the shared
VALERIA queue through the gateway protocol; VFlow itself never starts OpenCode.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..database import utcnow
from .actions import (
    Action,
    Advance,
    ArriveMerge,
    EnqueueTask,
    Fail,
    Finish,
    SpawnFork,
    Suspend,
)
from .capabilities import validate_handler_capabilities
from .gateway import VFlowTaskError
from .handlers import (
    DEFAULT_HANDLERS,
    TASK_POLL_SECONDS,
    TASK_RETRY_KIND,
    TASK_WAIT_KIND,
    NodeHandler,
    PlanContext,
    PointView,
)
from .json_schema import build_correction_prompt, validate_output
from .limits import (
    DEFAULT_LIMITS,
    VFlowLimitError,
    VFlowLimits,
    check_fork_count,
    check_json,
    check_point_budget,
    limits_from_json,
    limits_to_dict,
)
from .logic import LogicError
from .models import Graph
from .policy import effective_limits
from .redaction import redact_metadata
from .registry import DEFAULT_REGISTRY, MERGE, TASK, NodeRegistry

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
# Fairness guard for one scheduler tick: a single Run may contribute at most
# this many points before other due Runs get a turn.  The value is deliberately
# small enough that a wide Fork cannot monopolise a tick, while still allowing
# ordinary single-Run pipelines to batch a few transitions.  Callers may
# override it on the runtime or per ``claim_due`` invocation.
DEFAULT_PER_RUN_CLAIM_LIMIT = 4
ACTIVE_POINT_STATUSES = {
    "ready",
    "claimed",
    "waiting_task",
    "waiting_merge",
}
# A queue timeout only applies before the dispatcher starts the Task. Once a
# job reaches one of these states the separate execution deadline applies.
TASK_STARTED_STATES = {
    "dispatching",
    "running",
    "waiting_permission",
    "waiting_question",
}


class VFlowRuntimeError(Exception):
    """A safe runtime failure which is persisted on the Run."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class TaskGateway(Protocol):
    def enqueue(
        self,
        *,
        run_id: str,
        point_id: str,
        task_id: str,
        user_id: str,
        workspace_id: str,
        input_data: Any,
        result_mode: str,
        queue_timeout_seconds: int | None,
        execution_timeout_seconds: int | None,
        task_snapshot: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        node_id: str | None = None,
    ) -> str: ...

    def status(self, task_run_id: str) -> dict[str, Any]: ...

    def repair_output(
        self,
        *,
        task_run_id: str,
        run_id: str,
        point_id: str,
        repair_attempt: int,
        prompt: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> str: ...

    def cancel(self, task_run_id: str) -> None: ...


@dataclass(frozen=True)
class _Point:
    id: str
    run_id: str
    node_id: str
    status: str
    data: Any
    fork_stack: list[dict[str, Any]]
    visit_seq: int
    task_run_id: str | None
    attempt: int
    json_repair_attempt: int
    lease_token: str | None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _lease_until(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _timeout_seconds(value: Any) -> int | None:
    """Return a configured non-negative timeout or ``None`` when disabled."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _elapsed_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        started = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


def _frame(
    *,
    fork_instance_id: str,
    fork_node_id: str,
    merge_node_id: str,
    branch_index: int,
    expected_count: int,
) -> dict[str, Any]:
    return {
        "fork_instance_id": fork_instance_id,
        "fork_node_id": fork_node_id,
        "merge_node_id": merge_node_id,
        "branch_index": branch_index,
        "expected_count": expected_count,
    }


class VFlowRuntime:
    def __init__(
        self,
        database: Any,
        gateway: TaskGateway | None = None,
        registry: NodeRegistry = DEFAULT_REGISTRY,
        limits: VFlowLimits = DEFAULT_LIMITS,
        handlers: Mapping[str, NodeHandler] | None = None,
        per_run_claim_limit: int = DEFAULT_PER_RUN_CLAIM_LIMIT,
    ):
        self.database = database
        self.gateway = gateway
        self.registry = registry
        self.limits = limits
        self.handlers = dict(DEFAULT_HANDLERS if handlers is None else handlers)
        # Handler declarations are part of the trusted code contract. Validate
        # them against the registry when a spec is available; handlers without
        # an explicit declaration remain supported for backwards compatibility.
        for type_id, handler in self.handlers.items():
            spec = self.registry.get(type_id)
            if spec is not None:
                validate_handler_capabilities(spec, handler)
        self.per_run_claim_limit = max(1, int(per_run_claim_limit))

    def _check_json(
        self, value: Any, label: str, limits: VFlowLimits | None = None
    ) -> None:
        try:
            check_json(value, limits or self.limits, label=label)
        except VFlowLimitError as exc:
            raise VFlowRuntimeError(exc.code, exc.message) from exc

    def _check_fork_count(
        self, count: int, limits: VFlowLimits | None = None
    ) -> None:
        try:
            check_fork_count(count, limits or self.limits)
        except VFlowLimitError as exc:
            raise VFlowRuntimeError(exc.code, exc.message) from exc

    def _check_point_budget(
        self,
        active_points: int,
        total_points: int,
        additions: int,
        limits: VFlowLimits | None = None,
    ) -> None:
        try:
            check_point_budget(
                active_points, total_points, additions, limits or self.limits
            )
        except VFlowLimitError as exc:
            raise VFlowRuntimeError(exc.code, exc.message) from exc

    @staticmethod
    def _run_expired(started_at: str | None, limits: VFlowLimits) -> bool:
        elapsed = _elapsed_seconds(started_at, datetime.now(UTC))
        return elapsed is not None and elapsed >= limits.max_run_seconds

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------
    def create_run(
        self,
        *,
        definition_id: str,
        version_id: str,
        user_id: str,
        workspace_id: str,
        input_data: Any,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            definition = connection.execute(
                "SELECT * FROM vflow_definitions WHERE id = ?", (definition_id,)
            ).fetchone()
            if not definition or definition["status"] != "active":
                raise VFlowRuntimeError("DEFINITION_UNAVAILABLE", "Pipeline is unavailable")
            effective = effective_limits(definition["settings_json"], self.limits)
            self._check_json(input_data, "run input", effective)
            version = connection.execute(
                "SELECT * FROM vflow_versions WHERE id = ? AND definition_id = ?",
                (version_id, definition_id),
            ).fetchone()
            if (
                not version
                or version["status"] != "published"
                or definition["published_version_id"] != version_id
            ):
                raise VFlowRuntimeError("VERSION_UNAVAILABLE", "Pipeline version is unavailable")

            if idempotency_key:
                existing = connection.execute(
                    """SELECT id, request_hash FROM vflow_runs
                       WHERE started_by_user_id = ? AND idempotency_key = ?""",
                    (user_id, idempotency_key),
                ).fetchone()
                if existing:
                    if (
                        request_hash
                        and existing["request_hash"]
                        and existing["request_hash"] != request_hash
                    ):
                        raise VFlowRuntimeError(
                            "IDEMPOTENCY_CONFLICT",
                            "Idempotency-Key was already used with another request",
                        )
                    return self.get_run(existing["id"])  # type: ignore[return-value]

            graph = Graph.from_dict(_load(version["graph_ir_json"], {}))
            if not graph.entry:
                raise VFlowRuntimeError("INVALID_GRAPH", "Published graph has no entry")

            task_snapshots = self._task_snapshots(graph)

            run_id = str(uuid.uuid4())
            now = utcnow()
            connection.execute(
                """INSERT INTO vflow_runs
                   (id, definition_id, version_id, started_by_user_id, workspace_id, status,
                    input_json, task_snapshots_json, limits_json, idempotency_key,
                    request_hash, transition_count, point_count_total, started_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                (
                    run_id,
                    definition_id,
                    version_id,
                    user_id,
                    workspace_id,
                    _dump(input_data),
                    _dump(task_snapshots),
                    _dump(limits_to_dict(effective)),
                    idempotency_key,
                    request_hash,
                    now,
                    now,
                ),
            )
            point_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, ?, 'ready', ?, '[]', 1, 1, ?, ?)""",
                (point_id, run_id, graph.entry, _dump(input_data), now, now),
            )
            self._event(connection, run_id, point_id, graph.entry, "RUN_STARTED", {})
        return self.get_run(run_id)  # type: ignore[return-value]

    def _task_snapshots(self, graph: Graph) -> dict[str, dict[str, Any]]:
        """Bind every Task node to the version that existed when the Run started."""
        snapshots: dict[str, dict[str, Any]] = {}
        for node in graph.nodes:
            if node.type != TASK:
                continue
            task_id = str(node.config.get("task_id") or "")
            task = self.database.get_task(task_id) if task_id else None
            if task is None:
                raise VFlowRuntimeError(
                    "TASK_UNAVAILABLE",
                    "A task referenced by this pipeline is unavailable",
                )
            snapshots[node.id] = {
                "task_id": str(task["id"]),
                "title": task.get("title"),
                "updated_at": task.get("updated_at"),
                "agent": task.get("agent"),
                "provider_id": task.get("provider_id"),
                "model_id": task.get("model_id"),
                "model_variant": task.get("model_variant"),
                "queue_id": task.get("queue_id"),
            }
        return snapshots

    def _task_snapshot(
        self, connection: Any, run_id: str, node_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT task_snapshots_json FROM vflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        snapshots = _load(row["task_snapshots_json"] if row else None, {})
        if not isinstance(snapshots, dict):
            return None
        snapshot = snapshots.get(node_id)
        return snapshot if isinstance(snapshot, dict) else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.database._one(
            """SELECT r.*, d.slug, d.title, v.version_number
               FROM vflow_runs r
               JOIN vflow_definitions d ON d.id = r.definition_id
               JOIN vflow_versions v ON v.id = r.version_id
               WHERE r.id = ?""",
            (run_id,),
        )

    def list_runs(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        definition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["r.started_by_user_id = ?"]
        params: list[Any] = [user_id]
        if status is not None:
            clauses.append("r.status = ?")
            params.append(status)
        if definition_id is not None:
            clauses.append("r.definition_id = ?")
            params.append(definition_id)
        params.extend([limit, offset])
        return self.database._all(
            f"""SELECT r.*, d.slug, d.title, v.version_number
                FROM vflow_runs r
                JOIN vflow_definitions d ON d.id = r.definition_id
                JOIN vflow_versions v ON v.id = r.version_id
                WHERE {" AND ".join(clauses)}
                ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?""",
            tuple(params),
        )

    def points(self, run_id: str) -> list[dict[str, Any]]:
        return self.database._all(
            """SELECT id, run_id, node_id, status, data_json, fork_stack_json,
                      parent_point_id, visit_seq, task_run_id, attempt,
                      json_repair_attempt, next_check_at, created_at, updated_at
               FROM vflow_points WHERE run_id = ?
               ORDER BY created_at, id""",
            (run_id,),
        )

    # ------------------------------------------------------------------
    # Scheduler-facing operations
    # ------------------------------------------------------------------
    def claim_due(
        self,
        limit: int = 20,
        lease_seconds: int = 30,
        per_run_limit: int | None = None,
    ) -> list[tuple[str, str]]:
        """Claim due points fairly across Runs using atomic CAS leases.

        Runs are ordered by their earliest due point, which preserves due-time
        priority between Runs.  They are then served one point at a time in
        round-robin order, and each Run contributes at most ``per_run_limit``
        points per call (default ``self.per_run_claim_limit``, initially
        ``DEFAULT_PER_RUN_CLAIM_LIMIT`` = 4).  The cap is intentionally
        configurable on the runtime and per call; a single wide Run therefore
        cannot monopolise a scheduler tick while still making progress on
        subsequent ticks.

        Selection and lease claiming stay in one write transaction.  Every
        point is still claimed with a compare-and-set update, so an expired or
        concurrent lease can never lead to duplicate processing.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []
        configured_cap = self.per_run_claim_limit if per_run_limit is None else per_run_limit
        try:
            per_run_cap = max(1, min(int(configured_cap), limit))
        except (TypeError, ValueError):
            per_run_cap = min(self.per_run_claim_limit, limit)
        candidate_limit = limit * per_run_cap
        now = utcnow()
        claimed: list[tuple[str, str]] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                """WITH due AS (
                       SELECT p.id, p.run_id,
                              COALESCE(p.next_check_at, p.created_at) AS due_at,
                              p.created_at,
                              ROW_NUMBER() OVER (
                                PARTITION BY p.run_id
                                ORDER BY COALESCE(p.next_check_at, p.created_at),
                                         p.created_at, p.id
                              ) AS run_rank
                       FROM vflow_points p
                       JOIN vflow_runs r ON r.id = p.run_id
                       WHERE r.status = 'running'
                         AND (
                           p.status = 'ready'
                           OR (p.status = 'waiting_task'
                               AND p.next_check_at IS NOT NULL
                               AND p.next_check_at <= ?)
                         )
                         AND (p.lease_until IS NULL OR p.lease_until <= ?)
                   )
                   SELECT id, run_id FROM due
                   WHERE run_rank <= ?
                   ORDER BY due_at, created_at, id
                   LIMIT ?""",
                (now, now, per_run_cap, candidate_limit),
            ).fetchall()

            # ``rows`` is globally due-ordered, so first-seen Run order follows
            # each Run's earliest due point.  Repeated rounds then give every
            # Run one turn before any Run receives its second point.
            groups: dict[str, list[str]] = {}
            for row in rows:
                groups.setdefault(str(row["run_id"]), []).append(str(row["id"]))
            run_order = list(groups)
            positions = dict.fromkeys(run_order, 0)
            selected: list[str] = []
            while len(selected) < limit:
                progressed = False
                for run_id in run_order:
                    index = positions[run_id]
                    points = groups[run_id]
                    if index >= len(points):
                        continue
                    selected.append(points[index])
                    positions[run_id] = index + 1
                    progressed = True
                    if len(selected) >= limit:
                        break
                if not progressed:
                    break

            for point_id in selected:
                token = uuid.uuid4().hex
                cursor = connection.execute(
                    """UPDATE vflow_points
                       SET status = 'claimed', lease_owner = 'runtime',
                           lease_token = ?, lease_until = ?, updated_at = ?
                       WHERE id = ?
                         AND status IN ('ready', 'waiting_task')
                         AND (lease_until IS NULL OR lease_until <= ?)""",
                    (token, _lease_until(lease_seconds), now, point_id, now),
                )
                if cursor.rowcount:
                    claimed.append((point_id, token))
        return claimed

    def tick(self, limit: int = 20) -> int:
        self.reconcile_repair_outbox(limit)
        processed = 0
        for point_id, token in self.claim_due(limit):
            if self.process_point(point_id, token):
                processed += 1
        return processed

    def process_point(self, point_id: str, lease_token: str) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT p.*, r.status AS run_status, r.started_by_user_id,
                          r.workspace_id, r.version_id, r.input_json,
                          r.limits_json, r.started_at AS run_started_at,
                          v.graph_ir_json
                   FROM vflow_points p
                   JOIN vflow_runs r ON r.id = p.run_id
                   JOIN vflow_versions v ON v.id = r.version_id
                   WHERE p.id = ?""",
                (point_id,),
            ).fetchone()
            if (
                not row
                or row["status"] != "claimed"
                or row["lease_token"] != lease_token
                or row["run_status"] != "running"
            ):
                return False
            limits = limits_from_json(row["limits_json"], self.limits)
            if self._run_expired(row["run_started_at"], limits):
                self._fail(
                    connection,
                    str(row["run_id"]),
                    "LIMIT_RUN_SECONDS",
                    f"Run exceeds the {limits.max_run_seconds} second limit",
                )
                return True
            graph = Graph.from_dict(_load(row["graph_ir_json"], {}))
            point = _Point(
                id=row["id"],
                run_id=row["run_id"],
                node_id=row["node_id"],
                status=row["status"],
                data=_load(row["data_json"], None),
                fork_stack=_load(row["fork_stack_json"], []),
                visit_seq=int(row["visit_seq"]),
                task_run_id=row["task_run_id"],
                attempt=int(row["attempt"]),
                json_repair_attempt=int(row["json_repair_attempt"] or 0),
                lease_token=row["lease_token"],
            )
            try:
                node = next((item for item in graph.nodes if item.id == point.node_id), None)
                if node is None:
                    raise VFlowRuntimeError(
                        "UNKNOWN_NODE", f"Execution point references unknown node {point.node_id!r}"
                    )
                task_status: dict[str, Any] | None = None
                if node.type == TASK:
                    if self.gateway is None:
                        raise VFlowRuntimeError(
                            "TASK_GATEWAY_UNAVAILABLE", "Task gateway is unavailable"
                        )
                    if point.task_run_id:
                        task_status = self.gateway.status(point.task_run_id)
                        task_link = connection.execute(
                            "SELECT * FROM vflow_task_links WHERE task_run_id = ?",
                            (point.task_run_id,),
                        ).fetchone()
                        if self._handle_task_timeout(
                            connection, point, node, task_link, task_status
                        ):
                            return True
                        if self._handle_task_json_repair(
                            connection, point, node, task_link, task_status
                        ):
                            return True
                handler = self.handlers.get(node.type)
                if handler is None:
                    raise VFlowRuntimeError(
                        "UNSUPPORTED_NODE", f"Unsupported node type {node.type!r}"
                    )
                action = handler.plan(
                    PlanContext(
                        node=node,
                        point=PointView(
                            id=point.id,
                            run_id=point.run_id,
                            node_id=point.node_id,
                            data=point.data,
                            fork_stack=tuple(point.fork_stack),
                            visit_seq=point.visit_seq,
                            attempt=point.attempt,
                            task_run_id=point.task_run_id,
                            json_repair_attempt=point.json_repair_attempt,
                        ),
                        graph=graph,
                        task_status=task_status,
                    )
                )
                self._apply_action(connection, point, graph, action, limits)
            except (VFlowRuntimeError, LogicError) as exc:
                code = getattr(exc, "code", "LOGIC_ERROR")
                message = getattr(exc, "message", str(exc))
                self._fail(connection, point.run_id, code, message)
            except Exception:
                self._fail(
                    connection,
                    point.run_id,
                    "runtime_error",
                    "Внутренняя ошибка выполнения пайплайна",
                )
        return True

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _apply_action(
        self,
        connection: Any,
        point: _Point,
        graph: Graph,
        action: Action,
        limits: VFlowLimits,
    ) -> None:
        if isinstance(action, Advance):
            self._apply_advance(connection, point, graph, action, limits)
        elif isinstance(action, EnqueueTask):
            self._apply_enqueue_task(connection, point, action)
        elif isinstance(action, Suspend):
            self._apply_suspend(connection, point, action)
        elif isinstance(action, SpawnFork):
            self._spawn_fork(connection, point, graph, action, limits)
        elif isinstance(action, ArriveMerge):
            self._arrive_merge(connection, point, graph, action, limits)
        elif isinstance(action, Finish):
            self._apply_finish(connection, point, action, limits)
        elif isinstance(action, Fail):
            self._fail(connection, point.run_id, action.code, action.message)
        else:
            raise VFlowRuntimeError(
                "UNSUPPORTED_ACTION", f"Unsupported action {type(action).__name__!r}"
            )

    def _apply_advance(
        self,
        connection: Any,
        point: _Point,
        graph: Graph,
        action: Advance,
        limits: VFlowLimits,
    ) -> None:
        if point.task_run_id:
            connection.execute(
                """UPDATE vflow_task_links
                   SET status = 'completed', finished_at = ?
                   WHERE point_id = ? AND visit_seq = ? AND attempt = ?
                     AND finished_at IS NULL""",
                (utcnow(), point.id, point.visit_seq, point.attempt),
            )
        self._advance(connection, point, graph, action.data, action.port, limits)

    def _apply_suspend(
        self,
        connection: Any,
        point: _Point,
        action: Suspend,
    ) -> None:
        now = utcnow()
        condition = action.wake_condition or {}
        if action.kind == TASK_WAIT_KIND:
            seconds = int(condition.get("after_seconds", TASK_POLL_SECONDS))
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'waiting_task', next_check_at = ?,
                       lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                       updated_at = ? WHERE id = ?""",
                ((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), now, point.id),
            )
            return
        if action.kind == TASK_RETRY_KIND:
            attempt = int(condition.get("attempt", point.attempt + 1))
            if point.task_run_id:
                self._close_task_links(connection, point, "retried", now)
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'ready', attempt = ?, task_run_id = NULL,
                       json_repair_attempt = 0, next_check_at = NULL,
                       lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ? WHERE id = ?""",
                (attempt, now, point.id),
            )
            self._event(
                connection,
                point.run_id,
                point.id,
                point.node_id,
                "TASK_RETRY",
                {"attempt": attempt},
            )
            return
        raise VFlowRuntimeError(
            "UNSUPPORTED_SUSPEND", f"Unsupported suspend kind {action.kind!r}"
        )

    def _apply_finish(
        self,
        connection: Any,
        point: _Point,
        action: Finish,
        limits: VFlowLimits,
    ) -> None:
        self._check_json(action.data, "node output", limits)
        connection.execute(
            """UPDATE vflow_points
               SET status = 'consumed', lease_owner = NULL, lease_token = NULL,
                   lease_until = NULL, updated_at = ?
               WHERE id = ?""",
            (utcnow(), point.id),
        )
        self._bump_transition(connection, point.run_id, limits)
        self._finish(connection, point.run_id, action.data, point.node_id)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def _advance(
        self,
        connection: Any,
        point: _Point,
        graph: Graph,
        data: Any,
        port: str,
        limits: VFlowLimits,
    ) -> None:
        self._check_json(data, "node output", limits)
        edge = next(
            (
                item
                for item in graph.edges
                if item.source == point.node_id and item.source_port == port
            ),
            None,
        )
        if edge is None:
            raise VFlowRuntimeError(
                "NO_OUTGOING_EDGE",
                f"Node {point.node_id!r} has no edge from port {port!r}",
            )
        if edge.target == "end":
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'consumed', lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE id = ?""",
                (utcnow(), point.id),
            )
            self._bump_transition(connection, point.run_id, limits)
            self._finish(connection, point.run_id, data, point.node_id)
            return
        next_visit = connection.execute(
            "SELECT COALESCE(MAX(visit_seq), 0) + 1 FROM vflow_points WHERE run_id = ?",
            (point.run_id,),
        ).fetchone()[0]
        connection.execute(
            """UPDATE vflow_points
               SET node_id = ?, status = 'ready', data_json = ?, visit_seq = ?,
                   attempt = 1, task_run_id = NULL, json_repair_attempt = 0,
                   lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                   next_check_at = NULL, updated_at = ?
               WHERE id = ?""",
            (edge.target, _dump(data), next_visit, utcnow(), point.id),
        )
        self._bump_transition(connection, point.run_id, limits)
        self._event(
            connection,
            point.run_id,
            point.id,
            edge.target,
            "POINT_ADVANCED",
            {"from": point.node_id, "port": port},
        )

    def _spawn_fork(
        self,
        connection: Any,
        point: _Point,
        graph: Graph,
        action: SpawnFork,
        limits: VFlowLimits,
    ) -> None:
        count = int(action.count)
        join_id = str(action.join)
        if count < 2:
            raise VFlowRuntimeError("INVALID_FORK", "Fork needs at least two branches")
        if not any(node.id == join_id and node.type == MERGE for node in graph.nodes):
            raise VFlowRuntimeError("INVALID_FORK", "Fork join must reference a merge node")
        self._check_fork_count(count, limits)
        active_points = connection.execute(
            """SELECT COUNT(*) FROM vflow_points
               WHERE run_id = ?
                 AND status IN ('ready', 'claimed', 'waiting_task', 'waiting_merge')""",
            (point.run_id,),
        ).fetchone()[0]
        total_points = connection.execute(
            "SELECT COUNT(*) FROM vflow_points WHERE run_id = ?",
            (point.run_id,),
        ).fetchone()[0]
        self._check_point_budget(
            max(0, int(active_points) - 1), int(total_points), count, limits
        )

        fork_instance_id = str(uuid.uuid4())
        now = utcnow()
        source = copy.deepcopy(action.data)
        next_visit = int(
            connection.execute(
                "SELECT COALESCE(MAX(visit_seq), 0) + 1 FROM vflow_points WHERE run_id = ?",
                (point.run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO vflow_fork_instances
               (id, run_id, fork_node_id, merge_node_id, parent_point_id,
                parent_visit_seq, expected_count, source_data_json,
                parent_fork_stack_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                fork_instance_id,
                point.run_id,
                point.node_id,
                join_id,
                point.id,
                point.visit_seq,
                count,
                _dump(source),
                _dump(point.fork_stack),
                now,
            ),
        )
        for branch_index in range(count):
            edge = next(
                (
                    item
                    for item in graph.edges
                    if item.source == point.node_id
                    and item.source_port in (f"branch[{branch_index}]", "all")
                ),
                None,
            )
            if edge is None:
                raise VFlowRuntimeError(
                    "FORK_PORT_UNCONNECTED",
                    f"Fork branch {branch_index} has no outgoing edge",
                )
            if edge.target == "end":
                raise VFlowRuntimeError(
                    "FORK_BYPASS_MERGE",
                    "A fork branch cannot go directly to end",
                )
            frame = _frame(
                fork_instance_id=fork_instance_id,
                fork_node_id=point.node_id,
                merge_node_id=join_id,
                branch_index=branch_index,
                expected_count=count,
            )
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, 1, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    point.run_id,
                    edge.target,
                    _dump(copy.deepcopy(source)),
                    _dump([*point.fork_stack, frame]),
                    point.id,
                    next_visit + branch_index,
                    now,
                    now,
                ),
            )
        connection.execute(
            """UPDATE vflow_points
               SET status = 'forked', lease_owner = NULL, lease_token = NULL,
                   lease_until = NULL, updated_at = ?
               WHERE id = ?""",
            (now, point.id),
        )
        connection.execute(
            """UPDATE vflow_runs
               SET point_count_total = point_count_total + ?
               WHERE id = ?""",
            (count, point.run_id),
        )
        self._bump_transition(connection, point.run_id, limits)
        self._event(
            connection,
            point.run_id,
            point.id,
            point.node_id,
            "FORK_SPAWNED",
            {"fork_instance_id": fork_instance_id, "count": count},
        )

    def _arrive_merge(
        self,
        connection: Any,
        point: _Point,
        graph: Graph,
        action: ArriveMerge,
        limits: VFlowLimits,
    ) -> None:
        if not point.fork_stack:
            raise VFlowRuntimeError("MERGE_WITHOUT_FORK", "Merge reached without an open fork")
        frame = point.fork_stack[-1]
        if frame.get("merge_node_id") != point.node_id:
            raise VFlowRuntimeError(
                "MERGE_SCOPE_MISMATCH",
                "Merge does not match the innermost fork scope",
            )
        fork_instance_id = str(frame["fork_instance_id"])
        branch_index = int(frame["branch_index"])
        if (
            str(action.fork_instance_id) != fork_instance_id
            or int(action.branch_index) != branch_index
        ):
            raise VFlowRuntimeError(
                "MERGE_SCOPE_MISMATCH",
                "Merge arrival does not match the innermost fork scope",
            )
        now = utcnow()
        instance = connection.execute(
            "SELECT * FROM vflow_fork_instances WHERE id = ?", (fork_instance_id,)
        ).fetchone()
        if not instance:
            raise VFlowRuntimeError("FORK_INSTANCE_MISSING", "Fork instance is missing")
        if instance["status"] != "open":
            # A stale point from an already closed fork instance must not wait
            # forever or create a second continuation after a cycle/retry.
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'consumed', lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE id = ?""",
                (now, point.id),
            )
            self._event(
                connection,
                point.run_id,
                point.id,
                point.node_id,
                "MERGE_STALE_ARRIVAL",
                {"fork_instance_id": fork_instance_id, "branch_index": branch_index},
            )
            return
        inserted = connection.execute(
            """INSERT OR IGNORE INTO vflow_merge_arrivals
               (id, fork_instance_id, merge_node_id, branch_index, point_id, data_json, arrived_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                fork_instance_id,
                point.node_id,
                branch_index,
                point.id,
                _dump(action.data),
                now,
            ),
        )
        if not inserted.rowcount:
            # The same branch already arrived for this instance. Consume the
            # duplicate instead of leaving an immortal waiting point.
            connection.execute(
                """UPDATE vflow_points
                   SET status = 'consumed', lease_owner = NULL, lease_token = NULL,
                       lease_until = NULL, updated_at = ?
                   WHERE id = ?""",
                (now, point.id),
            )
            self._event(
                connection,
                point.run_id,
                point.id,
                point.node_id,
                "MERGE_DUPLICATE_ARRIVAL",
                {"fork_instance_id": fork_instance_id, "branch_index": branch_index},
            )
            return
        connection.execute(
            """UPDATE vflow_points
               SET status = 'waiting_merge', lease_owner = NULL, lease_token = NULL,
                   lease_until = NULL, updated_at = ?
               WHERE id = ?""",
            (now, point.id),
        )
        arrivals = connection.execute(
            """SELECT * FROM vflow_merge_arrivals
               WHERE fork_instance_id = ? ORDER BY branch_index""",
            (fork_instance_id,),
        ).fetchall()
        expected = int(instance["expected_count"])
        if len(arrivals) < expected:
            self._event(
                connection,
                point.run_id,
                point.id,
                point.node_id,
                "MERGE_ARRIVED",
                {"fork_instance_id": fork_instance_id, "branch_index": branch_index},
            )
            return
        indexes = [int(row["branch_index"]) for row in arrivals]
        if indexes != list(range(expected)):
            raise VFlowRuntimeError(
                "MERGE_INCOMPLETE",
                "Merge received an invalid set of branch indexes",
            )
        cursor = connection.execute(
            """UPDATE vflow_fork_instances SET status = 'merged', merged_at = ?
               WHERE id = ? AND status = 'open'""",
            (now, fork_instance_id),
        )
        if not cursor.rowcount:
            return
        output = {
            "source": _load(instance["source_data_json"], None),
            "branches": [
                {"index": int(row["branch_index"]), "data": _load(row["data_json"], None)}
                for row in arrivals
            ],
        }
        self._check_json(output, "merge output", limits)
        connection.execute(
            "UPDATE vflow_points SET status = 'consumed', updated_at = ? "
            "WHERE id IN (SELECT point_id FROM vflow_merge_arrivals WHERE fork_instance_id = ?)",
            (now, fork_instance_id),
        )
        edge = next(
            (
                item
                for item in graph.edges
                if item.source == point.node_id and item.source_port == "out"
            ),
            None,
        )
        if edge is None:
            raise VFlowRuntimeError(
                "NO_OUTGOING_EDGE", "Merge has no outgoing edge"
            )
        parent_stack = point.fork_stack[:-1]
        if edge.target == "end":
            self._finish(connection, point.run_id, output, point.node_id)
        else:
            continuation_id = str(uuid.uuid4())
            next_visit = int(
                connection.execute(
                    """SELECT COALESCE(MAX(visit_seq), 0) + 1
                       FROM vflow_points WHERE run_id = ?""",
                    (point.run_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO vflow_points
                   (id, run_id, node_id, status, data_json, fork_stack_json,
                    parent_point_id, visit_seq, attempt, created_at, updated_at)
                   VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, 1, ?, ?)""",
                (
                    continuation_id,
                    point.run_id,
                    edge.target,
                    _dump(output),
                    _dump(parent_stack),
                    point.id,
                    next_visit,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE vflow_fork_instances SET continuation_point_id = ? WHERE id = ?",
                (continuation_id, fork_instance_id),
            )
        connection.execute(
            "UPDATE vflow_runs SET point_count_total = point_count_total + 1 "
            "WHERE id = ?",
            (point.run_id,),
        )
        self._bump_transition(connection, point.run_id, limits)
        self._event(
            connection,
            point.run_id,
            point.id,
            point.node_id,
            "MERGE_COMPLETED",
            {"fork_instance_id": fork_instance_id, "count": expected},
        )

    def _finish(
        self,
        connection: Any,
        run_id: str,
        data: Any,
        point_id: str,
    ) -> None:
        active = connection.execute(
            """SELECT COUNT(*) FROM vflow_points
               WHERE run_id = ? AND status IN
                 ('ready', 'claimed', 'waiting_task', 'waiting_merge')""",
            (run_id,),
        ).fetchone()[0]
        open_forks = connection.execute(
            "SELECT COUNT(*) FROM vflow_fork_instances WHERE run_id = ? AND status = 'open'",
            (run_id,),
        ).fetchone()[0]
        if active or open_forks:
            raise VFlowRuntimeError(
                "RUN_NOT_COMPLETE",
                "Pipeline reached end while other points or forks are still active",
            )
        now = utcnow()
        connection.execute(
            """UPDATE vflow_runs
               SET status = 'succeeded', final_result_json = ?, public_result_json = NULL,
                   finished_at = ?
               WHERE id = ? AND status = 'running'""",
            (_dump(data), now, run_id),
        )
        self._event(connection, run_id, point_id, "end", "RUN_SUCCEEDED", {})

    # ------------------------------------------------------------------
    # Task nodes
    # ------------------------------------------------------------------
    def _handle_task_timeout(
        self,
        connection: Any,
        point: _Point,
        node: Any,
        task_link: Any,
        task_status: dict[str, Any] | None,
    ) -> bool:
        """Apply separate queue/execution deadlines to one Task attempt.

        Queue timeout may start a new attempt when ``retries`` allows it.
        Execution timeout never retries: a Task which already changed the
        workspace must not be repeated implicitly. Both paths are idempotent
        because they close the link and move the point in one transaction.
        """
        if task_link is None:
            return False
        status = task_status or {}
        state = str(status.get("status") or "")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        started_at = task_link["started_at"]
        if state in TASK_STARTED_STATES and not started_at:
            reported = status.get("started_at")
            started_at = reported if isinstance(reported, str) and reported else now
            connection.execute(
                """UPDATE vflow_task_links SET started_at = ?
                   WHERE task_run_id = ? AND started_at IS NULL""",
                (started_at, point.task_run_id),
            )

        queue_timeout = _timeout_seconds(node.config.get("queue_timeout_seconds"))
        if state == "queued" and not started_at and queue_timeout is not None:
            elapsed = _elapsed_seconds(task_link["queued_at"], now_dt)
            if elapsed is not None and elapsed >= queue_timeout:
                cancelled = self._cancel_task_run(point.task_run_id)
                if cancelled is False:
                    # The dispatcher claimed the job between polling and
                    # cancellation. Do not create a duplicate attempt: record
                    # the observed start and let the execution deadline apply.
                    connection.execute(
                        """UPDATE vflow_task_links
                           SET started_at = COALESCE(started_at, ?)
                           WHERE task_run_id = ?""",
                        (now, point.task_run_id),
                    )
                    return False
                retries = _timeout_seconds(node.config.get("retries")) or 0
                if point.json_repair_attempt == 0 and point.attempt <= retries:
                    self._close_task_links(connection, point, "timed_out", now)
                    connection.execute(
                        """UPDATE vflow_points
                           SET status = 'ready', attempt = ?, task_run_id = NULL,
                               json_repair_attempt = 0,
                               next_check_at = NULL, lease_owner = NULL,
                               lease_token = NULL, lease_until = NULL, updated_at = ?
                           WHERE id = ? AND status = 'claimed'""",
                        (point.attempt + 1, now, point.id),
                    )
                    self._event(
                        connection,
                        point.run_id,
                        point.id,
                        point.node_id,
                        "TASK_QUEUE_TIMEOUT",
                        {
                            "attempt": point.attempt,
                            "retry": True,
                            "next_attempt": point.attempt + 1,
                        },
                    )
                    return True
                self._close_task_links(connection, point, "timed_out", now)
                self._event(
                    connection,
                    point.run_id,
                    point.id,
                    point.node_id,
                    "TASK_QUEUE_TIMEOUT",
                    {
                        "attempt": point.attempt,
                        "repair_attempt": point.json_repair_attempt,
                        "retry": False,
                    },
                )
                self._fail(
                    connection,
                    point.run_id,
                    "TASK_QUEUE_TIMEOUT",
                    "Task did not start before its queue timeout",
                )
                return True

        execution_timeout = _timeout_seconds(
            node.config.get("execution_timeout_seconds")
        )
        if state in TASK_STARTED_STATES and execution_timeout is not None:
            elapsed = _elapsed_seconds(started_at, now_dt)
            if elapsed is not None and elapsed > execution_timeout:
                self._cancel_task_run(point.task_run_id)
                self._close_task_links(connection, point, "timed_out", now)
                self._event(
                    connection,
                    point.run_id,
                    point.id,
                    point.node_id,
                    "TASK_EXECUTION_TIMEOUT",
                    {
                        "attempt": point.attempt,
                        "repair_attempt": point.json_repair_attempt,
                        "elapsed_seconds": int(elapsed),
                    },
                )
                self._fail(
                    connection,
                    point.run_id,
                    "TASK_EXECUTION_TIMEOUT",
                    "Task exceeded its execution timeout",
                )
                return True
        return False

    @staticmethod
    def _repair_limit(config: dict[str, Any]) -> int:
        value = config.get("max_json_repairs", 1)
        if isinstance(value, bool):
            return 1
        try:
            return max(0, min(int(value), 3))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _repair_dedupe_key(point: _Point, repair_attempt: int) -> str:
        return (
            f"vflow-repair:{point.run_id}:{point.id}:"
            f"{point.visit_seq}:{point.attempt}:{repair_attempt}"
        )

    def _handle_task_json_repair(
        self,
        connection: Any,
        point: _Point,
        node: Any,
        task_link: Any,
        task_status: dict[str, Any] | None,
    ) -> bool:
        """Validate completed strict_json output and request one same-chat repair.

        Repair is deliberately limited to syntax/schema failures of a completed
        run.  Timeouts, cancellation, Task changes and execution failures keep
        their existing terminal/retry semantics and never enter this path.
        """
        if task_link is None or not task_status:
            return False
        if str(task_status.get("status") or "") != "completed":
            return False
        config = node.config
        if str(config.get("result_mode", "auto")) != "strict_json":
            return False
        schema = config.get("json_schema")
        _parsed, failure = validate_output(task_status.get("text"), schema)
        if failure is None:
            if point.json_repair_attempt:
                self._event(
                    connection,
                    point.run_id,
                    point.id,
                    point.node_id,
                    "TASK_JSON_REPAIR_COMPLETED",
                    {
                        "repair_attempt": point.json_repair_attempt,
                        "task_run_id": point.task_run_id,
                    },
                )
            return False
        repair_attempt = point.json_repair_attempt + 1
        repair_method = getattr(self.gateway, "repair_output", None)
        if repair_attempt > self._repair_limit(config) or not callable(repair_method):
            self._fail(connection, point.run_id, failure.code, failure.message)
            return True
        prompt = build_correction_prompt(failure, schema)
        dedupe_key = self._repair_dedupe_key(point, repair_attempt)
        now = utcnow()
        payload = {
            "task_run_id": point.task_run_id,
            "task_id": str(task_link["task_id"]),
            "run_id": point.run_id,
            "point_id": point.id,
            "node_id": point.node_id,
            "visit_seq": point.visit_seq,
            "attempt": point.attempt,
            "repair_attempt": repair_attempt,
            "prompt": prompt,
            "error_code": failure.code,
            "error_message": failure.message[:500],
            "error_path": failure.path,
        }
        connection.execute(
            """INSERT OR IGNORE INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json, status,
                attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, 'task_json_repair', ?, 'pending', 0, ?, ?)""",
            (
                str(uuid.uuid4()),
                dedupe_key,
                point.run_id,
                point.id,
                _dump(payload),
                now,
                now,
            ),
        )
        try:
            self._enqueue_repair_payload(connection, payload, dedupe_key, now)
        except VFlowTaskError as exc:
            connection.execute(
                """UPDATE vflow_outbox
                   SET status = 'dead', attempts = attempts + 1, last_error = ?,
                       processed_at = ?, next_attempt_at = ?
                   WHERE dedupe_key = ?""",
                (str(exc)[:500], now, now, dedupe_key),
            )
            self._fail(connection, point.run_id, exc.code, str(exc))
        return True

    def _enqueue_repair_payload(
        self,
        connection: Any,
        payload: dict[str, Any],
        dedupe_key: str,
        now: str,
    ) -> str:
        """Enqueue or reconcile one deterministic repair job and point update."""
        if self.gateway is None:
            raise VFlowTaskError(
                "TASK_REPAIR_UNAVAILABLE", "Task gateway is unavailable"
            )
        repair_method = getattr(self.gateway, "repair_output", None)
        if not callable(repair_method):
            raise VFlowTaskError(
                "TASK_REPAIR_UNAVAILABLE", "Task gateway cannot repair output"
            )
        old_job_id = str(payload["task_run_id"])
        new_job_id = repair_method(
            task_run_id=old_job_id,
            run_id=str(payload["run_id"]),
            point_id=str(payload["point_id"]),
            repair_attempt=int(payload["repair_attempt"]),
            prompt=str(payload["prompt"]),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )
        if not new_job_id:
            raise VFlowTaskError(
                "TASK_REPAIR_UNAVAILABLE", "Task gateway returned no repair job"
            )
        connection.execute(
            """INSERT OR IGNORE INTO vflow_task_links
               (id, run_id, point_id, node_id, visit_seq, attempt, task_id,
                task_run_id, enqueue_key, status, repair_attempt,
                parent_task_run_id, repair_error_code, repair_error_message,
                queued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                payload["run_id"],
                payload["point_id"],
                payload["node_id"],
                int(payload["visit_seq"]),
                int(payload["attempt"]),
                payload["task_id"],
                new_job_id,
                dedupe_key,
                int(payload["repair_attempt"]),
                old_job_id,
                payload.get("error_code"),
                payload.get("error_message"),
                now,
            ),
        )
        connection.execute(
            """UPDATE vflow_task_links SET status = 'repair_requested'
               WHERE task_run_id = ? AND finished_at IS NULL""",
            (old_job_id,),
        )
        cursor = connection.execute(
            """UPDATE vflow_points
               SET status = 'waiting_task', task_run_id = ?,
                   json_repair_attempt = ?, next_check_at = ?,
                   lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                   updated_at = ?
               WHERE id = ? AND task_run_id = ?""",
            (
                new_job_id,
                int(payload["repair_attempt"]),
                (datetime.now(UTC) + timedelta(seconds=TASK_POLL_SECONDS)).isoformat(),
                now,
                payload["point_id"],
                old_job_id,
            ),
        )
        if not cursor.rowcount:
            raise VFlowTaskError(
                "TASK_REPAIR_STALE", "Task point changed before repair was enqueued"
            )
        connection.execute(
            """UPDATE vflow_outbox
               SET status = 'sent', attempts = attempts + 1,
                   processed_at = ?, next_attempt_at = ?
               WHERE dedupe_key = ?""",
            (now, now, dedupe_key),
        )
        self._event(
            connection,
            str(payload["run_id"]),
            str(payload["point_id"]),
            str(payload["node_id"]),
            "TASK_JSON_REPAIR_REQUESTED",
            {
                "repair_attempt": int(payload["repair_attempt"]),
                "task_run_id": new_job_id,
                "parent_task_run_id": old_job_id,
                "error_code": payload.get("error_code"),
                "path": payload.get("error_path"),
            },
        )
        return new_job_id

    def reconcile_repair_outbox(self, limit: int = 20) -> int:
        """Finish repair enqueues interrupted between outbox intent and job link."""
        if self.gateway is None or not callable(
            getattr(self.gateway, "repair_output", None)
        ):
            return 0
        rows = self.database._all(
            """SELECT * FROM vflow_outbox
               WHERE kind = 'task_json_repair'
                 AND status IN ('pending', 'processing')
               ORDER BY next_attempt_at, created_at LIMIT ?""",
            (max(1, int(limit)),),
        )
        applied = 0
        for row in rows:
            with self.database.transaction() as connection:
                current = connection.execute(
                    "SELECT * FROM vflow_outbox WHERE id = ?", (row["id"],)
                ).fetchone()
                if not current or current["status"] not in {"pending", "processing"}:
                    continue
                payload = _load(current["payload_json"], {})
                now = utcnow()
                if not isinstance(payload, dict) or not payload.get("task_run_id"):
                    connection.execute(
                        """UPDATE vflow_outbox
                           SET status = 'dead', attempts = attempts + 1,
                               last_error = 'invalid_repair_payload',
                               processed_at = ?, next_attempt_at = ?
                           WHERE id = ?""",
                        (now, now, row["id"]),
                    )
                    continue
                run = connection.execute(
                    "SELECT status FROM vflow_runs WHERE id = ?",
                    (payload.get("run_id"),),
                ).fetchone()
                point = connection.execute(
                    "SELECT * FROM vflow_points WHERE id = ?",
                    (payload.get("point_id"),),
                ).fetchone()
                if not run or run["status"] != "running" or not point:
                    connection.execute(
                        """UPDATE vflow_outbox
                           SET status = 'dead', attempts = attempts + 1,
                               last_error = 'repair_target_unavailable',
                               processed_at = ?, next_attempt_at = ?
                           WHERE id = ?""",
                        (now, now, row["id"]),
                    )
                    continue
                applied_attempt = int(point["json_repair_attempt"] or 0)
                if applied_attempt >= int(payload["repair_attempt"]):
                    connection.execute(
                        """UPDATE vflow_outbox
                           SET status = 'sent', attempts = attempts + 1,
                               processed_at = ?, next_attempt_at = ?
                           WHERE id = ?""",
                        (now, now, row["id"]),
                    )
                    continue
                if (
                    point["task_run_id"] != payload["task_run_id"]
                    or point["status"] not in ("waiting_task", "claimed")
                    or point["node_id"] != payload["node_id"]
                    or int(point["visit_seq"]) != int(payload["visit_seq"])
                    or int(point["attempt"]) != int(payload["attempt"])
                ):
                    connection.execute(
                        """UPDATE vflow_outbox
                           SET status = 'dead', attempts = attempts + 1,
                               last_error = 'repair_point_changed',
                               processed_at = ?, next_attempt_at = ?
                           WHERE id = ?""",
                        (now, now, row["id"]),
                    )
                    continue
                try:
                    self._enqueue_repair_payload(
                        connection, payload, str(current["dedupe_key"]), now
                    )
                    applied += 1
                except VFlowTaskError as exc:
                    connection.execute(
                        """UPDATE vflow_outbox
                           SET status = 'dead', attempts = attempts + 1,
                               last_error = ?, processed_at = ?,
                               next_attempt_at = ?
                           WHERE id = ?""",
                        (str(exc)[:500], now, now, row["id"]),
                    )
                    self._fail(
                        connection,
                        str(payload["run_id"]),
                        exc.code,
                        str(exc),
                    )
        return applied

    def _cancel_task_run(self, task_run_id: str | None) -> bool | None:
        """Best-effort cancellation; the gateway may reject a running job."""
        if not task_run_id or self.gateway is None:
            return None
        try:
            return self.gateway.cancel(task_run_id)
        except Exception:
            return None

    @staticmethod
    def _close_task_links(
        connection: Any, point: _Point, status: str, now: str
    ) -> None:
        """Close every link in the current Task visit, including repairs."""
        connection.execute(
            """UPDATE vflow_task_links SET status = ?, finished_at = ?
               WHERE point_id = ? AND visit_seq = ? AND attempt = ?
                 AND finished_at IS NULL""",
            (status, now, point.id, point.visit_seq, point.attempt),
        )

    def _task_step(
        self,
        connection: Any,
        point: _Point,
        action: EnqueueTask,
        enqueue_key: str,
    ) -> str:
        """Enqueue one Task run bound to the snapshot captured at Run start."""
        if self.gateway is None:
            raise VFlowRuntimeError("TASK_GATEWAY_UNAVAILABLE", "Task gateway is unavailable")
        task_snapshot = self._task_snapshot(connection, point.run_id, point.node_id)
        return self.gateway.enqueue(
            run_id=point.run_id,
            point_id=point.id,
            task_id=action.task_id,
            user_id=self._user_id(connection, point.run_id),
            workspace_id=self._workspace(connection, point.run_id),
            input_data=action.input_data,
            result_mode=action.result_mode,
            queue_timeout_seconds=action.queue_timeout_seconds,
            execution_timeout_seconds=action.execution_timeout_seconds,
            task_snapshot=task_snapshot,
            idempotency_key=enqueue_key,
            node_id=point.node_id,
        )

    def _apply_enqueue_task(
        self,
        connection: Any,
        point: _Point,
        action: EnqueueTask,
    ) -> None:
        if self.gateway is None:
            raise VFlowRuntimeError("TASK_GATEWAY_UNAVAILABLE", "Task gateway is unavailable")
        now = utcnow()
        enqueue_key = f"vflow:{point.run_id}:{point.id}:{point.visit_seq}:{point.attempt}"
        connection.execute(
            """INSERT OR IGNORE INTO vflow_outbox
               (id, dedupe_key, run_id, point_id, kind, payload_json, status,
                attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, ?, 'task_enqueue', ?, 'pending', 0, ?, ?)""",
            (
                str(uuid.uuid4()),
                enqueue_key,
                point.run_id,
                point.id,
                _dump({"task_id": action.task_id, "result_mode": action.result_mode}),
                now,
                now,
            ),
        )
        try:
            task_run_id = self._task_step(connection, point, action, enqueue_key)
        except VFlowTaskError as exc:
            # A deterministic gateway rejection is not an ambiguous enqueue:
            # close the outbox intent so recovery does not retry it.
            connection.execute(
                """UPDATE vflow_outbox
                   SET status = 'dead', attempts = attempts + 1, last_error = ?,
                       processed_at = ?, next_attempt_at = ?
                   WHERE dedupe_key = ?""",
                (str(exc)[:500], now, now, enqueue_key),
            )
            raise VFlowRuntimeError(exc.code, str(exc)) from exc
        connection.execute(
            """INSERT OR IGNORE INTO vflow_task_links
               (id, run_id, point_id, node_id, visit_seq, attempt, task_id,
                task_run_id, enqueue_key, status, queued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (
                str(uuid.uuid4()),
                point.run_id,
                point.id,
                point.node_id,
                point.visit_seq,
                point.attempt,
                action.task_id,
                task_run_id,
                enqueue_key,
                now,
            ),
        )
        connection.execute(
            """UPDATE vflow_outbox
               SET status = 'sent', attempts = attempts + 1,
                   processed_at = ?, next_attempt_at = ?
               WHERE dedupe_key = ?""",
            (now, now, enqueue_key),
        )
        connection.execute(
            """UPDATE vflow_points
               SET status = 'waiting_task', task_run_id = ?, next_check_at = ?,
                   json_repair_attempt = 0,
                   lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (
                task_run_id,
                (datetime.now(UTC) + timedelta(seconds=TASK_POLL_SECONDS)).isoformat(),
                now,
                point.id,
            ),
        )
        self._event(
            connection,
            point.run_id,
            point.id,
            point.node_id,
            "TASK_ENQUEUED",
            {"task_run_id": task_run_id},
        )

    def _user_id(self, connection: Any, run_id: str) -> str:
        row = connection.execute(
            "SELECT started_by_user_id FROM vflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return str(row["started_by_user_id"])

    def _workspace(self, connection: Any, run_id: str) -> str:
        row = connection.execute(
            "SELECT workspace_id FROM vflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return str(row["workspace_id"])

    # ------------------------------------------------------------------
    # Failure, cancellation, diagnostics
    # ------------------------------------------------------------------
    def _fail(self, connection: Any, run_id: str, code: str, message: str) -> None:
        now = utcnow()
        connection.execute(
            """UPDATE vflow_points SET status = 'cancelled', updated_at = ?
               WHERE run_id = ? AND status IN
                 ('ready', 'claimed', 'waiting_task', 'waiting_merge')""",
            (now, run_id),
        )
        connection.execute(
            """UPDATE vflow_fork_instances SET status = 'cancelled'
               WHERE run_id = ? AND status = 'open'""",
            (run_id,),
        )
        connection.execute(
            """UPDATE vflow_task_links SET status = 'failed', finished_at = ?
               WHERE run_id = ? AND finished_at IS NULL""",
            (now, run_id),
        )
        connection.execute(
            """UPDATE vflow_runs
               SET status = 'failed', error_code = ?, safe_error_message = ?,
                   finished_at = ?
               WHERE id = ? AND status = 'running'""",
            (code, message[:500], now, run_id),
        )
        self._event(connection, run_id, None, None, "RUN_FAILED", {"code": code})

    def cancel_run(self, run_id: str) -> bool:
        cancelled_jobs: list[str] = []
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM vflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run or run["status"] in TERMINAL_RUN_STATUSES:
                return False
            now = utcnow()
            connection.execute(
                "UPDATE vflow_runs SET status = 'cancel_requested' WHERE id = ?",
                (run_id,),
            )
            connection.execute(
                """UPDATE vflow_points SET status = 'cancelled', updated_at = ?
                   WHERE run_id = ? AND status IN
                     ('ready', 'claimed', 'waiting_task', 'waiting_merge')""",
                (now, run_id),
            )
            connection.execute(
                "UPDATE vflow_fork_instances SET status = 'cancelled' "
                "WHERE run_id = ? AND status = 'open'",
                (run_id,),
            )
            connection.execute(
                """UPDATE vflow_task_links SET status = 'cancelled', finished_at = ?
                   WHERE run_id = ? AND finished_at IS NULL""",
                (now, run_id),
            )
            cancelled_jobs = self.database.cancel_queued_vflow_jobs(run_id)
            connection.execute(
                """UPDATE vflow_runs SET status = 'cancelled', finished_at = ?
                   WHERE id = ?""",
                (now, run_id),
            )
            self._event(connection, run_id, None, None, "RUN_CANCELLED", {})
        for job_id in cancelled_jobs:
            self._cancel_task_run(job_id)
        return True

    def run_until_idle(self, run_id: str, max_steps: int = 1000) -> dict[str, Any]:
        """Deterministic helper for tests and admin preview runs."""
        self.reconcile_repair_outbox()
        for _ in range(max_steps):
            run = self.get_run(run_id)
            if not run or run["status"] in TERMINAL_RUN_STATUSES:
                break
            claimed = self.claim_due(limit=20)
            if not claimed:
                break
            for point_id, token in claimed:
                self.process_point(point_id, token)
        return self.get_run(run_id) or {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _bump_transition(
        self,
        connection: Any,
        run_id: str,
        limits: VFlowLimits | None = None,
    ) -> None:
        max_transitions = (limits or self.limits).max_transitions
        cursor = connection.execute(
            "UPDATE vflow_runs SET transition_count = transition_count + 1 "
            "WHERE id = ? AND transition_count < ?",
            (run_id, max_transitions),
        )
        if not cursor.rowcount:
            raise VFlowRuntimeError("LIMIT_TRANSITIONS", "Run transition limit exceeded")

    @staticmethod
    def _event(
        connection: Any,
        run_id: str,
        point_id: str | None,
        node_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO vflow_events
               (run_id, point_id, node_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                point_id,
                node_id,
                event_type,
                _dump(redact_metadata(payload)),
                utcnow(),
            ),
        )
