from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import FakeOpenCodeClient, login

from opencode_web.app import can_receive_event, create_app
from opencode_web.opencode_client import OpenCodeError
from opencode_web.questions import question_event, session_questions, validate_answers

REQUEST = {
    "id": "que-root",
    "sessionID": "root",
    "questions": [
        {
            "header": "Action",
            "question": "Continue?",
            "options": [
                {"label": "Yes", "description": "Continue"},
                {"label": "No", "description": "Stop"},
            ],
            "custom": False,
        },
        {
            "header": "Targets",
            "question": "Which?",
            "multiple": True,
            "options": [
                {"label": "A", "description": "First"},
                {"label": "B", "description": "Second"},
            ],
        },
    ],
}


class QuestionClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.pending = [deepcopy(REQUEST)]
        self.replies = []
        self.failure = None
        self.unavailable = False

    async def questions(self):
        if self.unavailable:
            raise OpenCodeError("offline")
        return deepcopy(self.pending)

    async def respond_question(self, question_id, answers):
        self.replies.append((question_id, answers))
        if self.failure != "before":
            self.pending = [q for q in self.pending if q["id"] != question_id]
        if self.failure:
            raise OpenCodeError("timeout")
        return True


class Manager:
    def __init__(self, client):
        self.client = client

    @asynccontextmanager
    async def use(self, workspace, *, wake=True, activity=True):
        yield self.client

    async def ensure(self, workspace):
        return self.client


@pytest.fixture
def question_api(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    with TestClient(app) as client:
        upstream = QuestionClient()
        app.state.manager = Manager(upstream)
        db = app.state.database
        user = db.create_user("owner", "long-user-password", "user", str(workspace))
        other = db.create_user("other", "long-user-password", "user", str(workspace))
        db.create_user("taskonly", "long-user-password", "user", str(workspace), tasks_only=True)
        chat = db.create_chat(user["id"], "root", "Question", str(workspace))
        foreign = db.create_chat(other["id"], "foreign", "Other", str(workspace))
        upstream.session_records["foreign"] = {"id": "foreign", "directory": str(workspace)}
        csrf = login(client, "owner", "long-user-password")
        yield SimpleNamespace(
            app=app,
            client=client,
            upstream=upstream,
            chat=chat,
            foreign=foreign,
            headers={"x-csrf-token": csrf},
            user=user,
            workspace=str(workspace),
            path=f"/api/chats/{chat['id']}/questions",
        )


def test_reply_requires_csrf_and_valid_answers_and_does_not_duplicate(question_api):
    ctx = question_api
    assert ctx.client.get(ctx.path).json() == [REQUEST]
    path = ctx.path + "/que-root/reply"
    body = {"answers": [["Yes"], ["A", "custom target"]]}
    assert ctx.client.post(path, json=body).status_code == 403
    invalid = ctx.client.post(path, headers=ctx.headers, json={"answers": [["Yes"]]})
    assert invalid.status_code == 422
    assert (
        ctx.client.post(
            path, headers=ctx.headers, json={"answers": [["unknown"], ["A"]]}
        ).status_code
        == 422
    )
    assert ctx.upstream.replies == []
    response = ctx.client.post(path, headers=ctx.headers, json=body)
    assert response.status_code == 200, response.text
    assert ctx.client.post(path, headers=ctx.headers, json=body).status_code == 409
    assert ctx.upstream.replies == [("que-root", body["answers"])]
    assert ctx.client.get(ctx.path).json() == []


def test_question_scope_owner_other_chat_and_task_only(question_api):
    ctx = question_api
    foreign_question = {**deepcopy(REQUEST), "id": "que-other", "sessionID": "foreign"}
    ctx.upstream.pending.append(foreign_question)
    assert ctx.client.get(ctx.path).json() == [REQUEST]
    assert ctx.client.get(f"/api/chats/{ctx.foreign['id']}/questions").status_code == 404
    assert (
        ctx.client.post(
            ctx.path + "/que-other/reply", headers=ctx.headers, json={"answers": [["Yes"], ["A"]]}
        ).status_code
        == 409
    )
    event = question_event(ctx.workspace, "root")
    assert can_receive_event(ctx.app.state.database, ctx.user, event)
    assert not can_receive_event(
        ctx.app.state.database, ctx.user, question_event(ctx.workspace, "foreign")
    )
    assert not can_receive_event(
        ctx.app.state.database, ctx.user, question_event("/another", "root")
    )
    login(ctx.client, "taskonly", "long-user-password")
    assert ctx.client.get(ctx.path).status_code == 403
    login(ctx.client, "admin", "correct-horse-battery-staple")
    assert ctx.client.get(ctx.path).status_code == 200


@pytest.mark.parametrize(("failure", "status", "pending"), [("before", 503, 1), ("after", 409, 0)])
def test_timeout_checks_pending_without_resending(question_api, failure, status, pending):
    ctx = question_api
    ctx.upstream.failure = failure
    response = ctx.client.post(
        ctx.path + "/que-root/reply", headers=ctx.headers, json={"answers": [["No"], ["B"]]}
    )
    assert response.status_code == status
    assert len(ctx.upstream.replies) == 1
    assert len(ctx.client.get(ctx.path).json()) == pending


def test_upstream_offline_is_visible(question_api):
    question_api.upstream.unavailable = True
    assert question_api.client.get(question_api.path).status_code == 503


async def test_descendant_scope_rejects_other_workspaces_cycles_and_unrelated_sessions(tmp_path):
    client = QuestionClient()
    client.session_records = {
        "child": {"id": "child", "parentID": "root", "directory": str(tmp_path)},
        "grandchild": {"id": "grandchild", "parentID": "child", "directory": str(tmp_path)},
        "foreign": {"id": "foreign", "parentID": "root", "directory": "/elsewhere"},
        "cycle": {"id": "cycle", "parentID": "cycle", "directory": str(tmp_path)},
        "unrelated": {"id": "unrelated", "directory": str(tmp_path)},
    }
    client.pending.extend(
        {**REQUEST, "id": f"que-{sid}", "sessionID": sid} for sid in client.session_records
    )
    result = await session_questions(client, "root", str(tmp_path))
    assert [item["id"] for item in result] == ["que-root", "que-child", "que-grandchild"]


@pytest.mark.parametrize(
    "answers", [[], [["Yes"], []], [["Yes", "No"], ["A"]], [["Yes"], [" "]], [["Yes"], ["A", "A"]]]
)
def test_invalid_answers(answers):
    with pytest.raises(ValueError):
        validate_answers(REQUEST, answers)


def test_multiple_and_free_text_answers():
    validate_answers(REQUEST, [["Yes"], ["A", "B", "my answer"]])
