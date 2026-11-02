from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from .config import Settings

logger = logging.getLogger(__name__)
OPENCODE_RETRY_INITIAL_SECONDS = 1
OPENCODE_RETRY_MAX_SECONDS = 3 * 60 * 60


class OpenCodeError(RuntimeError):
    pass


class OpenCodeTransportError(OpenCodeError):
    """A temporary transport failure, possibly after OpenCode accepted a request."""

    def __init__(self, message: str, *, request_not_sent: bool = False):
        super().__init__(message)
        self.request_not_sent = request_not_sent


class OpenCodeReadTimeout(OpenCodeTransportError):
    """OpenCode accepted a connection but did not return a response in time."""


async def _await_or_cancel(
    awaitable: Awaitable[Any], cancel_event: asyncio.Event | None = None,
) -> Any:
    """Allow cancellation of a safe read or backoff to release the job lock."""
    if cancel_event is None:
        return await awaitable
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel_event.wait())
    try:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        return await operation
    finally:
        operation.cancel()
        cancellation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


async def _wait_for_retry(delay: float, cancel_event: asyncio.Event | None = None) -> None:
    await _await_or_cancel(asyncio.sleep(delay), cancel_event)


@dataclass(slots=True)
class OpenCodeInstance:
    workspace: Path
    port: int
    process: asyncio.subprocess.Process
    client: OpenCodeClient
    process_group: int | None = None
    event_task: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    startup_error: str | None = None
    users: int = 0
    activity_version: int = 0
    idle_since: float | None = None


class OpenCodeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._providers_cache: dict[str, Any] | None = None
        self._providers_cache_time = 0.0
        self._providers_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(username, password) if password else None,
            timeout=httpx.Timeout(30, connect=5, read=2 * 60 * 60),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, before_send: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Any:
        if before_send is not None and not before_send():
            raise asyncio.CancelledError
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            if len(detail) > 2_000:
                detail = detail[:2_000] + "…"
            suffix = f": {detail}" if detail else ""
            raise OpenCodeError(
                f"OpenCode returned HTTP {exc.response.status_code} for {method} {path}{suffix}"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise OpenCodeReadTimeout(
                f"OpenCode response timed out: {method} {path}"
            ) from exc
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ) as exc:
            raise OpenCodeTransportError(
                f"OpenCode transport failed: {method} {path} ({type(exc).__name__})",
                request_not_sent=isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"OpenCode request failed: {method} {path}") from exc
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/global/health")

    async def path(self) -> dict[str, Any]:
        return await self._request("GET", "/path")

    async def create_session(
        self, title: str | None = None, *, permission: list[dict[str, str]] | None = None,
        creation_key: str | None = None, recover_only: bool = False,
        cancel_event: asyncio.Event | None = None,
        creation_state: Callable[[bool], bool] | None = None,
    ) -> dict[str, Any]:
        return await self._create_session(
            self._request, title, permission=permission, creation_key=creation_key,
            recover_only=recover_only, cancel_event=cancel_event, creation_state=creation_state,
        )

    @staticmethod
    async def _create_session(
        request: Callable[..., Awaitable[Any]],
        title: str | None = None, *, permission: list[dict[str, str]] | None = None,
        creation_key: str | None = None, recover_only: bool = False,
        cancel_event: asyncio.Event | None = None,
        creation_state: Callable[[bool], bool] | None = None,
    ) -> dict[str, Any]:
        """Keep creation state across retries while resolving a live client per request."""
        reconcile = creation_key is not None or recover_only
        creation_key = creation_key or uuid4().hex
        body = {"title": title} if title else {}
        body["metadata"] = {"shodanCreationKey": creation_key}
        if permission is not None:
            body["permission"] = permission
        may_create = not recover_only
        delay = OPENCODE_RETRY_INITIAL_SECONDS
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                if reconcile:
                    # OpenCode defaults to only the 100 latest sessions. SQLite's
                    # unlimited limit also finds sessions created during long outages.
                    sessions = await _await_or_cancel(
                        request("GET", "/session", params={"limit": -1}), cancel_event,
                    )
                    if not isinstance(sessions, list):
                        raise OpenCodeError("OpenCode returned invalid session creation recovery")
                    matches = [
                        session for session in sessions
                        if isinstance(session, dict)
                        and isinstance(session.get("metadata"), dict)
                        and session["metadata"].get("shodanCreationKey") == creation_key
                    ]
                    if len(matches) > 1:
                        raise OpenCodeError("OpenCode returned multiple sessions for creation key")
                    if matches:
                        return matches[0]
                if may_create:
                    try:
                        # Mark at the raw client's send boundary, after any
                        # workspace startup/lease acquisition has finished.
                        return await request(
                            "POST", "/session", json=body,
                            **({"before_send": partial(creation_state, True)}
                               if creation_state is not None else {}),
                        )
                    except OpenCodeTransportError as exc:
                        # An empty list cannot prove that an accepted POST has
                        # finished. Only connection/pool failures are safe to resend.
                        may_create = exc.request_not_sent
                        if may_create and creation_state is not None:
                            creation_state(False)
                        reconcile = True
                        raise
            except OpenCodeTransportError:
                logger.warning(
                    "OpenCode session creation transport failed; retrying %s in %s seconds",
                    "creation recovery" if not may_create else "safe creation",
                    delay,
                    extra={"creation_key": creation_key},
                )
            else:
                logger.warning(
                    "OpenCode session creation outcome unknown; checking again in %s seconds",
                    delay,
                    extra={"creation_key": creation_key},
                )
            await _wait_for_retry(delay, cancel_event)
            delay = min(delay * 2, OPENCODE_RETRY_MAX_SECONDS)

    async def sessions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/session")
        return result if isinstance(result, list) else []

    async def session(self, session_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/session/{session_id}")
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned invalid session details")
        return result

    async def delete_session(self, session_id: str) -> bool:
        return bool(await self._request("DELETE", f"/session/{session_id}"))

    async def update_session_permissions(
        self, session_id: str, permission: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Append rules on OpenCode versions whose session PATCH merges permissions."""
        result = await self._request(
            "PATCH", f"/session/{session_id}", json={"permission": permission},
        )
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode returned invalid session details")
        return result

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/session/{session_id}/message")

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
        body: dict[str, Any] = {
            "messageID": message_id,
            "agent": agent,
            "parts": [{"type": "text", "text": content}],
        }
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
            if model_variant:
                body["variant"] = model_variant
        if system is not None:
            body["system"] = system
        await self._request("POST", f"/session/{session_id}/prompt_async", json=body)

    async def abort(self, session_id: str) -> bool:
        return bool(await self._request("POST", f"/session/{session_id}/abort"))

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str, remember: bool
    ) -> bool:
        result = await self._request(
            "POST",
            f"/session/{session_id}/permissions/{permission_id}",
            json={"response": response, "remember": remember},
        )
        return bool(result)

    async def session_statuses(self) -> dict[str, Any]:
        return await self._request("GET", "/session/status")

    async def permissions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/permission")
        return result if isinstance(result, list) else []

    async def questions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/question")
        if not isinstance(result, list):
            raise OpenCodeError("OpenCode returned invalid question requests")
        return result

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        return bool(
            await self._request("POST", f"/question/{request_id}/reply", json={"answers": answers})
        )

    async def mcp_statuses(self) -> dict[str, Any]:
        result = await self._request("GET", "/mcp", timeout=30)
        return result if isinstance(result, dict) else {}

    async def global_events(self) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream("GET", "/global/event", timeout=None) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except httpx.HTTPError as exc:
            raise OpenCodeError("OpenCode event stream failed") from exc

    async def providers(self) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        if self._providers_cache is not None and now - self._providers_cache_time < 60:
            return self._providers_cache
        async with self._providers_lock:
            now = asyncio.get_running_loop().time()
            if self._providers_cache is not None and now - self._providers_cache_time < 60:
                return self._providers_cache
            result = await self._request("GET", "/provider")
            if not isinstance(result, dict):
                raise OpenCodeError("OpenCode returned an invalid provider catalog")
            self._providers_cache = result
            self._providers_cache_time = asyncio.get_running_loop().time()
            return result

    async def agents(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/agent")


class WorkspaceClient:
    """Resolve and lease the live client for every call, including after idle-stop."""

    def __init__(self, manager: OpenCodeManager, workspace: str):
        self.manager = manager
        self.workspace = workspace

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with self.manager.use(self.workspace) as client:
            return await client._request(method, path, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in {"close", "global_events"}:
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            if name == "create_session":
                return await OpenCodeClient._create_session(self._request, *args, **kwargs)
            async with self.manager.use(self.workspace) as client:
                return await getattr(client, name)(*args, **kwargs)

        return call


class OpenCodeManager:
    def __init__(
        self,
        settings: Settings,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.settings = settings
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

    async def ensure(self, workspace_raw: str) -> WorkspaceClient:
        workspace = str(self.settings.validate_workspace(workspace_raw))
        async with self.use(workspace):
            return self._clients.setdefault(workspace, WorkspaceClient(self, workspace))

    @asynccontextmanager
    async def use(
        self, workspace_raw: str, *, wake: bool = True, activity: bool = True
    ) -> AsyncIterator[OpenCodeClient | None]:
        """Pin a process across requests; background probes never wake or refresh it."""
        key = str(self.settings.validate_workspace(workspace_raw))
        instance = None
        while instance is None:
            if wake:
                client = await self._ensure_client(key)
            async with self._lock:
                candidate = self._instances.get(key)
                if (candidate and candidate.process.returncode is None
                        and candidate.ready.is_set() and not candidate.startup_error
                        and (not wake or candidate.client is client)):
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

    async def _ensure_client(self, workspace_raw: str) -> OpenCodeClient:
        workspace = self.settings.validate_workspace(workspace_raw)
        key = str(workspace)
        starter = False
        async with self._lock:
            if self._closing:
                raise OpenCodeError("OpenCode manager is shutting down")
            existing = self._instances.get(key)
            if existing and existing.process.returncode is None:
                instance = existing
            if existing and existing.process.returncode is not None:
                await self._stop_instance(existing)
                self._instances.pop(key, None)
                existing = None

            if not existing:
                starter = True
                if not self.settings.opencode_bin.exists():
                    raise OpenCodeError(f"OpenCode binary not found: {self.settings.opencode_bin}")
                port = self._free_port()
                environment = os.environ.copy()
                environment["OPENCODE_SERVER_USERNAME"] = self.settings.opencode_username
                if self.settings.opencode_password:
                    environment["OPENCODE_SERVER_PASSWORD"] = self.settings.opencode_password
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
                client = OpenCodeClient(
                    f"http://{self.settings.opencode_host}:{port}",
                    self.settings.opencode_username,
                    self.settings.opencode_password,
                )
                instance = OpenCodeInstance(
                    workspace, port, process, client,
                    process_group=process.pid if os.name == "posix" else None,
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

    async def _start_instance(self, instance: OpenCodeInstance) -> OpenCodeClient:
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
                    if event_type.startswith(("session.", "message.", "question.", "permission.")):
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
                if (self._closing or self._instances.get(key) is not instance
                        or not instance.ready.is_set() or instance.startup_error):
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
                        instance.client._request("GET", "/session/status"),
                        instance.client._request("GET", "/permission"),
                        instance.client._request("GET", "/question"),
                        return_exceptions=True,
                    )
                # Invalid/incomplete responses cannot prove idleness.
                idle = (
                    isinstance(statuses, dict)
                    and all(self._confirmed_idle(value) for value in statuses.values())
                    and isinstance(permissions, list) and not permissions
                    and isinstance(questions, list) and not questions
                )
            finally:
                instance.users -= 1
            async with self._lock:
                if self._closing or self._instances.get(key) is not instance:
                    return
                if (not idle or version != instance.activity_version
                        or self._has_pending_jobs(key)):
                    self._touch(instance)
                    return
                if instance.users:
                    return
                now = monotonic()
                if instance.idle_since is None:
                    instance.idle_since = now
                elif now - instance.idle_since >= self.settings.opencode_idle_timeout_seconds:
                    # Final check + removal share the lock used by request leases/startup.
                    self._instances.pop(key)
                    await self._stop_instance(instance)
                    logger.info("Stopped idle OpenCode workspace %s", key)
        except Exception:
            self._touch(instance)
            logger.exception("Cannot confirm idle OpenCode workspace %s; keeping it alive", key)

    @staticmethod
    def _confirmed_idle(status: Any) -> bool:
        if isinstance(status, dict):
            status = status.get("type")
        return status == "idle"

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
            instance = self._instances.pop(key, None)
            if instance:
                await self._stop_instance(instance)

    @staticmethod
    def _signal_instance(instance: OpenCodeInstance, sig: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            if instance.process_group is not None:
                os.killpg(instance.process_group, sig)
            elif instance.process.returncode is None:
                instance.process.send_signal(sig)

    async def _stop_instance(self, instance: OpenCodeInstance) -> None:
        try:
            try:
                if instance.event_task:
                    instance.event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await instance.event_task
            finally:
                await instance.client.close()
        finally:
            self._signal_instance(instance, signal.SIGTERM)
            try:
                await asyncio.wait_for(instance.process.wait(), 10)
            except TimeoutError:
                pass
            finally:
                # MCP/LSP children can outlive even a cleanly exited OpenCode.
                self._signal_instance(instance, signal.SIGKILL)
                await instance.process.wait()

    async def close(self) -> None:
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
        for instance, result in zip(instances, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("OpenCode cleanup failed for %s: %s", instance.workspace, result)
