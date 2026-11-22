from __future__ import annotations

import hashlib
import json
from typing import Any

from ..database import Database


class VFlowTaskError(RuntimeError):
    """Deterministic task gateway failure with a safe machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def vflow_repair_identity(
    *,
    run_id: str,
    point_id: str,
    node_id: str | None,
    visit_seq: int | None,
    repair_root_task_run_id: str,
    parent_task_run_id: str,
    repair_attempt: int,
) -> str:
    """Canonical identity of one repair request.

    A VFlow point row is reused while it traverses the graph, so ``point_id``
    alone cannot identify a Task node.  The root Task run and the immediate
    parent run pin the repair to one concrete node visit, while the node/visit
    components keep loop iterations and retries distinct.
    """
    return ":".join(
        (
            run_id,
            point_id,
            node_id or "",
            str(visit_seq or 0),
            repair_root_task_run_id,
            parent_task_run_id,
            str(repair_attempt),
        )
    )


class ValeriaTaskGateway:
    """Thin adapter between VFlow task points and the existing VALERIA queue.

    VFlow never calls OpenCode directly.  A task point becomes a normal
    ``queue_jobs`` row and the dispatcher remains the only component that runs
    the agent.  Task points are bound to the snapshot captured when the Run
    started; editing or deleting the Task afterwards makes enqueue fail with a
    deterministic ``TASK_CHANGED``/``TASK_UNAVAILABLE`` error instead of
    silently executing another Task version.
    """

    def __init__(self, database: Database, max_queued: int = 1000) -> None:
        self.database = database
        self.max_queued = max_queued

    def enqueue(
        self,
        *,
        run_id: str,
        point_id: str,
        task_id: str,
        user_id: str,
        workspace_id: str,
        input_data: Any,
        result_mode: str = "auto",
        queue_timeout_seconds: int | None = None,
        execution_timeout_seconds: int | None = None,
        task_snapshot: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        node_id: str | None = None,
    ) -> str:
        del queue_timeout_seconds, execution_timeout_seconds
        # VFlow passes its deterministic per-attempt enqueue key. A retry or a
        # revisited Task point therefore gets a distinct queue job, while a
        # repeated dispatch of the same attempt still resolves to one job.
        enqueue_seed = idempotency_key or f"{run_id}:{point_id}"
        # OpenCode v2 requires message IDs to start with ``msg_``.  Keep the
        # VFlow marker inside the prefix so queue jobs remain easy to trace.
        message_id = "msg_vflow_" + hashlib.sha256(
            enqueue_seed.encode()
        ).hexdigest()[:40]
        task = self.database.get_task(task_id)
        if not task:
            raise VFlowTaskError("TASK_UNAVAILABLE", "Task is unavailable")
        if task_snapshot is not None:
            # The snapshot pins the Task version captured at Run start.  VFlow
            # deliberately does not fall back to the newer Task: an active Run
            # must never silently execute a different version.  A full snapshot
            # override is not possible because prompts/content are intentionally
            # not stored in task_snapshots_json.
            snapshot_task_id = task_snapshot.get("task_id")
            if snapshot_task_id is not None and str(snapshot_task_id) != str(task["id"]):
                raise VFlowTaskError(
                    "TASK_CHANGED", "Task changed after the run started"
                )
            if task_snapshot.get("updated_at") != task.get("updated_at"):
                raise VFlowTaskError(
                    "TASK_CHANGED", "Task changed after the run started"
                )
        user = self.database.get_user(user_id)
        if not user or user["status"] != "active":
            raise VFlowTaskError("USER_UNAVAILABLE", "User is unavailable")
        if not user.get("workspace_path"):
            raise VFlowTaskError("WORKSPACE_UNAVAILABLE", "User has no workspace")
        if workspace_id != user["workspace_path"]:
            raise VFlowTaskError(
                "WORKSPACE_MISMATCH", "Workspace does not belong to the user"
            )

        payload = json.dumps(
            {"vflow_input": input_data, "result_mode": result_mode},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            job = self.database.create_task_job(
                task=task,
                user=user,
                actor_user_id=user_id,
                session_id=None,
                user_input=payload,
                max_queued=self.max_queued,
                session_title=f"VFlow: {task['title']}",
                require_published=False,
                allow_duplicate=True,
                pipeline_json="",
                message_id=message_id,
                vflow_run_id=run_id,
                vflow_point_id=point_id,
                origin_node_id=node_id,
            )
        except ValueError as exc:
            message = str(exc)
            if message == "Task changed":
                raise VFlowTaskError(
                    "TASK_CHANGED", "Task changed after the run started"
                ) from exc
            if message == "Task is unavailable":
                raise VFlowTaskError("TASK_UNAVAILABLE", "Task is unavailable") from exc
            raise VFlowTaskError("TASK_ENQUEUE_FAILED", message) from exc
        if job.get("status") == "cancelled":
            raise VFlowTaskError(
                "TASK_CANCELLED", "Task was cancelled while enqueuing"
            )
        return str(job["id"])

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
        node_id: str | None = None,
        visit_seq: int | None = None,
        repair_root_task_run_id: str | None = None,
    ) -> str:
        """Send one bounded correction turn into the original Task chat.

        The new queue job deliberately has ``task_id = NULL``: it is a normal
        chat continuation in the same session, not a second Task Run.  A
        deterministic message id makes repeated polling/crash recovery
        idempotent.

        ``task_run_id`` is the immediate run being corrected (a Task run or a
        previous repair turn).  ``repair_root_task_run_id`` is the original
        Task run of that node visit and stays stable across a repair chain.
        """
        del error_code, error_message
        if not isinstance(prompt, str) or not prompt.strip():
            raise VFlowTaskError("TASK_REPAIR_UNAVAILABLE", "Repair prompt is empty")
        if (
            not isinstance(repair_attempt, int)
            or isinstance(repair_attempt, bool)
            or repair_attempt < 1
        ):
            raise VFlowTaskError("TASK_REPAIR_UNAVAILABLE", "Repair attempt is invalid")
        if node_id is not None and (
            not isinstance(node_id, str) or not node_id.strip()
        ):
            raise VFlowTaskError("TASK_REPAIR_UNAVAILABLE", "Repair node is invalid")
        if visit_seq is not None and (
            not isinstance(visit_seq, int)
            or isinstance(visit_seq, bool)
            or visit_seq < 1
        ):
            raise VFlowTaskError("TASK_REPAIR_UNAVAILABLE", "Repair visit is invalid")
        if repair_root_task_run_id is not None and (
            not isinstance(repair_root_task_run_id, str)
            or not repair_root_task_run_id
        ):
            raise VFlowTaskError("TASK_REPAIR_UNAVAILABLE", "Repair root is invalid")

        root_task_run_id = (
            repair_root_task_run_id
            or self.database.resolve_vflow_repair_root(task_run_id)
        )
        if not root_task_run_id:
            raise VFlowTaskError(
                "TASK_REPAIR_UNAVAILABLE", "Original job is not a task run"
            )
        seed = vflow_repair_identity(
            run_id=run_id,
            point_id=point_id,
            node_id=node_id,
            visit_seq=visit_seq,
            repair_root_task_run_id=root_task_run_id,
            parent_task_run_id=task_run_id,
            repair_attempt=repair_attempt,
        )
        message_id = "msg_vflow_repair_" + hashlib.sha256(
            seed.encode()
        ).hexdigest()[:40]
        try:
            job = self.database.create_vflow_repair_job(
                original_job_id=task_run_id,
                repair_root_task_run_id=root_task_run_id,
                run_id=run_id,
                point_id=point_id,
                node_id=node_id,
                visit_seq=visit_seq,
                repair_attempt=repair_attempt,
                prompt=prompt,
                message_id=message_id,
            )
        except ValueError as exc:
            raise VFlowTaskError(
                "TASK_REPAIR_UNAVAILABLE",
                f"Task output cannot be repaired: {exc}",
            ) from exc
        return str(job["id"])

    def status(self, task_run_id: str) -> dict[str, Any]:
        job = self.database.get_job(task_run_id)
        if not job:
            return {"status": "failed", "safe_error": "Task run not found"}
        status = str(job["status"])
        if status == "completed":
            # Both Task runs and same-chat repair turns persist their final
            # assistant text in task_run_results.
            result = self.database.get_task_result(task_run_id)
            text = result["text"] if result else None
            return {"status": "completed", "text": text}
        if status in {"failed", "cancelled"}:
            result = {
                "status": status,
                "safe_error": job.get("error_code") or f"Task {status}",
            }
            if job.get("error_code"):
                result["code"] = str(job["error_code"])
            if job.get("started_at"):
                result["started_at"] = job["started_at"]
            return result
        result = {"status": status}
        if job.get("started_at"):
            result["started_at"] = job["started_at"]
        return result

    def cancel(self, task_run_id: str) -> bool:
        return self.database.cancel_queued_job(task_run_id)
