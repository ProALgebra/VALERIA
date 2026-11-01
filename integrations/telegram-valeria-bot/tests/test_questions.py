from __future__ import annotations

import secrets
from copy import deepcopy

import pytest

from valeria_telegram.errors import UserError
from valeria_telegram.questions import QuestionFlows
from valeria_telegram.store import Session, Store
from valeria_telegram.valeria_client import ValeriaError


def request(*groups):
    return [{"id": "question_1", "questions": list(groups)}]


def group(*labels, multiple=False, custom=False, header="Выбор"):
    return {
        "header": header,
        "question": "Что сделать?",
        "multiple": multiple,
        "custom": custom,
        "options": [{"label": label, "description": f"Описание {label}"} for label in labels],
    }


class Bot(QuestionFlows):
    def __init__(self, store):
        self.store = store
        self.messages = []
        self.calls = []
        self.views = []
        self.restored = []
        self.pending = []
        self.post_result = {"accepted": True}
        self.post_errors = []
        self.get_error = None

    async def _say(self, uid, text, buttons=None):
        self.messages.append({"uid": uid, "text": text, "buttons": deepcopy(buttons or [])})

    def _flow(self, uid, kind, data):
        data = deepcopy(data)
        data["_flow_id"] = secrets.token_hex(8)
        self.store.set_flow(uid, kind, data)
        return data

    def _require_flow(self, uid, callbackdata, kinds):
        flow = self.store.get_flow(uid)
        if flow.get("kind") not in kinds or (
            flow.get("data", {}).get("_flow_id") != callbackdata.get("flow_id")
        ):
            raise UserError("Кнопка устарела")
        return deepcopy(flow["data"])

    async def _api_request(self, session, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **deepcopy(kwargs)})
        if method == "GET":
            if self.get_error:
                raise self.get_error
            return deepcopy(self.pending)
        if self.post_errors:
            raise self.post_errors.pop(0)
        return deepcopy(self.post_result)

    async def _view_resource(self, uid, session, kind, rid, history=False):
        self.views.append((uid, kind, rid))

    async def _restore_draft(self, uid, session, kind, rid, draft):
        self.restored.append((uid, kind, rid, draft))

    def button(self, action, *, option=None):
        for message in reversed(self.messages):
            for row in message["buttons"]:
                for caption, candidate, data in row:
                    if candidate == action and (option is None or data.get("option") == option):
                        return caption, candidate, data
        raise AssertionError(f"Button {action} missing")

    async def click(self, session, action, *, option=None, uid=1):
        _, action, data = self.button(action, option=option)
        return await self._questions_action(uid, session, action, data, 42)


@pytest.fixture
def session():
    return Session(1, 1, "private-session", "private-csrf", {"id": "user_1", "login": "alice"})


@pytest.fixture
def bot(tmp_path):
    with Store(tmp_path / "bot.sqlite3") as store:
        yield Bot(store)


async def test_single_answer_posts_labels_and_restores_only_message_draft(bot, session):
    bot.pending = request(group("Первый", "Второй"))
    await bot._questions_start(
        1,
        session,
        "dialog",
        "chat_1",
        draft={
            "message": "Продолжить работу",
            "password": "NO",
            "agent": "plan",
            "model": "provider/model",
            "model_variant": None,
        },
    )
    assert "Описание Первый" in bot.messages[-1]["text"]
    assert bot.calls[0]["path"] == "api/chats/chat_1/questions"
    assert "NO" not in str(bot.store.get_flow(1))
    await bot.click(session, "question_option", option=1)
    assert bot.store.get_flow(1)["data"]["selected"] == ["Второй"]
    await bot.click(session, "question_next")
    assert bot.calls[-1] == {
        "method": "POST",
        "path": "api/chats/chat_1/questions/question_1/reply",
        "body": {"answers": [["Второй"]]},
    }
    assert not bot.store.get_flow(1)
    assert bot.views == [(1, "dialog", "chat_1")]
    assert bot.restored == [
        (
            1,
            "dialog",
            "chat_1",
            {
                "message": "Продолжить работу",
                "agent": "plan",
                "model": "provider/model",
                "model_variant": None,
            },
        )
    ]


async def test_multiple_groups_toggle_options_and_custom_answer_without_duplicates(bot, session):
    bot.pending = request(group("A", "B", multiple=True, custom=True), group("C", "D"))
    await bot._questions_start(1, session, "consultation", "consultation_1")
    await bot.click(session, "question_option", option=0)
    await bot.click(session, "question_option", option=1)
    await bot.click(session, "question_option", option=0)
    assert bot.store.get_flow(1)["data"]["selected"] == ["B"]
    await bot.click(session, "question_custom")
    assert bot.store.get_flow(1)["kind"] == "question_custom"
    assert await bot._questions_text(1, session, "  Другой  ", 43, bot.store.get_flow(1))
    assert bot.store.get_flow(1)["data"]["selected"] == ["B", "Другой"]
    await bot.click(session, "question_custom")
    await bot._questions_text(1, session, "Другой", 44, bot.store.get_flow(1))
    assert bot.store.get_flow(1)["data"]["selected"] == ["B", "Другой"]
    await bot.click(session, "question_next")
    assert bot.store.get_flow(1)["data"]["index"] == 1
    await bot.click(session, "question_option", option=1)
    await bot.click(session, "question_next")
    assert bot.calls[-1] == {
        "method": "POST",
        "path": "api/consultations/consultation_1/questions/question_1/reply",
        "body": {"answers": [["B", "Другой"], ["D"]]},
    }


async def test_previous_group_preserves_answers_and_single_selection_replaces(bot, session):
    bot.pending = request(group("A", "B"), group("C", "D"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    await bot.click(session, "question_option", option=1)
    assert bot.store.get_flow(1)["data"]["selected"] == ["B"]
    await bot.click(session, "question_next")
    await bot.click(session, "question_option", option=0)
    await bot.click(session, "question_back")
    assert bot.store.get_flow(1)["data"]["selected"] == ["B"]
    await bot.click(session, "question_option", option=0)
    await bot.click(session, "question_next")
    assert bot.store.get_flow(1)["data"]["selected"] == ["C"]
    await bot.click(session, "question_next")
    assert bot.calls[-1]["body"] == {"answers": [["A"], ["C"]]}


async def test_empty_answer_and_disallowed_custom_cannot_be_submitted(bot, session):
    bot.pending = request(group("A", custom=False))
    await bot._questions_start(1, session, "dialog", "chat_1")
    with pytest.raises(UserError, match="Выберите"):
        await bot.click(session, "question_next")
    flow = bot.store.get_flow(1)["data"]
    with pytest.raises(UserError, match="предложенные"):
        await bot._questions_action(
            1,
            session,
            "question_custom",
            {"flow_id": flow["_flow_id"]},
            42,
        )
    assert not [call for call in bot.calls if call["method"] == "POST"]


@pytest.mark.parametrize("text", ["   ", "x" * 10_001])
async def test_custom_answer_limits_preserve_active_flow(bot, session, text):
    bot.pending = request(group(custom=True))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_custom")
    with pytest.raises(UserError):
        await bot._questions_text(1, session, text, 42, bot.store.get_flow(1))
    assert bot.store.get_flow(1)["kind"] == "question_custom"
    assert not [call for call in bot.calls if call["method"] == "POST"]


async def test_custom_cancel_keeps_selected_option(bot, session):
    bot.pending = request(group("A", custom=True))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    await bot.click(session, "question_custom")
    await bot.click(session, "question_cancel_custom")
    assert bot.store.get_flow(1)["kind"] == "question"
    assert bot.store.get_flow(1)["data"]["selected"] == ["A"]


async def test_stale_buttons_and_other_users_cannot_mutate_flow(bot, session):
    bot.pending = request(group("A", "B"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    _, action, old = bot.button("question_option", option=1)
    await bot.click(session, "question_option", option=0)
    before = bot.store.get_flow(1)
    with pytest.raises(UserError, match="устарела"):
        await bot._questions_action(1, session, action, old, 42)
    with pytest.raises(UserError):
        await bot.click(session, "question_next", uid=2)
    assert bot.store.get_flow(1) == before
    assert not bot.store.get_flow(2)


@pytest.mark.parametrize("closed_by", ["pending", "conflict"])
async def test_closed_question_is_graceful_and_draft_is_restored(bot, session, closed_by):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "consultation", "consultation_1", {"message": "draft"})
    await bot.click(session, "question_option", option=0)
    if closed_by == "pending":
        bot.pending = []
    else:
        bot.post_errors = [ValeriaError("closed", status_code=409)]
    await bot.click(session, "question_next")
    assert not bot.store.get_flow(1)
    assert "уже закрыт" in bot.messages[-1]["text"]
    assert bot.views == [(1, "consultation", "consultation_1")]
    assert bot.restored[0][-1] == {"message": "draft"}
    if closed_by == "pending":
        assert not [call for call in bot.calls if call["method"] == "POST"]


async def test_503_retains_answers_and_retry_revalidates_pending_request(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.post_errors = [ValeriaError("Временно недоступно", status_code=503)]
    await bot.click(session, "question_next")
    saved = bot.store.get_flow(1)
    assert saved["kind"] == "question_pending"
    assert saved["data"]["answers"] == [["A"]]
    assert "сохранены" in bot.messages[-1]["text"]
    await bot.click(session, "question_retry")
    assert [call["method"] for call in bot.calls] == ["GET", "GET", "POST", "GET", "POST"]
    assert not bot.store.get_flow(1)


async def test_retry_after_uncertain_acceptance_does_not_resubmit_closed_question(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.post_errors = [ValeriaError("Timeout", acceptance_unknown=True)]
    await bot.click(session, "question_next")
    bot.pending = []
    await bot.click(session, "question_retry")
    assert len([call for call in bot.calls if call["method"] == "POST"]) == 1
    assert not bot.store.get_flow(1)


async def test_expired_session_propagates_without_losing_saved_answers(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.post_errors = [ValeriaError("expired", status_code=401)]
    with pytest.raises(ValeriaError) as caught:
        await bot.click(session, "question_next")
    assert caught.value.status_code == 401
    assert bot.store.get_flow(1)["data"]["answers"] == [["A"]]


async def test_validation_error_returns_to_editable_answers(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.post_errors = [ValeriaError("validation", status_code=422)]
    await bot.click(session, "question_next")
    assert bot.store.get_flow(1)["kind"] == "question"
    assert bot.store.get_flow(1)["data"]["selected"] == ["A"]
    bot.button("question_next")


async def test_false_acceptance_retains_answers_for_explicit_retry(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.post_result = {"accepted": False}
    await bot.click(session, "question_next")
    assert bot.store.get_flow(1)["kind"] == "question_pending"
    assert not bot.views
    bot.button("question_retry")


async def test_changed_server_question_is_loaded_again_without_post(bot, session):
    bot.pending = request(group("A"))
    await bot._questions_start(1, session, "dialog", "chat_1")
    await bot.click(session, "question_option", option=0)
    bot.pending = request(group("NEW"))
    await bot.click(session, "question_next")
    assert bot.store.get_flow(1)["data"]["questions"][0]["options"][0]["label"] == "NEW"
    assert not [call for call in bot.calls if call["method"] == "POST"]


async def test_no_pending_question_returns_false_without_disrupting_dialog(bot, session):
    bot.store.set_flow(1, "dialog", {"message": "saved"})
    assert not await bot._questions_start(1, session, "dialog", "chat_1", {"message": "saved"})
    assert bot.store.get_flow(1) == {"kind": "dialog", "data": {"message": "saved"}}
    assert not bot.messages
    assert not bot.views
    assert not bot.restored


async def test_non_question_text_and_unrelated_actions_are_not_consumed(bot, session):
    assert not await bot._questions_text(1, session, "ordinary", 42, {"kind": "dialog"})
    assert not await bot._questions_action(1, session, "dialog_open", {}, 42)


@pytest.mark.parametrize("kind", ["question", "question_pending", "permissions"])
async def test_text_in_button_flow_is_consumed_without_submitting_message(bot, session, kind):
    assert await bot._questions_text(1, session, "ordinary", 42, {"kind": kind})
    assert bot.messages
    assert not bot.calls


@pytest.mark.parametrize("response", ["once", "always", "reject"])
async def test_permission_choices_send_expected_body_and_refresh(bot, session, response):
    bot.pending = [
        {
            "id": "permission_1",
            "permission": "bash",
            "patterns": ["git status"],
            "always": ["git *"],
        }
    ]
    await bot._permissions(1, session, "chat_1")
    assert "bash" in bot.messages[-1]["text"]
    assert "git status" in bot.messages[-1]["text"]
    await bot.click(session, f"permission_{response}")
    assert bot.calls[-1] == {
        "method": "POST",
        "path": "api/chats/chat_1/permissions/permission_1",
        "body": {"response": response, "remember": response == "always"},
    }
    assert not bot.store.get_flow(1)
    assert bot.views == [(1, "dialog", "chat_1")]


async def test_permission_without_always_hides_and_rejects_always_choice(bot, session):
    bot.pending = [{"id": "permission_1", "permission": "edit", "patterns": ["file.py"]}]
    await bot._permissions(1, session, "chat_1")
    with pytest.raises(AssertionError):
        bot.button("permission_always")
    _, _, payload = bot.button("permission_once")
    with pytest.raises(UserError):
        await bot._questions_action(1, session, "permission_always", payload, 42)
    assert not [call for call in bot.calls if call["method"] == "POST"]


async def test_permission_not_accepted_reports_failure_and_refreshes(bot, session):
    bot.pending = [{"id": "permission_1", "permission": "bash", "metadata": {"command": "pwd"}}]
    bot.post_result = {"accepted": False}
    await bot._permissions(1, session, "chat_1")
    assert "pwd" in bot.messages[-1]["text"]
    await bot.click(session, "permission_once")
    assert "не подтверждено" in bot.messages[-1]["text"]
    assert bot.views == [(1, "dialog", "chat_1")]


async def test_stale_or_other_user_permission_cannot_be_submitted(bot, session):
    bot.pending = [{"id": "permission_1", "permission": "bash", "patterns": ["pwd"]}]
    await bot._permissions(1, session, "chat_1")
    _, action, payload = bot.button("permission_once")
    with pytest.raises(UserError):
        await bot._questions_action(2, session, action, payload, 42)
    await bot._permissions(1, session, "chat_1")
    with pytest.raises(UserError):
        await bot._questions_action(1, session, action, payload, 42)
    assert not [call for call in bot.calls if call["method"] == "POST"]
