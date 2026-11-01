"""Inline interactive questions and permissions for the user conversation flows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .errors import UserError
from .presentation import resource_id as checked_resource_id
from .valeria_client import ValeriaError

if TYPE_CHECKING:
    from .store import Session

_QUESTION_KINDS = ("question", "question_custom", "question_pending")


def _question_path(kind: str, resource_id: str) -> str:
    if kind not in {"dialog", "consultation"}:
        raise UserError("Этот раздел не поддерживает уточняющие вопросы.")
    prefix = "chats" if kind == "dialog" else "consultations"
    return f"api/{prefix}/{checked_resource_id(resource_id)}/questions"


def _questions(payload: Any) -> list[dict[str, Any]]:
    """Keep only the API fields needed to display and validate an answer."""
    if not isinstance(payload, list):
        raise UserError("Не удалось прочитать вопросы. Обновите диалог.")
    requests = []
    for request in payload:
        if not isinstance(request, dict) or not isinstance(request.get("questions"), list):
            continue
        groups = []
        for group in request["questions"]:
            if not isinstance(group, dict) or not isinstance(group.get("question"), str):
                continue
            options = []
            for option in group.get("options", []):
                if isinstance(option, dict) and isinstance(option.get("label"), str):
                    options.append(
                        {
                            "label": option["label"],
                            "description": str(option.get("description") or ""),
                        }
                    )
            groups.append(
                {
                    "question": group["question"],
                    "header": str(group.get("header") or ""),
                    "multiple": bool(group.get("multiple", False)),
                    "custom": group.get("custom", True) is not False,
                    "options": options,
                }
            )
        if groups:
            requests.append({"id": checked_resource_id(request.get("id")), "questions": groups})
    return requests


class QuestionFlows:
    """Mixin using the bot's authenticated API, durable flow store and button helpers."""

    async def _questions_start(
        self,
        uid: int,
        session: Session,
        kind: str,
        resource_id: str,
        draft: dict[str, Any] | None = None,
    ) -> bool:
        path = _question_path(kind, resource_id)
        pending = _questions(await self._api_request(session, "GET", path))
        if not pending:
            return False
        request = pending[0]
        data = {
            "kind": kind,
            "resource_id": checked_resource_id(resource_id),
            "request_id": request["id"],
            "questions": request["questions"],
            "answers": [[] for _ in request["questions"]],
            "index": 0,
            "selected": [],
        }
        if draft and isinstance(draft.get("message"), str):
            data["draft"] = {
                key: value
                for key, value in draft.items()
                if key in {"message", "agent", "model", "model_variant"}
                and (value is None or isinstance(value, str))
            }
        if len(pending) > 1:
            await self._say(uid, "Есть несколько запросов. Ответим на них по очереди.")
        await self._question_render(uid, data)
        return True

    async def _question_render(self, uid: int, data: dict[str, Any]) -> None:
        data = self._flow(uid, "question", data)
        index = data["index"]
        group = data["questions"][index]
        selected = data["selected"]
        title = group["header"] or f"Вопрос {index + 1}"
        lines = [f"{title} ({index + 1}/{len(data['questions'])})", group["question"]]
        lines.append(
            "Можно выбрать несколько вариантов." if group["multiple"] else "Выберите один вариант."
        )
        buttons = []
        flow_data = {"flow_id": data["_flow_id"]}
        for option_index, option in enumerate(group["options"]):
            label = option["label"]
            description = option["description"]
            if description:
                lines.append(f"• {label}: {description}")
            mark = "✓ " if label in selected else ""
            buttons.append(
                [
                    (
                        f"{mark}{label}"[:64],
                        "question_option",
                        {
                            **flow_data,
                            "option": option_index,
                        },
                    )
                ]
            )
        if group["custom"]:
            buttons.append([("Свой ответ", "question_custom", flow_data)])
        if selected:
            lines.append("Выбрано: " + "; ".join(selected))
        next_title = "Отправить ответы" if index + 1 == len(data["questions"]) else "Далее"
        buttons.append([(next_title, "question_next", flow_data)])
        if index:
            buttons.append([("Предыдущий вопрос", "question_back", flow_data)])
        await self._say(uid, "\n\n".join(lines), buttons)

    async def _questions_action(
        self,
        uid: int,
        session: Session,
        action: str,
        data: dict[str, Any],
        update_id: int,
    ) -> bool:
        if action.startswith("permission_"):
            return await self._permission_action(uid, session, action, data)
        if not action.startswith("question_"):
            return False
        allowed = ("question_pending",) if action == "question_retry" else _QUESTION_KINDS[:2]
        flow = self._require_flow(uid, data, allowed)
        if action == "question_retry":
            await self._question_send(uid, session, flow, update_id)
            return True
        group = flow["questions"][flow["index"]]
        if action == "question_option":
            option_index = data.get("option")
            if (
                not isinstance(option_index, int)
                or isinstance(option_index, bool)
                or not 0 <= option_index < len(group["options"])
            ):
                raise UserError("Этот вариант ответа недоступен. Обновите вопрос.")
            label = group["options"][option_index]["label"]
            selected = flow["selected"]
            if group["multiple"]:
                flow["selected"] = (
                    [value for value in selected if value != label]
                    if label in selected
                    else [*selected, label]
                )
            else:
                flow["selected"] = [label]
            await self._question_render(uid, flow)
        elif action == "question_custom":
            if not group["custom"]:
                raise UserError("Для этого вопроса доступны только предложенные варианты.")
            flow = self._flow(uid, "question_custom", flow)
            await self._say(
                uid,
                "Введите свой ответ одним сообщением (до 10 000 символов).",
                [
                    [
                        (
                            "Вернуться к вариантам",
                            "question_cancel_custom",
                            {
                                "flow_id": flow["_flow_id"],
                            },
                        )
                    ],
                ],
            )
        elif action == "question_cancel_custom":
            await self._question_render(uid, flow)
        elif action in {"question_next", "question_back"}:
            if action == "question_next" and not flow["selected"]:
                raise UserError("Выберите вариант или введите свой ответ.")
            flow["answers"][flow["index"]] = list(flow["selected"])
            if action == "question_next" and flow["index"] + 1 == len(flow["questions"]):
                await self._question_send(uid, session, flow, update_id)
            else:
                flow["index"] += 1 if action == "question_next" else -1
                if flow["index"] < 0:
                    raise UserError("Вы уже на первом вопросе.")
                flow["selected"] = list(flow["answers"][flow["index"]])
                await self._question_render(uid, flow)
        else:
            raise UserError("Эта кнопка вопроса устарела. Обновите диалог.")
        return True

    async def _questions_text(
        self,
        uid: int,
        session: Session,
        text: str,
        update_id: int,
        flow: dict[str, Any],
    ) -> bool:
        if flow.get("kind") not in (*_QUESTION_KINDS, "permissions"):
            return False
        if flow["kind"] != "question_custom":
            prompt = {
                "question": "Используйте кнопки вопроса. Для текста выберите «Свой ответ».",
                "question_pending": "Ответы сохранены. Используйте кнопку повтора отправки.",
                "permissions": "Используйте кнопки разрешения под запросом действия.",
            }[flow["kind"]]
            await self._say(uid, prompt)
            return True
        data = flow["data"]
        # Compare with the current durable version before accepting text from a
        # handler holding an older snapshot of a flow.
        data = self._require_flow(uid, {"flow_id": data.get("_flow_id")}, ("question_custom",))
        value = text.strip()
        if not value or len(value) > 10_000:
            raise UserError("Ответ должен содержать от 1 до 10 000 символов.")
        group = data["questions"][data["index"]]
        if not group["custom"]:
            raise UserError("Для этого вопроса доступны только предложенные варианты.")
        if group["multiple"]:
            if value not in data["selected"]:
                data["selected"].append(value)
        else:
            data["selected"] = [value]
        await self._question_render(uid, data)
        return True

    async def _question_send(
        self,
        uid: int,
        session: Session,
        data: dict[str, Any],
        update_id: int,
    ) -> None:
        path = _question_path(data["kind"], data["resource_id"])
        data = self._flow(uid, "question_pending", data)
        try:
            pending = _questions(await self._api_request(session, "GET", path))
            request = next((item for item in pending if item["id"] == data["request_id"]), None)
            if request is None:
                await self._say(uid, "Вопрос уже закрыт. Состояние диалога обновлено.")
                await self._question_finished(uid, session, data)
                return
            if request["questions"] != data["questions"]:
                await self._say(uid, "Вопрос изменился. Ответьте на обновлённый вопрос.")
                started = await self._questions_start(
                    uid,
                    session,
                    data["kind"],
                    data["resource_id"],
                    data.get("draft"),
                )
                if not started:
                    await self._question_finished(uid, session, data)
                return
            result = await self._api_request(
                session,
                "POST",
                f"{path}/{data['request_id']}/reply",
                body={"answers": data["answers"]},
            )
            if not isinstance(result, dict) or result.get("accepted") is not True:
                raise ValeriaError("Ответ не подтверждён. Обновите вопрос и повторите попытку.")
        except ValeriaError as exc:
            if exc.status_code == 401:
                raise
            if exc.status_code == 409:
                await self._say(uid, "Вопрос уже закрыт. Состояние диалога обновлено.")
                await self._question_finished(uid, session, data)
                return
            if exc.status_code == 422:
                await self._say(uid, "Ответ не принят. Проверьте выбранные варианты.")
                await self._question_render(uid, data)
                return
            await self._say(
                uid,
                f"{exc}\n\nОтветы сохранены. Можно повторить отправку.",
                [
                    [
                        (
                            "Повторить отправку ответа",
                            "question_retry",
                            {
                                "flow_id": data["_flow_id"],
                            },
                        )
                    ],
                ],
            )
            return
        await self._say(uid, "Ответ отправлен. Работа продолжается.")
        await self._question_finished(uid, session, data)

    async def _question_finished(self, uid: int, session: Session, data: dict[str, Any]) -> None:
        self.store.clear_flow(uid)
        await self._view_resource(uid, session, data["kind"], data["resource_id"])
        if data.get("draft"):
            await self._restore_draft(
                uid,
                session,
                data["kind"],
                data["resource_id"],
                data["draft"],
            )

    async def _permissions(self, uid: int, session: Session, chat_id: str) -> None:
        chat_id = checked_resource_id(chat_id)
        pending = await self._api_request(session, "GET", f"api/chats/{chat_id}/permissions")
        if not isinstance(pending, list):
            raise UserError("Не удалось прочитать разрешения. Обновите диалог.")
        permissions = {}
        for item in pending:
            if isinstance(item, dict):
                always = item.get("always")
                permissions[checked_resource_id(item.get("id"))] = isinstance(
                    always, list
                ) and bool(always)
        if not permissions:
            await self._say(uid, "Сейчас нет действий, ожидающих разрешения.")
            return
        flow = self._flow(uid, "permissions", {"chat_id": chat_id, "permissions": permissions})
        for item in pending:
            if not isinstance(item, dict):
                continue
            permission_id = checked_resource_id(item.get("id"))
            patterns = item.get("patterns")
            details = (
                "\n".join(str(value) for value in patterns) if isinstance(patterns, list) else ""
            )
            if not details:
                details = json.dumps(item.get("metadata") or {}, ensure_ascii=False, indent=2)
            text = f"Требуется разрешение: {item.get('permission') or 'действие'}\n\n{details}"
            payload = {"flow_id": flow["_flow_id"], "permission_id": permission_id}
            buttons = [[("Разрешить один раз", "permission_once", payload)]]
            if permissions[permission_id]:
                buttons.append([("Всегда разрешать", "permission_always", payload)])
            buttons.append([("Отклонить", "permission_reject", payload)])
            await self._say(uid, text, buttons)

    async def _permission_action(
        self,
        uid: int,
        session: Session,
        action: str,
        data: dict[str, Any],
    ) -> bool:
        flow = self._require_flow(uid, data, ("permissions",))
        response = action.removeprefix("permission_")
        permission_id = checked_resource_id(data.get("permission_id"))
        if (
            response not in {"once", "always", "reject"}
            or permission_id not in flow["permissions"]
            or (response == "always" and not flow["permissions"][permission_id])
        ):
            raise UserError("Это разрешение недоступно. Обновите диалог.")
        chat_id = checked_resource_id(flow["chat_id"])
        result = await self._api_request(
            session,
            "POST",
            f"api/chats/{chat_id}/permissions/{permission_id}",
            body={"response": response, "remember": response == "always"},
        )
        accepted = isinstance(result, dict) and result.get("accepted") is True
        self.store.clear_flow(uid)
        await self._say(
            uid,
            "Решение отправлено."
            if accepted
            else "Решение не подтверждено. Проверьте разрешения снова.",
        )
        await self._view_resource(uid, session, "dialog", chat_id)
        return True
