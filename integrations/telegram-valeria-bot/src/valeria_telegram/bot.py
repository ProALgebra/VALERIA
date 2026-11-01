"""Private-chat user interface backed exclusively by each user's Valeria session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections import defaultdict
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .config import Settings
from .dialogs import DialogFlows
from .errors import UserError
from .presentation import (
    TERMINAL,
    history_text,
    message_id,
    message_role,
    message_text,
    resource_id,
    status_label,
)
from .questions import QuestionFlows
from .store import Session, Store
from .telegram_client import TelegramClient, TelegramError
from .valeria_client import ValeriaClient, ValeriaError

logger = logging.getLogger(__name__)
PAGE_SIZE = 8


class ValeriaBot(DialogFlows, QuestionFlows):
    def __init__(
        self,
        settings: Settings,
        store: Store,
        telegram: TelegramClient,
        *,
        api_factory=None,
    ):
        self.settings, self.store, self.telegram = settings, store, telegram
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._api_factory = api_factory or self._new_client

    def _new_client(self, session: Session | None) -> ValeriaClient:
        return ValeriaClient(
            self.settings,
            session_token=session.session_token if session else None,
            csrf_token=session.csrf_token if session else None,
        )

    async def aclose(self) -> None:
        pass

    async def _api_request(self, session: Session, method: str, path: str, **kwargs):
        async with self._api_factory(session) as client:
            return await client.request(method, path, **kwargs)

    async def _say(self, uid: int, text: str, buttons=None) -> None:
        keyboard = None
        if buttons:
            keyboard = []
            for row in buttons:
                keyboard.append(
                    [
                        {
                            "text": caption,
                            "callback_data": "c:" + self.store.create_callback(uid, action, data),
                        }
                        for caption, action, data in row
                    ]
                )
        session = self.store.get_session(uid)
        await self.telegram.send_text(session.chat_id if session else uid, text, keyboard=keyboard)

    def _flow(self, uid: int, kind: str, data: dict | None = None) -> dict:
        saved = {**(data or {}), "_flow_id": secrets.token_hex(8)}
        self.store.set_flow(uid, kind, saved)
        return saved

    def _require_flow(self, uid: int, callback_data: dict, kinds: tuple[str, ...]) -> dict:
        flow = self.store.get_flow(uid)
        data = flow.get("data", {})
        if flow.get("kind") not in kinds or callback_data.get("flow_id") != data.get("_flow_id"):
            raise UserError("Эта форма устарела. Откройте действие заново через /menu.")
        return data

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or self.store.is_processed(update_id):
            return
        callback = update.get("callback_query")
        message = (
            callback.get("message", {}) if isinstance(callback, dict) else update.get("message")
        )
        sender = (
            callback.get("from", {})
            if isinstance(callback, dict)
            else (message or {}).get("from", {})
        )
        uid = sender.get("id")
        if (
            not isinstance(message, dict)
            or not isinstance(uid, int)
            or uid <= 0
            or sender.get("is_bot")
            or message.get("chat", {}).get("type") != "private"
            or message.get("chat", {}).get("id") != uid
            or (self.settings.allowed_user_ids and uid not in self.settings.allowed_user_ids)
        ):
            if isinstance(callback, dict) and callback.get("id"):
                with suppress(TelegramError):
                    await self.telegram.answer_callback_query(
                        callback["id"], "Откройте личный чат с ботом."
                    )
            self.store.mark_processed(update_id)
            return
        async with self._locks[uid]:
            if self.store.is_processed(update_id):
                return
            if isinstance(callback, dict):
                # Telegram expires callback acknowledgements, but the action may
                # still need replay after a crash or a failed result delivery.
                with suppress(TelegramError):
                    await self.telegram.answer_callback_query(callback["id"])
            try:
                try:
                    await self._handle(uid, update_id, message, callback)
                except UserError as exc:
                    await self._say(uid, str(exc))
                except ValeriaError as exc:
                    if exc.status_code == 401 and self.store.get_session(uid):
                        await self._expired(uid)
                    else:
                        await self._say(uid, str(exc))
            except TelegramError as exc:
                if exc.error_code != 403:
                    raise
                # A blocked/deactivated chat cannot recover by replaying this
                # update. Release the batch so other users keep receiving work.
                self.store.clear_user(uid)
            self.store.mark_processed(update_id)

    async def _handle(self, uid: int, update_id: int, message: dict, callback: dict | None):
        text = message.get("text") if callback is None else None
        flow = self.store.get_flow(uid)
        if text is not None and flow.get("kind") == "login_password":
            try:
                await self.telegram.delete_message(uid, message["message_id"])
            except TelegramError:
                await self._say(uid, "Не удалось удалить сообщение с паролем. Удалите его вручную.")
        command = (
            text.split(maxsplit=1)[0].split("@")[0].lower() if text and text.startswith("/") else ""
        )
        if command == "/logout":
            await self._logout(uid)
            return
        if command == "/login":
            await self._begin_login(uid)
            return
        session = self.store.get_session(uid)
        if not session and not callback and flow.get("kind") in {"login_name", "login_password"}:
            if command in {"/start", "/menu", "/cancel", "/help"}:
                self.store.clear_flow(uid)
                await self._login_menu(uid)
            elif text is not None:
                await self._login_text(uid, message, text, flow)
            else:
                await self._say(
                    uid, "Введите данные текстовым сообщением или отмените вход: /cancel."
                )
            return
        if not session:
            if callback:
                token = str(callback.get("data", "")).removeprefix("c:")
                button = self.store.get_callback(uid, token)
                if button and button["action"] == "login":
                    await self._begin_login(uid)
                    return
            await self._login_menu(uid)
            return
        me = await self._api_request(session, "GET", "api/auth/me")
        if me["user"].get("id") != session.user.get("id"):
            await self._expired(uid)
            return
        session = replace(session, user=me["user"], csrf_token=me["csrf_token"])
        self.store.save_session(session)
        if command:
            routes = {
                "/start": "home",
                "/menu": "home",
                "/cancel": "home",
                "/tasks": "tasks",
                "/consultations": "consultations",
                "/dialogs": "dialog_menu",
                "/profile": "profile",
            }
            if command == "/help":
                await self._say(
                    uid,
                    "Выберите действие кнопками.\n/menu — главное меню\n"
                    "/tasks — таски\n/consultations — консультации\n/dialogs — диалоги\n"
                    "/profile — профиль\n/cancel — отменить ввод\n/logout — выйти",
                )
            elif command in routes:
                await self._action(uid, session, routes[command], {}, update_id)
            else:
                await self._say(uid, "Неизвестная команда. /menu — главное меню.")
            return
        previous = self.store.get_submission(uid, update_id)
        if previous:
            await self._execute_submission(uid, session, previous)
            return
        if callback:
            raw = callback.get("data", "")
            button = self.store.get_callback(uid, raw[2:]) if raw.startswith("c:") else None
            if button is None:
                raise UserError("Кнопка устарела или принадлежит другой сессии. Откройте /menu.")
            await self._action(uid, session, button["action"], button["data"], update_id)
        elif text is not None:
            if await self._questions_text(uid, session, text, update_id, flow):
                return
            if await self._dialog_text(uid, session, text, update_id, flow):
                return
            await self._text(uid, session, text, update_id, flow)
        else:
            await self._say(uid, "Сейчас поддерживаются текстовые сообщения. Откройте /menu.")

    async def _login_menu(self, uid):
        await self._say(
            uid,
            "Valeria: войдите, чтобы запускать таски, задавать вопросы и вести диалоги.",
            [[("Войти", "login", {})]],
        )

    async def _begin_login(self, uid):
        await self._logout(uid, announce=False)
        self._flow(uid, "login_name")
        await self._say(uid, "Введите логин Valeria. /cancel — отмена.")

    async def _login_text(self, uid, message, text, flow):
        if flow["kind"] == "login_name":
            login = text.strip()
            if not 1 <= len(login) <= 80:
                raise UserError("Введите логин длиной от 1 до 80 символов.")
            self._flow(uid, "login_password", {"login": login})
            await self._say(
                uid, "Введите пароль Valeria. После получения бот попробует удалить сообщение."
            )
            return
        if not 1 <= len(text) <= 1024:
            raise UserError("Некорректная длина пароля. Повторите ввод.")
        async with self._api_factory(None) as client:
            logged_in = await client.login(flow["data"]["login"], text)
        self.store.clear_user(uid)
        self.store.save_session(
            Session(
                telegram_id=uid,
                chat_id=uid,
                session_token=logged_in["session_token"],
                csrf_token=logged_in["csrf_token"],
                user=logged_in["user"],
            )
        )
        await self._home(uid, self.store.get_session(uid))

    async def _logout(self, uid, *, announce=True):
        session = self.store.get_session(uid)
        if session:
            with suppress(ValeriaError):
                await self._api_request(session, "POST", "api/auth/logout", expected_status=204)
        self.store.clear_user(uid)
        if announce:
            await self._say(uid, "Вы вышли. Для повторного входа используйте /login.")

    async def _expired(self, uid):
        self.store.clear_user(uid)
        await self._say(uid, "Сессия истекла или доступ изменился. Войдите снова: /login.")

    async def _home(self, uid, session):
        self.store.clear_flow(uid)
        rows = [[("Таски", "tasks", {}), ("Консультации", "consultations", {})]]
        if not session.user.get("tasks_only"):
            rows += [[("Диалоги", "dialog_menu", {}), ("Шаблоны", "templates", {})]]
        rows += [[("Профиль", "profile", {}), ("Выйти", "logout", {})]]
        hint = (
            ""
            if session.user.get("workspace_path")
            else "\nАдминистратор должен назначить рабочую папку."
        )
        await self._say(
            uid, f"Вы вошли как {session.user['login']}. Выберите действие.{hint}", rows
        )

    @staticmethod
    def _workspace(session):
        if not session.user.get("workspace_path"):
            raise UserError("Для создания нужна рабочая папка. Обратитесь к администратору.")

    async def _action(self, uid, session, action, data, update_id):
        if await self._questions_action(uid, session, action, data, update_id):
            return
        if await self._dialog_action(uid, session, action, data, update_id):
            return
        if action == "home":
            await self._home(uid, session)
        elif action == "logout":
            await self._logout(uid)
        elif action == "tasks":
            self.store.clear_flow(uid)
            await self._say(
                uid,
                "Таски: выберите действие.",
                [
                    [("Создать таску", "task_list", {}), ("Мои запуски", "run_list", {})],
                    [("Главное меню", "home", {})],
                ],
            )
        elif action == "task_list":
            items = await self._api_request(session, "GET", "api/tasks")
            await self._paged(uid, "Доступные таски", items, data, "task_list", "task_choose")
        elif action == "task_choose":
            task_id = resource_id(data["id"])
            tasks = await self._api_request(session, "GET", "api/tasks")
            task = next((item for item in tasks if item["id"] == task_id), None)
            if not task:
                raise UserError("Таска больше недоступна. Обновите список.")
            self._workspace(session)
            form = self._flow(uid, "task_input", {"task_id": task_id, "title": task["title"]})
            await self._say(
                uid,
                f"{task['title']}\n{task.get('description') or ''}\n\n"
                "Отправьте дополнение или запустите без дополнения.",
                [
                    [("Запустить без дополнения", "task_empty", {"flow_id": form["_flow_id"]})],
                    [("Отмена", "tasks", {})],
                ],
            )
        elif action == "task_empty":
            form = self._require_flow(uid, data, ("task_input",))
            await self._start_task(uid, session, update_id, form, "")
        elif action == "run_list":
            items = await self._api_request(session, "GET", "api/task-runs")
            await self._paged(uid, "Мои запуски", items, data, "run_list", "run_view")
        elif action == "run_view":
            await self._run_view(uid, session, resource_id(data["id"]))
        elif action == "run_result":
            result = await self._api_request(
                session, "GET", f"api/task-runs/{resource_id(data['id'])}/result"
            )
            await self._say(
                uid,
                result.get("text")
                or {
                    "not_completed": "Запуск ещё не завершён успешно.",
                    "unavailable": "Итоговое сообщение отсутствует.",
                }.get(result.get("status"), "Результат пока недоступен."),
            )
        elif action == "run_stop":
            run = await self._api_request(
                session,
                "POST",
                f"api/task-runs/{resource_id(data['id'])}/stop",
                expected_status=202,
            )
            await self._say(uid, "Состояние запуска: " + status_label(run["status"]))
            await self._run_view(uid, session, run["id"])
        elif action == "consultations":
            self.store.clear_flow(uid)
            await self._say(
                uid,
                "Консультации",
                [
                    [
                        ("Новая консультация", "consultation_new", {}),
                        ("Мои консультации", "consultation_list", {}),
                    ],
                    [("Главное меню", "home", {})],
                ],
            )
        elif action == "consultation_new":
            self._workspace(session)
            self._flow(uid, "consultation_new")
            await self._say(uid, "Введите первый вопрос для новой консультации. /cancel — отмена.")
        elif action == "consultation_list":
            items = await self._api_request(session, "GET", "api/consultations")
            await self._paged(
                uid, "Мои консультации", items, data, "consultation_list", "consultation_view"
            )
        elif action == "consultation_view":
            await self._consultation_view(uid, session, resource_id(data["id"]))
        elif action == "consultation_questions":
            if not await self._questions_start(
                uid, session, "consultation", resource_id(data["id"])
            ):
                await self._say(uid, "Ожидающих вопросов нет.")
        elif action == "consultation_send_saved":
            draft = self._require_flow(uid, data, ("consultation_draft",))
            await self._consultation_send(
                uid, session, update_id, draft["chat_id"], draft["message"]
            )
        elif action == "retry_submission":
            record = self.store.get_submission(uid, data["update_id"])
            if not record:
                raise UserError("Запрос больше недоступен. Проверьте историю.")
            await self._execute_submission(uid, session, record)
        elif action == "profile":
            await self._profile(uid, session)
        elif action == "profile_username":
            self._flow(uid, "profile_username")
            await self._say(uid, "Отправьте Mattermost username, можно с @. /cancel — отмена.")
        elif action == "profile_disconnect":
            user = await self._api_request(
                session, "PATCH", "api/profile", body={"mattermost_username": None}
            )
            session = replace(session, user=user)
            self.store.save_session(session)
            await self._profile(uid, session)
        else:
            raise UserError("Действие устарело. Откройте /menu.")

    async def _text(self, uid, session, text, update_id, flow):
        kind, data = flow.get("kind"), flow.get("data", {})
        if kind == "task_input":
            await self._start_task(uid, session, update_id, data, text)
        elif kind == "consultation_new":
            self._workspace(session)
            await self._submit(
                uid,
                session,
                update_id,
                "consultation_create",
                "api/consultations",
                {"message": self._message(text)},
                201,
            )
        elif kind in {"consultation_active", "consultation_draft"}:
            await self._consultation_send(uid, session, update_id, data["chat_id"], text)
        elif kind == "profile_username":
            name = text.strip()
            if not name or len(name) > 23:
                raise UserError("Введите Mattermost username длиной до 23 символов.")
            user = await self._api_request(
                session, "PATCH", "api/profile", body={"mattermost_username": name}
            )
            session = replace(session, user=user)
            self.store.save_session(session)
            self.store.clear_flow(uid)
            await self._profile(uid, session)
        elif kind == "pending_submission":
            await self._say(
                uid,
                "Предыдущая отправка ещё не подтверждена. "
                "Повторите её кнопкой или откройте историю.",
            )
        else:
            await self._say(uid, "Выберите действие в /menu или откройте нужный диалог.")

    @staticmethod
    def _message(text):
        if not text.strip() or len(text) > 100_000:
            raise UserError("Введите непустой текст длиной до 100 000 символов.")
        return text

    async def _start_task(self, uid, session, update_id, data, text):
        self._workspace(session)
        if len(text) > 100_000:
            raise UserError("Дополнение слишком длинное.")
        await self._submit(
            uid,
            session,
            update_id,
            "task_run",
            f"api/tasks/{resource_id(data['task_id'])}/runs",
            {"user_input": text},
            202,
        )

    async def _paged(self, uid, title, items, data, list_action, select_action):
        page = max(0, min(int(data.get("page", 0)), max(0, (len(items) - 1) // PAGE_SIZE)))
        rows = [
            [
                (
                    str(item.get("title") or item.get("name") or item["id"])[:60],
                    select_action,
                    {"id": item["id"]},
                )
            ]
            for item in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        ]
        navigation = []
        if page:
            navigation.append(("←", list_action, {"page": page - 1}))
        if (page + 1) * PAGE_SIZE < len(items):
            navigation.append(("→", list_action, {"page": page + 1}))
        if navigation:
            rows.append(navigation)
        rows.append([("Главное меню", "home", {})])
        await self._say(
            uid, f"{title}\nСтраница {page + 1}" if items else f"{title}\nПока пусто.", rows
        )

    async def _run_view(self, uid, session, rid):
        run = await self._api_request(session, "GET", f"api/task-runs/{rid}")
        rows = [[("Обновить", "run_view", {"id": rid}), ("Результат", "run_result", {"id": rid})]]
        if run["status"] not in TERMINAL and run["status"] != "cancelling":
            rows.append([("Остановить", "run_stop", {"id": rid})])
        rows.append([("Мои запуски", "run_list", {}), ("Главное меню", "home", {})])
        await self._say(
            uid,
            f"{run['title']}\n{status_label(run['status'])}\n"
            f"Этап: {run.get('phase', 'build')}\nСоздано: {run.get('created_at', '—')}",
            rows,
        )
        if run["status"] not in TERMINAL or self.store.get_watch(uid, "task", rid) is not None:
            await self._watch_resource(uid, session, "task", rid)

    async def _consultation_view(self, uid, session, rid, history=True):
        detail = await self._api_request(session, "GET", f"api/consultations/{rid}")
        self._flow(uid, "consultation_active", {"chat_id": rid})
        if history:
            await self._say(uid, history_text(detail.get("messages", [])))
        await self._say(
            uid,
            f"{detail['title']}\n{status_label(detail['status'])}\n"
            "Отправьте следующий вопрос текстовым сообщением.",
            [
                [
                    ("Обновить и история", "consultation_view", {"id": rid}),
                    ("Вопросы агента", "consultation_questions", {"id": rid}),
                ],
                [("Мои консультации", "consultation_list", {}), ("Главное меню", "home", {})],
            ],
        )
        await self._watch_resource(uid, session, "consultation", rid, detail if history else None)

    async def _consultation_send(self, uid, session, update_id, rid, text):
        rid, text = resource_id(rid), self._message(text)
        self._workspace(session)
        if await self._questions_start(uid, session, "consultation", rid, draft={"message": text}):
            return
        await self._submit(
            uid,
            session,
            update_id,
            "consultation_message",
            f"api/consultations/{rid}/messages",
            {"message": text},
            202,
            {"chat_id": rid},
        )

    async def _profile(self, uid, session):
        user = session.user
        self.store.clear_flow(uid)
        rows = [[("Mattermost username", "profile_username", {})]]
        if user.get("mattermost_user_id") or user.get("mattermost_username"):
            rows.append([("Отключить Mattermost", "profile_disconnect", {})])
        rows.append([("Главное меню", "home", {})])
        await self._say(
            uid,
            f"Профиль: {user['login']}\n"
            f"Режим: {'Таски и консультации' if user.get('tasks_only') else 'Пользователь'}\n"
            f"Рабочая папка: {user.get('workspace_path') or 'Не назначена'}\n"
            f"Лимит задач: {user.get('max_active_jobs') or 'Общий'}\n"
            f"Mattermost: {user.get('mattermost_username') or 'Не настроен'}",
            rows,
        )

    async def _submit(
        self, uid, session, update_id, kind, path, body, expected_status, context=None
    ):
        record = self.store.save_submission(
            uid,
            update_id,
            valeria_user_id=session.user["id"],
            path=path,
            body=body,
            expected_status=expected_status,
            key=f"telegram:{self.settings.bot_token.split(':', 1)[0]}:{uid}:{update_id}",
            kind=kind,
            context=context or {},
        )
        await self._execute_submission(uid, session, record)

    async def _execute_submission(self, uid, session, record):
        if record["valeria_user_id"] != session.user["id"]:
            raise UserError("Запрос принадлежит другой учётной записи. Откройте новое действие.")
        result = record["result"]
        if result is None:
            try:
                result = await self._api_request(
                    session,
                    "POST",
                    record["path"],
                    body=record["body"],
                    expected_status=record["expected_status"],
                    idempotency_key=record["key"],
                )
            except ValeriaError as exc:
                if exc.status_code == 401:
                    raise
                self._flow(uid, "pending_submission", {"update_id": record["update_id"]})
                await self._say(
                    uid,
                    str(exc) + "\nПовтор использует исходный запрос без создания дубля.",
                    [
                        [
                            (
                                "Повторить отправку",
                                "retry_submission",
                                {"update_id": record["update_id"]},
                            )
                        ],
                        [("Главное меню", "home", {})],
                    ],
                )
                return
            if not isinstance(result, dict):
                raise UserError("Не удалось подтвердить отправку. Повторите исходную отправку.")
            self.store.complete_submission(uid, record["update_id"], result)
        kind = record["kind"]
        self.store.clear_flow(uid)
        if kind == "task_run":
            await self._watch_resource(uid, session, "task", resource_id(result["id"]))
            await self._say(uid, "Таска принята: " + status_label(result["status"]))
            await self._run_view(uid, session, resource_id(result["id"]))
        elif kind in {"consultation_create", "consultation_message"}:
            rid = result["id"] if kind == "consultation_create" else record["context"]["chat_id"]
            await self._watch_resource(uid, session, "consultation", resource_id(rid))
            await self._say(uid, "Вопрос принят. Ответ появится здесь после подготовки.")
            await self._consultation_view(uid, session, resource_id(rid), history=False)
        elif kind == "dialog_create":
            await self._watch_resource(uid, session, "dialog", resource_id(result["chat"]["id"]))
            await self._say(uid, "Диалог создан.")
            await self._dialog_view(uid, session, resource_id(result["chat"]["id"]), history=False)
        elif kind == "dialog_message":
            await self._watch_resource(
                uid, session, "dialog", resource_id(record["context"]["chat_id"])
            )
            await self._say(
                uid, "Сообщение принято: " + status_label(result.get("status", "queued"))
            )
            await self._dialog_view(
                uid, session, resource_id(record["context"]["chat_id"]), history=False
            )

    async def _view_resource(self, uid, session, kind, rid, history=False):
        if kind == "dialog":
            await self._dialog_view(uid, session, rid, history=history)
        else:
            await self._consultation_view(uid, session, rid, history=history)

    async def _restore_draft(self, uid, session, kind, rid, draft):
        if not draft:
            return
        if kind == "dialog":
            await self._dialog_restore_draft(uid, session, rid, draft)
            return
        form = self._flow(uid, "consultation_draft", {"chat_id": rid, **draft})
        await self._say(
            uid,
            "Ваш следующий вопрос сохранён. Отправить его?",
            [
                [("Отправить вопрос", "consultation_send_saved", {"flow_id": form["_flow_id"]})],
                [("Отмена", "consultations", {})],
            ],
        )

    async def _watch_resource(self, uid, session, kind, resource_id, snapshot=None):
        current = self.store.get_watch(uid, kind, resource_id) or {}
        current["paused"] = False
        if snapshot:
            source = snapshot.get("chat", snapshot)
            current["last_ids"] = [
                message_id(m)
                for m in snapshot.get("messages", [])
                if message_id(m)
                and message_role(m) == "assistant"
                and (
                    kind != "dialog"
                    or m.get("info", {}).get("time", {}).get("completed")
                    or source.get("status") in TERMINAL
                )
            ]
            current["status"] = source.get("status", "idle")
        self.store.upsert_watch(uid, kind, resource_id, current)

    async def poll_notifications(self):
        by_user = defaultdict(list)
        for watch in self.store.list_watches():
            if watch["data"].get("paused"):
                continue
            by_user[watch["telegram_id"]].append(watch)
        limit = asyncio.Semaphore(8)

        async def poll_user(uid, watches):
            if self._locks[uid].locked():
                return
            async with limit, self._locks[uid]:
                session = self.store.get_session(uid)
                if not session:
                    return
                try:
                    for watch in watches:
                        try:
                            await self._poll_watch(uid, session, watch)
                        except ValeriaError as exc:
                            if exc.status_code == 401:
                                raise
                            if exc.status_code in {403, 404}:
                                self.store.remove_watch(uid, watch["kind"], watch["resource_id"])
                        except TelegramError as exc:
                            if exc.error_code == 403:
                                self.store.remove_watch(uid, watch["kind"], watch["resource_id"])
                except ValeriaError:
                    await self._expired(uid)

        await asyncio.gather(*(poll_user(uid, watches) for uid, watches in by_user.items()))

    async def _poll_watch(self, uid, session, watch):
        kind, rid, state = watch["kind"], resource_id(watch["resource_id"]), watch["data"]
        if kind == "task":
            detail = await self._api_request(session, "GET", f"api/task-runs/{rid}")
            messages = []
            action = "run_view"
        elif kind == "consultation":
            detail = await self._api_request(session, "GET", f"api/consultations/{rid}")
            messages = detail.get("messages", [])
            action = "consultation_view"
        else:
            detail = await self._api_request(session, "GET", f"api/chats/{rid}")
            messages = await self._api_request(session, "GET", f"api/chats/{rid}/messages")
            action = "dialog_view"
        status = detail.get("status", "idle")
        buttons = [[("Открыть", action, {"id": rid})]]
        if state.get("status") != status:
            await self._say(
                uid, f"{detail.get('title', 'Задание')}\n{status_label(status)}", buttons
            )
            state["status"] = status
            self.store.upsert_watch(uid, kind, rid, state)
        seen = set(state.get("last_ids", []))
        for message in messages:
            mid = message_id(message)
            if not mid or mid in seen or message_role(message) != "assistant":
                continue
            info = message.get("info", {})
            if kind == "dialog" and not (
                info.get("time", {}).get("completed") or status in TERMINAL
            ):
                continue
            text = message_text(message)
            if text:
                await self._say(uid, f"{detail.get('title', 'Valeria')}\n\n{text}", buttons)
                seen.add(mid)
                state["last_ids"] = sorted(seen)
                self.store.upsert_watch(uid, kind, rid, state)
        state["last_ids"] = sorted(seen)
        if kind == "task" and status == "completed":
            result = await self._api_request(session, "GET", f"api/task-runs/{rid}/result")
            if result["status"] == "not_completed":
                self.store.upsert_watch(uid, kind, rid, state)
                return
            await self._say(
                uid, result.get("text") or "Таска завершена без итогового сообщения.", buttons
            )
            self.store.remove_watch(uid, kind, rid)
            return
        if kind == "task" and status in {"failed", "cancelled"}:
            self.store.remove_watch(uid, kind, rid)
            return
        if kind != "task":
            prefix = "chats" if kind == "dialog" else "consultations"
            questions = await self._api_request(session, "GET", f"api/{prefix}/{rid}/questions")
            permissions = (
                await self._api_request(session, "GET", f"api/chats/{rid}/permissions")
                if kind == "dialog"
                else []
            )
            notice = hashlib.sha256(
                json.dumps([questions, permissions], sort_keys=True).encode()
            ).hexdigest()
            if (questions or permissions) and notice != state.get("notice"):
                button = "dialog_questions" if kind == "dialog" else "consultation_questions"
                rows = [[("Ответить на вопросы", button, {"id": rid})]] if questions else []
                if permissions:
                    rows.append([("Разрешения", "dialog_permissions", {"id": rid})])
                await self._say(uid, "Valeria ожидает вашего ответа или разрешения.", rows)
            state["notice"] = notice
            # Keep the delivery cursor, but stop waking completed environments.
            # Opening the conversation or sending another message resumes it.
            state["paused"] = status in {*TERMINAL, "idle"} and not (questions or permissions)
        self.store.upsert_watch(uid, kind, rid, state)
