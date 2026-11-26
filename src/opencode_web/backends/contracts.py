"""Stable operations and browser projections shared by OpenCode adapters."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, NotRequired, Protocol, TypedDict, runtime_checkable


class OpenCodeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OpenCodeTransportError(OpenCodeError):
    """The transport failed; read operations can be retried without resubmitting input."""

    def __init__(self, message: str, *, request_not_sent: bool = False):
        super().__init__(message)
        self.request_not_sent = request_not_sent


class OpenCodeReadTimeout(OpenCodeTransportError):
    """The response timed out; a submitted input may already have been accepted."""


class OpenCodeProtocolError(OpenCodeError):
    """A server response does not satisfy the application contract."""


class OpenCodeSubmissionUncertain(OpenCodeError):
    """An input may still be admitted; retain its slot and reconcile its ID."""


class Session(TypedDict):
    id: str
    directory: str
    title: NotRequired[str]
    parentID: NotRequired[str]


class Message(TypedDict):
    info: dict[str, Any]
    parts: list[dict[str, Any]]


class Event(TypedDict):
    directory: str
    payload: dict[str, Any]


@runtime_checkable
class OpenCodeBackend(Protocol):
    """Adapters own routes, version-specific data and durable input reconciliation.

    History uses the application's ``info``/``parts`` projection. Statuses use
    ``type`` (busy/idle/failed/aborted). Event envelopes use ``payload`` with
    explicit session routing fields. No operation retries a prompt blindly.
    """

    async def close(self) -> None: ...
    async def health(self) -> dict[str, Any]: ...
    async def path(self) -> dict[str, Any]: ...
    async def create_session(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str | None = None,
        recover_only: bool = False,
        cancel_event: asyncio.Event | None = None,
        creation_state: Callable[[bool], bool] | None = None,
    ) -> dict[str, Any]: ...
    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None: ...
    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...
    async def sessions(self) -> list[dict[str, Any]]: ...
    async def session(self, session_id: str) -> dict[str, Any]: ...
    async def delete_session(self, session_id: str) -> bool: ...
    async def update_session_permissions(
        self,
        session_id: str,
        permission: list[dict[str, str]],
    ) -> dict[str, Any]: ...
    async def messages(self, session_id: str) -> list[dict[str, Any]]: ...
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
    ) -> None: ...
    async def abort(self, session_id: str) -> bool: ...
    async def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str,
        remember: bool,
    ) -> bool: ...
    async def session_statuses(self) -> dict[str, Any]: ...
    async def permissions(self) -> list[dict[str, Any]]: ...
    async def questions(self) -> list[dict[str, Any]]: ...
    async def respond_question(self, request_id: str, answers: list[list[str]]) -> bool: ...
    async def mcp_statuses(self) -> dict[str, Any]: ...
    def global_events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def providers(self) -> dict[str, Any]: ...
    async def agents(self) -> list[dict[str, Any]]: ...
