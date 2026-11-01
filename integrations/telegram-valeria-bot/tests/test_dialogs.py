from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from valeria_telegram.dialogs import DialogFlows
from valeria_telegram.errors import UserError
from valeria_telegram.store import Session, Store


class Bot(DialogFlows):
    def __init__(self, store: Store) -> None:
        self.store = store
        self.responses = {
            ("GET", "/api/options"): {
                "agents": ["build", "plan"],
                "default_agent": "build",
                "default_model": "p/m1",
                "models": [
                    {"id": "p/m1", "name": "Model One", "provider": "p"},
                    {"id": "p/m2", "name": "Model Two", "provider": "p"},
                ],
            },
            ("GET", "/api/chats"): [{"id": "c1", "title": "Первый диалог"}],
            ("GET", "/api/chats/c1"): {"id": "c1", "title": "Первый диалог"},
            ("GET", "/api/chats/c1/messages"): [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Привет"}]},
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "reasoning", "text": "Hidden reasoning"},
                        {"type": "tool", "tool": "read", "state": {"output": "Hidden tool output"}},
                        {"type": "text", "text": "Здравствуйте"},
                    ],
                },
            ],
            ("GET", "/api/queue/me"): [],
            ("GET", "/api/templates"): [
                {"id": "t1", "title": "Шаблон", "content": "Текст опубликованного шаблона"},
            ],
        }
        self.messages: list[tuple[str, Any]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.submissions: list[dict] = []
        self.watches: list[tuple] = []
        self.question_calls: list[dict] = []
        self.permission_calls: list[str] = []
        self.pending_questions = False
        self.sequence = 0

    def _flow(self, uid: int, kind: str, data: dict) -> dict:
        self.sequence += 1
        result = {**copy.deepcopy(data), "_flow_id": str(self.sequence)}
        self.store.set_flow(uid, kind, result)
        return result

    def _require_flow(self, uid: int, data: dict, kinds: tuple[str, ...]) -> dict:
        saved = self.store.get_flow(uid)
        if saved.get("kind") not in kinds or data.get("flow_id") != saved["data"]["_flow_id"]:
            raise UserError("Эта кнопка устарела.")
        return saved["data"]

    async def _say(self, uid: int, text: str, buttons: Any = None) -> None:
        self.messages.append((text, buttons))

    async def _api_request(self, session: Session, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs))
        return copy.deepcopy(self.responses[(method, path)])

    async def _submit(
        self,
        uid: int,
        session: Session,
        update_id: int,
        kind: str,
        path: str,
        body: dict,
        expected_status: int,
        context: dict | None = None,
    ) -> None:
        self.submissions.append(
            {
                "uid": uid,
                "update_id": update_id,
                "kind": kind,
                "path": path,
                "body": body,
                "expected_status": expected_status,
                "context": context or {},
            }
        )

    async def _watch_resource(self, *args: Any, **kwargs: Any) -> None:
        self.watches.append((args, kwargs))

    async def _questions_start(
        self,
        uid: int,
        session: Session,
        kind: str,
        resource_id: str,
        draft: dict | None = None,
    ) -> bool:
        self.question_calls.append({"kind": kind, "resource_id": resource_id, "draft": draft})
        return self.pending_questions

    async def _permissions(self, uid: int, session: Session, chat_id: str) -> None:
        self.permission_calls.append(chat_id)

    def payload(self, action: str) -> dict:
        for _, buttons in reversed(self.messages):
            for row in buttons or []:
                for _, candidate, data in row:
                    if candidate == action:
                        return data
        raise AssertionError(f"Button not found: {action}")

    async def click(self, action: str, data: dict | None = None, update_id: int = 100) -> bool:
        return await self._dialog_action(
            1, session(), action, data if data is not None else self.payload(action), update_id
        )


def session(**extra: Any) -> Session:
    return Session(
        1,
        1001,
        "token",
        "csrf",
        {
            "id": "u1",
            "workspace_path": "/work/u1",
            "tasks_only": False,
            **extra,
        },
    )


@pytest.fixture
def bot(tmp_path: Path):
    with Store(tmp_path / "state.db") as store:
        yield Bot(store)


async def setup_new(bot: Bot) -> None:
    await bot._dialog_new(1, session())
    agent = bot.payload("dialog_agent")
    await bot.click("dialog_agent", {**agent, "agent": "plan"})
    model = bot.payload("dialog_model")
    await bot.click("dialog_model", {**model, "model": "p/m2"})


async def test_create_dialog_with_selected_agent_and_model(bot: Bot) -> None:
    await setup_new(bot)
    flow = bot.store.get_flow(1)
    assert flow["kind"] == "dialog_input"
    assert await bot._dialog_text(1, session(), "  Сделай анализ  ", 101, flow)
    assert bot.submissions == [
        {
            "uid": 1,
            "update_id": 101,
            "kind": "dialog_create",
            "path": "/api/chats",
            "body": {
                "agent": "plan",
                "model": "p/m2",
                "model_variant": None,
                "message": "Сделай анализ",
            },
            "expected_status": 201,
            "context": {},
        }
    ]


async def test_create_empty_dialog_with_unavailable_catalog(bot: Bot) -> None:
    bot.responses[("GET", "/api/options")]["models"] = []
    await bot._dialog_new(1, session())
    await bot.click("dialog_agent")
    assert "Каталог сейчас недоступен" in bot.messages[-1][0]
    await bot.click("dialog_model")
    await bot.click("dialog_empty")
    assert bot.submissions[0]["body"] == {"agent": None, "model": None, "model_variant": None}
    assert bot.submissions[0]["expected_status"] == 201


async def test_templates_are_editable_drafts_and_require_explicit_send(bot: Bot) -> None:
    await setup_new(bot)
    await bot.click("dialog_templates")
    await bot.click("dialog_template")
    assert bot.submissions == []
    assert bot.store.get_flow(1)["data"]["message"] == "Текст опубликованного шаблона"
    stale_send = bot.payload("dialog_send_saved")
    await bot.click("dialog_edit")
    assert await bot._dialog_text(
        1, session(), "Мой исправленный текст", 101, bot.store.get_flow(1)
    )
    assert bot.submissions == []
    with pytest.raises(UserError):
        await bot.click("dialog_send_saved", stale_send)
    await bot.click("dialog_send_saved", update_id=102)
    assert bot.submissions[0]["body"]["message"] == "Мой исправленный текст"
    assert bot.submissions[0]["body"]["agent"] == "plan"
    assert "template_id" not in bot.submissions[0]["body"]


async def test_unpublished_template_cannot_be_used(bot: Bot) -> None:
    await bot.click("dialog_templates", {})
    selection = bot.payload("dialog_template")
    bot.responses[("GET", "/api/templates")] = []
    with pytest.raises(UserError, match="больше недоступен"):
        await bot.click("dialog_template", selection)
    assert bot.submissions == []


async def test_active_dialog_history_keeps_details_separate(bot: Bot) -> None:
    bot.responses[("GET", "/api/chats/c1/messages")][1]["parts"].extend(
        [
            {"type": "text", "synthetic": True, "text": "Internal synthetic text"},
            {"type": "text", "ignored": True, "text": "Internal ignored text"},
        ]
    )
    await bot._dialog_view(1, session(), "c1")
    history = bot.messages[-1][0]
    assert "Привет" in history
    assert "Здравствуйте" in history
    assert "Hidden reasoning" not in history
    assert "Hidden tool output" not in history
    assert "Internal synthetic text" not in history
    assert "Internal ignored text" not in history
    assert (
        bot.watches[-1][1]["snapshot"]["messages"]
        == bot.responses[("GET", "/api/chats/c1/messages")]
    )
    await bot.click("dialog_details")
    assert "Hidden reasoning" in bot.messages[-1][0]
    assert "Hidden tool output" in bot.messages[-1][0]


async def test_continue_dialog_uses_server_preferences(bot: Bot) -> None:
    await bot._dialog_view(1, session(), "c1")
    await bot._dialog_text(1, session(), "Продолжай", 101, bot.store.get_flow(1))
    assert bot.submissions[0]["path"] == "/api/chats/c1/messages"
    assert bot.submissions[0]["body"] == {"message": "Продолжай"}
    assert bot.submissions[0]["context"] == {"chat_id": "c1"}
    assert bot.submissions[0]["expected_status"] == 202


async def test_pending_questions_keep_new_text_as_draft(bot: Bot) -> None:
    await bot._dialog_view(1, session(), "c1")
    bot.pending_questions = True
    await bot._dialog_text(1, session(), "Новый вопрос", 101, bot.store.get_flow(1))
    assert bot.submissions == []
    assert bot.question_calls[-1] == {
        "kind": "dialog",
        "resource_id": "c1",
        "draft": {"message": "Новый вопрос"},
    }
    await bot._dialog_restore_draft(
        1,
        session(),
        "c1",
        {
            "message": "Новый вопрос",
            "agent": "plan",
            "model": "p/m1",
            "model_variant": "high",
        },
    )
    assert bot.store.get_flow(1)["kind"] == "dialog_draft"
    assert bot.store.get_flow(1)["data"]["settings"]["model_variant"] == "high"


async def test_settings_use_current_chat_and_keep_selected_variant(bot: Bot) -> None:
    options = bot.responses[("GET", "/api/options")]
    options.update(selected_agent="plan", selected_model="p/m2", selected_model_variant="high")
    await bot._dialog_view(1, session(), "c1")
    await bot.click("dialog_settings")
    assert bot.calls[-1] == ("GET", "/api/options", {"params": {"chat_id": "c1"}})
    await bot.click("dialog_agent", {**bot.payload("dialog_agent"), "agent": "plan"})
    await bot.click("dialog_model", {**bot.payload("dialog_model"), "model": "p/m2"})
    assert bot.store.get_flow(1)["data"]["settings"]["model_variant"] == "high"
    await bot._dialog_text(1, session(), "Продолжай", 101, bot.store.get_flow(1))
    assert bot.submissions[0]["body"]["model_variant"] == "high"


async def test_stale_settings_buttons_cannot_change_new_form(bot: Bot) -> None:
    await bot._dialog_new(1, session())
    old = bot.payload("dialog_agent")
    await bot._dialog_new(1, session())
    with pytest.raises(UserError):
        await bot.click("dialog_agent", old)
    flow = bot.store.get_flow(1)
    with pytest.raises(UserError):
        await bot.click("dialog_agent", {**bot.payload("dialog_agent"), "agent": "forbidden"})
    assert bot.store.get_flow(1) == flow


async def test_job_cancellation_and_active_abort_use_distinct_endpoints(bot: Bot) -> None:
    bot.responses[("GET", "/api/queue/me")] = [
        {"id": "queued1", "chat_id": "c1", "status": "queued"},
        {"id": "running1", "chat_id": "c1", "status": "running"},
        {"id": "other", "chat_id": "c2", "status": "queued"},
    ]
    bot.responses[("DELETE", "/api/jobs/queued1")] = {"ok": True}
    bot.responses[("POST", "/api/chats/c1/abort")] = {"aborted": False}
    await bot._dialog_view(1, session(), "c1")
    await bot.click("dialog_stop")
    assert bot.payload("dialog_cancel_job")["job_id"] == "queued1"
    await bot.click("dialog_cancel_job")
    assert ("DELETE", "/api/jobs/queued1", {"expected_status": 204}) in bot.calls
    await bot.click("dialog_stop")
    await bot.click("dialog_abort")
    assert ("POST", "/api/chats/c1/abort", {"expected_status": 202}) in bot.calls
    assert any("Остановка запрошена" in text for text, _ in bot.messages)
    assert not any(text == "Диалог остановлен." for text, _ in bot.messages)


async def test_refresh_keeps_watch_cursor_and_permissions_route(bot: Bot) -> None:
    await bot._dialog_view(1, session(), "c1")
    await bot.click("dialog_refresh")
    assert bot.watches[-1][1] == {}
    await bot.click("dialog_permissions")
    assert bot.permission_calls == ["c1"]
    await bot.click("dialog_questions")
    assert "нет вопросов" in bot.messages[-1][0]


async def test_main_menu_and_notification_buttons_open_the_expected_dialog(bot: Bot) -> None:
    assert await bot.click("dialog_menu", {})
    assert "Диалоги:" in bot.messages[-1][0]
    assert await bot.click("dialog_view", {"id": "c1"})
    assert bot.store.get_flow(1)["data"]["chat_id"] == "c1"
    bot.store.clear_flow(1)
    assert await bot.click("dialog_permissions", {"id": "c1"})
    assert bot.permission_calls == ["c1"]
    bot.store.clear_flow(1)
    assert await bot.click("dialog_questions", {"id": "c1"})
    assert bot.question_calls[-1]["resource_id"] == "c1"


async def test_restricted_users_and_unassigned_workspace(bot: Bot) -> None:
    for user in (session(tasks_only=True), session(workspace_path=None)):
        with pytest.raises(UserError):
            await bot._dialog_new(1, user)
        with pytest.raises(UserError):
            await bot._dialog_view(1, user, "c1")
    assert bot.calls == []


async def test_dialog_pagination_is_bounded(bot: Bot) -> None:
    bot.responses[("GET", "/api/chats")] = [
        {"id": f"c{i}", "title": f"Диалог {i}"} for i in range(10)
    ]
    await bot._dialog_list(1, session())
    assert len(bot.messages[-1][1]) == 10
    await bot._dialog_list(1, session(), 1)
    assert len(bot.messages[-1][1]) == 4
    with pytest.raises(UserError):
        await bot._dialog_list(1, session(), -1)


async def test_unrelated_actions_and_text_are_not_handled(bot: Bot) -> None:
    assert not await bot._dialog_action(1, session(), "task_new", {}, 1)
    assert not await bot._dialog_text(1, session(), "hi", 1, {"kind": "consultation_input"})


async def test_text_rejects_empty_and_too_long_messages(bot: Bot) -> None:
    await setup_new(bot)
    for value in ("  ", "a" * 100001):
        with pytest.raises(UserError):
            await bot._dialog_text(1, session(), value, 101, bot.store.get_flow(1))
    assert bot.submissions == []
