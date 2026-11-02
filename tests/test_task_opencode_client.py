from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opencode_web.opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeInstance,
    OpenCodeManager,
    OpenCodeReadTimeout,
    OpenCodeTransportError,
    WorkspaceClient,
)


@asynccontextmanager
async def mock_client(
    transport: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> AsyncIterator[OpenCodeClient]:
    client = OpenCodeClient("http://opencode.test", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(transport),
    )
    try:
        yield client
    finally:
        await client.close()


class LeaseTrackingManager:
    def __init__(self, client: OpenCodeClient):
        self.client = client
        self.leases = 0
        self.clients: list[OpenCodeClient] = []

    @asynccontextmanager
    async def use(self, workspace: str) -> AsyncIterator[OpenCodeClient]:
        client = self.client
        self.clients.append(client)
        self.leases += 1
        try:
            yield client
        finally:
            self.leases -= 1


@pytest.fixture
def recovery_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def record_delay(delay: float, cancel_event: asyncio.Event | None = None) -> None:
        delays.append(delay)

    monkeypatch.setattr("opencode_web.opencode_client._wait_for_retry", record_delay)
    return delays


async def test_client_allows_two_hours_for_response_body() -> None:
    client = OpenCodeClient("http://opencode.test", "", "")
    try:
        assert client._client.timeout.connect == 5
        assert client._client.timeout.read == 2 * 60 * 60
        assert client._client.timeout.write == 30
        assert client._client.timeout.pool == 30
    finally:
        await client.close()


async def test_read_timeout_has_a_distinct_error_type() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow OpenCode response", request=request)

    client = OpenCodeClient("http://opencode.test", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(transport),
    )
    try:
        with pytest.raises(OpenCodeReadTimeout, match="GET /session/status"):
            await client.session_statuses()
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    ("exception_type", "request_not_sent"),
    [
        (httpx.ReadTimeout, False),
        (httpx.ReadError, False),
        (httpx.RemoteProtocolError, False),
        (httpx.ConnectError, True),
        (httpx.ConnectTimeout, True),
        (httpx.WriteError, False),
        (httpx.WriteTimeout, False),
        (httpx.PoolTimeout, True),
    ],
)
async def test_transport_failures_keep_retry_and_delivery_information(
    method: str,
    exception_type: type[httpx.TransportError],
    request_not_sent: bool,
) -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exception_type("temporary disconnect", request=request)

    async with mock_client(transport) as client:
        with pytest.raises(OpenCodeTransportError, match=f"{method} /session/status") as error:
            await client._request(method, "/session/status")

    assert isinstance(error.value.__cause__, exception_type)
    assert error.value.request_not_sent is request_not_sent
    assert isinstance(error.value, OpenCodeReadTimeout) is (exception_type is httpx.ReadTimeout)
    assert len(requests) == 1


async def test_lost_creation_response_recovers_session_without_replaying_post(
    recovery_delays: list[float],
) -> None:
    requests: list[httpx.Request] = []
    created: dict[str, Any] | None = None

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal created
        requests.append(request)
        if request.method == "POST":
            created = {"id": "created-session", **json.loads(request.content)}
            raise httpx.RemoteProtocolError("response lost", request=request)
        assert request.url.params["limit"] == "-1"
        return httpx.Response(200, json=[created] if created else [])

    async with mock_client(transport) as client:
        session = await client.create_session("same title", creation_key="creation-1")

    assert session == created
    assert session["metadata"]["shodanCreationKey"] == "creation-1"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/session"),
        ("POST", "/session"),
        ("GET", "/session"),
    ]


async def test_ambiguous_creation_waits_for_delayed_visibility_and_caps_backoff(
    recovery_delays: list[float],
) -> None:
    posts = 0
    empty_scans = 0
    created: dict[str, Any] | None = None

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts, empty_scans, created
        if request.method == "POST":
            posts += 1
            created = {"id": "eventually-visible", **json.loads(request.content)}
            raise httpx.ReadError("response lost", request=request)
        empty_scans += 1
        return httpx.Response(200, json=[created] if empty_scans > 20 and created else [])

    async with mock_client(transport) as client:
        session = await client.create_session(creation_key="delayed-creation")

    assert session["id"] == "eventually-visible"
    assert posts == 1
    assert len(recovery_delays) >= 19
    assert recovery_delays[0] == 1
    assert max(recovery_delays) == 3 * 60 * 60
    assert recovery_delays[-3:] == [3 * 60 * 60] * 3


@pytest.mark.parametrize(
    "exception_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout],
)
async def test_creation_retries_post_only_when_transport_proves_request_was_not_sent(
    recovery_delays: list[float],
    exception_type: type[httpx.TransportError],
) -> None:
    posts: list[dict[str, Any]] = []
    scans = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal scans
        if request.method == "GET":
            scans += 1
            return httpx.Response(200, json=[])
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            raise exception_type("connection failed before send", request=request)
        return httpx.Response(200, json={"id": "safe-retry", **posts[-1]})

    permission = [{"permission": "edit", "pattern": "*", "action": "allow"}]
    async with mock_client(transport) as client:
        session = await client.create_session(
            "title preserved", permission=permission, creation_key="safe-creation",
        )

    assert session["id"] == "safe-retry"
    assert scans >= 2
    assert posts == [
        {
            "title": "title preserved",
            "permission": permission,
            "metadata": {"shodanCreationKey": "safe-creation"},
        },
    ] * 2


async def test_creation_state_is_marked_only_after_preflight_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False
    scans = 0
    events: list[tuple[str, bool]] = []

    def creation_state(value: bool) -> bool:
        nonlocal started
        started = value
        events.append(("state", started))
        return True

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal scans
        events.append((request.method, started))
        if request.method == "GET":
            scans += 1
            assert not started
            if scans == 1:
                raise httpx.ReadError("preflight disconnected", request=request)
            return httpx.Response(200, json=[])
        assert started
        return httpx.Response(200, json={"id": "created-after-preflight"})

    async def observe_backoff(delay: float, cancel_event: asyncio.Event | None = None) -> None:
        events.append(("backoff", started))
        assert not started

    monkeypatch.setattr("opencode_web.opencode_client._wait_for_retry", observe_backoff)
    async with mock_client(transport) as client:
        session = await client.create_session(
            creation_key="preflight-state", creation_state=creation_state,
        )

    assert session["id"] == "created-after-preflight"
    assert events == [
        ("GET", False),
        ("backoff", False),
        ("GET", False),
        ("state", True),
        ("POST", True),
    ]


@pytest.mark.parametrize(
    "exception_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout],
)
async def test_proven_unsent_creation_resets_state_before_backoff_and_can_restart(
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[httpx.TransportError],
) -> None:
    started = False
    states: list[bool] = []
    posts = 0

    def creation_state(value: bool) -> bool:
        nonlocal started
        started = value
        states.append(value)
        return True

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            assert not started
            return httpx.Response(200, json=[])
        assert started
        posts += 1
        if posts == 1:
            raise exception_type("request was not sent", request=request)
        return httpx.Response(200, json={"id": "created-after-restart"})

    async def stop_at_backoff(delay: float, cancel_event: asyncio.Event | None = None) -> None:
        assert not started
        assert states == [True, False]
        raise asyncio.CancelledError

    monkeypatch.setattr("opencode_web.opencode_client._wait_for_retry", stop_at_backoff)
    async with mock_client(transport) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.create_session(
                creation_key="safe-restart", creation_state=creation_state,
            )
    assert not started

    async with mock_client(transport) as restarted_client:
        session = await restarted_client.create_session(
            creation_key="safe-restart", recover_only=started, creation_state=creation_state,
        )

    assert session["id"] == "created-after-restart"
    assert posts == 2
    assert states == [True, False, True]


@pytest.mark.parametrize(
    "exception_type",
    [httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, httpx.WriteError],
)
async def test_ambiguous_creation_keeps_started_state_through_recovery(
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[httpx.TransportError],
) -> None:
    started = False
    states: list[bool] = []
    created: dict[str, Any] | None = None
    posts = 0

    def creation_state(value: bool) -> bool:
        nonlocal started
        started = value
        states.append(value)
        return True

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal created, posts
        if request.method == "GET":
            assert started is (created is not None)
            return httpx.Response(200, json=[created] if created else [])
        assert started
        posts += 1
        created = {"id": "ambiguous-created", **json.loads(request.content)}
        raise exception_type("creation response unknown", request=request)

    async def observe_backoff(delay: float, cancel_event: asyncio.Event | None = None) -> None:
        assert started
        assert states == [True]

    monkeypatch.setattr("opencode_web.opencode_client._wait_for_retry", observe_backoff)
    async with mock_client(transport) as client:
        session = await client.create_session(
            creation_key="ambiguous-state", creation_state=creation_state,
        )

    assert session["id"] == "ambiguous-created"
    assert posts == 1
    assert states == [True]
    assert started


async def test_refused_creation_state_mark_prevents_sending_post() -> None:
    methods: list[str] = []
    states: list[bool] = []

    def creation_state(value: bool) -> bool:
        states.append(value)
        return False

    def transport(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    async with mock_client(transport) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.create_session(
                creation_key="rejected-state-mark", creation_state=creation_state,
            )

    assert states == [True]
    assert methods == ["GET"]


@pytest.mark.parametrize("recover_only", [False, True])
async def test_existing_creation_key_is_recovered_without_post(recover_only: bool) -> None:
    requests: list[httpx.Request] = []
    existing = {
        "id": "existing-session",
        "metadata": {"shodanCreationKey": "saved-creation-key"},
    }

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=[existing])

    def creation_state(started: bool) -> bool:
        pytest.fail("Recovering an existing session must not change creation state")

    async with mock_client(transport) as client:
        session = await client.create_session(
            creation_key="saved-creation-key", recover_only=recover_only,
            creation_state=creation_state,
        )

    assert session == existing
    assert len(requests) == 1


async def test_recovery_only_keeps_polling_without_creating_a_session(
    recovery_delays: list[float],
) -> None:
    scans = 0
    existing = {"id": "restored", "metadata": {"shodanCreationKey": "restore-key"}}

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal scans
        assert request.method == "GET"
        scans += 1
        return httpx.Response(200, json=[existing] if scans == 4 else [])

    def creation_state(started: bool) -> bool:
        pytest.fail("Recovery-only polling must not change creation state")

    async with mock_client(transport) as client:
        session = await client.create_session(
            creation_key="restore-key", recover_only=True, creation_state=creation_state,
        )

    assert session == existing
    assert scans == 4
    assert recovery_delays == [1, 2, 4]


async def test_creation_recovery_retries_list_transport_errors_without_replaying_post(
    recovery_delays: list[float],
) -> None:
    posts = 0
    scans = 0
    created: dict[str, Any] | None = None
    failures = [httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout]

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts, scans, created
        if request.method == "POST":
            posts += 1
            created = {"id": "recovered-after-outage", **json.loads(request.content)}
            raise httpx.ReadError("response lost", request=request)
        scans += 1
        if 2 <= scans <= 4:
            raise failures[scans - 2]("recovery request disconnected", request=request)
        return httpx.Response(200, json=[created] if created else [])

    async with mock_client(transport) as client:
        session = await client.create_session(creation_key="recovery-outage")

    assert session["id"] == "recovered-after-outage"
    assert posts == 1
    assert scans == 5
    assert recovery_delays == [1, 2, 4, 8]


async def test_concurrent_creations_with_the_same_title_recover_their_own_marker(
    recovery_delays: list[float],
) -> None:
    created: list[dict[str, Any]] = []
    both_created = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=created)
        created.append({"id": f"session-{len(created)}", **json.loads(request.content)})
        if len(created) == 2:
            both_created.set()
        await both_created.wait()
        raise httpx.ReadError("lost response", request=request)

    async with mock_client(transport) as client:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                client.create_session("identical title", creation_key="first-key"),
                client.create_session("identical title", creation_key="second-key"),
            ),
            timeout=1,
        )

    assert first["metadata"]["shodanCreationKey"] == "first-key"
    assert second["metadata"]["shodanCreationKey"] == "second-key"
    assert first["id"] != second["id"]
    assert len(created) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 409, 500])
async def test_creation_does_not_retry_http_status_errors(status: int) -> None:
    methods: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(status, json={"error": "request rejected"})

    async with mock_client(transport) as client:
        with pytest.raises(OpenCodeError, match=f"HTTP {status}"):
            await client.create_session(creation_key="rejected")

    assert methods == ["GET", "POST"]


async def test_duplicate_creation_markers_fail_without_picking_or_creating_a_session() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=[
                {"id": session_id, "metadata": {"shodanCreationKey": "duplicate-key"}}
                for session_id in ["first", "second"]
            ],
        )

    async with mock_client(transport) as client:
        with pytest.raises(OpenCodeError, match="(?i)multiple|duplicate"):
            await client.create_session(creation_key="duplicate-key")

    assert len(requests) == 1


async def test_cancelling_creation_interrupts_a_recovery_backoff() -> None:
    scanned = asyncio.Event()
    requests = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.method == "GET"
        scanned.set()
        return httpx.Response(200, json=[])

    cancel_event = asyncio.Event()
    async with mock_client(transport) as client:
        pending = asyncio.create_task(client.create_session(
            creation_key="cancelled-creation", recover_only=True, cancel_event=cancel_event,
        ))
        await asyncio.wait_for(scanned.wait(), timeout=1)
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.5)

    assert requests == 1


async def test_creation_recovery_releases_leases_and_uses_replacement_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] | None = None
    old_requests: list[str] = []
    replacement_requests: list[str] = []

    def old_transport(request: httpx.Request) -> httpx.Response:
        nonlocal created
        old_requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        created = {"id": "created-before-restart", **json.loads(request.content)}
        raise httpx.RemoteProtocolError("process disconnected after creation", request=request)

    def replacement_transport(request: httpx.Request) -> httpx.Response:
        replacement_requests.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json=[created])

    async with (
        mock_client(old_transport) as old_client,
        mock_client(replacement_transport) as replacement_client,
    ):
        manager = LeaseTrackingManager(old_client)
        workspace_client = WorkspaceClient(manager, "workspace")

        async def replace_during_backoff(
            delay: float, cancel_event: asyncio.Event | None = None,
        ) -> None:
            assert manager.leases == 0
            await old_client.close()
            manager.client = replacement_client

        monkeypatch.setattr(
            "opencode_web.opencode_client._wait_for_retry", replace_during_backoff,
        )
        session = await workspace_client.create_session(creation_key="replacement-key")

        assert session["id"] == "created-before-restart"
        assert session["metadata"]["shodanCreationKey"] == "replacement-key"
        assert manager.leases == 0
        assert manager.clients == [old_client, old_client, replacement_client]

    assert old_requests == ["GET", "POST"]
    assert replacement_requests == ["GET"]


@pytest.mark.parametrize("acquisition_fails", [False, True])
async def test_creation_state_stays_clear_until_post_client_acquisition_completes(
    acquisition_fails: bool,
) -> None:
    acquisitions = 0
    acquiring_post_client = asyncio.Event()
    never_acquire = asyncio.Event()
    states: list[bool] = []
    methods: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    def creation_state(started: bool) -> bool:
        states.append(started)
        return True

    async with mock_client(transport) as client:
        @asynccontextmanager
        async def use(workspace: str) -> AsyncIterator[OpenCodeClient]:
            nonlocal acquisitions
            acquisitions += 1
            if acquisitions == 2:
                acquiring_post_client.set()
                if acquisition_fails:
                    raise OpenCodeError("replacement startup failed before POST")
                await never_acquire.wait()
            yield client

        workspace_client = WorkspaceClient(SimpleNamespace(use=use), "workspace")
        pending = asyncio.create_task(workspace_client.create_session(
            creation_key="post-acquisition", creation_state=creation_state,
        ))
        await asyncio.wait_for(acquiring_post_client.wait(), timeout=1)
        assert states == []
        if acquisition_fails:
            with pytest.raises(OpenCodeError, match="replacement startup failed"):
                await pending
        else:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert methods == ["GET"]
    assert states == []


async def test_cancelling_inflight_recovery_get_cancels_request_and_releases_lease() -> None:
    requested = asyncio.Event()
    request_cancelled = asyncio.Event()
    never_complete = asyncio.Event()
    methods: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.method == "GET"
        requested.set()
        try:
            await never_complete.wait()
            return httpx.Response(200, json=[])
        finally:
            request_cancelled.set()

    cancel_event = asyncio.Event()
    async with mock_client(transport) as client:
        manager = LeaseTrackingManager(client)
        workspace_client = WorkspaceClient(manager, "workspace")
        pending = asyncio.create_task(workspace_client.create_session(
            creation_key="cancel-inflight-get", recover_only=True, cancel_event=cancel_event,
        ))
        await asyncio.wait_for(requested.wait(), timeout=1)
        assert manager.leases == 1
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.5)

        assert request_cancelled.is_set()
        assert manager.leases == 0

    assert methods == ["GET"]


async def test_startup_probe_cannot_outlive_manager_deadline(settings, workspace) -> None:
    manager = OpenCodeManager(replace(settings, opencode_start_timeout=0.05))

    async def stalled_health() -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    client = SimpleNamespace(health=stalled_health)
    process = SimpleNamespace(returncode=None)
    instance = OpenCodeInstance(workspace, 4100, process, client)

    with pytest.raises(OpenCodeError, match="Timed out while starting OpenCode"):
        async with asyncio.timeout(0.5):
            await manager._start_instance(instance)


async def test_question_api_uses_original_labels_and_array_per_question() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "q-1", "sessionID": "s-1"}])
        return httpx.Response(200, json=True)

    client = OpenCodeClient("http://opencode.test", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(transport),
    )
    try:
        assert await client.questions() == [{"id": "q-1", "sessionID": "s-1"}]
        assert await client.respond_question("q-1", [["A (Recommended)"], ["B", "C"]])
    finally:
        await client.close()

    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", "/question"),
        ("POST", "/question/q-1/reply"),
    ]
    assert requests[1].content == b'{"answers":[["A (Recommended)"],["B","C"]]}'


async def test_invalid_question_payload_is_not_treated_as_an_empty_queue() -> None:
    client = OpenCodeClient("http://opencode.test", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://opencode.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(OpenCodeError, match="invalid question requests"):
            await client.questions()
    finally:
        await client.close()
