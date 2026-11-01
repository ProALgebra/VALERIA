"""Plain-text projections for Telegram; no untrusted HTML or Markdown parsing."""

import json
import re
from typing import Any

from .errors import UserError

TERMINAL = frozenset({"completed", "failed", "cancelled"})
STATUSES = {
    "queued": "В очереди",
    "dispatching": "Подготовка окружения",
    "running": "В работе",
    "waiting_question": "Ожидает вашего ответа",
    "waiting_permission": "Ожидает разрешения",
    "cancelling": "Останавливается",
    "completed": "Завершено",
    "failed": "Завершено с ошибкой",
    "cancelled": "Отменено",
    "idle": "Готов к работе",
}


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise UserError("Некорректный идентификатор. Откройте список заново.")
    return value


def status_label(value: str) -> str:
    return STATUSES.get(value, value or "Готов к работе")


def message_text(message: dict[str, Any], *, details: bool = False) -> str:
    if isinstance(message.get("text"), str):
        return message["text"]
    parts = []
    for part in message.get("parts", []):
        if part.get("ignored") or part.get("synthetic"):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif details and kind == "reasoning" and isinstance(part.get("text"), str):
            parts.append("Рассуждение:\n" + part["text"])
        elif details and kind == "tool":
            parts.append("Инструмент:\n" + json.dumps(part, ensure_ascii=False, indent=2))
    return "\n".join(parts).strip()


def message_role(message: dict[str, Any]) -> str:
    return message.get("role") or message.get("info", {}).get("role", "assistant")


def message_id(message: dict[str, Any]) -> str:
    return str(message.get("id") or message.get("info", {}).get("id") or "")


def history_text(messages: list[dict[str, Any]], *, details: bool = False) -> str:
    lines = []
    for message in messages:
        text = message_text(message, details=details)
        if text:
            role = "Вы" if message_role(message) == "user" else "Valeria"
            lines.append(f"{role}:\n{text}")
    return "\n\n".join(lines) or "Сообщений пока нет."
