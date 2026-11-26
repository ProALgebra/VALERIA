"""Hermes Agent adapter for the application backend contract.

Hermes exposes durable sessions and native runs instead of OpenCode prompts.
This adapter keeps the OpenCode-shaped projection used by the dispatcher and
browser, while persisting the message -> run binding needed for reconciliation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .contracts import (
    OpenCodeError,
    OpenCodeProtocolError,
    OpenCodeReadTimeout,
    OpenCodeSubmissionUncertain,
    OpenCodeTransportError,
)
from .creation import SessionCreation
from .retry import _await_or_cancel, _wait_for_retry

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATUSES = {
    "started",
    "queued",
    "running",
    "waiting_for_approval",
    "stopping",
    "unknown",
}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


class HermesStateStore(Protocol):
    def save_hermes_binding(
        self,
        *,
        message_id: str,
        job_id: str | None,
        session_id: str,
        content: str,
        phase: str | None = None,
        run_id: str | None = None,
        status: str = "started",
        admission_id: str | None = None,
        execution_generation: int | None = None,
        output: str | None = None,
        request_json: str | None = None,
    ) -> dict[str, Any]: ...

    def get_hermes_binding(self, message_id: str) -> dict[str, Any] | None: ...

    def get_hermes_binding_by_run(self, run_id: str) -> dict[str, Any] | None: ...

    def list_hermes_bindings(
        self, session_id: str | None = None, *, active_only: bool = False
    ) -> list[dict[str, Any]]: ...

    def update_hermes_binding(
        self, message_id: str, **fields: Any
    ) -> dict[str, Any] | None: ...

    def save_hermes_session_meta(
        self,
        session_id: str,
        *,
        workspace: str,
        title: str | None = None,
        permission: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]: ...

    def get_hermes_session_meta(self, session_id: str) -> dict[str, Any] | None: ...


class MemoryHermesStateStore:
    """Small in-memory store for unit tests and one-off backend construction."""

    def __init__(self) -> None:
        self.bindings: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}

    def save_hermes_binding(self, **values: Any) -> dict[str, Any]:
        message_id = str(values["message_id"])
        current = self.bindings.get(message_id, {})
        ordinal = current.get("ordinal")
        if ordinal is None:
            ordinal = (
                max(
                    (
                        int(item.get("ordinal", 0))
                        for item in self.bindings.values()
                        if item.get("session_id") == values["session_id"]
                    ),
                    default=0,
                )
                + 1
            )
        row = {
            **current,
            **{key: value for key, value in values.items() if value is not None},
            "message_id": message_id,
            "ordinal": ordinal,
        }
        row.setdefault("status", "started")
        row.setdefault("content", "")
        self.bindings[message_id] = row
        return dict(row)

    def get_hermes_binding(self, message_id: str) -> dict[str, Any] | None:
        value = self.bindings.get(message_id)
        return dict(value) if value else None

    def get_hermes_binding_by_run(self, run_id: str) -> dict[str, Any] | None:
        for value in self.bindings.values():
            if value.get("run_id") == run_id:
                return dict(value)
        return None

    def list_hermes_bindings(
        self, session_id: str | None = None, *, active_only: bool = False
    ) -> list[dict[str, Any]]:
        values = [
            dict(item)
            for item in self.bindings.values()
            if session_id is None or item.get("session_id") == session_id
        ]
        if active_only:
            values = [item for item in values if item.get("status") in _ACTIVE_RUN_STATUSES]
        return sorted(values, key=lambda item: (item.get("session_id", ""), item["ordinal"]))

    def update_hermes_binding(
        self, message_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        if message_id not in self.bindings:
            return None
        self.bindings[message_id].update(
            {key: value for key, value in fields.items() if value is not None}
        )
        return dict(self.bindings[message_id])

    def save_hermes_session_meta(
        self,
        session_id: str,
        *,
        workspace: str,
        title: str | None = None,
        permission: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        self.sessions[session_id] = {
            "session_id": session_id,
            "workspace": workspace,
            "title": title,
            "permission": list(permission or []),
        }
        return dict(self.sessions[session_id])

    def get_hermes_session_meta(self, session_id: str) -> dict[str, Any] | None:
        value = self.sessions.get(session_id)
        return dict(value) if value else None


def _compact_id(value: str, limit: int = 120) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"valeria-{digest[:limit]}"


def _control_id(run_id: str, request_id: str, generation: int | None) -> str:
    raw = json.dumps(
        {"run_id": run_id, "request_id": request_id, "generation": generation},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_control_id(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, TypeError) as exc:
        raise OpenCodeError("Invalid Hermes control request") from exc
    if not isinstance(decoded, dict) or not decoded.get("run_id"):
        raise OpenCodeError("Invalid Hermes control request")
    return decoded


class HermesBackend(SessionCreation):
    supports_phase = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        workspace: str | None = None,
        state_store: HermesStateStore | None = None,
        source: str = "valeria",
        model_name: str = "hermes",
    ):
        self.base_url = base_url.rstrip("/")
        self.workspace = str(Path(workspace).expanduser().resolve()) if workspace else str(
            Path.cwd()
        )
        self.source = source
        self.model_name = model_name
        self._store: HermesStateStore = state_store or MemoryHermesStateStore()
        self._providers_cache: dict[str, Any] | None = None
        self._providers_cache_time = 0.0
        self._providers_lock = asyncio.Lock()
        self._run_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._run_cache_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=httpx.Timeout(30, connect=5, read=120),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        before_send: Callable[[], bool] | None = None,
        uncertain_on_timeout: bool = False,
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
                f"Hermes returned HTTP {exc.response.status_code} for {method} {path}{suffix}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.ReadTimeout as exc:
            if uncertain_on_timeout:
                raise OpenCodeSubmissionUncertain(
                    f"Hermes response timed out after a possible submission: {method} {path}"
                ) from exc
            raise OpenCodeReadTimeout(f"Hermes response timed out: {method} {path}") from exc
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ) as exc:
            error = OpenCodeTransportError(
                f"Hermes transport failed: {method} {path} ({type(exc).__name__})",
                request_not_sent=isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ),
            )
            if uncertain_on_timeout:
                raise OpenCodeSubmissionUncertain(str(error)) from exc
            raise error from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"Hermes request failed: {method} {path}") from exc
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeProtocolError(
                f"Hermes returned invalid JSON: {method} {path}"
            ) from exc

    async def _events(self, path: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream("GET", path, timeout=None) as response:
                response.raise_for_status()
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        raw = "\n".join(data)
                        data.clear()
                        try:
                            event = json.loads(raw)
                        except ValueError as exc:
                            raise OpenCodeProtocolError(
                                "Hermes returned an invalid event"
                            ) from exc
                        if isinstance(event, dict):
                            yield event
        except httpx.HTTPError as exc:
            raise OpenCodeError("Hermes event stream failed") from exc

    async def health(self) -> dict[str, Any]:
        try:
            result = await self._request("GET", "/health")
        except OpenCodeError as exc:
            if exc.status_code != 404:
                raise
            result = await self._request("GET", "/v1/health")
        return result if isinstance(result, dict) else {"status": "ok"}

    async def path(self) -> dict[str, Any]:
        return {"directory": self.workspace}

    def _session_id(self, creation_key: str) -> str:
        return _compact_id(creation_key)

    def _session_view(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get("id") or session.get("session_id") or "")
        if not session_id:
            raise OpenCodeProtocolError("Hermes returned a session without an ID")
        meta = self._store.get_hermes_session_meta(session_id) or {}
        permission = meta.get("permission")
        if permission is None and meta.get("permission_json"):
            try:
                permission = json.loads(str(meta["permission_json"]))
            except ValueError:
                permission = []
        result: dict[str, Any] = {
            "id": session_id,
            "directory": self.workspace,
            "permission": list(permission or []),
        }
        title = session.get("title") or (meta or {}).get("title")
        if title:
            result["title"] = str(title)
        parent = session.get("parent_session_id") or session.get("parentID")
        if parent:
            result["parentID"] = str(parent)
        return result

    async def _get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = await self._request(
                "GET", f"/api/sessions/{quote(session_id, safe='')}"
            )
        except OpenCodeError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            raise OpenCodeProtocolError("Hermes returned invalid session details")
        session = payload.get("session", payload)
        if not isinstance(session, dict):
            raise OpenCodeProtocolError("Hermes returned invalid session details")
        return session

    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None:
        session = await self._get_session(self._session_id(creation_key))
        return self._session_view(session) if session else None

    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        session_id = self._session_id(creation_key)
        body: dict[str, Any] = {"id": session_id, "source": self.source}
        if title:
            body["title"] = title
        try:
            payload = await self._request(
                "POST",
                "/api/sessions",
                json=body,
                before_send=before_send,
            )
        except OpenCodeError as exc:
            if exc.status_code != 409:
                raise
            existing = await self._get_session(session_id)
            if existing is None:
                raise
            payload = {"session": existing}
        if not isinstance(payload, dict):
            raise OpenCodeProtocolError("Hermes returned invalid session creation")
        session = payload.get("session", payload)
        if not isinstance(session, dict):
            raise OpenCodeProtocolError("Hermes returned invalid session creation")
        view = self._session_view(session)
        self._store.save_hermes_session_meta(
            view["id"],
            workspace=self.workspace,
            title=title,
            permission=permission,
        )
        view["permission"] = list(permission or [])
        return view

    async def sessions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = await self._request(
                "GET",
                "/api/sessions",
                params={"limit": 200, "offset": offset, "include_children": "true"},
            )
            data = payload.get("data", []) if isinstance(payload, dict) else payload
            if not isinstance(data, list):
                raise OpenCodeProtocolError("Hermes returned an invalid session list")
            for session in data:
                if isinstance(session, dict):
                    result.append(self._session_view(session))
            if len(data) < 200:
                return result
            offset += len(data)

    async def session(self, session_id: str) -> dict[str, Any]:
        session = await self._get_session(session_id)
        if session is None:
            raise OpenCodeError("Hermes session not found", status_code=404)
        return self._session_view(session)

    async def delete_session(self, session_id: str) -> bool:
        result = await self._request(
            "DELETE", f"/api/sessions/{quote(session_id, safe='')}"
        )
        if isinstance(result, dict):
            return bool(result.get("deleted", True))
        return result is not None

    async def update_session_permissions(
        self,
        session_id: str,
        permission: list[dict[str, str]],
    ) -> dict[str, Any]:
        session = await self.session(session_id)
        self._store.save_hermes_session_meta(
            session_id,
            workspace=self.workspace,
            title=session.get("title"),
            permission=permission,
        )
        session["permission"] = list(permission)
        return session

    async def _transcript_rows(self, session_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = await self._request(
                "GET",
                f"/api/sessions/{quote(session_id, safe='')}/messages",
                params={
                    "limit": 500,
                    "offset": offset,
                    "order": "oldest",
                    "include_compacted": "true",
                    "inline_images": "true",
                },
            )
            data = payload.get("data", []) if isinstance(payload, dict) else payload
            if not isinstance(data, list):
                raise OpenCodeProtocolError("Hermes returned invalid message history")
            rows.extend(row for row in data if isinstance(row, dict))
            if len(data) < 500:
                return rows
            offset += len(data)

    @staticmethod
    def _text_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            chunks: list[str] = []
            for block in value:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict) and block.get("type") in {
                    "text",
                    "input_text",
                    "output_text",
                }:
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
            return "".join(chunks)
        if isinstance(value, dict):
            text = value.get("text") or value.get("content")
            return text if isinstance(text, str) else ""
        return ""

    @staticmethod
    def _timestamp_ms(value: Any) -> int | None:
        if isinstance(value, (int, float)):
            return int(value if value > 10_000_000_000 else value * 1000)
        if isinstance(value, str):
            try:
                from datetime import datetime

                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                return None
        return None

    def _normalize_finish(self, row: dict[str, Any]) -> str:
        finish = str(row.get("finish_reason") or "").strip().lower()
        if row.get("tool_calls") or finish in {"tool_calls", "tool-call", "tool-calls"}:
            return "tool-calls"
        if finish in {"length", "max_tokens", "max-tokens"}:
            return "length"
        if finish in {"error", "content_filter", "content-filter"}:
            return "error"
        return "stop"

    def _parts_for_row(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        role = str(row.get("role") or "")
        content = self._text_content(row.get("content"))
        parts: list[dict[str, Any]] = []
        if role == "tool":
            parts.append(
                {
                    "type": "tool",
                    "tool": str(row.get("tool_name") or "tool"),
                    "state": {"status": "completed", "output": content},
                }
            )
            return parts
        reasoning = row.get("reasoning_content") or row.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            parts.append({"type": "reasoning", "text": reasoning})
        if content:
            parts.append({"type": "text", "text": content})
        tool_calls = row.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                arguments = function.get("arguments", call.get("arguments"))
                if isinstance(arguments, str):
                    with suppress(ValueError):
                        arguments = json.loads(arguments)
                parts.append(
                    {
                        "type": "tool",
                        "tool": str(function.get("name") or call.get("name") or "tool"),
                        "state": {"status": "completed", "input": arguments},
                    }
                )
        if not parts:
            parts.append({"type": "text", "text": ""})
        return parts

    def _normalized_messages(
        self,
        session_id: str,
        rows: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_ordinal = {int(binding["ordinal"]): binding for binding in bindings}
        messages: list[dict[str, Any]] = []
        user_ordinal = 0
        current_user_id: str | None = None
        last_assistant_text: list[str] = []
        for row in rows:
            role = str(row.get("role") or "")
            row_id = str(row.get("id") or f"hermes-message-{len(messages)}")
            timestamp = self._timestamp_ms(row.get("timestamp"))
            hidden = row.get("display_kind") in {"hidden", "summary", "compaction"}
            if role == "user" and not hidden:
                user_ordinal += 1
                binding = by_ordinal.get(user_ordinal)
                if binding is not None:
                    row_id = str(binding["message_id"])
                current_user_id = row_id
            info: dict[str, Any] = {"id": row_id, "role": role}
            if role in {"assistant", "tool"} and current_user_id:
                info["parentID"] = current_user_id
            if timestamp is not None:
                info["time"] = {"created": timestamp, "completed": timestamp}
            if role == "assistant":
                info.setdefault("time", {}).setdefault("completed", 1)
                finish = self._normalize_finish(row)
                info["finish"] = finish
                if finish == "error":
                    info["error"] = {"name": "HermesError"}
                last_assistant_text.append(
                    self._text_content(row.get("content")).strip()
                )
            messages.append({"info": info, "parts": self._parts_for_row(row)})

        known_user_ids = {
            str(message["info"]["id"])
            for message in messages
            if message["info"].get("role") == "user"
        }
        for binding in bindings:
            message_id = str(binding["message_id"])
            if message_id not in known_user_ids:
                timestamp = self._timestamp_ms(binding.get("created_at"))
                info = {
                    "id": message_id,
                    "role": "user",
                    **({"time": {"created": timestamp}} if timestamp else {}),
                }
                messages.append(
                    {
                        "info": info,
                        "parts": [{"type": "text", "text": binding.get("content") or ""}],
                        "hermes_synthetic": True,
                    }
                )
                known_user_ids.add(message_id)
            if not binding.get("run_id"):
                continue
            output = binding.get("output")
            if not output:
                continue
            output_text = str(output).strip()
            if not output_text or output_text in last_assistant_text:
                continue
            timestamp = self._timestamp_ms(binding.get("updated_at")) or self._timestamp_ms(
                binding.get("created_at")
            )
            info: dict[str, Any] = {
                "id": f"hermes-output-{binding['run_id']}",
                "role": "assistant",
                "parentID": message_id,
                "finish": "stop",
                "time": {"completed": timestamp or 1},
            }
            messages.append(
                {
                    "info": info,
                    "parts": [{"type": "text", "text": output_text}],
                    "hermes_synthetic": True,
                }
            )
            last_assistant_text.append(output_text)
        return messages

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self._transcript_rows(session_id)
        bindings = self._store.list_hermes_bindings(session_id)
        for binding in bindings:
            if binding.get("status") not in _ACTIVE_RUN_STATUSES or not binding.get("run_id"):
                continue
            try:
                await self._refresh_binding(binding)
            except OpenCodeError:
                logger.debug("Cannot refresh Hermes binding %s", binding["message_id"])
        bindings = self._store.list_hermes_bindings(session_id)
        return self._normalized_messages(session_id, rows, bindings)

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
        phase: str | None = None,
    ) -> None:
        async with self._send_lock:
            existing = self._store.get_hermes_binding(message_id)
            if existing and existing.get("run_id"):
                return
            instructions = system or ""
            mode = (phase or agent or "").strip().lower()
            if mode == "plan":
                planning = (
                    "Planning phase. Inspect the workspace and produce a concrete execution "
                    "plan. Do not modify files or run destructive commands. Finish with the plan."
                )
                instructions = f"{instructions}\n\n{planning}".strip()
            body: dict[str, Any] = {"input": content, "session_id": session_id}
            if instructions:
                body["instructions"] = instructions
            if provider_id and model_id:
                body["provider"] = provider_id
                body["model"] = model_id
                if model_variant:
                    body["model_options"] = {"reasoning_effort": model_variant}
            binding = self._store.save_hermes_binding(
                message_id=message_id,
                job_id=None,
                session_id=session_id,
                content=content,
                phase=phase or agent,
                status="sending",
                request_json=json.dumps(body, ensure_ascii=False),
            )
            try:
                payload = await self._request(
                    "POST",
                    "/v1/runs",
                    json=body,
                    headers={"Idempotency-Key": message_id},
                    uncertain_on_timeout=True,
                )
            except OpenCodeSubmissionUncertain:
                self._store.update_hermes_binding(
                    message_id, status="unknown"
                )
                raise
            except OpenCodeTransportError as exc:
                if exc.request_not_sent:
                    self._store.update_hermes_binding(message_id, status="failed")
                raise
            except OpenCodeError:
                self._store.update_hermes_binding(message_id, status="failed")
                raise
            if not isinstance(payload, dict) or not payload.get("run_id"):
                self._store.update_hermes_binding(message_id, status="failed")
                raise OpenCodeProtocolError("Hermes returned no run ID")
            self._store.update_hermes_binding(
                binding["message_id"],
                run_id=str(payload["run_id"]),
                status=str(payload.get("status") or "started"),
            )

    async def abort(self, session_id: str) -> bool:
        bindings = self._store.list_hermes_bindings(session_id)
        stopped = False
        for binding in bindings:
            run_id = binding.get("run_id")
            if not run_id or binding.get("status") not in _ACTIVE_RUN_STATUSES:
                continue
            payload = await self._request(
                "POST", f"/v1/runs/{quote(str(run_id), safe='')}/stop"
            )
            status = payload.get("status") if isinstance(payload, dict) else "stopping"
            self._store.update_hermes_binding(
                binding["message_id"], status=str(status or "stopping")
            )
            stopped = True
        return stopped

    async def respond_permission(
        self, session_id: str, permission_id: str, response: str, remember: bool
    ) -> bool:
        del session_id, remember
        control = _decode_control_id(permission_id)
        choice = {
            "reject": "deny",
            "deny": "deny",
            "always": "always",
            "session": "session",
            "once": "once",
        }.get(response, response)
        try:
            await self._request(
                "POST",
                f"/v1/runs/{quote(str(control['run_id']), safe='')}/approval",
                json={
                    "request_id": control.get("request_id"),
                    "execution_generation": control.get("generation"),
                    "choice": choice,
                },
            )
        except OpenCodeError as exc:
            if exc.status_code in {404, 409}:
                return False
            raise
        self._run_cache.pop(str(control["run_id"]), None)
        return True

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        control = _decode_control_id(request_id)
        answer = ""
        for group in answers:
            selected = [str(item) for item in group if str(item).strip()]
            if selected:
                answer = "\n".join(selected)
                break
        if not answer:
            return False
        try:
            await self._request(
                "POST",
                f"/v1/runs/{quote(str(control['run_id']), safe='')}/clarify",
                json={
                    "request_id": control.get("request_id"),
                    "execution_generation": control.get("generation"),
                    "answer": answer,
                },
            )
        except OpenCodeError as exc:
            if exc.status_code in {404, 409}:
                return False
            raise
        self._run_cache.pop(str(control["run_id"]), None)
        return True

    async def _get_run(self, run_id: str, *, force: bool = False) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        if not force:
            cached = self._run_cache.get(run_id)
            if cached and now - cached[0] < 1.0:
                return cached[1]
        payload = await self._request(
            "GET", f"/v1/runs/{quote(run_id, safe='')}"
        )
        if not isinstance(payload, dict):
            raise OpenCodeProtocolError("Hermes returned invalid run status")
        async with self._run_cache_lock:
            self._run_cache[run_id] = (now, payload)
            if len(self._run_cache) > 256:
                oldest = min(self._run_cache, key=lambda key: self._run_cache[key][0])
                self._run_cache.pop(oldest, None)
        return payload

    def _apply_run(self, binding: dict[str, Any], run: dict[str, Any]) -> None:
        fields: dict[str, Any] = {}
        for source, target in (
            ("status", "status"),
            ("admission_id", "admission_id"),
            ("execution_generation", "execution_generation"),
            ("output", "output"),
        ):
            if run.get(source) is not None:
                fields[target] = run[source]
        if fields:
            self._store.update_hermes_binding(binding["message_id"], **fields)

    async def _recover_submission(
        self, binding: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_json = binding.get("request_json")
        if not request_json:
            return None
        try:
            body = json.loads(str(request_json))
            payload = await self._request(
                "POST",
                "/v1/runs",
                json=body,
                headers={"Idempotency-Key": str(binding["message_id"])},
                uncertain_on_timeout=True,
            )
        except (OpenCodeSubmissionUncertain, OpenCodeTransportError):
            return None
        except OpenCodeError as exc:
            if exc.status_code is not None and exc.status_code < 500:
                self._store.update_hermes_binding(binding["message_id"], status="failed")
                return None
            raise
        if not isinstance(payload, dict) or not payload.get("run_id"):
            raise OpenCodeProtocolError("Hermes returned no run ID during recovery")
        updated = self._store.update_hermes_binding(
            binding["message_id"],
            run_id=str(payload["run_id"]),
            status=str(payload.get("status") or "started"),
        )
        return updated

    async def _resolve_unknown_run(
        self, binding: dict[str, Any], run: dict[str, Any]
    ) -> dict[str, Any]:
        if str(run.get("status") or "").lower() != "unknown":
            return run
        admission_id = run.get("admission_id") or binding.get("admission_id")
        generation = run.get("execution_generation")
        if generation is None:
            generation = binding.get("execution_generation")
        if not admission_id or generation is None:
            return run
        try:
            await self._request(
                "POST",
                f"/v1/runs/{quote(str(binding['run_id']), safe='')}/resolve-unknown",
                json={
                    "admission_id": admission_id,
                    "execution_generation": generation,
                },
            )
        except OpenCodeError as exc:
            if exc.status_code not in {404, 409}:
                raise
            return run
        return await self._get_run(str(binding["run_id"]), force=True)

    async def _refresh_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        run_id = binding.get("run_id")
        if not run_id:
            if binding.get("status") in {"sending", "unknown"}:
                recovered = await self._recover_submission(binding)
                if recovered is not None:
                    binding = recovered
                    run_id = binding.get("run_id")
            if not run_id:
                return binding
        run = await self._get_run(str(run_id), force=True)
        run = await self._resolve_unknown_run(binding, run)
        self._apply_run(binding, run)
        return self._store.get_hermes_binding(binding["message_id"]) or binding

    async def session_statuses(self) -> dict[str, Any]:
        bindings = self._store.list_hermes_bindings(active_only=True)
        result: dict[str, str] = {}
        for binding in bindings:
            session_id = str(binding["session_id"])
            run_id = binding.get("run_id")
            if not run_id:
                binding = await self._refresh_binding(binding)
                run_id = binding.get("run_id")
            if not run_id:
                result[session_id] = "busy"
                continue
            try:
                run = await self._refresh_binding(binding)
            except OpenCodeError as exc:
                if exc.status_code == 404:
                    self._store.update_hermes_binding(
                        binding["message_id"], status="interrupted"
                    )
                    result[session_id] = "aborted"
                    continue
                raise
            status = str(run.get("status") or "unknown").lower()
            if status in {
                "queued",
                "started",
                "running",
                "waiting_for_approval",
                "stopping",
                "unknown",
            }:
                result[session_id] = "busy"
            elif status == "completed":
                result[session_id] = "idle"
            elif status in {"cancelled", "interrupted"}:
                result[session_id] = "aborted"
            else:
                result[session_id] = "failed"
        return result

    async def _active_controls(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        result: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for binding in self._store.list_hermes_bindings(active_only=True):
            if not binding.get("run_id"):
                continue
            try:
                refreshed = await self._refresh_binding(binding)
            except OpenCodeError as exc:
                if exc.status_code == 404:
                    continue
                raise
            run = await self._get_run(str(refreshed["run_id"]))
            controls = run.get("pending_controls")
            if isinstance(controls, list):
                result.extend(
                    (refreshed, control)
                    for control in controls
                    if isinstance(control, dict)
                )
        return result

    async def permissions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for binding, control in await self._active_controls():
            if str(control.get("kind") or "") != "approval":
                continue
            request_id = str(control.get("request_id") or control.get("prompt_id") or "")
            if not request_id:
                continue
            run_id = str(binding["run_id"])
            generation = control.get("execution_generation")
            if generation is None:
                generation = binding.get("execution_generation")
            command = control.get("command") or control.get("description") or ""
            patterns = [str(command)] if command else ["Hermes approval"]
            result.append(
                {
                    "id": _control_id(run_id, request_id, generation),
                    "sessionID": str(binding["session_id"]),
                    "permission": "approval",
                    "patterns": patterns,
                    "metadata": {
                        "source": "hermes",
                        "run_id": run_id,
                        "request_id": request_id,
                        "execution_generation": generation,
                        "description": control.get("description"),
                        "command": control.get("command"),
                        "choices": control.get("choices"),
                    },
                    "always": ["once", "always"],
                }
            )
        return result

    async def questions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for binding, control in await self._active_controls():
            if str(control.get("kind") or "") != "clarify":
                continue
            request_id = str(control.get("request_id") or control.get("prompt_id") or "")
            if not request_id:
                continue
            run_id = str(binding["run_id"])
            generation = control.get("execution_generation")
            if generation is None:
                generation = binding.get("execution_generation")
            question = str(
                control.get("question")
                or control.get("prompt")
                or control.get("description")
                or "Hermes needs clarification"
            )
            result.append(
                {
                    "id": _control_id(run_id, request_id, generation),
                    "sessionID": str(binding["session_id"]),
                    "metadata": {"source": "hermes"},
                    "questions": [
                        {
                            "question": question,
                            "header": "Hermes",
                            "multiple": False,
                            "custom": True,
                            "options": [],
                        }
                    ],
                }
            )
        return result

    async def mcp_statuses(self) -> dict[str, Any]:
        return {}

    async def global_events(self) -> AsyncIterator[dict[str, Any]]:
        previous: dict[str, str] = {}
        while True:
            try:
                statuses = await self.session_statuses()
                for session_id, status in statuses.items():
                    if previous.get(session_id) != status:
                        previous[session_id] = status
                        yield {
                            "directory": self.workspace,
                            "payload": {
                                "type": "session.status",
                                "properties": {
                                    "sessionID": session_id,
                                    "status": {"type": status},
                                },
                            },
                        }
                    if status == "busy":
                        # Hermes does not expose a global SSE stream. Refresh the
                        # browser periodically while a run is active.
                        yield {
                            "directory": self.workspace,
                            "payload": {
                                "type": "message.updated",
                                "properties": {
                                    "sessionID": session_id,
                                    "info": {"id": f"status-{session_id}"},
                                },
                            },
                        }
                if not statuses:
                    previous.clear()
            except asyncio.CancelledError:
                raise
            except OpenCodeError:
                raise
            except Exception as exc:
                raise OpenCodeError("Hermes event projection failed") from exc
            await asyncio.sleep(2)

    async def providers(self) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        if self._providers_cache is not None and now - self._providers_cache_time < 60:
            return self._providers_cache
        async with self._providers_lock:
            now = asyncio.get_running_loop().time()
            if self._providers_cache is not None and now - self._providers_cache_time < 60:
                return self._providers_cache
            payload = await self._request("GET", "/api/model/options")
            all_providers: dict[str, Any] = {}
            connected: list[str] = []
            rows = payload.get("providers", []) if isinstance(payload, dict) else payload
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not rows and isinstance(payload, dict) and payload.get("model"):
                rows = [
                    {
                        "slug": str(payload.get("provider") or self.model_name),
                        "name": str(payload.get("provider") or self.model_name),
                        "models": [str(payload["model"])],
                    }
                ]
            if not isinstance(rows, list):
                raise OpenCodeProtocolError("Hermes returned an invalid model catalog")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                provider_id = row.get("slug") or row.get("id") or row.get("provider")
                if not provider_id:
                    continue
                provider_id = str(provider_id)
                models_raw = row.get("models") or []
                if isinstance(models_raw, dict):
                    model_items = list(models_raw.items())
                else:
                    model_items = [
                        (
                            str(model) if isinstance(model, str) else str(
                                model.get("id") or model.get("model") or ""
                            ),
                            {"name": model} if isinstance(model, str) else model,
                        )
                        for model in models_raw
                        if isinstance(model, (str, dict))
                    ]
                models: dict[str, Any] = {}
                for model_id, model in model_items:
                    if not model_id:
                        continue
                    models[str(model_id)] = {
                        "id": str(model_id),
                        "name": str(
                            (model.get("name") if isinstance(model, dict) else None)
                            or model_id
                        ),
                    }
                all_providers[provider_id] = {
                    "id": provider_id,
                    "name": str(row.get("name") or provider_id),
                    "models": models,
                }
                if row.get("authenticated") is not False:
                    connected.append(provider_id)
            result = {"all": all_providers, "connected": connected}
            self._providers_cache = result
            self._providers_cache_time = asyncio.get_running_loop().time()
            return result

    async def agents(self) -> list[dict[str, Any]]:
        return [
            {"name": "build", "permission": []},
            {"name": "plan", "permission": []},
        ]

    async def wait_for_run(
        self,
        run_id: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Optional helper used by diagnostics; the dispatcher polls status instead."""
        delay = 1.0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            run = await _await_or_cancel(self._get_run(run_id, force=True), cancel_event)
            status = str(run.get("status") or "")
            if status in _TERMINAL_RUN_STATUSES:
                return run
            await _wait_for_retry(delay, cancel_event)
            delay = min(delay * 2, 15.0)
