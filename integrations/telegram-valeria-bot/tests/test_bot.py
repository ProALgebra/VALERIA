"""Exercise real update routing, durable state, and opaque Telegram callbacks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from valeria_telegram.bot import ValeriaBot
from valeria_telegram.config import Settings
from valeria_telegram.store import Store
from valeria_telegram.telegram_client import TelegramError
from valeria_telegram.valeria_client import ValeriaError

ALICE = 101
BOB = 202


def pending_question():
    return [
        {
            "id": "q1",
            "questions": [
                {
                    "header": "Уточнение",
                    "question": "Продолжить?",
                    "custom": False,
                    "options": [{"label": "Да", "description": "Продолжить работу"}],
                }
            ],
        }
    ]


class Telegram:
    def __init__(self, events):
        self.events = events
        self.messages = []
        self.deleted = []
        self.answered = []
        self.fail_text = None
        self.fail_code = 503
        self.fail_ack = False

    async def send_text(self, chat_id, text, *, keyboard=None):
        if self.fail_text and self.fail_text in text:
            self.fail_text = None
            raise TelegramError(self.fail_code)
        self.messages.append({"chat_id": chat_id, "text": text, "keyboard": keyboard or []})

    async def answer_callback_query(self, identifier, text=None):
        self.answered.append(identifier)
        if self.fail_ack:
            raise TelegramError(400)

    async def delete_message(self, chat_id, message_id):
        self.events.append("delete-password")
        self.deleted.append((chat_id, message_id))

    def text(self, uid=ALICE):
        return "\n".join(item["text"] for item in self.messages if item["chat_id"] == uid)

    def button(self, caption, uid=ALICE):
        for message in reversed(self.messages):
            if message["chat_id"] == uid:
                for row in message["keyboard"]:
                    for button in row:
                        if button["text"] == caption:
                            return button["callback_data"]
        raise AssertionError(f"Missing button: {caption}")


class Backend:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.expired = set()
        self.fail_after_accept_once = False
        self.accepted = {}
        self.users = {
            login: {
                "id": f"user_{login}",
                "login": login,
                "workspace_path": f"/work/{login}",
                "tasks_only": False,
                "status": "active",
            }
            for login in ("alice", "bob")
        }
        self.states = {
            login: {
                "runs": {},
                "consultations": {},
                "questions": {},
                "permissions": {},
                "chats": {"d1": {"id": "d1", "title": f"Диалог {login}", "status": "running"}},
                "messages": {"d1": []},
            }
            for login in self.users
        }

    def factory(self, session):
        backend = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def login(self, login, password):
                assert session is None
                backend.events.append("login")
                if login not in backend.users or password != "private-password":
                    raise ValeriaError("Неверный логин или пароль", status_code=401)
                return {
                    "user": deepcopy(backend.users[login]),
                    "session_token": f"token-{login}",
                    "csrf_token": f"csrf-{login}",
                }

            async def request(self, method, path, **kwargs):
                assert session is not None
                login = session.user["login"]
                assert session.session_token == f"token-{login}"
                assert session.csrf_token == f"csrf-{login}"
                return backend.request(login, method, path.lstrip("/"), **kwargs)

        return Client()

    def request(self, login, method, path, **kwargs):
        self.calls.append({"login": login, "method": method, "path": path, **deepcopy(kwargs)})
        if login in self.expired:
            raise ValeriaError("Сессия истекла", status_code=401)
        return deepcopy(self._request(login, method, path, **kwargs))

    def _request(self, login, method, path, **kwargs):
        state = self.states[login]
        if path == "api/auth/me":
            return {"user": self.users[login], "csrf_token": f"csrf-{login}"}
        if path == "api/auth/logout":
            return None
        if method == "GET" and path == "api/tasks":
            return [{"id": "task1", "title": "Отчёт", "description": "Собрать отчёт"}]
        if method == "GET" and path == "api/task-runs":
            return list(state["runs"].values())
        if method == "GET" and path == "api/options":
            return {
                "agents": ["build", "plan"],
                "default_agent": "build",
                "default_model": "p/m1",
                "models": [
                    {"id": "p/m1", "name": "One", "provider": "p"},
                    {"id": "p/m2", "name": "Two", "provider": "p"},
                ],
            }
        if method == "GET" and path == "api/templates":
            return [{"id": "tpl1", "title": "Шаблон отчёта", "content": "Подготовь отчёт"}]
        if method == "GET" and path == "api/queue/me":
            return []
        parts = path.split("/")
        if method == "POST" and (
            path in {"api/tasks/task1/runs", "api/consultations", "api/chats"}
            or (
                len(parts) == 4
                and parts[1] in {"consultations", "chats"}
                and parts[3] == "messages"
            )
        ):
            key = (login, kwargs["idempotency_key"])
            if key in self.accepted:
                return self.accepted[key]
            body = kwargs["body"]
            if path == "api/tasks/task1/runs":
                identifier = f"r{len(state['runs']) + 1}"
                result = {"id": identifier, "title": "Отчёт", "status": "queued"}
                state["runs"][identifier] = result
            elif path == "api/consultations":
                identifier = f"con{len(state['consultations']) + 1}"
                result = {"id": identifier, "title": "Консультация", "status": "running"}
                state["consultations"][identifier] = {
                    **result,
                    "messages": [{"id": "u1", "role": "user", "text": body["message"]}],
                }
            elif path == "api/chats":
                identifier = f"d{len(state['chats']) + 1}"
                chat = {"id": identifier, "title": "Новый диалог", "status": "running"}
                state["chats"][identifier] = chat
                state["messages"][identifier] = []
                result = {"chat": chat}
            else:
                result = {"id": "job1", "status": "queued"}
            self.accepted[key] = deepcopy(result)
            if self.fail_after_accept_once:
                self.fail_after_accept_once = False
                raise ValeriaError("Ответ сервера потерян", acceptance_unknown=True)
            return result
        if parts[:2] == ["api", "task-runs"]:
            run = state["runs"][parts[2]]
            if parts[-1] == "stop":
                run["status"] = "cancelled"
            elif parts[-1] == "result":
                return {"status": "available", "text": f"Итог {login}: отчёт готов"}
            return run
        if parts[:2] in (["api", "consultations"], ["api", "chats"]):
            kind = parts[1]
            if len(parts) == 2:
                return list(state[kind].values())
            identifier = parts[2]
            if len(parts) == 3:
                return state[kind][identifier]
            if parts[3] == "messages":
                return state["messages"][identifier]
            if parts[3] == "questions":
                if method == "POST":
                    state["questions"][identifier] = []
                    return {"accepted": True}
                return state["questions"].get(identifier, [])
            if parts[3] == "permissions":
                if method == "POST":
                    state["permissions"][identifier] = []
                    return {"accepted": True}
                return state["permissions"].get(identifier, [])
        raise AssertionError(f"Unexpected API request: {method} {path}")

    def writes(self, path=None):
        return [
            call
            for call in self.calls
            if call["method"] == "POST" and (path is None or call["path"] == path)
        ]


class Harness:
    def __init__(self, store, settings):
        self.store = store
        self.events = []
        self.telegram = Telegram(self.events)
        self.backend = Backend(self.events)
        self.bot = ValeriaBot(settings, store, self.telegram, api_factory=self.backend.factory)
        self.sequence = 0

    def message(self, text, uid=ALICE, *, chat_type="private"):
        self.sequence += 1
        return {
            "update_id": self.sequence,
            "message": {
                "message_id": self.sequence,
                "from": {"id": uid},
                "chat": {"id": uid, "type": chat_type},
                "text": text,
            },
        }

    async def send(self, text, uid=ALICE, **kwargs):
        update = self.message(text, uid, **kwargs)
        await self.bot.handle_update(update)
        return update

    async def click(self, caption=None, uid=ALICE, *, raw=None):
        self.sequence += 1
        update = {
            "update_id": self.sequence,
            "callback_query": {
                "id": f"callback-{self.sequence}",
                "from": {"id": uid},
                "data": raw or self.telegram.button(caption, uid),
                "message": {"message_id": self.sequence, "chat": {"id": uid, "type": "private"}},
            },
        }
        await self.bot.handle_update(update)
        return update

    async def login(self, uid=ALICE, login="alice"):
        await self.send("/start", uid)
        await self.click("Войти", uid)
        await self.send(login, uid)
        await self.send("private-password", uid)

    async def choose_task(self, uid=ALICE):
        await self.send("/menu", uid)
        await self.click("Таски", uid)
        await self.click("Создать таску", uid)
        await self.click("Отчёт", uid)

    async def open_dialog(self, uid=ALICE):
        await self.send("/dialogs", uid)
        await self.click("Мои диалоги", uid)
        await self.click(f"Диалог {'alice' if uid == ALICE else 'bob'}", uid)


@pytest.fixture
def app(tmp_path):
    with Store(tmp_path / "state.db") as store:
        yield Harness(store, Settings("123:test-token", "https://valeria.example", tmp_path / "db"))


async def test_login_is_private_and_deletes_password_before_authentication(app):
    await app.send("/start", chat_type="group")
    assert not app.telegram.messages
    assert not app.store.get_flow(ALICE)
    await app.login()
    assert app.events == ["delete-password", "login"]
    assert app.store.get_session(ALICE).user["login"] == "alice"
    assert "Таски" in [
        button["text"] for row in app.telegram.messages[-1]["keyboard"] for button in row
    ]
    assert "private-password" not in app.telegram.text()
    assert "private-password" not in str(app.store.get_flow(ALICE))
    assert "private-password" not in "\n".join(app.store.connection.iterdump())


async def test_opaque_callback_cannot_be_used_by_another_logged_in_user(app):
    await app.login()
    alice_button = app.telegram.button("Таски")
    await app.login(BOB, "bob")
    before = len(app.backend.calls)
    await app.click(uid=BOB, raw=alice_button)
    assert "принадлежит другой сессии" in app.telegram.text(BOB)
    assert [call["path"] for call in app.backend.calls[before:]] == ["api/auth/me"]
    assert app.store.get_session(ALICE).session_token != app.store.get_session(BOB).session_token
    assert app.store.get_callback(BOB, alice_button[2:]) is None


async def test_tasks_only_and_missing_workspace_restrict_creation(app):
    app.backend.users["alice"].update(tasks_only=True, workspace_path=None)
    await app.login()
    menu = app.telegram.messages[-1]
    assert "Диалоги" not in str(menu["keyboard"])
    assert "назначить рабочую папку" in menu["text"]
    await app.send("/dialogs")
    assert "доступны таски и консультации" in app.telegram.text()
    await app.choose_task()
    assert "Для создания нужна рабочая папка" in app.telegram.text()
    await app.send("/consultations")
    await app.click("Новая консультация")
    assert not app.backend.writes()


async def test_task_launch_input_history_result_and_stop(app):
    await app.login()
    await app.choose_task()
    await app.send("За прошлую неделю")
    posts = app.backend.writes("api/tasks/task1/runs")
    assert posts[0]["body"] == {"user_input": "За прошлую неделю"}
    assert posts[0]["login"] == "alice"
    assert "Таска принята" in app.telegram.text()
    await app.click("Мои запуски")
    await app.click("Отчёт")
    await app.click("Результат")
    assert "Итог alice: отчёт готов" in app.telegram.text()
    await app.click("Остановить")
    assert app.backend.writes("api/task-runs/r1/stop")
    assert "Отменено" in app.telegram.text()


async def test_consultation_followup_answers_question_then_sends_saved_draft(app):
    await app.login()
    await app.click("Консультации")
    await app.click("Новая консультация")
    await app.send("Первый вопрос")
    assert app.backend.writes("api/consultations")[0]["body"] == {"message": "Первый вопрос"}
    app.backend.states["alice"]["questions"]["con1"] = pending_question()
    await app.send("Следующий вопрос")
    assert not app.backend.writes("api/consultations/con1/messages")
    await app.click("Да")
    await app.click("Отправить ответы")
    reply = app.backend.writes("api/consultations/con1/questions/q1/reply")
    assert reply[0]["body"] == {"answers": [["Да"]]}
    await app.click("Отправить вопрос")
    assert app.backend.writes("api/consultations/con1/messages")[0]["body"] == {
        "message": "Следующий вопрос",
    }


async def test_dialog_menu_wizard_template_and_followup_use_selected_settings(app):
    await app.login()
    await app.click("Диалоги")
    await app.click("Новый диалог")
    await app.click("plan")
    await app.click("p / Two")
    await app.click("Выбрать шаблон")
    await app.click("Шаблон отчёта")
    await app.click("Отправить текст")
    body = app.backend.writes("api/chats")[0]["body"]
    assert body == {
        "agent": "plan",
        "model": "p/m2",
        "model_variant": None,
        "message": "Подготовь отчёт",
    }
    await app.send("Продолжи")
    assert app.backend.writes("api/chats/d2/messages")[0]["body"] == {"message": "Продолжи"}


async def test_timeout_retry_reuses_body_and_key_without_duplicate_job(app):
    await app.login()
    await app.choose_task()
    app.backend.fail_after_accept_once = True
    update = await app.send("Исходный запрос")
    await app.send("/menu")
    await app.click("Повторить отправку")
    writes = app.backend.writes("api/tasks/task1/runs")
    assert len(writes) == 2
    assert writes[0]["body"] == writes[1]["body"] == {"user_input": "Исходный запрос"}
    assert writes[0]["idempotency_key"] == writes[1]["idempotency_key"]
    assert len(app.backend.states["alice"]["runs"]) == 1
    calls = len(app.backend.calls)
    await app.bot.handle_update(update)
    assert len(app.backend.calls) == calls


async def test_crash_after_acceptance_replay_uses_saved_result_even_after_flow_changed(app):
    await app.login()
    await app.choose_task()
    app.telegram.fail_text = "Таска принята"
    original = app.message("Важное дополнение")
    with pytest.raises(TelegramError):
        await app.bot.handle_update(original)
    assert not app.store.is_processed(original["update_id"])
    await app.send("/consultations")
    await app.click("Новая консультация")
    await app.bot.handle_update(original)
    assert len(app.backend.writes("api/tasks/task1/runs")) == 1
    assert not app.backend.writes("api/consultations")
    assert app.store.is_processed(original["update_id"])
    assert "Таска принята" in app.telegram.text()


async def test_expired_session_clears_own_state_and_preserves_other_user(app):
    await app.login()
    await app.choose_task()
    await app.send("alice-task")
    callback = app.telegram.button("Обновить")
    await app.login(BOB, "bob")
    await app.choose_task(BOB)
    await app.send("bob-task", BOB)
    app.backend.expired.add("alice")
    await app.send("/profile")
    assert app.store.get_session(ALICE) is None
    assert app.store.get_callback(ALICE, callback[2:]) is None
    assert not app.store.list_watches(ALICE)
    assert app.store.get_session(BOB).user["login"] == "bob"
    assert app.store.list_watches(BOB)
    assert "bob-task" not in app.telegram.text(ALICE)
    assert "Войдите снова" in app.telegram.text(ALICE)


async def test_logout_revokes_backend_session_and_clears_local_callbacks_and_watches(app):
    await app.login()
    await app.choose_task()
    await app.send("запрос")
    callback = app.telegram.button("Обновить")
    await app.send("/logout")
    assert app.backend.writes("api/auth/logout")
    assert app.store.get_session(ALICE) is None
    assert not app.store.list_watches(ALICE)
    assert app.store.get_callback(ALICE, callback[2:]) is None
    await app.click(raw=callback)
    assert "войдите" in app.telegram.messages[-1]["text"]


async def test_completed_task_notification_delivers_result_once(app):
    await app.login()
    await app.choose_task()
    await app.send("запрос")
    app.backend.states["alice"]["runs"]["r1"]["status"] = "completed"
    await app.bot.poll_notifications()
    await app.bot.poll_notifications()
    assert app.telegram.text().count("Итог alice: отчёт готов") == 1
    assert not app.store.list_watches(ALICE)


async def test_dialog_notification_waits_for_completed_same_message_and_routes_questions(app):
    state = app.backend.states["alice"]
    state["messages"]["d1"] = [
        {
            "info": {"id": "a1", "role": "assistant", "time": {}},
            "parts": [{"type": "text", "text": "Черновик"}],
        }
    ]
    await app.login()
    await app.open_dialog()
    app.telegram.messages.clear()
    await app.bot.poll_notifications()
    assert "Черновик" not in app.telegram.text()
    state["messages"]["d1"][0]["info"]["time"]["completed"] = 123
    state["messages"]["d1"][0]["parts"][0]["text"] = "Готовый ответ"
    state["questions"]["d1"] = pending_question()
    state["permissions"]["d1"] = [{"id": "p1", "permission": "read", "patterns": ["src/*"]}]
    await app.bot.poll_notifications()
    await app.bot.poll_notifications()
    assert app.telegram.text().count("Готовый ответ") == 1
    question_callback = app.telegram.button("Ответить на вопросы")
    permission_callback = app.telegram.button("Разрешения")
    await app.click(raw=question_callback)
    await app.click("Да")
    await app.click("Отправить ответы")
    assert app.backend.writes("api/chats/d1/questions/q1/reply")
    await app.click(raw=permission_callback)
    await app.click("Разрешить один раз")
    assert app.backend.writes("api/chats/d1/permissions/p1")[0]["body"] == {
        "response": "once",
        "remember": False,
    }


async def test_expired_callback_ack_does_not_discard_user_action(app):
    await app.login()
    app.telegram.fail_ack = True
    await app.click("Таски")
    assert "Создать таску" in str(app.telegram.messages[-1]["keyboard"])


async def test_blocked_telegram_chat_does_not_hold_the_entire_update_batch(app):
    await app.login()
    await app.login(BOB, "bob")
    app.telegram.fail_text = "Профиль"
    app.telegram.fail_code = 403
    update = await app.send("/profile")
    assert app.store.is_processed(update["update_id"])
    assert app.store.get_session(ALICE) is None
    assert app.store.get_session(BOB).user["login"] == "bob"
    await app.send("/profile", BOB)
    assert "Профиль: bob" in app.telegram.text(BOB)


async def test_idle_dialog_stops_polling_and_resumes_without_repeating_old_answers(app):
    await app.login()
    await app.open_dialog()
    state = app.backend.states["alice"]
    state["chats"]["d1"]["status"] = "idle"
    state["messages"]["d1"] = [
        {
            "info": {"id": "answer-1", "role": "assistant", "time": {"completed": 123}},
            "parts": [{"type": "text", "text": "Первый готовый ответ"}],
        }
    ]
    await app.bot.poll_notifications()
    calls = len(app.backend.calls)
    await app.bot.poll_notifications()
    assert len(app.backend.calls) == calls
    assert app.telegram.text().count("Первый готовый ответ") == 1
    await app.send("Новый запрос")
    state["chats"]["d1"]["status"] = "running"
    await app.bot.poll_notifications()
    assert len(app.backend.calls) > calls
    assert app.telegram.text().count("Первый готовый ответ") == 1


async def test_long_dialog_does_not_repeat_answers_after_delivery_cursor_grows(app):
    await app.login()
    await app.open_dialog()
    state = app.backend.states["alice"]
    state["messages"]["d1"] = [
        {
            "info": {"id": f"a-{index}", "role": "assistant", "time": {"completed": 123}},
            "parts": [{"type": "text", "text": f"Ответ номер {index}"}],
        }
        for index in range(2001)
    ]
    app.store.upsert_watch(
        ALICE,
        "dialog",
        "d1",
        {
            "status": "running",
            "last_ids": [f"a-{index}" for index in range(2000)],
        },
    )
    app.telegram.messages.clear()
    await app.bot.poll_notifications()
    await app.bot.poll_notifications()
    assert len(app.telegram.messages) == 1
    assert "Ответ номер 2000" in app.telegram.text()
