"""OpenCode 2.3 HTTP contract, projected onto the application's backend API.

The wire shapes are pinned to @opencode/client 2.3.0. In particular a prompt
acknowledges durable admission, messages are paginated, and events are volatile.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .contracts import OpenCodeError, OpenCodeProtocolError, OpenCodeSubmissionUncertain
from .http import HttpBackend


def _object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpenCodeProtocolError(f"OpenCode returned invalid {description}")
    return value


def _data(value: Any, description: str) -> Any:
    value = _object(value, description)
    if "data" not in value:
        raise OpenCodeProtocolError(f"OpenCode returned invalid {description}")
    return value["data"]


def _list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OpenCodeProtocolError(f"OpenCode returned invalid {description}")
    return value


def _rules(permission: list[dict[str, str]]) -> list[dict[str, str]]:
    actions = {"bash": "shell", "task": "subagent", "write": "edit", "patch": "edit"}
    result = []
    for rule in permission:
        action = rule.get("permission", rule.get("action", ""))
        effect = (
            rule.get("effect", rule.get("action", ""))
            if "permission" in rule
            else (rule.get("effect", ""))
        )
        if not action or effect not in {"allow", "deny", "ask"}:
            raise OpenCodeProtocolError("Invalid application permission rule")
        result.append(
            {
                "action": actions.get(action, action),
                "resource": rule.get("resource", rule.get("pattern", "*")),
                "effect": effect,
            }
        )
    return result


class OpenCodeV2Backend(HttpBackend):
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        workspace: str | None = None,
    ):
        super().__init__(base_url, "opencode", password, workspace=workspace)
        self._forms: dict[str, dict[str, Any]] = {}

    def _location(self) -> dict[str, str]:
        return {"location[directory]": self.workspace} if self.workspace else {}

    @staticmethod
    def _session_path(session_id: str) -> str:
        return f"/api/session/{quote(session_id, safe='')}"

    def _session(self, value: Any) -> dict[str, Any]:
        session = _object(value, "session details")
        location = _object(session.get("location"), "session location")
        directory = location.get("directory")
        if not isinstance(session.get("id"), str) or not isinstance(directory, str):
            raise OpenCodeProtocolError("OpenCode returned invalid session details")
        if self.workspace and Path(directory).resolve() != Path(self.workspace).resolve():
            raise OpenCodeProtocolError("OpenCode returned a session from another workspace")
        return {**session, "directory": directory, "permission": session.get("permissions", [])}

    async def health(self) -> dict[str, Any]:
        info = _object(await self._request("GET", "/api/info"), "server info")
        version = info.get("version", "")
        if not isinstance(version, str) or not version.removeprefix("v").startswith("2."):
            raise OpenCodeProtocolError("Configured OpenCode v2 backend requires a 2.x server")
        return {**info, "healthy": True}

    async def path(self) -> dict[str, Any]:
        return _object(
            await self._request("GET", "/api/location", params=self._location()), "location"
        )

    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None:
        matches = [
            session
            for session in await self.sessions()
            if isinstance(session.get("metadata"), dict)
            and session["metadata"].get("shodanCreationKey") == creation_key
        ]
        if len(matches) > 1:
            raise OpenCodeProtocolError("OpenCode returned multiple sessions for creation key")
        return matches[0] if matches else None

    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"metadata": {"shodanCreationKey": creation_key}}
        if title:
            body["title"] = title
        if self.workspace:
            body["location"] = {"directory": self.workspace}
        if permission is not None:
            body["permissions"] = _rules(permission)
        return self._session(
            _data(
                await self._request(
                    "POST",
                    "/api/session",
                    json=body,
                    **({"before_send": before_send} if before_send is not None else {}),
                ),
                "session",
            )
        )

    async def _pages(
        self,
        path: str,
        params: dict[str, Any],
        description: str,
    ) -> list[dict[str, Any]]:
        result = []
        cursors: set[str] = set()
        while True:
            page = _object(await self._request("GET", path, params=params), description)
            result.extend(_list(_data(page, description), description))
            cursor = _object(page.get("cursor"), "pagination").get("next")
            if cursor is None:
                return result
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise OpenCodeProtocolError("OpenCode returned an invalid pagination cursor")
            cursors.add(cursor)
            # Ordering and filters are encoded in the opaque cursor.
            params = {"cursor": cursor, "limit": params.get("limit", 100)}

    async def sessions(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"order": "asc", "limit": 100}
        if self.workspace:
            params["directory"] = self.workspace
        return [
            self._session(item) for item in await self._pages("/api/session", params, "sessions")
        ]

    async def session(self, session_id: str) -> dict[str, Any]:
        return self._session(
            _data(await self._request("GET", self._session_path(session_id)), "session")
        )

    async def delete_session(self, session_id: str) -> bool:
        await self._request("DELETE", self._session_path(session_id))
        return True

    async def update_session_permissions(
        self,
        session_id: str,
        permission: list[dict[str, str]],
    ) -> dict[str, Any]:
        await self._request(
            "PATCH",
            self._session_path(session_id),
            json={"permissions": _rules(permission)},
        )
        return await self.session(session_id)

    @staticmethod
    def _message(raw: dict[str, Any], session_id: str, parent: str | None) -> dict[str, Any]:
        kind = raw.get("type")
        info = {**raw, "sessionID": session_id, "role": kind}
        parts: list[dict[str, Any]] = []
        if kind in {"user", "system", "synthetic"}:
            info["role"] = "user" if kind == "synthetic" else kind
            parts = [{"type": "text", "text": raw.get("text", "")}]
            if kind == "synthetic":
                parts[0]["synthetic"] = True
        elif kind == "assistant":
            info["parentID"] = parent
            for part in _list(raw.get("content"), "message content"):
                normalized = dict(part)
                if part.get("type") == "tool":
                    normalized["tool"] = part.get("name")
                    normalized["callID"] = part.get("id")
                parts.append(normalized)
        elif kind == "compaction":
            info.update(role="assistant", parentID=parent, summary=True)
            parts = [{"type": "text", "text": raw.get("summary", ""), "synthetic": True}]
        elif kind == "idle" and raw.get("outcome") in {"failed", "interrupted"}:
            info.update(role="assistant", parentID=parent, finish="error")
            info["error"] = raw.get("error") or {"type": raw["outcome"]}
        return {"info": info, "parts": parts}

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        path = self._session_path(session_id)
        raw = await self._pages(f"{path}/message", {"order": "asc", "limit": 100}, "messages")
        result = []
        parent = None
        for item in raw:
            if item.get("type") in {"user", "synthetic"}:
                parent = item.get("id")
            result.append(self._message(item, session_id, parent))
        # An admitted input may still be queued and absent from message history.
        inbox = _list(_data(await self._request("GET", f"{path}/inbox"), "inbox"), "inbox")
        ids = {item["info"].get("id") for item in result}
        for item in inbox:
            if item.get("type") == "user" and item.get("id") not in ids:
                payload = _object(item.get("payload"), "inbox input")
                result.append(
                    self._message({**item, "text": payload.get("text", "")}, session_id, None)
                )
        return result

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
        path = self._session_path(session_id)
        await self._request("POST", f"{path}/agent", json={"agent": agent})
        if provider_id and model_id:
            model = {"providerID": provider_id, "id": model_id}
            if model_variant:
                model["variant"] = model_variant
            await self._request("POST", f"{path}/model", json={"model": model})
        instructions = (
            f"/api/experimental/session/{quote(session_id, safe='')}/instructions/entries"
        )
        if system is not None:
            await self._request(
                "PUT",
                f"{instructions}/valeria.system",
                json={"value": system},
            )
        else:
            try:
                await self._request("DELETE", f"{instructions}/valeria.system")
            except OpenCodeError as exc:
                if exc.status_code != 404:
                    raise
        try:
            admitted = _object(
                _data(
                    await self._request(
                        "POST",
                        f"{path}/prompt",
                        json={
                            "id": message_id,
                            "text": content,
                            "delivery": "queue",
                            "resume": True,
                        },
                    ),
                    "prompt receipt",
                ),
                "prompt receipt",
            )
            if admitted.get("id") != message_id or admitted.get("sessionID") != session_id:
                raise OpenCodeProtocolError("OpenCode returned a mismatched prompt receipt")
        except OpenCodeError as exc:
            # Includes HTTP 409 for an already admitted ID. Never resend here.
            if exc.status_code in {400, 401, 403, 404, 405, 413, 415, 422}:
                raise
            try:
                if any(
                    message["info"].get("id") == message_id
                    for message in await self.messages(session_id)
                ):
                    return
            except OpenCodeError:
                pass
            raise OpenCodeSubmissionUncertain(
                "OpenCode prompt admission is not yet confirmed"
            ) from exc

    async def abort(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        # Interrupting an execution does not discard its queued future inputs.
        inbox = _list(_data(await self._request("GET", f"{path}/inbox"), "inbox"), "inbox")
        for item in inbox:
            try:
                await self._request("DELETE", f"{path}/inbox/{quote(item['id'], safe='')}")
            except OpenCodeError as exc:
                if exc.status_code != 404:
                    raise
        await self._request("POST", f"{path}/interrupt", json={"resume": False})
        return True

    async def _active_sessions(self) -> dict[str, Any]:
        active = _object(
            _data(await self._request("GET", "/api/session/active"), "active sessions"),
            "active sessions",
        )
        if any(
            not isinstance(value, dict) or value.get("type") != "running"
            for value in active.values()
        ):
            raise OpenCodeProtocolError("OpenCode returned an unknown execution status")
        return active

    async def session_statuses(self) -> dict[str, Any]:
        active = await self._active_sessions()
        result = {key: {"type": "busy"} for key in active}
        for session in await self.sessions():
            if session["id"] not in result:
                inbox = _list(
                    _data(
                        await self._request(
                            "GET",
                            f"{self._session_path(session['id'])}/inbox",
                        ),
                        "inbox",
                    ),
                    "inbox",
                )
                if inbox:
                    result[session["id"]] = {"type": "busy"}
                    continue
                if session.get("outcome") not in {None, "succeeded", "failed", "interrupted"}:
                    raise OpenCodeProtocolError("OpenCode returned an unknown execution outcome")
                kind = {"failed": "failed", "interrupted": "aborted"}.get(
                    session.get("outcome"), "idle"
                )
                result[session["id"]] = {"type": kind}
        # A queued input can start between the first active snapshot and the
        # inbox read. Its empty inbox must not expose the previous turn's outcome.
        for session_id in await self._active_sessions():
            result[session_id] = {"type": "busy"}
        return result

    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        remember: bool,
    ) -> bool:
        decision = "always" if remember and response != "reject" else response
        if decision not in {"once", "always", "reject"}:
            raise OpenCodeProtocolError("Invalid permission response")
        await self._request(
            "POST",
            f"{self._session_path(session_id)}/permission/{quote(permission_id, safe='')}/reply",
            json={"decision": decision},
        )
        return True

    async def permissions(self) -> list[dict[str, Any]]:
        return _list(
            _data(
                await self._request(
                    "GET",
                    "/api/permission/request",
                    params=self._location(),
                ),
                "permissions",
            ),
            "permissions",
        )

    @staticmethod
    def _question(field: dict[str, Any], title: str) -> dict[str, Any]:
        kind = field.get("type")
        if kind not in {"string", "multiselect", "boolean", "number", "integer"} or field.get(
            "when"
        ):
            raise OpenCodeProtocolError("This OpenCode form cannot be represented as a question")
        options = field.get("options", [])
        if kind == "boolean":
            options = [{"label": "true", "value": True}, {"label": "false", "value": False}]
        return {
            "question": field.get("title") or field.get("description") or field["key"],
            "header": title,
            "multiple": kind == "multiselect",
            "custom": kind in {"number", "integer"}
            or (kind != "boolean" and field.get("custom", not options)),
            "options": [
                {
                    "label": option["label"],
                    **({"description": option["description"]} if "description" in option else {}),
                }
                for option in options
            ],
        }

    async def questions(self) -> list[dict[str, Any]]:
        forms = _list(
            _data(await self._request("GET", "/api/form", params=self._location()), "forms"),
            "forms",
        )
        result = []
        self._forms = {form["id"]: form for form in forms}
        for form in forms:
            fields = [
                field
                for field in _list(form.get("fields"), "form fields")
                if not field.get("hidden")
            ]
            result.append(
                {
                    "id": form["id"],
                    "sessionID": form["sessionID"],
                    "questions": [self._question(field, form.get("title", "")) for field in fields],
                }
            )
        return result

    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool:
        if request_id not in self._forms:
            await self.questions()
        form = self._forms.get(request_id)
        if form is None:
            raise OpenCodeError("OpenCode question is no longer pending")
        fields = [field for field in form["fields"] if not field.get("hidden")]
        if len(fields) != len(answers):
            raise OpenCodeProtocolError("Answer every form field")
        answer: dict[str, Any] = {}
        for field in form["fields"]:
            if field.get("hidden") and "default" in field:
                answer[field["key"]] = field["default"]
        for field, selected in zip(fields, answers, strict=True):
            if not selected:
                raise OpenCodeProtocolError("An empty answer is not supported")
            options = {option["label"]: option["value"] for option in field.get("options", [])}
            values = [options.get(label, label) for label in selected]
            kind = field["type"]
            value: Any = values if kind == "multiselect" else values[0]
            try:
                if kind == "boolean":
                    if value not in {"true", "false"}:
                        raise ValueError
                    value = value == "true"
                elif kind in {"number", "integer"}:
                    number = float(value)
                    if not math.isfinite(number) or (kind == "integer" and not number.is_integer()):
                        raise ValueError
                    value = int(number) if kind == "integer" else number
            except (ValueError, TypeError) as exc:
                raise OpenCodeProtocolError("Invalid typed form answer") from exc
            answer[field["key"]] = value
        await self._request(
            "POST",
            f"{self._session_path(form['sessionID'])}/form/{quote(request_id, safe='')}/reply",
            json={"answer": answer},
        )
        return True

    async def mcp_statuses(self) -> dict[str, Any]:
        servers = _list(
            _data(
                await self._request("GET", "/api/mcp", params=self._location(), timeout=30),
                "MCP servers",
            ),
            "MCP servers",
        )
        return {
            server["name"]: {
                **server,
                "status": _object(server.get("status"), "MCP status").get("type", "unknown"),
            }
            for server in servers
        }

    async def providers(self) -> dict[str, Any]:
        async with self._providers_lock:
            now = asyncio.get_running_loop().time()
            if self._providers_cache is not None and now - self._providers_cache_time < 60:
                return self._providers_cache
            providers = _list(
                _data(
                    await self._request("GET", "/api/provider", params=self._location()),
                    "providers",
                ),
                "providers",
            )
            models = _list(
                _data(await self._request("GET", "/api/model", params=self._location()), "models"),
                "models",
            )
            catalog = {provider["id"]: {**provider, "models": {}} for provider in providers}
            for model in models:
                if model.get("enabled") is False:
                    continue
                provider = catalog.setdefault(
                    model["providerID"], {"id": model["providerID"], "models": {}}
                )
                provider["models"][model["id"]] = model
            self._providers_cache = {"all": list(catalog.values())}
            self._providers_cache_time = now
            return self._providers_cache

    async def agents(self) -> list[dict[str, Any]]:
        agents = _list(
            _data(await self._request("GET", "/api/agent", params=self._location()), "agents"),
            "agents",
        )
        return [
            {**agent, "name": agent["id"], "permission": agent.get("permissions", [])}
            for agent in agents
        ]

    async def global_events(self) -> AsyncIterator[dict[str, Any]]:
        async for event in self._events("/api/event", params=self._location()):
            kind = event.get("type", "")
            data = _object(event.get("data", {}), "event data")
            location = event.get("location") or {}
            directory = location.get("directory")
            if (
                self.workspace
                and directory
                and Path(directory).resolve() != Path(self.workspace).resolve()
            ):
                continue
            # Forms carry routing inside data.form. Never recursively route on
            # unrelated nested session IDs (for example a subagent tool result).
            owner = data.get("form", {}) if kind.startswith("form.") else data
            properties = {**data}
            if isinstance(owner.get("sessionID"), str):
                properties["sessionID"] = owner["sessionID"]
            yield {
                "directory": directory or self.workspace,
                "payload": {
                    "type": kind,
                    "properties": properties,
                },
            }
