from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import FakeManager, FakeOpenCodeClient, login
from test_task_dispatcher import TaskClient

from opencode_web.app import create_app, event_for_user
from opencode_web.consultations import CONSULTATION_PERMISSIONS, CONSULTATION_SYSTEM, final_answer
from opencode_web.database import Database
from opencode_web.dispatcher import QueueDispatcher
from opencode_web.events import EventBroker
from opencode_web.opencode_client import OpenCodeClient, OpenCodeError


class ConsultationClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.pending_questions = []
        self.question_replies = []

    async def create_session(self, title=None, *, permission=None):
        self.permission = permission
        session = await super().create_session(title)
        return {**session, "permission": permission}

    async def delete_session(self, session_id):
        self.session_records.pop(session_id, None)
        return True

    async def questions(self):
        return deepcopy(self.pending_questions)

    async def respond_question(self, question_id, answers):
        self.question_replies.append((question_id, answers))
        self.pending_questions = [
            item for item in self.pending_questions if item["id"] != question_id
        ]
        return True


@pytest.fixture
def consultation(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        manager = FakeManager()
        manager.client = ConsultationClient()
        app.state.manager = manager
        db = app.state.database
        users = {}
        for name in ("worker", "stranger"):
            folder = workspace / name
            folder.mkdir()
            users[name] = db.create_user(name, "long-enough-password", "user", str(folder))
        admin_headers = {"x-csrf-token": login(client, "admin", "correct-horse-battery-staple")}
        assert (
            client.put(
                "/api/admin/settings/consultations",
                headers=admin_headers,
                json={"model": "test/coder"},
            ).status_code
            == 200
        )
        headers = {"x-csrf-token": login(client, "worker", "long-enough-password")}
        yield SimpleNamespace(
            app=app,
            client=client,
            db=db,
            users=users,
            headers=headers,
            admin_headers=admin_headers,
            manager=manager,
        )


def create(t, message="Первый вопрос"):
    response = t.client.post("/api/consultations", headers=t.headers, json={"message": message})
    assert response.status_code == 201, response.text
    return response.json()


def test_consultations_remain_available_when_ordinary_dialogs_are_disabled(consultation):
    t = consultation
    existing = create(t)
    user = t.db.update_user(t.users["worker"]["id"], tasks_only=True)
    # The existing login and consultation continue working after the setting changes.
    assert t.client.get(f"/api/consultations/{existing['id']}").status_code == 200
    response = t.client.post(
        f"/api/consultations/{existing['id']}/messages",
        headers=t.headers, json={"message": "Продолжение"},
    )
    assert response.status_code == 202
    assert set(response.json()) == {"id", "chat_id", "status", "created_at", "finished_at"}
    new = create(t, "Новая консультация")
    assert {c["id"] for c in t.client.get("/api/consultations").json()} == {
        existing["id"], new["id"],
    }
    job = t.db.consultation_jobs(new["id"])[0]
    visible = event_for_user(t.db, user, {"type": "job.updated", "job": job})
    assert visible["type"] == "consultation.updated"
    assert set(visible["job"]) == {"id", "chat_id", "status", "created_at", "finished_at"}
    assert event_for_user(t.db, user, {
        "type": "opencode.event", "session_id": job["session_id"],
        "workspace": user["workspace_path"], "event": {"reasoning": "secret"},
    }) is None
    for path in ("/api/chats", "/api/templates", "/api/options", "/api/admin/consultations"):
        assert t.client.get(path).status_code == 403
    assert t.client.post("/api/chats", headers=t.headers, json={}).status_code == 403
    assert t.client.post(
        "/api/consultations", json={"message": "No CSRF"},
    ).status_code == 403

    stranger = t.db.update_user(t.users["stranger"]["id"], tasks_only=True)
    stranger_headers = {
        "x-csrf-token": login(t.client, "stranger", "long-enough-password"),
    }
    assert t.client.get(f"/api/consultations/{new['id']}").status_code == 404
    assert t.client.post(
        f"/api/consultations/{new['id']}/messages",
        headers=stranger_headers, json={"message": "Чужая консультация"},
    ).status_code == 404
    assert t.client.get("/api/consultations").json() == []
    assert event_for_user(t.db, stranger, {"type": "job.updated", "job": job}) is None
    assert event_for_user(t.db, {**user, "workspace_path": "/another"}, {
        "type": "job.updated", "job": job,
    }) is None


def test_consultation_is_private_and_legacy_endpoints_cannot_override_it(consultation):
    t = consultation
    chat = create(t)
    job = t.db.list_jobs()[0]
    assert job["agent"] == "build"
    assert job["model_id"] == "coder"
    assert job["consultation"] == 1
    assert t.manager.client.session_count == 0
    assert job["session_pending"] == 1
    assert t.client.get("/api/chats").json() == []
    [queued] = t.client.get("/api/queue/me").json()
    assert queued == {
        "id": job["id"], "chat_id": chat["id"], "kind": "question", "status": "queued",
        "title": chat["title"], "task_id": None, "queue_id": job["queue_id"],
        "queue_name": job["queue_name"], "created_at": job["created_at"], "started_at": None,
        "finished_at": None, "error_code": None, "position": 1,
    }
    for suffix in ("", "/messages", "/questions", "/permissions", "/mcp"):
        assert t.client.get(f"/api/chats/{chat['id']}{suffix}").status_code == 404
    assert t.client.get(f"/api/options?chat_id={chat['id']}").status_code == 404
    assert (
        t.client.post(
            f"/api/chats/{chat['id']}/messages",
            headers=t.headers,
            json={"message": "Bypass", "agent": "build"},
        ).status_code
        == 404
    )
    public = t.client.get(f"/api/jobs/{job['id']}").json()
    assert set(public) == {"id", "chat_id", "status", "created_at", "finished_at"}
    for field in ("model", "agent", "model_variant", "template_id"):
        response = t.client.post(
            f"/api/consultations/{chat['id']}/messages",
            headers=t.headers,
            json={"message": "Bypass", field: "build"},
        )
        assert response.status_code == 422
    login(t.client, "stranger", "long-enough-password")
    assert t.client.get(f"/api/consultations/{chat['id']}").status_code == 404
    assert t.client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert t.client.get("/api/consultations").json() == []
    assert t.client.get("/api/admin/consultations").status_code == 403


def test_admin_views_all_dialogues_and_model_is_snapshotted_per_question(consultation):
    t = consultation
    chat = create(t)
    first = t.db.list_jobs()[0]
    t.admin_headers = {"x-csrf-token": login(t.client, "admin", "correct-horse-battery-staple")}
    response = t.client.get(f"/api/admin/consultations/{chat['id']}")
    assert response.status_code == 200
    assert response.json()["owner_login"] == "worker"
    assert response.json()["jobs"][0]["model"] == "test/coder"
    assert t.client.get("/api/admin/consultations?owner_id=missing").json() == []
    assert len(t.client.get("/api/admin/consultations?q=worker").json()) == 1
    assert t.client.get("/api/admin/consultations?q=absent").json() == []
    # Even an administrator cannot send on another user's behalf.
    assert (
        t.client.post(
            f"/api/consultations/{chat['id']}/messages",
            headers=t.admin_headers,
            json={"message": "Impersonate"},
        ).status_code
        == 404
    )
    assert (
        t.client.post(
            f"/api/admin/chats/{chat['id']}/messages",
            headers=t.admin_headers,
            json={"message": "Impersonate"},
        ).status_code
        == 404
    )
    consultation_queue = t.db.create_work_queue("Консультации", 80, 2)
    response = t.client.put(
        "/api/admin/settings/consultations",
        headers=t.admin_headers,
        json={"model": "test/new-model", "queue_id": consultation_queue["id"]},
    )
    assert response.status_code == 200
    assert (
        t.client.get("/api/admin/settings/consultations").json()["queue_id"]
        == consultation_queue["id"]
    )
    login(t.client, "worker", "long-enough-password")
    t.headers = {"x-csrf-token": t.client.get("/api/auth/me").json()["csrf_token"]}
    assert (
        t.client.post(
            f"/api/consultations/{chat['id']}/messages",
            headers=t.headers,
            json={"message": "Второй вопрос"},
        ).status_code
        == 202
    )
    jobs = t.db.consultation_jobs(chat["id"])
    assert [job["model_id"] for job in jobs] == ["coder", "new-model"]
    assert [job["queue_id"] for job in jobs] == ["default", consultation_queue["id"]]
    assert len({job["session_id"] for job in jobs}) == 1
    assert t.db.claim_next_job(4)["id"] == first["id"]
    assert t.db.claim_next_job(4) is None  # no concurrent turns in the same session
    t.db.set_job_status(first["id"], "completed")
    assert t.db.claim_next_job(4)["id"] == jobs[1]["id"]


def test_only_persisted_completed_answer_is_visible_and_events_are_sanitized(consultation):
    t = consultation
    chat = create(t)
    job = t.db.list_jobs()[0]
    calls = t.manager.ensure_calls
    t.db.save_consultation_answer(job["id"], "Итоговый ответ")
    assert len(t.client.get(f"/api/consultations/{chat['id']}").json()["messages"]) == 1
    t.db.set_job_status(job["id"], "completed")
    data = t.client.get(f"/api/consultations/{chat['id']}").json()
    assert [m["text"] for m in data["messages"]] == ["Первый вопрос", "Итоговый ответ"]
    assert t.manager.ensure_calls == calls  # history doesn't wake OpenCode
    assert not {"jobs", "model", "workspace_snapshot", "opencode_session_id"} & data.keys()
    event = {"type": "job.updated", "job": t.db.get_job(job["id"])}
    visible = event_for_user(t.db, t.users["worker"], event)
    assert visible["type"] == "consultation.updated"
    assert set(visible["job"]) == {"id", "chat_id", "status", "created_at", "finished_at"}
    assert event_for_user(t.db, t.users["stranger"], event) is None
    raw = {
        "type": "opencode.event",
        "session_id": job["session_id"],
        "workspace": job["workspace_snapshot"],
        "event": {"reasoning": "secret"},
    }
    assert event_for_user(t.db, t.users["worker"], raw) is None
    assert event_for_user(t.db, t.db.get_user_by_login("admin"), raw) is None


def test_owner_answers_sanitized_question_and_monitor_is_requeued(consultation):
    t = consultation
    chat = create(t)
    claimed = t.db.claim_next_job(1)
    assert t.db.attach_task_session(claimed["id"], "consultation-session")
    t.manager.client.session_records["consultation-session"] = {
        "id": "consultation-session", "directory": t.users["worker"]["workspace_path"]
    }
    t.db.set_job_status(claimed["id"], "waiting_question")
    t.manager.client.pending_questions = [{
        "id": "question-1",
        "sessionID": "consultation-session",
        "secret": "must-not-reach-browser",
        "questions": [{
            "header": "Выбор",
            "question": "Как продолжить?",
            "options": [{
                "label": "Первый",
                "description": "Описание",
                "secret": "hidden",
            }],
            "custom": False,
            "internal": {"reasoning": "hidden"},
        }],
    }]
    path = f"/api/consultations/{chat['id']}/questions"
    response = t.client.get(path)
    assert response.status_code == 200
    assert response.json() == [{
        "id": "question-1",
        "questions": [{
            "header": "Выбор",
            "question": "Как продолжить?",
            "multiple": False,
            "custom": False,
            "options": [{"label": "Первый", "description": "Описание"}],
        }],
    }]
    assert "secret" not in response.text
    blocked = t.client.post(
        f"/api/consultations/{chat['id']}/messages",
        headers=t.headers,
        json={"message": "Не ждать ответа"},
    )
    assert blocked.status_code == 409
    reply_path = path + "/question-1/reply"
    assert t.client.post(reply_path, json={"answers": [["Первый"]]}).status_code == 403
    response = t.client.post(
        reply_path, headers=t.headers, json={"answers": [["Первый"]]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True}
    assert t.manager.client.question_replies == [("question-1", [["Первый"]])]
    resumed = t.db.get_job(claimed["id"])
    assert resumed["status"] == "queued"
    assert resumed["resume_monitor"] == 1
    assert t.client.post(
        reply_path, headers=t.headers, json={"answers": [["Первый"]]}
    ).status_code == 409
    assert t.manager.client.question_replies == [("question-1", [["Первый"]])]


def test_only_owner_can_view_or_answer_consultation_questions(consultation):
    t = consultation
    chat = create(t)
    job = t.db.claim_next_job(1)
    assert t.db.attach_task_session(job["id"], "consultation-session")
    t.db.set_job_status(job["id"], "waiting_question")
    path = f"/api/consultations/{chat['id']}/questions"
    login(t.client, "admin", "correct-horse-battery-staple")
    assert t.client.get(path).status_code == 404
    assert t.client.post(
        path + "/q/reply",
        headers={"x-csrf-token": t.client.get("/api/auth/me").json()["csrf_token"]},
        json={"answers": [["Да"]]},
    ).status_code == 404


def test_configuration_queue_limit_and_csrf(consultation):
    t = consultation
    assert t.client.post("/api/consultations", json={"message": "No csrf"}).status_code == 403
    assert (
        t.client.post("/api/consultations", headers=t.headers, json={"message": "   "}).status_code
        == 422
    )
    assert t.client.get("/api/admin/settings/consultations").status_code == 403
    create(t)
    object.__setattr__(t.app.state.settings, "max_queued_messages", 1)
    count = t.manager.client.session_count
    assert (
        t.client.post(
            "/api/consultations", headers=t.headers, json={"message": "Overflow"}
        ).status_code
        == 429
    )
    assert t.manager.client.session_count == count
    t.admin_headers = {"x-csrf-token": login(t.client, "admin", "correct-horse-battery-staple")}
    for model in (" ", "invalid"):
        assert (
            t.client.put(
                "/api/admin/settings/consultations", headers=t.admin_headers, json={"model": model}
            ).status_code
            == 422
        )
    assert t.client.put(
        "/api/admin/settings/consultations", headers=t.admin_headers,
        json={"model": "test/new", "queue_id": "missing"},
    ).status_code == 422
    assert t.db.consultation_model() == "test/coder"


def transcript(message_id="question"):
    return [
        {"info": {"id": message_id, "role": "user"}},
        {
            "info": {
                "role": "assistant",
                "parentID": message_id,
                "finish": "tool-calls",
                "time": {"completed": 1},
            },
            "parts": [{"type": "text", "text": "Intermediate"}],
        },
        {
            "info": {
                "role": "assistant",
                "parentID": message_id,
                "finish": "stop",
                "time": {"completed": 2},
                "modelID": "secret-model",
            },
            "parts": [
                {"type": "reasoning", "text": "Secret reasoning"},
                {"type": "tool", "state": {"output": "Secret output"}},
                {"type": "text", "text": "Synthetic", "synthetic": True},
                {"type": "text", "text": "Final answer"},
            ],
        },
    ]


def test_final_answer_excludes_intermediate_tools_reasoning_and_later_turns():
    messages = transcript()
    assert final_answer(messages, "question") == "Final answer"
    messages += transcript("second-question")
    assert final_answer(messages, "question") == "Final answer"
    assert final_answer(messages, "missing") is None


@pytest.mark.parametrize(
    "continuation_part",
    [
        {"type": "compaction", "auto": True},
        {"type": "text", "text": "Continue", "synthetic": True},
        {"type": "text", "text": "Continue", "ignored": True},
    ],
)
def test_final_answer_follows_internal_compaction_continuations(continuation_part):
    messages = transcript()[:-1] + [
        {
            "info": {
                "id": "continuation",
                "role": "user",
                "summary": {"diffs": []},
            },
            "parts": [continuation_part],
        },
        {
            "info": {
                "role": "assistant",
                "parentID": "continuation",
                "finish": "stop",
                "time": {"completed": 3},
            },
            "parts": [{"type": "text", "text": "Final after compaction"}],
        },
    ]
    assert final_answer(messages, "question") == "Final after compaction"


@pytest.mark.parametrize(
    "change",
    [
        {"finish": "tool-calls"},
        {"finish": "unknown"},
        {"finish": "error"},
        {"time": {}},
        {"summary": True},
        {"error": {"message": "Private error"}},
    ],
)
def test_unfinished_or_failed_answers_are_not_published(change):
    messages = transcript()
    messages[-1]["info"].update(change)
    assert final_answer(messages, "question") is None


@pytest.fixture
def dispatcher_case(tmp_path, monkeypatch):
    import asyncio

    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr("opencode_web.dispatcher.asyncio.sleep", fast_sleep)
    db = Database(tmp_path / "consultations.db", "secret")
    db.initialize()
    user = db.create_user("worker", "long-enough-password", "user", str(tmp_path))
    chat = db.create_chat(
        user["id"], "consultation-session", "Consultation", str(tmp_path), consultation=True
    )
    job = db.create_job(
        chat=chat,
        effective_user_id=user["id"],
        actor_user_id=user["id"],
        content="Question",
        agent="build",
        provider_id="test",
        model_id="coder",
        model_variant=None,
    )
    client = TaskClient()
    client.message_list = transcript(job["message_id"])
    client.session = AsyncMock(return_value={"id": job["session_id"], "permission": []})
    client.agents = AsyncMock(return_value=[{"name": "build", "permission": [
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "edit", "pattern": "*", "action": "deny"},
    ]}])
    client.update_session_permissions = AsyncMock(
        return_value={"id": job["session_id"], "permission": [
            {"permission": "*", "pattern": "*", "action": "deny"},
            *client.agents.return_value[0]["permission"],
        ]}
    )

    async def ensure(workspace):
        return client

    runner = QueueDispatcher(db, SimpleNamespace(ensure=ensure), EventBroker(), 1)
    db.claim_next_job(1)
    yield db, runner, client, db.get_job(job["id"])
    db.close()


async def test_dispatcher_persists_only_final_answer(dispatcher_case):
    db, runner, client, job = dispatcher_case
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "completed"
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] == "Final answer"
    assert client.prompts[0]["agent"] == "build"
    assert client.prompts[0]["system"] == CONSULTATION_SYSTEM
    assert client.prompts[0]["content"] == "Question"
    assert db.get_job(job["id"])["content"] == "Question"
    client.update_session_permissions.assert_not_awaited()


async def test_old_consultation_inherits_standard_tools_without_losing_history(dispatcher_case):
    db, runner, client, job = dispatcher_case
    client.session.return_value["permission"] = [
        {"permission": "*", "pattern": "*", "action": "deny"}
    ]

    async def submit(session_id, **kwargs):
        client.update_session_permissions.assert_awaited_once_with(
            job["session_id"], client.agents.return_value[0]["permission"]
        )
        assert session_id == job["session_id"]
        client.prompts.append(kwargs)

    client.send_prompt_async = submit
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "completed"
    assert db.get_chat(job["chat_id"])["opencode_session_id"] == job["session_id"]
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] == "Final answer"
    assert len(client.prompts) == 1


@pytest.mark.parametrize("changed", [False, True])
async def test_migrated_consultation_tracks_agent_permissions_without_duplicate_rules(
    dispatcher_case, changed,
):
    db, runner, client, job = dispatcher_case
    client.session.return_value["permission"] = deepcopy(
        client.update_session_permissions.return_value["permission"]
    )
    if changed:
        # Config changes must apply to existing consultations as well as new ones.
        rule = {"permission": "example_mcp_*", "pattern": "*", "action": "ask"}
        client.agents.return_value[0]["permission"].append(rule)
        client.update_session_permissions.return_value["permission"].append(rule)
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "completed"
    if changed:
        client.update_session_permissions.assert_awaited_once_with(
            job["session_id"], client.agents.return_value[0]["permission"]
        )
    else:
        client.update_session_permissions.assert_not_awaited()


@pytest.mark.parametrize("failure", ["ignored", "unavailable"])
async def test_old_permissions_must_be_overridden_before_submission(dispatcher_case, failure):
    db, runner, client, job = dispatcher_case
    client.session.return_value["permission"] = [
        {"permission": "*", "pattern": "*", "action": "deny"}
    ]
    if failure == "ignored":
        client.update_session_permissions.return_value = client.session.return_value
    else:
        client.update_session_permissions.side_effect = OpenCodeError("Update unavailable")
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "failed"
    assert client.prompts == []


@pytest.mark.parametrize("pending", ["permission", "incomplete"])
async def test_hidden_permission_or_partial_answer_does_not_hang(dispatcher_case, pending):
    db, runner, client, job = dispatcher_case
    if pending == "permission":
        client.pending_permissions = [{"id": "p", "sessionID": job["session_id"]}]
    else:
        client.message_list[-1]["info"]["finish"] = "tool-calls"
    await runner._execute(job)
    assert db.get_job(job["id"])["status"] == "failed"
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] is None
    assert client.aborted == [job["session_id"]]


async def test_consultation_question_pauses_without_aborting_or_holding_slot(dispatcher_case):
    db, runner, client, job = dispatcher_case
    client.pending_questions = [{
        "id": "q", "sessionID": job["session_id"],
        "questions": [{"question": "Продолжить?", "options": [{"label": "Да"}]}],
    }]
    await runner._execute(job)
    paused = db.get_job(job["id"])
    assert paused["status"] == "waiting_question"
    assert paused["finished_at"] is None
    assert db.active_slot_count() == 0
    assert db.workspace_has_pending_jobs(job["workspace_snapshot"])
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] is None
    assert client.aborted == []


async def test_answered_consultation_resumes_monitor_without_resending(dispatcher_case):
    db, runner, client, job = dispatcher_case
    db.set_job_status(job["id"], "waiting_question")
    assert db.queue_consultation_resume(job["id"])
    resumed = db.claim_next_job(1)
    assert resumed["resume_monitor"] == 1
    await runner._execute(resumed)
    assert db.get_job(job["id"])["status"] == "completed"
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] == "Final answer"
    assert client.prompts == []


async def test_recovery_preserves_waiting_question_without_worker(dispatcher_case):
    db, runner, client, job = dispatcher_case
    client.pending_questions = [{
        "id": "q", "sessionID": job["session_id"],
        "questions": [{"question": "Продолжить?", "options": [{"label": "Да"}]}],
    }]
    db.set_job_status(job["id"], "waiting_question")
    await runner.recover()
    assert db.get_job(job["id"])["status"] == "waiting_question"
    assert db.active_slot_count() == 0
    assert runner._workers == set()
    assert client.prompts == []


async def test_recovery_queues_monitor_when_question_was_already_answered(dispatcher_case):
    db, runner, client, job = dispatcher_case
    db.set_job_status(job["id"], "waiting_question")
    await runner.recover()
    recovered = db.get_job(job["id"])
    assert recovered["status"] == "queued"
    assert recovered["resume_monitor"] == 1
    assert client.prompts == []


async def test_recovery_persists_final_answer_without_resending(dispatcher_case):
    db, runner, client, job = dispatcher_case
    db.set_job_status(job["id"], "running")
    await runner.recover()
    assert db.get_job(job["id"])["status"] == "completed"
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] == "Final answer"
    assert client.prompts == []
    client.session.assert_not_awaited()
    client.update_session_permissions.assert_not_awaited()
    db.initialize()  # repeated migration preserves data
    assert db.consultation_jobs(job["chat_id"])[0]["answer"] == "Final answer"


async def test_opencode_client_sends_session_rules_and_system():
    import json

    requests = []

    def transport(request):
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "session"})

    client = OpenCodeClient("http://test", "", "")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(transport),
    )
    try:
        await client.create_session("Consultation", permission=deepcopy(CONSULTATION_PERMISSIONS))
        await client.update_session_permissions("session", [])
        await client.send_prompt_async(
            "session",
            message_id="msg_test",
            content="Question",
            agent="build",
            provider_id="test",
            model_id="coder",
            model_variant=None,
            system="Consultation instruction",
        )
    finally:
        await client.close()
    assert requests[0] == ("POST", "/session", {"title": "Consultation", "permission": []})
    assert requests[1] == ("PATCH", "/session/session", {"permission": []})
    assert requests[2][2]["agent"] == "build"
    assert requests[2][2]["system"] == "Consultation instruction"
    assert requests[2][2]["parts"] == [{"type": "text", "text": "Question"}]


def test_missing_configuration_and_unexpected_session_restrictions(consultation, monkeypatch):
    t = consultation
    with t.db.transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key = 'consultation_model'")
    response = t.client.post("/api/consultations", headers=t.headers, json={"message": "Question"})
    assert response.status_code == 503
    assert t.manager.client.session_count == 0
    t.db.set_consultation_model("test/coder", t.db.get_user_by_login("admin")["id"])
    original = t.manager.client.create_session

    async def restricted_session(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["permission"] = [{"permission": "*", "pattern": "*", "action": "deny"}]
        return result

    monkeypatch.setattr(t.manager.client, "create_session", restricted_session)
    response = t.client.post("/api/consultations", headers=t.headers, json={"message": "Question"})
    assert response.status_code == 201
    runner = t.app.state.dispatcher
    runner.manager = t.manager
    job = t.db.claim_next_job(1)
    t.client.portal.call(runner._execute, job)
    assert t.db.get_job(job["id"])["status"] == "failed"
    assert t.db.get_job(job["id"])["error_code"] == "consultation_permissions_unavailable"
    assert t.manager.client.session_records == {}


def test_queue_filling_during_creation_does_not_leave_an_empty_dialogue(consultation, monkeypatch):
    t = consultation
    original = t.db.create_job

    def queue_filled(**kwargs):
        kwargs["max_queued"] = 0
        return original(**kwargs)

    monkeypatch.setattr(t.db, "create_job", queue_filled)
    response = t.client.post("/api/consultations", headers=t.headers, json={"message": "Question"})
    assert response.status_code == 429
    assert t.db.list_chats(consultation=True) == []
    assert t.manager.client.session_records == {}
