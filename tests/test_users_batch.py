from __future__ import annotations

from test_chat_actions import ui_app as ui_app
from test_tasks_api import login


async def batch(t, ids, status):
    response = await t.client.post("/api/admin/users/batch", json={"ids": ids, "status": status})
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["results"]}


async def test_batch_block_skips_self_and_users_already_blocked(ui_app):
    t = ui_app
    ids = {name: user["id"] for name, user in t.users.items()}
    await login(t.client, "worker")
    worker_cookies = dict(t.client.cookies)
    await login(t.client, "admin")
    admin_cookies = dict(t.client.cookies)
    t.db.update_user(ids["stranger"], status="disabled")

    order = [ids["worker"], ids["admin"], ids["stranger"], "missing", ids["tasker"], ids["worker"]]
    response = await t.client.post(
        "/api/admin/users/batch", json={"ids": order, "status": "blocked"}
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    assert [row["id"] for row in rows] == [
        ids["worker"], ids["admin"], ids["stranger"], "missing", ids["tasker"],
    ]
    assert rows == [
        {"id": ids["worker"], "login": "worker", "ok": True, "skipped": False, "reason": None},
        {"id": ids["admin"], "login": "admin", "ok": False, "skipped": True, "reason": "self"},
        {"id": ids["stranger"], "login": "stranger", "ok": True, "skipped": True,
         "reason": "already"},
        {"id": "missing", "login": None, "ok": False, "skipped": False, "reason": "not_found"},
        {"id": ids["tasker"], "login": "tasker", "ok": True, "skipped": False, "reason": None},
    ]
    assert {name: t.db.get_user(ids[name])["status"] for name in ids} == {
        "worker": "disabled", "tasker": "disabled", "stranger": "disabled", "admin": "active",
    }
    changed = [
        (entry["object_id"], entry["metadata_json"]) for entry in t.db.list_audit()
        if entry["action"] == "user.update"
    ]
    assert sorted(changed) == sorted(
        (ids[name], '{"status": "disabled", "batch": true}') for name in ("worker", "tasker")
    )

    # Blocking ends the user's sessions and stops new logins.
    t.client.cookies.clear()
    t.client.cookies.update(worker_cookies)
    assert (await t.client.get("/api/auth/me")).status_code == 401
    t.client.cookies.clear()
    t.client.cookies.update(admin_cookies)
    response = await t.client.post(
        "/api/auth/login", json={"login": "worker", "password": "a-strong-password"}
    )
    assert response.status_code in {401, 403}

    await login(t.client, "admin")
    rows = await batch(t, [ids["worker"], ids["admin"], ids["stranger"]], "active")
    assert rows[ids["worker"]] == {
        "id": ids["worker"], "login": "worker", "ok": True, "skipped": False, "reason": None,
    }
    assert rows[ids["admin"]]["reason"] == "self" and rows[ids["admin"]]["skipped"]
    assert rows[ids["stranger"]]["ok"] and not rows[ids["stranger"]]["skipped"]
    assert t.db.get_user(ids["worker"])["status"] == "active"
    rows = await batch(t, [ids["worker"]], "active")
    assert rows[ids["worker"]] == {
        "id": ids["worker"], "login": "worker", "ok": True, "skipped": True, "reason": "already",
    }
    await login(t.client, "worker")  # unblocked users can sign in again


async def test_batch_validation_and_access(ui_app):
    t = ui_app
    await login(t.client, "admin")
    for body in (
        {"ids": [], "status": "blocked"},
        {"ids": [t.users["worker"]["id"]], "status": "disabled"},
        {"ids": [t.users["worker"]["id"]]},
    ):
        response = await t.client.post("/api/admin/users/batch", json=body)
        assert response.status_code == 422, body
    csrf = t.client.headers.pop("x-csrf-token")
    response = await t.client.post(
        "/api/admin/users/batch", json={"ids": [t.users["worker"]["id"]], "status": "blocked"}
    )
    assert response.status_code == 403
    t.client.headers["x-csrf-token"] = csrf
    await login(t.client, "worker")
    response = await t.client.post(
        "/api/admin/users/batch", json={"ids": [t.users["tasker"]["id"]], "status": "blocked"}
    )
    assert response.status_code == 403
    assert t.db.get_user(t.users["tasker"]["id"])["status"] == "active"


async def test_batch_unblock_refuses_a_workspace_taken_by_another_active_user(ui_app):
    t = ui_app
    ids = {name: user["id"] for name, user in t.users.items()}
    await login(t.client, "admin")
    worker_path = t.db.get_user(ids["worker"])["workspace_path"]
    # While stranger was blocked, worker kept using the same workspace.
    t.db.update_user(ids["stranger"], status="disabled", workspace_path=worker_path)
    t.db.update_user(ids["tasker"], status="disabled")

    rows = await batch(t, [ids["stranger"], ids["tasker"]], "active")

    assert rows[ids["stranger"]] == {
        "id": ids["stranger"], "login": "stranger", "ok": False, "skipped": False,
        "reason": "workspace_in_use",
    }
    assert rows[ids["tasker"]]["ok"] is True
    assert t.db.get_user(ids["stranger"])["status"] == "disabled"
    assert t.db.get_user(ids["tasker"])["status"] == "active"
    audited = [
        entry["object_id"] for entry in t.db.list_audit() if entry["action"] == "user.update"
    ]
    assert ids["stranger"] not in audited
