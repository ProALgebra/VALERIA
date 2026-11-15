"""Task users see queue and execution states without losing their draft."""

from pathlib import Path
from urllib.parse import urlparse

import pytest


def test_task_status_launch_events_and_reconnect():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    task = {"id": "task", "title": "Проверка", "description": "Проверка CI"}
    run = {
        "id": "run", "task_id": "task", "title": "Проверка",
        "status": "queued", "created_at": "2026-10-03", "finished_at": None,
    }
    runs = []

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
                "user": {
                    "id": "worker", "login": "worker", "role": "user",
                    "tasks_only": True, "workspace_path": "/workspace",
                },
                "csrf_token": "csrf",
            }
        elif path == "/api/tasks":
            payload = [task]
        elif path == "/api/task-runs":
            payload = runs
        elif path == "/api/tasks/task/runs":
            assert route.request.method == "POST"
            runs.append(run)
            payload = run
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.EventSource = class {
                constructor() { window.taskEvents = this; }
                close() {}
            };
        """)
        page.route("**/*", route_handler)
        page.goto("http://tasks.test/")
        page.get_by_role("button", name="Запустить таску", exact=True).click()
        badge = page.locator('[data-task-run="run"] .badge')
        playwright.expect(badge).to_have_text("В очереди")
        playwright.expect(page.locator("#toast")).to_have_text("Таска поставлена в очередь")
        draft = page.locator("#task-user-input")
        draft.fill("Следующая задача")
        run["status"] = "dispatching"
        page.evaluate(
            "task => taskEvents.onmessage({data: JSON.stringify({type: 'task.updated', task})})",
            run,
        )
        playwright.expect(badge).to_have_text("Подготовка окружения")
        playwright.expect(page.locator('[data-stop-task-run="run"]')).to_be_visible()
        run["status"] = "running"
        page.evaluate(
            "task => taskEvents.onmessage({data: JSON.stringify({type: 'task.updated', task})})",
            run,
        )
        playwright.expect(badge).to_have_text("В работе")
        playwright.expect(badge).to_have_class("badge green")
        playwright.expect(draft).to_have_value("Следующая задача")
        run["plan_first"] = True
        for phase, label in (("plan", "Планирование"), ("build", "Выполнение")):
            run["phase"] = phase
            page.evaluate(
                "task => taskEvents.onmessage({data: JSON.stringify({type: 'task.updated', task})})",
                run,
            )
            playwright.expect(badge).to_have_text(label)
            playwright.expect(draft).to_have_value("Следующая задача")
        run["status"] = "completed"
        page.evaluate("taskEvents.onopen()")
        playwright.expect(badge).to_have_text("Завершена")
        playwright.expect(draft).to_have_value("Следующая задача")
        assert not errors
        browser.close()
