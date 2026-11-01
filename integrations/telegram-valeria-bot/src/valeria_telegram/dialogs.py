"""User dialogs, model selection, templates and conversation history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import UserError
from .presentation import history_text, resource_id, status_label

if TYPE_CHECKING:
    from .store import Session

_PAGE_SIZE = 8
_SETTINGS = ("agent", "model", "model_variant")


def _page(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise UserError("Эта страница недоступна. Откройте список снова.")
    return value


def _history(messages: Any, *, details: bool = False) -> str:
    if not isinstance(messages, list):
        raise UserError("Не удалось прочитать историю диалога.")
    return history_text(messages, details=details)


def _settings(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.get("settings", {}).items() if key in _SETTINGS}


class DialogFlows:
    """Mixin using authenticated API requests and user-scoped durable forms."""

    def _dialog_allowed(self, session: Session) -> None:
        if session.user.get("tasks_only"):
            raise UserError("Для вашей учётной записи доступны таски и консультации.")
        if not session.user.get("workspace_path"):
            raise UserError("Для диалогов администратор должен назначить рабочую папку.")

    async def _dialogs_menu(self, uid: int, session: Session) -> None:
        self._dialog_allowed(session)
        self.store.clear_flow(uid)
        await self._say(
            uid,
            "Диалоги: создайте новый или продолжите существующий.",
            [
                [("Новый диалог", "dialog_new", {})],
                [("Мои диалоги", "dialog_list", {})],
                [("Шаблоны", "dialog_templates", {})],
                [("Главное меню", "home", {})],
            ],
        )

    async def _dialog_new(self, uid: int, session: Session) -> None:
        self._dialog_allowed(session)
        await self._dialog_setup(uid, session, {})

    async def _dialog_setup(self, uid: int, session: Session, data: dict[str, Any]) -> None:
        chat_id = data.get("chat_id")
        options = await self._api_request(
            session,
            "GET",
            "/api/options",
            params={"chat_id": resource_id(chat_id)} if chat_id else None,
        )
        if not isinstance(options, dict):
            raise UserError("Не удалось получить настройки диалога.")
        selected = {
            "agent": options.get("selected_agent") or options.get("default_agent"),
            "model": options.get("selected_model") or options.get("default_model"),
            "model_variant": options.get("selected_model_variant"),
            **_settings(data),
        }
        flow = self._flow(
            uid,
            "dialog_setup",
            {
                **data,
                "options": options,
                "settings": selected,
                "step": "agent",
            },
        )
        payload = {"flow_id": flow["_flow_id"]}
        buttons = [[("По умолчанию", "dialog_agent", {**payload, "agent": None})]]
        for agent in options.get("agents", []):
            if isinstance(agent, str):
                buttons.append([(agent[:64], "dialog_agent", {**payload, "agent": agent})])
        await self._say(uid, "Выберите агента для диалога.", buttons)

    async def _dialog_models(self, uid: int, data: dict[str, Any], page: int = 0) -> None:
        models = [
            model
            for model in data["options"].get("models", [])
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        ]
        page = min(_page(page), max(0, (len(models) - 1) // _PAGE_SIZE))
        flow = self._flow(uid, "dialog_setup", {**data, "step": "model"})
        payload = {"flow_id": flow["_flow_id"]}
        buttons = [[("По умолчанию", "dialog_model", {**payload, "model": None})]]
        for model in models[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]:
            caption = f"{model.get('provider') or ''} / {model.get('name') or model['id']}"
            buttons.append([(caption[:64], "dialog_model", {**payload, "model": model["id"]})])
        navigation = []
        if page:
            navigation.append(("Назад", "dialog_model_page", {**payload, "page": page - 1}))
        if (page + 1) * _PAGE_SIZE < len(models):
            navigation.append(("Далее", "dialog_model_page", {**payload, "page": page + 1}))
        if navigation:
            buttons.append(navigation)
        notice = "Выберите модель для диалога."
        if not models:
            notice += " Каталог сейчас недоступен; можно использовать модель по умолчанию."
        await self._say(uid, notice, buttons)

    async def _dialog_ready(self, uid: int, data: dict[str, Any]) -> None:
        if isinstance(data.get("message"), str):
            await self._dialog_draft(uid, data)
            return
        flow = self._flow(uid, "dialog_input", data)
        payload = {"flow_id": flow["_flow_id"]}
        buttons = [[("Выбрать шаблон", "dialog_templates", payload)]]
        if not data.get("chat_id"):
            buttons.append([("Создать пустой диалог", "dialog_empty", payload)])
        await self._say(uid, "Напишите сообщение для диалога (до 100 000 символов).", buttons)

    async def _dialog_list(self, uid: int, session: Session, page: int = 0) -> None:
        self._dialog_allowed(session)
        chats = await self._api_request(session, "GET", "/api/chats")
        if not isinstance(chats, list):
            raise UserError("Не удалось прочитать список диалогов.")
        page = min(_page(page), max(0, (len(chats) - 1) // _PAGE_SIZE))
        self.store.clear_flow(uid)
        buttons = []
        for chat in chats[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]:
            if isinstance(chat, dict):
                buttons.append(
                    [
                        (
                            str(chat.get("title") or "Диалог")[:64],
                            "dialog_open",
                            {
                                "chat_id": resource_id(chat.get("id")),
                            },
                        )
                    ]
                )
        navigation = []
        if page:
            navigation.append(("Назад", "dialog_list", {"page": page - 1}))
        if (page + 1) * _PAGE_SIZE < len(chats):
            navigation.append(("Далее", "dialog_list", {"page": page + 1}))
        if navigation:
            buttons.append(navigation)
        buttons.append([("Новый диалог", "dialog_new", {})])
        await self._say(uid, "Ваши диалоги:" if chats else "У вас пока нет диалогов.", buttons)

    async def _dialog_view(
        self, uid: int, session: Session, chat_id: str, history: bool = True
    ) -> None:
        self._dialog_allowed(session)
        chat_id = resource_id(chat_id)
        chat = await self._api_request(session, "GET", f"/api/chats/{chat_id}")
        if not isinstance(chat, dict):
            raise UserError("Не удалось открыть диалог.")
        jobs = await self._api_request(session, "GET", "/api/queue/me")
        if not isinstance(jobs, list):
            raise UserError("Не удалось прочитать очередь диалога.")
        relevant = [job for job in jobs if isinstance(job, dict) and job.get("chat_id") == chat_id]
        flow = self._flow(uid, "dialog_active", {"chat_id": chat_id})
        payload = {"flow_id": flow["_flow_id"], "chat_id": chat_id}
        lines = [str(chat.get("title") or "Диалог"), f"ID: {chat_id}"]
        for job in relevant:
            lines.append(f"Запуск {job.get('id')}: {status_label(job.get('status') or '')}")
        lines.append("Напишите сообщение, чтобы продолжить диалог.")
        buttons = [
            [("Обновить", "dialog_refresh", payload), ("История", "dialog_history", payload)],
            [("Агент и модель", "dialog_settings", payload)],
            [("Шаблоны", "dialog_templates", payload)],
            [
                ("Вопросы", "dialog_questions", payload),
                ("Разрешения", "dialog_permissions", payload),
            ],
            [("Подробности", "dialog_details", payload), ("MCP", "dialog_mcp", payload)],
            [("Остановить запуск", "dialog_stop", payload)],
            [("Мои диалоги", "dialog_list", {}), ("Главное меню", "home", {})],
        ]
        await self._say(uid, "\n".join(lines), buttons)
        if history:
            messages = await self._api_request(session, "GET", f"/api/chats/{chat_id}/messages")
            await self._say(uid, _history(messages))
            await self._watch_resource(
                uid,
                session,
                "dialog",
                chat_id,
                snapshot={"chat": chat, "messages": messages},
            )
        else:
            await self._watch_resource(uid, session, "dialog", chat_id)

    async def _dialog_templates(
        self, uid: int, session: Session, data: dict[str, Any], page: int = 0
    ) -> None:
        templates = await self._api_request(session, "GET", "/api/templates")
        if not isinstance(templates, list):
            raise UserError("Не удалось получить шаблоны.")
        page = min(_page(page), max(0, (len(templates) - 1) // _PAGE_SIZE))
        flow = self._flow(uid, "dialog_templates", data)
        payload = {"flow_id": flow["_flow_id"]}
        buttons = []
        for template in templates[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]:
            if isinstance(template, dict):
                buttons.append(
                    [
                        (
                            str(template.get("title") or "Шаблон")[:64],
                            "dialog_template",
                            {
                                **payload,
                                "template_id": resource_id(template.get("id")),
                            },
                        )
                    ]
                )
        navigation = []
        if page:
            navigation.append(("Назад", "dialog_template_page", {**payload, "page": page - 1}))
        if (page + 1) * _PAGE_SIZE < len(templates):
            navigation.append(("Далее", "dialog_template_page", {**payload, "page": page + 1}))
        if navigation:
            buttons.append(navigation)
        buttons.append([("Написать сообщение", "dialog_write", payload)])
        await self._say(
            uid, "Выберите шаблон:" if templates else "Опубликованных шаблонов нет.", buttons
        )

    async def _dialog_draft(self, uid: int, data: dict[str, Any]) -> None:
        flow = self._flow(uid, "dialog_draft", data)
        payload = {"flow_id": flow["_flow_id"]}
        await self._say(
            uid,
            f"Черновик сообщения:\n\n{data['message']}",
            [
                [("Отправить текст", "dialog_send_saved", payload)],
                [("Изменить текст", "dialog_edit", payload)],
                [("Агент и модель", "dialog_settings", payload)],
            ],
        )

    async def _dialog_restore_draft(
        self, uid: int, session: Session, chat_id: str, draft: dict[str, Any]
    ) -> None:
        self._dialog_allowed(session)
        settings = {key: draft[key] for key in _SETTINGS if key in draft}
        await self._dialog_draft(
            uid,
            {
                "chat_id": resource_id(chat_id),
                "message": draft["message"],
                "settings": settings,
            },
        )

    async def _dialog_send(
        self,
        uid: int,
        session: Session,
        update_id: int,
        data: dict[str, Any],
        message: str | None,
    ) -> None:
        body = _settings(data)
        if message is not None:
            message = message.strip()
            if not message or len(message) > 100_000:
                raise UserError("Сообщение должно содержать от 1 до 100 000 символов.")
            body["message"] = message
        chat_id = data.get("chat_id")
        if chat_id:
            chat_id = resource_id(chat_id)
            if await self._questions_start(uid, session, "dialog", chat_id, draft=body):
                return
            await self._submit(
                uid,
                session,
                update_id,
                "dialog_message",
                f"/api/chats/{chat_id}/messages",
                body,
                202,
                context={"chat_id": chat_id},
            )
        else:
            await self._submit(uid, session, update_id, "dialog_create", "/api/chats", body, 201)

    async def _dialog_stop_menu(self, uid: int, session: Session, flow: dict[str, Any]) -> None:
        chat_id = resource_id(flow["chat_id"])
        jobs = await self._api_request(session, "GET", "/api/queue/me")
        if not isinstance(jobs, list):
            raise UserError("Не удалось прочитать очередь диалога.")
        payload = {"flow_id": flow["_flow_id"], "chat_id": chat_id}
        buttons = []
        active = False
        for job in jobs:
            if not isinstance(job, dict) or job.get("chat_id") != chat_id:
                continue
            if job.get("status") == "queued":
                job_id = resource_id(job.get("id"))
                buttons.append(
                    [
                        (
                            f"Отменить в очереди: {job_id}"[:64],
                            "dialog_cancel_job",
                            {
                                **payload,
                                "job_id": job_id,
                            },
                        )
                    ]
                )
            elif job.get("status") in {
                "dispatching",
                "running",
                "waiting_permission",
                "waiting_question",
                "cancelling",
            }:
                active = True
        if active:
            buttons.append([("Остановить активный запуск", "dialog_abort", payload)])
        await self._say(
            uid,
            "Выберите запуск для остановки:"
            if buttons
            else "В этом диалоге нет активных запусков.",
            buttons or None,
        )

    async def _dialog_action(
        self, uid: int, session: Session, action: str, data: dict[str, Any], update_id: int
    ) -> bool:
        if action == "templates":
            action = "dialog_templates"
        if not action.startswith("dialog_"):
            return False
        self._dialog_allowed(session)
        if action == "dialog_menu":
            await self._dialogs_menu(uid, session)
            return True
        if action == "dialog_new":
            await self._dialog_new(uid, session)
            return True
        if action == "dialog_list":
            await self._dialog_list(uid, session, _page(data.get("page", 0)))
            return True
        if action in {"dialog_open", "dialog_view"}:
            chat_id = resource_id(data.get("chat_id") or data.get("id"))
            await self._dialog_view(uid, session, chat_id)
            return True
        if action in {"dialog_questions", "dialog_permissions"} and not data.get("flow_id"):
            chat_id = resource_id(data.get("id"))
            await self._dialog_view(uid, session, chat_id, history=False)
            if action == "dialog_permissions":
                await self._permissions(uid, session, chat_id)
            elif not await self._questions_start(uid, session, "dialog", chat_id):
                await self._say(uid, "Сейчас нет вопросов, ожидающих ответа.")
            return True
        if action == "dialog_templates" and not data.get("flow_id"):
            await self._dialog_templates(uid, session, {})
            return True
        flow = self._require_flow(
            uid,
            data,
            (
                "dialog_setup",
                "dialog_input",
                "dialog_active",
                "dialog_draft",
                "dialog_templates",
            ),
        )
        if action == "dialog_agent":
            if flow.get("step") != "agent":
                raise UserError("Выбор агента устарел. Откройте настройки снова.")
            agent = data.get("agent")
            if agent is not None and agent not in flow["options"].get("agents", []):
                raise UserError("Этот агент недоступен.")
            flow["settings"]["agent"] = agent
            await self._dialog_models(uid, flow)
        elif action in {"dialog_model", "dialog_model_page"}:
            if flow.get("step") != "model":
                raise UserError("Выбор модели устарел. Откройте настройки снова.")
            if action == "dialog_model_page":
                await self._dialog_models(uid, flow, _page(data.get("page", 0)))
            else:
                model = data.get("model")
                model_ids = {
                    item.get("id")
                    for item in flow["options"].get("models", [])
                    if isinstance(item, dict)
                }
                if model is not None and model not in model_ids:
                    raise UserError("Эта модель недоступна.")
                if model != flow["settings"].get("model"):
                    flow["settings"]["model_variant"] = None
                flow["settings"]["model"] = model
                await self._dialog_ready(uid, flow)
        elif action == "dialog_settings":
            await self._dialog_setup(uid, session, flow)
        elif action in {"dialog_templates", "dialog_template_page"}:
            await self._dialog_templates(uid, session, flow, _page(data.get("page", 0)))
        elif action == "dialog_template":
            template_id = resource_id(data.get("template_id"))
            templates = await self._api_request(session, "GET", "/api/templates")
            if not isinstance(templates, list):
                raise UserError("Не удалось получить шаблон.")
            template = next(
                (
                    item
                    for item in templates
                    if isinstance(item, dict) and item.get("id") == template_id
                ),
                None,
            )
            if template is None or not isinstance(template.get("content"), str):
                raise UserError("Этот шаблон больше недоступен.")
            await self._dialog_draft(uid, {**flow, "message": template["content"]})
        elif action in {"dialog_write", "dialog_edit"}:
            flow = self._flow(uid, "dialog_input", {**flow, "edit_draft": action == "dialog_edit"})
            await self._say(uid, "Введите новый текст сообщения (до 100 000 символов).")
        elif action == "dialog_send_saved":
            flow = self._require_flow(uid, data, ("dialog_draft",))
            await self._dialog_send(uid, session, update_id, flow, flow["message"])
        elif action == "dialog_empty":
            flow = self._require_flow(uid, data, ("dialog_input",))
            if flow.get("chat_id"):
                raise UserError("Этот диалог уже создан.")
            await self._dialog_send(uid, session, update_id, flow, None)
        else:
            flow = self._require_flow(uid, data, ("dialog_active",))
            chat_id = resource_id(flow["chat_id"])
            if data.get("chat_id") != chat_id:
                raise UserError("Эта кнопка относится к другому диалогу.")
            await self._dialog_active_action(uid, session, action, data, flow, chat_id)
        return True

    async def _dialog_active_action(
        self,
        uid: int,
        session: Session,
        action: str,
        data: dict[str, Any],
        flow: dict[str, Any],
        chat_id: str,
    ) -> None:
        if action in {"dialog_refresh", "dialog_history"}:
            await self._dialog_view(uid, session, chat_id, history=action == "dialog_history")
        elif action == "dialog_questions":
            if not await self._questions_start(uid, session, "dialog", chat_id):
                await self._say(uid, "Сейчас нет вопросов, ожидающих ответа.")
        elif action == "dialog_permissions":
            await self._permissions(uid, session, chat_id)
        elif action == "dialog_details":
            messages = await self._api_request(session, "GET", f"/api/chats/{chat_id}/messages")
            await self._say(uid, _history(messages, details=True))
        elif action == "dialog_mcp":
            status = await self._api_request(session, "GET", f"/api/chats/{chat_id}/mcp")
            if not isinstance(status, dict):
                raise UserError("Не удалось получить состояние MCP.")
            lines = [
                f"{item.get('name')}: {item.get('status')}"
                for item in status.get("servers", [])
                if isinstance(item, dict)
            ]
            await self._say(uid, "\n".join(lines) or "MCP-серверы ещё не подключены.")
        elif action == "dialog_stop":
            await self._dialog_stop_menu(uid, session, flow)
        elif action == "dialog_cancel_job":
            await self._api_request(
                session,
                "DELETE",
                f"/api/jobs/{resource_id(data.get('job_id'))}",
                expected_status=204,
            )
            await self._say(uid, "Запуск в очереди отменён.")
            await self._dialog_view(uid, session, chat_id, history=False)
        elif action == "dialog_abort":
            result = await self._api_request(
                session,
                "POST",
                f"/api/chats/{chat_id}/abort",
                expected_status=202,
            )
            stopped = isinstance(result, dict) and result.get("aborted") is True
            await self._say(
                uid,
                "Диалог остановлен."
                if stopped
                else "Остановка запрошена. Обновите состояние через несколько секунд.",
            )
            await self._dialog_view(uid, session, chat_id, history=False)
        else:
            raise UserError("Эта кнопка диалога устарела. Откройте диалог снова.")

    async def _dialog_text(
        self, uid: int, session: Session, text: str, update_id: int, flow: dict[str, Any]
    ) -> bool:
        if flow.get("kind") not in {"dialog_input", "dialog_active", "dialog_draft"}:
            return False
        self._dialog_allowed(session)
        data = self._require_flow(
            uid,
            {"flow_id": flow["data"].get("_flow_id")},
            ("dialog_input", "dialog_active", "dialog_draft"),
        )
        text = text.strip()
        if not text or len(text) > 100_000:
            raise UserError("Сообщение должно содержать от 1 до 100 000 символов.")
        if flow["kind"] == "dialog_draft" or data.get("edit_draft"):
            await self._dialog_draft(uid, {**data, "message": text, "edit_draft": False})
        else:
            await self._dialog_send(uid, session, update_id, data, text)
        return True
