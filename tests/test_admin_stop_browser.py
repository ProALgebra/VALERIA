"""Browser coverage for stopping a task or chat iteration from the global queue."""

from pathlib import Path
from urllib.parse import urlparse

import pytest


def test_global_queue_stop_actions_and_live_updates():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    jobs = [
        {
            "id": status, "sequence": index + 1, "status": status,
            "effective_login": "worker", "actor_login": "worker",
            "chat_title": f"Запуск {status}", "task_title": "Таска",
            "task_id": "task" if status in {"queued", "running"} else None,
            "content": "Ввод", "agent": "build", "model_id": "model",
        }
        for index, status in enumerate([
            "queued", "dispatching", "running", "waiting_permission",
            "cancelling", "completed", "failed", "cancelled",
        ])
    ]
    jobs[4]["error_code"] = "stop_unconfirmed"
    requests, dialogs, errors = [], [], []
    accept = False
    fail = False

    def route_handler(route):
        path = urlparse(route.request.url).path
        if path == "/" or path.startswith("/static/"):
            file = static / (path.removeprefix("/static/") if path != "/" else "index.html")
            mime = {
                ".js": "text/javascript", ".mjs": "text/javascript",
                ".css": "text/css", ".html": "text/html",
            }
            route.fulfill(body=file.read_text(), content_type=mime[file.suffix])
            return
        payload = []
        if path == "/api/auth/me":
            payload = {
                "user": {"id": "admin", "login": "admin", "role": "admin"},
                "csrf_token": "csrf",
            }
        elif path in ("/api/config/public", "/api/options"):
            payload = {"agents": ["build"], "models": [], "default_agent": "build"}
        elif path == "/api/admin/queue":
            payload = {
                "jobs": jobs, "limit": 5, "hard_limit": 10, "env_limit": 5,
                "reserved": 0, "shared_limit": 5,
                "default_chat_queue_id": "default",
                "queues": [
                    {"id": "default", "name": "Основная", "priority": 0,
                     "max_workers": 10, "reserved_workers": 0,
                     "status": "active", "is_system": 1,
                     "active": 1, "queued": 1},
                    {"id": "urgent", "name": "Срочные", "priority": 100,
                     "max_workers": 2, "reserved_workers": 0,
                     "status": "active", "is_system": 0,
                     "active": 2, "queued": 3},
                ],
                "active": sum(j["status"] in {
                    "running", "dispatching", "waiting_permission", "cancelling",
                } for j in jobs),
                "queued": sum(j["status"] == "queued" for j in jobs),
            }
        elif path.endswith("/stop"):
            assert route.request.method == "POST"
            assert route.request.headers["x-csrf-token"] == "csrf"
            requests.append(path)
            if fail:
                route.fulfill(status=503, json={"detail": "Временная ошибка"})
                return
            job = next(j for j in jobs if j["id"] == path.split("/")[-2])
            job["status"] = "cancelled" if job["status"] == "queued" else "cancelling"
            payload = job
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        try:
            browser = engine.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"Install Playwright chromium: {exc}")
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def confirm(dialog):
            dialogs.append(dialog.message)
            dialog.accept() if accept else dialog.dismiss()

        page.on("dialog", confirm)
        page.add_init_script("""
            window.EventSource = class {
                constructor() { window.queueEvents = this; }
                close() {}
            };
        """)
        page.route("**/*", route_handler)
        page.goto("http://queue.test/")
        page.locator('[data-route="queue"]').click()
        playwright.expect(page.locator("[data-queue-form]")).to_have_count(2)
        playwright.expect(page.locator('[data-queue-form="urgent"]')).to_contain_text(
            "2 активных · 3 ожидают"
        )
        playwright.expect(page.locator("[data-stop-job]")).to_have_count(4)
        playwright.expect(page.get_by_text(
            "Остановка пока не подтверждена. Повторяем автоматически.", exact=True,
        )).to_be_visible()
        page.locator('[data-stop-job="queued"]').click()
        assert not requests
        assert "worker" in dialogs[-1] and "таску" in dialogs[-1]
        accept = True
        page.locator('[data-stop-job="queued"]').click()
        playwright.expect(page.locator('[data-stop-job="queued"]')).to_have_count(0)
        playwright.expect(page.locator("#toast")).to_have_text("Задание отменено")

        fail = True
        page.locator('[data-stop-job="running"]').click()
        playwright.expect(page.locator("#toast")).to_have_text("Временная ошибка")
        playwright.expect(page.locator('[data-stop-job="running"]')).to_be_enabled()
        fail = False
        page.locator('[data-stop-job="running"]').click()
        row = page.locator("tr").filter(has=page.get_by_text("Запуск running", exact=True))
        playwright.expect(row.get_by_role("button", name="Останавливается…")).to_be_disabled()
        jobs[2]["status"] = "cancelled"
        page.evaluate("queueEvents.onmessage({data: JSON.stringify({type: 'task.updated'})})")
        playwright.expect(row.get_by_text("Отменено", exact=True)).to_be_visible()
        playwright.expect(row.locator("button")).to_have_count(0)

        page.locator('[data-stop-job="waiting_permission"]').click()
        assert "итерацию" in dialogs[-1]
        playwright.expect(page.locator('[data-stop-job="waiting_permission"]')).to_have_count(0)
        jobs[3]["status"] = "cancelled"
        page.evaluate("queueEvents.onmessage({data: JSON.stringify({type: 'job.updated'})})")
        row = page.locator("tr").filter(
            has=page.get_by_text("Запуск waiting_permission", exact=True),
        )
        playwright.expect(row.get_by_text("Отменено", exact=True)).to_be_visible()
        assert len(requests) == 4
        assert not errors
        browser.close()
