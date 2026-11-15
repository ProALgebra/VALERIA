from __future__ import annotations

from pathlib import Path
from typing import Any

from .backends import OpenCodeBackend


def public_questions(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only the fields required to render and answer interactive questions."""
    result: list[dict[str, Any]] = []
    for request in requests:
        request_id = request.get("id")
        questions = request.get("questions")
        if not isinstance(request_id, str) or not isinstance(questions, list):
            continue
        visible = []
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("question"), str):
                continue
            options = []
            for option in question.get("options", []):
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    continue
                item = {"label": option["label"]}
                if isinstance(option.get("description"), str):
                    item["description"] = option["description"]
                options.append(item)
            item = {
                "question": question["question"],
                "multiple": bool(question.get("multiple", False)),
                "custom": question.get("custom", True) is not False,
                "options": options,
            }
            if isinstance(question.get("header"), str):
                item["header"] = question["header"]
            visible.append(item)
        if visible:
            result.append({"id": request_id, "questions": visible})
    return result


async def session_questions(
    client: OpenCodeBackend, session_id: str, workspace: str
) -> list[dict[str, Any]]:
    """Only requests for this session or verified descendants in its workspace."""
    pending = await client.questions()
    ancestry: dict[str, bool] = {session_id: True}
    directory = Path(workspace).resolve()

    async def belongs(candidate: str) -> bool:
        chain: list[str] = []
        current = candidate
        for _ in range(64):
            if current in ancestry:
                result = ancestry[current]
                break
            if current in chain:
                result = False
                break
            chain.append(current)
            info = await client.session(current)
            if info.get("id") != current or Path(info.get("directory", "")).resolve() != directory:
                result = False
                break
            parent = info.get("parentID")
            if not isinstance(parent, str) or not parent:
                result = False
                break
            current = parent
        else:
            result = False
        for item in chain:
            ancestry[item] = result
        return result

    result = []
    for request in pending:
        candidate = request.get("sessionID")
        if isinstance(candidate, str) and await belongs(candidate):
            result.append(request)
    return result


def validate_answers(request: dict[str, Any], answers: list[list[str]]) -> None:
    questions = request.get("questions")
    if not isinstance(questions, list) or not questions or len(answers) != len(questions):
        raise ValueError("Ответьте на каждый вопрос")
    for question, chosen in zip(questions, answers, strict=True):
        if not chosen or any(not value.strip() or len(value) > 10_000 for value in chosen):
            raise ValueError("Выберите вариант или введите ответ")
        if len(set(chosen)) != len(chosen):
            raise ValueError("В ответе повторяются варианты")
        if not question.get("multiple", False) and len(chosen) != 1:
            raise ValueError("Для этого вопроса можно выбрать только один ответ")
        labels = {option["label"] for option in question.get("options", [])}
        if question.get("custom", True) is False and any(value not in labels for value in chosen):
            raise ValueError("Можно выбрать только предложенные варианты")


def question_event(workspace: str, session_id: str) -> dict[str, str]:
    # Contents are fetched through the authorized chat endpoint, not broadcast.
    return {"type": "chat.questions.updated", "workspace": workspace, "session_id": session_id}
