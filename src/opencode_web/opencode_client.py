from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from .backends import OpenCodeBackend, OpenCodeError, create_backend
from .backends import OpenCodeReadTimeout as OpenCodeReadTimeout
from .backends.contracts import OpenCodeProtocolError
from .backends.contracts import OpenCodeTransportError as OpenCodeTransportError
from .backends.creation import SessionCreation
from .backends.retry import OPENCODE_RETRY_INITIAL_SECONDS as OPENCODE_RETRY_INITIAL_SECONDS
from .backends.retry import OPENCODE_RETRY_MAX_SECONDS as OPENCODE_RETRY_MAX_SECONDS
from .backends.retry import _await_or_cancel as _await_or_cancel
from .backends.retry import _wait_for_retry as _wait_for_retry
from .backends.v1 import OpenCodeV1Backend
from .config import Settings
from .podman_runtime import PodmanError, PodmanRuntime

# Compatibility import for integrations using the original V1 client.
OpenCodeClient = OpenCodeV1Backend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OpenCodeInstance:
    workspace: Path
    port: int
    process: asyncio.subprocess.Process
    client: OpenCodeBackend
    process_group: int | None = None
    event_task: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    startup_error: str | None = None
    users: int = 0
    activity_version: int = 0
    idle_since: float | None = None
    container_name: str | None = None
    cleanup_pending: bool = False


class WorkspaceClient(SessionCreation):
    """Resolve and lease the live client for every call, including after idle-stop."""

    def __init__(self, manager: OpenCodeManager, workspace: str):
        self.manager = manager
        self.workspace = workspace
        self.execution_mode = manager.execution_mode

    async def close(self) -> None:
        """Workspace handles own no transport; each call releases its lease."""

    async def health(self) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.health()

    async def path(self) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.path()

    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.find_created_session(creation_key)

    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.create_session_once(
                title,
                permission=permission,
                creation_key=creation_key,
                before_send=before_send,
            )

    async def sessions(self) -> list[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.sessions()

    async def session(self, session_id: str) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.delete_session(session_id)

    async def update_session_permissions(
        self, session_id: str, permission: list[dict[str, str]]
    ) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.update_session_permissions(session_id, permission)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.messages(session_id)

    async def send_prompt_async(
        self,
        session_id: str,
        *,
        message_id: str,
        content: str,
        agent: str,
        provider_id: str | None,
        model_id: str | None,
        model_variant: str | None,
        system: str | None = None,
    ) -> None:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.send_prompt_async(
                session_id,
                message_id=message_id,
                content=content,
                agent=agent,
                provider_id=provider_id,
                model_id=model_id,
                model_variant=model_variant,
                system=system,
            )

    async def abort(self, session_id: str) -> bool:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.abort(session_id)

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str, remember: bool
    ) -> bool:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.respond_permission(session_id, permission_id, response, remember)

    async def session_statuses(self) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.session_statuses()

    async def permissions(self) -> list[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.permissions()

    async def questions(self) -> list[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.questions()

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.respond_question(request_id, answers)

    async def mcp_statuses(self) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.mcp_statuses()

    async def providers(self) -> dict[str, Any]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.providers()

    async def agents(self) -> list[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            return await client.agents()

    async def global_events(self) -> AsyncIterator[dict[str, Any]]:
        async with self.manager.use(self.workspace, expected_mode=self.execution_mode) as client:
            async for event in client.global_events():
                yield event


class OpenCodeManager:
    def __init__(
        self,
        settings: Settings,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        *,
        execution_mode: str = "direct",
    ):
        if execution_mode not in {"direct", "podman"}:
            raise ValueError("Unknown execution mode")
        self.settings = settings
        self.execution_mode = execution_mode
        self.podman = PodmanRuntime(settings)
        self._podman_preparation_lock = asyncio.Lock()
        self._podman_reconciled = False
        self._switching_mode = False
        self.event_sink = event_sink
        self._instances: dict[str, OpenCodeInstance] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._clients: dict[str, WorkspaceClient] = {}
        self._idle_task: asyncio.Task[None] | None = None
        self._has_pending_jobs: Callable[[str], bool] | None = None

    def _free_port(self) -> int:
        used = {instance.port for instance in self._instances.values()}
        for port in range(self.settings.opencode_port_start, self.settings.opencode_port_end + 1):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind((self.settings.opencode_host, port))
                except OSError:
                    continue
            return port
        raise OpenCodeError("No free port in OPENCODE_PORT_RANGE")

    async def ensure(
        self,
        workspace_raw: str,
        *,
        expected_mode: str | None = None,
    ) -> WorkspaceClient:
        workspace = str(self.settings.validate_workspace(workspace_raw))
        async with self.use(workspace, expected_mode=expected_mode or self.execution_mode):
            return self._clients.setdefault(workspace, WorkspaceClient(self, workspace))

    @asynccontextmanager
    async def use(
        self,
        workspace_raw: str,
        *,
        wake: bool = True,
        activity: bool = True,
        expected_mode: str | None = None,
    ) -> AsyncIterator[OpenCodeBackend | None]:
        """Pin a process across requests; background probes never wake or refresh it."""
        key = str(self.settings.validate_workspace(workspace_raw))
        mode = expected_mode or self.execution_mode
        instance = None
        while instance is None:
            if wake:
                client = await self._ensure_client(key, expected_mode=mode)
            async with self._lock:
                if mode != self.execution_mode:
                    raise OpenCodeError("Режим работы диалога изменился")
                candidate = self._instances.get(key)
                if (
                    candidate
                    and candidate.process.returncode is None
                    and not candidate.cleanup_pending
                    and candidate.ready.is_set()
                    and not candidate.startup_error
                    and (not wake or candidate.client is client)
                ):
                    instance = candidate
                    instance.users += 1
                    if activity:
                        self._touch(instance)
                elif not wake:
                    break
        try:
            yield instance.client if instance else None
        finally:
            if instance:
                # No await: cancellation must not leak a lease.
                instance.users -= 1
                if activity:
                    self._touch(instance)

    @staticmethod
    def _touch(instance: OpenCodeInstance) -> None:
        instance.activity_version += 1
        instance.idle_since = None

    async def _ensure_client(
        self,
        workspace_raw: str,
        *,
        expected_mode: str | None = None,
    ) -> OpenCodeBackend:
        workspace = self.settings.validate_workspace(workspace_raw)
        key = str(workspace)
        starter = False
        async with self._lock:
            if expected_mode is not None and expected_mode != self.execution_mode:
                raise OpenCodeError("Режим работы диалога изменился")
            if self._closing:
                raise OpenCodeError("OpenCode manager is shutting down")
            if self._switching_mode:
                raise OpenCodeError("Меняется режим работы. Повторите запрос позже")
            existing = self._instances.get(key)
            if existing and (existing.process.returncode is not None or existing.cleanup_pending):
                await self._stop_instance(existing)
                self._instances.pop(key, None)
                existing = None
            if existing:
                instance = existing

            if not existing:
                starter = True
                port = self._free_port()
                container_name = None
                username = (
                    "opencode"
                    if self.settings.opencode_api_version == "v2"
                    else self.settings.opencode_username
                )
                password = self.settings.opencode_password
                client_host = self.settings.opencode_host
                if self.execution_mode == "podman":
                    try:
                        await self._prepare_podman()
                        password = secrets.token_urlsafe(32)
                        runtime = await self.podman.start(workspace, port, username, password)
                        process = runtime.process
                        container_name = runtime.container_name
                        client_host = "127.0.0.1"
                    except PodmanError as exc:
                        raise OpenCodeError(str(exc)) from exc
                else:
                    if not self.settings.opencode_bin.exists():
                        raise OpenCodeError(
                            f"OpenCode binary not found: {self.settings.opencode_bin}"
                        )
                    environment = os.environ.copy()
                    environment["OPENCODE_SERVER_USERNAME"] = username
                    if password:
                        environment["OPENCODE_SERVER_PASSWORD"] = password
                    if self.settings.opencode_api_version == "v2":
                        environment.pop("OPENCODE_SERVER_USERNAME", None)
                        environment["OPENCODE_PASSWORD"] = password
                        state = self.settings.opencode_v2_state_root.expanduser().resolve()
                        state = state / hashlib.sha256(str(workspace).encode()).hexdigest()
                        for variable, directory in (
                            ("XDG_DATA_HOME", "data"),
                            ("XDG_STATE_HOME", "state"),
                            ("XDG_CACHE_HOME", "cache"),
                            ("XDG_CONFIG_HOME", "config"),
                        ):
                            path = state / directory
                            path.mkdir(parents=True, exist_ok=True, mode=0o700)
                            environment[variable] = str(path)
                        # Do not inherit a V1 global config containing V1 plugins.
                        environment.pop("OPENCODE_CONFIG", None)
                        environment.pop("OPENCODE_CONFIG_CONTENT", None)
                        environment.pop("OPENCODE_CONFIG_DIR", None)
                        if self.settings.opencode_v2_config_dir:
                            environment["OPENCODE_CONFIG"] = str(
                                self.settings.opencode_v2_config_dir.resolve() / "opencode.jsonc"
                            )
                    process = await asyncio.create_subprocess_exec(
                        str(self.settings.opencode_bin),
                        "serve",
                        "--hostname",
                        self.settings.opencode_host,
                        "--port",
                        str(port),
                        cwd=workspace,
                        env=environment,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=os.name == "posix",
                    )
                client = create_backend(
                    f"http://{client_host}:{port}",
                    username,
                    password,
                    api_version=self.settings.opencode_api_version,
                    workspace=key,
                )
                instance = OpenCodeInstance(
                    workspace,
                    port,
                    process,
                    client,
                    process_group=process.pid if os.name == "posix" else None,
                    container_name=container_name,
                )
                self._instances[key] = instance

        if not starter:
            try:
                await asyncio.wait_for(instance.ready.wait(), self.settings.opencode_start_timeout)
            except TimeoutError as exc:
                raise OpenCodeError("Timed out while waiting for OpenCode startup") from exc
            if instance.startup_error:
                raise OpenCodeError(instance.startup_error)
            if instance.process.returncode is not None:
                raise OpenCodeError(f"OpenCode exited with code {instance.process.returncode}")
            return instance.client

        try:
            return await self._start_instance(instance)
        except BaseException as exc:
            instance.startup_error = str(exc) or "OpenCode startup was cancelled"
            instance.ready.set()
            await self.stop_workspace(key, expected_instance=instance)
            raise

    async def _prepare_podman(self) -> None:
        async with self._podman_preparation_lock:
            if not self._podman_reconciled:
                await self.podman.preflight()
                await self.podman.reconcile()
                self._podman_reconciled = True

    async def recover_runtime(self) -> None:
        """Reap previous workers before accepting work, even with an empty queue."""
        if self.execution_mode != "podman":
            return
        async with self._podman_preparation_lock:
            try:
                # No image/config requirement: an operator may have removed either.
                await self.podman.reconcile()
            except PodmanError as exc:
                self._podman_reconciled = False
                raise OpenCodeError(str(exc)) from exc
            self._podman_reconciled = True

    async def change_execution_mode(self, mode: str) -> None:
        """Drain process leases before changing the runtime for future requests."""
        if mode not in {"direct", "podman"}:
            raise ValueError("Unknown execution mode")
        async with self._lock:
            if self._closing or self._switching_mode:
                raise OpenCodeError("Режим работы сейчас нельзя изменить")
            if any(item.users or not item.ready.is_set() for item in self._instances.values()):
                raise OpenCodeError("Дождитесь завершения текущих запросов OpenCode")
            self._switching_mode = True
            try:
                for key, instance in tuple(self._instances.items()):
                    await self._stop_instance(instance)
                    self._instances.pop(key, None)
                if self.execution_mode == "podman" or mode == "podman":
                    try:
                        await self.podman.reconcile()
                    except PodmanError as exc:
                        raise OpenCodeError(str(exc)) from exc
                self._clients.clear()
                self.execution_mode = mode
                self._podman_reconciled = False
            finally:
                self._switching_mode = False

    async def _start_instance(self, instance: OpenCodeInstance) -> OpenCodeBackend:
        process, client, workspace = instance.process, instance.client, instance.workspace
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.opencode_start_timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                raise OpenCodeError(f"OpenCode exited with code {process.returncode}")
            try:
                remaining = deadline - loop.time()
                await asyncio.wait_for(client.health(), timeout=min(5.0, remaining))
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                path_info = await asyncio.wait_for(client.path(), timeout=min(5.0, remaining))
            except OpenCodeProtocolError:
                raise
            except (OpenCodeError, TimeoutError):
                await asyncio.sleep(0.25)
                continue
            reported = path_info.get("directory") or path_info.get("cwd") or path_info.get("root")
            if reported and Path(reported).resolve() != workspace:
                raise OpenCodeError("OpenCode reported a different workspace")
            if self.event_sink and instance.event_task is None:
                instance.event_task = asyncio.create_task(
                    self._relay_events(instance), name=f"opencode-events-{instance.port}"
                )
            instance.ready.set()
            return client
        raise OpenCodeError("Timed out while starting OpenCode")

    async def _relay_events(self, instance: OpenCodeInstance) -> None:
        retry_delay = 0.5
        while instance.process.returncode is None:
            try:
                async for envelope in instance.client.global_events():
                    payload = envelope.get("payload", envelope)
                    event_type = payload.get("type", "") if isinstance(payload, dict) else ""
                    if event_type.startswith(
                        ("session.", "message.", "question.", "permission.", "form.")
                    ):
                        self._touch(instance)
                    if self.event_sink:
                        await self.event_sink(
                            {
                                "type": "opencode.event",
                                "workspace": str(instance.workspace),
                                "event": envelope,
                            }
                        )
                    retry_delay = 0.5
            except asyncio.CancelledError:
                raise
            except OpenCodeError:
                if instance.process.returncode is not None:
                    return
                if self.event_sink:
                    await self.event_sink(
                        {
                            "type": "opencode.disconnected",
                            "workspace": str(instance.workspace),
                        }
                    )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10)

    def start_idle_reaper(self, has_pending_jobs: Callable[[str], bool]) -> None:
        self._has_pending_jobs = has_pending_jobs
        if self.settings.opencode_idle_timeout_seconds and self._idle_task is None:
            self._idle_task = asyncio.create_task(self._reap_loop(), name="opencode-idle-reaper")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60, self.settings.opencode_idle_timeout_seconds))
            try:
                await self.reap_idle()
            except Exception:
                logger.exception("OpenCode idle inspection failed; keeping processes alive")

    async def reap_idle(self) -> None:
        if not self.settings.opencode_idle_timeout_seconds or self._has_pending_jobs is None:
            return
        await asyncio.gather(*(self._reap_instance(item) for item in self.instances()))

    async def _reap_instance(self, instance: OpenCodeInstance) -> None:
        key = str(instance.workspace)
        try:
            async with self._lock:
                if (
                    self._closing
                    or self._instances.get(key) is not instance
                    or not instance.ready.is_set()
                    or instance.startup_error
                ):
                    return
                if self._has_pending_jobs(key):
                    self._touch(instance)
                    return
                if instance.users:
                    return
                version = instance.activity_version
                # Pin while inspecting; ordinary requests may still proceed.
                instance.users += 1
            try:
                async with asyncio.timeout(10):
                    statuses, permissions, questions = await asyncio.gather(
                        instance.client.session_statuses(),
                        instance.client.permissions(),
                        instance.client.questions(),
                        return_exceptions=True,
                    )
                # Invalid/incomplete responses cannot prove idleness.
                idle = (
                    isinstance(statuses, dict)
                    and all(self._confirmed_idle(value) for value in statuses.values())
                    and isinstance(permissions, list)
                    and not permissions
                    and isinstance(questions, list)
                    and not questions
                )
            finally:
                instance.users -= 1
            async with self._lock:
                if self._closing or self._instances.get(key) is not instance:
                    return
                if not idle or version != instance.activity_version or self._has_pending_jobs(key):
                    self._touch(instance)
                    return
                if instance.users:
                    return
                now = monotonic()
                if instance.idle_since is None:
                    instance.idle_since = now
                elif now - instance.idle_since >= self.settings.opencode_idle_timeout_seconds:
                    # Keep ownership until cleanup succeeds, including a failed Podman rm.
                    await self._stop_instance(instance)
                    self._instances.pop(key, None)
                    logger.info("Stopped idle OpenCode workspace %s", key)
        except Exception:
            self._touch(instance)
            logger.exception("Cannot confirm idle OpenCode workspace %s; keeping it alive", key)

    @staticmethod
    def _confirmed_idle(status: Any) -> bool:
        if isinstance(status, dict):
            status = status.get("type")
        return status in {"idle", "completed", "failed", "aborted"}

    def instances(self) -> list[OpenCodeInstance]:
        return [
            instance for instance in self._instances.values() if instance.process.returncode is None
        ]

    async def stop_workspace(
        self, workspace_raw: str, *, expected_instance: OpenCodeInstance | None = None
    ) -> None:
        key = str(Path(workspace_raw).resolve())
        # Do not start a replacement until the old process group is gone.
        async with self._lock:
            if expected_instance is not None and self._instances.get(key) is not expected_instance:
                return
            instance = self._instances.get(key)
            if instance:
                await self._stop_instance(instance)
                self._instances.pop(key, None)

    @staticmethod
    def _signal_instance(instance: OpenCodeInstance, sig: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            if instance.process_group is not None:
                os.killpg(instance.process_group, sig)
            elif instance.process.returncode is None:
                instance.process.send_signal(sig)

    async def _stop_instance(self, instance: OpenCodeInstance) -> None:
        instance.cleanup_pending = True
        try:
            try:
                if instance.event_task:
                    instance.event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await instance.event_task
            finally:
                await instance.client.close()
        finally:
            if instance.container_name:
                try:
                    await self.podman.stop(instance.container_name)
                    instance.container_name = None
                except PodmanError as exc:
                    # Do not release ownership/capacity until the container is removed.
                    raise OpenCodeError(str(exc)) from exc
            self._signal_instance(instance, signal.SIGTERM)
            try:
                await asyncio.wait_for(instance.process.wait(), 10)
            except TimeoutError:
                pass
            finally:
                # MCP/LSP children can outlive even a cleanly exited OpenCode.
                self._signal_instance(instance, signal.SIGKILL)
                await instance.process.wait()

    async def close(self, *, strict: bool = False) -> None:
        self._closing = True
        if self._idle_task:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        self._clients.clear()
        async with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        # One timeout for all instances, rather than ten seconds per workspace.
        results = await asyncio.gather(
            *(self._stop_instance(instance) for instance in instances), return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        for instance, result in zip(instances, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("OpenCode cleanup failed for %s: %s", instance.workspace, result)
        try:
            await self.podman.cleanup_pending_starts()
        except PodmanError as exc:
            failures.append(exc)
            logger.exception("Failed to clean up an incomplete isolated worker launch")
        if strict and failures:
            raise OpenCodeError("Cannot confirm cleanup before the backend switch") from failures[0]
