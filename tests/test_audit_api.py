from __future__ import annotations

from test_chat_actions import ui_app as ui_app
from test_tasks_api import login

AUDIT_KEYS = {
    "id", "actor_user_id", "effective_user_id", "action", "object_type", "object_id",
    "metadata_json", "created_at", "actor_login", "effective_login",
}


async def audit(t, **params):
    response = await t.client.get("/api/admin/audit", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def actions(page):
    return [item["action"] for item in page["items"]]


async def clear_audit(t):
    # Logging in is audited too; start every test from an empty log.
    await login(t.client, "admin")
    with t.db.transaction() as connection:
        connection.execute("DELETE FROM audit_log")


async def test_audit_pages_with_a_cursor_newest_first(ui_app):
    t = ui_app
    await clear_audit(t)
    for number in range(5):
        t.db.audit(t.users["worker"]["id"], f"test.step{number}")

    first = await audit(t, limit=2)
    assert set(first) == {"items", "next_cursor"}
    assert all(set(item) == AUDIT_KEYS for item in first["items"])
    assert actions(first) == ["test.step4", "test.step3"]
    assert first["next_cursor"] == str(first["items"][-1]["id"])
    second = await audit(t, limit=2, cursor=first["next_cursor"])
    assert actions(second) == ["test.step2", "test.step1"]
    # Rows written meanwhile do not shift later pages.
    t.db.audit(t.users["worker"]["id"], "test.late")
    last = await audit(t, limit=2, cursor=second["next_cursor"])
    assert actions(last) == ["test.step0"] and last["next_cursor"] is None
    whole = await audit(t)
    assert len(whole["items"]) == 6 and whole["next_cursor"] is None
    exact = await audit(t, limit=6)
    assert len(exact["items"]) == 6 and exact["next_cursor"] is None


async def test_audit_filters_by_user_action_dates_and_text(ui_app):
    t = ui_app
    await clear_audit(t)
    worker, admin, stranger = (t.users[name]["id"] for name in ("worker", "admin", "stranger"))
    t.db.audit(worker, "chat.archive", effective_user_id=worker, object_type="chat",
               object_id="chat-1", metadata={"kind": "question"})
    t.db.audit(admin, "message.impersonated", effective_user_id=worker, object_type="chat",
               object_id="chat-2", metadata={"title": "Château Margaux"})
    t.db.audit(stranger, "chat.archive", effective_user_id=stranger, object_type="chat",
               object_id="chat-3")
    t.db.audit(admin, "user.update", object_type="user", object_id=stranger)

    # `user` matches the actor or the user acted for.
    assert actions(await audit(t, user="Worker")) == ["message.impersonated", "chat.archive"]
    assert actions(await audit(t, user="admin")) == ["user.update", "message.impersonated"]
    assert actions(await audit(t, user="worker,stranger", action="chat.archive")) == [
        "chat.archive", "chat.archive",
    ]
    assert actions(await audit(t, action="chat.archive,user.update")) == [
        "user.update", "chat.archive", "chat.archive",
    ]
    assert actions(await audit(t, action="chat")) == []

    assert actions(await audit(t, q="chateau MARGAUX")) == ["message.impersonated"]
    assert actions(await audit(t, q='"margaux chateau"')) == []
    assert actions(await audit(t, q="stranger archive")) == ["chat.archive"]
    assert actions(await audit(t, q="chat-1")) == ["chat.archive"]

    with t.db.transaction() as connection:
        connection.execute(
            "UPDATE audit_log SET created_at = '2026-02-01T10:00:00+00:00' "
            "WHERE action = 'user.update'"
        )
        connection.execute(
            "UPDATE audit_log SET created_at = '2026-02-02T10:00:00+00:00' "
            "WHERE action = 'message.impersonated'"
        )
    assert actions(await audit(t, **{"from": "2026-02-01", "to": "2026-02-01"})) == [
        "user.update"
    ]
    assert actions(await audit(
        t, **{"from": "2026-02-01T10:00:00Z", "to": "2026-02-02T10:00:00Z"}
    )) == ["user.update"]
    # Order stays by insertion (id), as before.
    assert actions(await audit(t, **{"to": "2026-02-03"})) == [
        "user.update", "message.impersonated",
    ]


async def test_audit_text_search_scans_past_the_first_batch(ui_app):
    t = ui_app
    await clear_audit(t)
    t.db.audit(t.users["worker"]["id"], "test.needle", metadata={"note": "иголка"})
    with t.db.transaction() as connection:
        connection.executemany(
            "INSERT INTO audit_log (action, metadata_json, created_at) VALUES (?, '{}', ?)",
            [("test.hay", "2026-01-01T00:00:00+00:00")] * 1200,
        )
    page = await audit(t, q="ИГОЛКА")
    assert actions(page) == ["test.needle"] and page["next_cursor"] is None
    page = await audit(t, action="test.hay", limit=500)
    assert len(page["items"]) == 500
    page = await audit(t, action="test.hay", limit=500, cursor=page["next_cursor"])
    page = await audit(t, action="test.hay", limit=500, cursor=page["next_cursor"])
    assert len(page["items"]) == 200 and page["next_cursor"] is None


async def test_audit_validation_and_access(ui_app):
    t = ui_app
    await login(t.client, "admin")
    for params in (
        {"limit": 0}, {"limit": 501}, {"cursor": "abc"}, {"from": "later"},
        {"timezone": "Mars/Base"},
    ):
        response = await t.client.get("/api/admin/audit", params=params)
        assert response.status_code == 422, params
    for name in ("worker", "tasker"):
        await login(t.client, name)
        assert (await t.client.get("/api/admin/audit")).status_code == 403
