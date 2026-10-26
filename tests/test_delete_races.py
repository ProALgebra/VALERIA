from __future__ import annotations

import pytest
from test_chat_actions import make_folder, make_question
from test_chat_actions import ui_app as ui_app

from opencode_web.database import ChatDeleted


def jobs_of(t, chat_id):
    return t.db._all("SELECT * FROM queue_jobs WHERE chat_id = ? ORDER BY sequence", (chat_id,))


def delete_first(t, monkeypatch, method, chat_id):
    """Delete the chat right before the message reaches the queue."""
    original = getattr(t.db, method)

    def racing(*args, **kwargs):
        t.db.delete_chat(chat_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(t.db, method, racing)


async def test_message_racing_a_delete_is_not_queued(ui_app, monkeypatch):
    t = ui_app
    folder = await make_folder(t, "worker")
    before = jobs_of(t, folder["chat_id"])
    delete_first(t, monkeypatch, "queued_count", folder["chat_id"])
    response = await t.client.post(
        f"/api/chats/{folder['chat_id']}/messages", json={"message": "Ещё"}
    )
    assert response.status_code == 404
    assert jobs_of(t, folder["chat_id"]) == before


async def test_consultation_message_racing_a_delete_is_not_queued(ui_app, monkeypatch):
    t = ui_app
    question = await make_question(t, "worker")
    for job in t.db.unfinished_chat_jobs(question["chat_id"]):
        t.db.set_job_status(job["id"], "completed")
    before = jobs_of(t, question["chat_id"])
    delete_first(t, monkeypatch, "consultation_queue_id", question["chat_id"])
    response = await t.client.post(
        f"/api/consultations/{question['chat_id']}/messages", json={"message": "Уточню"}
    )
    assert response.status_code == 404
    assert jobs_of(t, question["chat_id"]) == before


async def test_create_job_rejects_a_chat_deleted_meanwhile(ui_app):
    t = ui_app
    folder = await make_folder(t, "worker")
    chat = t.db.get_chat(folder["chat_id"])
    t.db.delete_chat(chat["id"])
    with pytest.raises(ChatDeleted):
        t.db.create_job(
            chat=chat, effective_user_id=chat["owner_user_id"],
            actor_user_id=chat["owner_user_id"], content="Поздно", agent="build",
            provider_id=None, model_id=None, model_variant=None,
        )


@pytest.mark.parametrize("limit", [5, 0])
async def test_claim_cancels_queued_jobs_of_deleted_chats(ui_app, limit):
    t = ui_app
    folder = await make_folder(t, "worker")
    question = await make_question(t, "stranger")
    [orphan] = jobs_of(t, folder["chat_id"])
    [live] = jobs_of(t, question["chat_id"])
    t.db.delete_chat(folder["chat_id"])  # without stopping its job first

    claimed = t.db.claim_next_job(limit)
    assert (claimed["id"] if claimed else None) == (live["id"] if limit else None)
    job = t.db.get_job(orphan["id"])
    assert job["status"] == "cancelled" and job["finished_at"]
    assert t.db.get_chat(folder["chat_id"])["status"] != "queued"
