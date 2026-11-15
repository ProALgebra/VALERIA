"""Reconcile session creation without replaying a possibly accepted POST."""

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol
from uuid import uuid4

from .contracts import OpenCodeTransportError
from .retry import (
    OPENCODE_RETRY_INITIAL_SECONDS,
    OPENCODE_RETRY_MAX_SECONDS,
    _await_or_cancel,
    _wait_for_retry,
)

logger = logging.getLogger(__name__)


class SessionCreator(Protocol):
    async def find_created_session(self, creation_key: str) -> dict[str, Any] | None: ...
    async def create_session_once(
        self,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str,
        before_send: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...


class SessionCreation:
    async def create_session(
        self: SessionCreator,
        title: str | None = None,
        *,
        permission: list[dict[str, str]] | None = None,
        creation_key: str | None = None,
        recover_only: bool = False,
        cancel_event: asyncio.Event | None = None,
        creation_state: Callable[[bool], bool] | None = None,
    ) -> dict[str, Any]:
        reconcile = creation_key is not None or recover_only
        creation_key = creation_key or uuid4().hex
        may_create = not recover_only
        delay = OPENCODE_RETRY_INITIAL_SECONDS
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                if reconcile:
                    found = await _await_or_cancel(
                        self.find_created_session(creation_key),
                        cancel_event,
                    )
                    if found is not None:
                        return found
                if may_create:
                    try:
                        return await self.create_session_once(
                            title,
                            permission=permission,
                            creation_key=creation_key,
                            before_send=partial(creation_state, True) if creation_state else None,
                        )
                    except OpenCodeTransportError as exc:
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
