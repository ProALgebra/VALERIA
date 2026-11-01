"""Optional integration smoke test: real Telegram controller against real Valeria API."""

from pathlib import Path

import httpx
import pytest
from test_api import FakeManager

from opencode_web.app import create_app

Bot = pytest.importorskip("valeria_telegram.bot").ValeriaBot
BotSettings = pytest.importorskip("valeria_telegram.config").Settings
Store = pytest.importorskip("valeria_telegram.store").Store
ValeriaClient = pytest.importorskip("valeria_telegram.valeria_client").ValeriaClient


class TelegramRecorder:
    def __init__(self):
        self.messages = []
        self.deleted = []

    async def send_text(self, chat_id, text, *, keyboard=None):
        self.messages.append({"text": text, "keyboard": keyboard or []})

    async def answer_callback_query(self, identifier, text=None):
        pass

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    def button(self, caption):
        for message in reversed(self.messages):
            for row in message["keyboard"]:
                for button in row:
                    if button["text"] == caption:
                        return button["callback_data"]
        raise AssertionError(f"Missing button: {caption}")


@pytest.mark.parametrize("tasks_only", [False, True])
async def test_telegram_user_flows_use_real_backend_api(settings, workspace, tmp_path, tasks_only):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        app.state.manager = FakeManager()
        db = app.state.database
        folder = workspace / "telegram"
        folder.mkdir()
        user = db.create_user(
            "worker",
            "private-password",
            "user",
            str(folder),
            tasks_only=tasks_only,
        )
        admin = db.get_user_by_login("admin")
        task = db.create_task(
            admin["id"],
            "Тестовая таска",
            "Описание",
            "Скрытый промпт",
            "build",
            "test",
            "coder",
            None,
            True,
        )
        db.set_consultation_model("test/coder", admin["id"])
        config = BotSettings("123:fake-token", "http://test", tmp_path / "telegram.db")
        telegram = TelegramRecorder()

        def factory(session):
            return ValeriaClient(
                config,
                session_token=session.session_token if session else None,
                csrf_token=session.csrf_token if session else None,
                transport=httpx.ASGITransport(app=app),
            )

        with Store(config.database_path) as store:
            bot = Bot(config, store, telegram, api_factory=factory)
            update_id = 0

            async def send(text=None, *, button=None):
                nonlocal update_id
                update_id += 1
                message = {
                    "message_id": update_id,
                    "from": {"id": 101},
                    "chat": {"id": 101, "type": "private"},
                }
                if button:
                    update = {
                        "update_id": update_id,
                        "callback_query": {
                            "id": str(update_id),
                            "from": {"id": 101},
                            "message": message,
                            "data": telegram.button(button),
                        },
                    }
                else:
                    update = {"update_id": update_id, "message": {**message, "text": text}}
                await bot.handle_update(update)
                return update

            await send("/login")
            await send("worker")
            await send("private-password")
            assert store.get_session(101).user["id"] == user["id"]
            assert telegram.deleted == [3]
            assert "private-password" not in "\n".join(store.connection.iterdump())

            await send(button="Таски")
            await send(button="Создать таску")
            await send(button="Тестовая таска")
            submitted = await send("Дополнение из Telegram")
            await bot.handle_update(submitted)
            runs = db.list_task_runs(user["id"])
            assert len(runs) == 1
            run = runs[0]
            assert run["task_id"] == task["id"]
            assert run["user_input"] == "Дополнение из Telegram"
            assert "Скрытый промпт" not in str(telegram.messages)
            db.set_job_status(run["id"], "completed")
            db.save_task_result(
                run["id"],
                {
                    "message_id": "answer-1",
                    "text": "Результат из реального API",
                },
            )
            await bot.poll_notifications()
            await bot.poll_notifications()
            assert str(telegram.messages).count("Результат из реального API") == 1

            await send("/consultations")
            await send(button="Новая консультация")
            await send("Первый вопрос")
            consultation_id = store.get_flow(101)["data"]["chat_id"]
            await send("Второй вопрос")
            assert [job["content"] for job in db.consultation_jobs(consultation_id)] == [
                "Первый вопрос",
                "Второй вопрос",
            ]

            await send("/dialogs")
            if tasks_only:
                assert "доступны таски и консультации" in telegram.messages[-1]["text"]
            else:
                await send(button="Новый диалог")
                await send(button="plan")
                await send(button="test / Test Coder")
                await send("Сообщение для диалога")
                chat_id = store.get_flow(101)["data"]["chat_id"]
                chat = db.get_chat(chat_id)
                assert chat["preferred_agent"] == "plan"
                assert chat["preferred_model_id"] == "coder"
                await send("Продолжение диалога")
                jobs = [job for job in db.list_jobs(user["id"]) if job["chat_id"] == chat_id]
                assert {job["content"] for job in jobs} == {
                    "Сообщение для диалога",
                    "Продолжение диалога",
                }
                assert {job["agent"] for job in jobs} == {"plan"}

            session = store.get_session(101)
            await send("/logout")
            assert store.get_session(101) is None
            assert store.list_watches(101) == []
            assert db.get_user_for_token(session.session_token) is None
            assert Path(config.database_path).is_file()
