"""Admin execution mode controls with mocked API and live queue updates."""

from pathlib import Path
from urllib.parse import urlparse

import pytest


class ExecutionModeAPI:
    def __init__(self):
        self.mode = "direct"
        self.available = True
        self.reason = None
        self.can_change = True
        self.role = "admin"
        self.tasks_only = False
        self.get_error = None
        self.put_error = None
        self.defer_mode = False
        self.pending = None
        self.reads = 0
        self.puts = []
        self.active = 0
        self.queue_reads = 0
        self.chat = None
        self.models = []
        self.defer_options = False
        self.pending_options = None
        self.consultation = None
        self.consultation_sends = []

    def settings(self):
        return {
            "mode": self.mode,
            "availability": {"available": self.available, "reason": self.reason},
            "can_change": self.can_change,
        }

    def handle(self, route):
        path = urlparse(route.request.url).path
        if path == "/" or path.startswith("/static/"):
            static = Path(__file__).parents[1] / "src/opencode_web/static"
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
                "user": {"id": "owner", "login": "owner", "role": self.role,
                         "tasks_only": self.tasks_only, "workspace_path": "/workspace"},
                "csrf_token": "csrf",
            }
        elif path in ("/api/config/public", "/api/options"):
            if path == "/api/options" and self.defer_options:
                self.pending_options = route
                return
            payload = {
                "agents": ["build"], "models": self.models, "default_agent": "build",
                "workspace": "/workspace", "max_active_sessions": 2,
            }
        elif path == "/api/chats":
            payload = [self.chat] if self.chat else []
        elif path == "/api/chats/chat":
            payload = self.chat
        elif path == "/api/chats/chat/messages":
            payload = [{"info": {"role": "user"},
                        "parts": [{"type": "text", "text": "Сохранённая история"}]}]
        elif path == "/api/chats/chat/mcp":
            payload = {"servers": []}
        elif path == "/api/consultations/consultation":
            payload = self.consultation
        elif path == "/api/consultations/consultation/messages":
            assert route.request.method == "POST"
            self.consultation_sends.append(route.request.post_data_json)
            payload = {"id": "consultation-job", "status": "queued"}
        elif path == "/api/admin/settings/consultations":
            payload = {"model": "old/model", "queue_id": "default"}
        elif path == "/api/admin/queues":
            payload = [
                {"id": "default", "name": "Основная", "status": "active"},
                {"id": "other", "name": "Дополнительная", "status": "active"},
            ]
        elif path == "/api/admin/queue":
            self.queue_reads += 1
            payload = {
                "jobs": [], "limit": 2, "hard_limit": 10, "env_limit": 2,
                "active": self.active, "queued": 0,
                "queues": [{"id": "default", "name": "Основная", "priority": 0,
                            "max_workers": 2, "status": "active", "is_system": True,
                            "active": self.active, "queued": 0}],
            }
        elif path == "/api/admin/settings/execution-mode":
            if route.request.method == "GET":
                self.reads += 1
                if self.defer_mode:
                    self.pending = route
                    return
                if self.get_error:
                    route.fulfill(status=503, json={"detail": self.get_error})
                    return
            else:
                assert route.request.method == "PUT"
                assert route.request.headers["x-csrf-token"] == "csrf"
                self.puts.append(route.request.post_data_json)
                if self.put_error:
                    status, message = self.put_error
                    route.fulfill(status=status, json={"detail": message})
                    return
                self.mode = route.request.post_data_json["mode"]
            payload = self.settings()
        route.fulfill(json=payload)


@pytest.fixture
def execution_page():
    playwright = pytest.importorskip("playwright.sync_api")
    api = ExecutionModeAPI()
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.EventSource = class {
                constructor() { window.queueEvents = this; }
                close() {}
            };
        """)
        page.route("**/*", api.handle)
        yield playwright, page, api
        browser.close()
        assert not errors


def test_execution_mode_save_and_live_updates_preserve_settings(execution_page):
    playwright, page, api = execution_page
    api.defer_mode = True
    page.goto("http://execution.test/#/queue")
    playwright.expect(page.locator("#queue-jobs")).to_be_attached()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Загружаем режим работы…"
    )
    playwright.expect(page.get_by_role("button", name="Сохранить режим")).to_be_disabled()
    page.wait_for_function("document.querySelector('#new-queue-form') !== null")
    assert api.pending is not None
    api.defer_mode = False
    api.pending.fulfill(json=api.settings())
    playwright.expect(page.get_by_role("radio", name="Без изоляции", exact=True)).to_be_checked()
    playwright.expect(page.locator("#execution-mode-description")).to_contain_text(
        "старые диалоги архивируются"
    )
    playwright.expect(page.locator("#execution-mode-description")).to_contain_text(
        "задачи и результаты сохраняются"
    )
    page.get_by_role("radio", name="С изоляцией", exact=True).check()
    page.get_by_role("button", name="Сохранить режим").click()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Активный режим: С изоляцией"
    )
    assert api.puts == [{"mode": "podman"}]

    page.get_by_role("radio", name="Без изоляции", exact=True).check()
    page.locator('#new-queue-form [name="name"]').fill("Черновик очереди")
    page.locator('[data-queue-form="default"] [name="priority"]').fill("42")
    page.locator("#execution-mode-form").evaluate("form => { form.dataset.retained = 'yes'; }")
    api.active = 1
    api.can_change = False
    page.evaluate("queueEvents.onmessage({data: JSON.stringify({type: 'job.updated'})})")
    playwright.expect(page.locator("#queue-active")).to_have_text("1 из 2")
    playwright.expect(page.locator("#execution-mode-form")).to_have_attribute(
        "data-retained", "yes"
    )
    playwright.expect(page.get_by_role("radio", name="Без изоляции", exact=True)).to_be_checked()
    playwright.expect(page.locator('#new-queue-form [name="name"]')).to_have_value(
        "Черновик очереди"
    )
    playwright.expect(page.locator('[data-queue-form="default"] [name="priority"]')).to_have_value(
        "42"
    )
    playwright.expect(page.get_by_role("button", name="Сохранить режим")).to_be_disabled()
    playwright.expect(page.locator("#execution-mode-busy")).to_contain_text("незавершённые задания")
    assert api.reads >= 2

    api.active = 0
    api.can_change = True
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'podman'
    })})""")
    playwright.expect(page.get_by_role("button", name="Сохранить режим")).to_be_enabled()
    page.get_by_role("button", name="Сохранить режим").click()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Активный режим: Без изоляции"
    )
    assert api.puts[-1] == {"mode": "direct"}
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("status,message", [
    (409, "Сначала завершите все задания"),
    (503, "Окружение изоляции не готово"),
])
def test_execution_mode_put_failure_restores_confirmed_mode(execution_page, status, message):
    playwright, page, api = execution_page
    page.goto("http://execution.test/#/queue")
    isolated = page.get_by_role("radio", name="С изоляцией", exact=True)
    playwright.expect(isolated).to_be_enabled()
    isolated.check()
    api.put_error = (status, message)
    if status == 503:
        api.available = False
        api.reason = "Не удалось запустить окружение"
    page.get_by_role("button", name="Сохранить режим").click()
    playwright.expect(page.locator("#execution-mode-error")).to_have_text(message)
    playwright.expect(page.get_by_role("radio", name="Без изоляции", exact=True)).to_be_checked()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Активный режим: Без изоляции"
    )
    playwright.expect(page.locator("#queue-jobs")).to_be_attached()
    assert api.reads == 2
    assert api.mode == "direct"
    if status == 503:
        playwright.expect(isolated).to_be_disabled()
        playwright.expect(page.locator("#execution-mode-availability")).to_contain_text(api.reason)


def test_execution_mode_unavailable_and_retryable_load_error(execution_page):
    playwright, page, api = execution_page
    api.get_error = "Не удалось проверить режим"
    page.goto("http://execution.test/#/queue")
    playwright.expect(page.locator("#execution-mode-error")).to_have_text(api.get_error)
    playwright.expect(page.locator("#new-queue-form")).to_be_visible()
    api.get_error = None
    api.available = False
    api.reason = "Изоляция не настроена"
    page.get_by_role("button", name="Повторить проверку").click()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Активный режим: Без изоляции"
    )
    playwright.expect(page.get_by_role("radio", name="С изоляцией", exact=True)).to_be_disabled()
    playwright.expect(page.locator("#execution-mode-availability")).to_contain_text(api.reason)
    assert not api.puts


def test_execution_mode_can_recheck_successful_unavailable_response(execution_page):
    playwright, page, api = execution_page
    api.available = False
    api.reason = "Окружение ещё не подготовлено"
    page.goto("http://execution.test/#/queue")
    isolated = page.get_by_role("radio", name="С изоляцией", exact=True)
    retry = page.get_by_role("button", name="Повторить проверку")
    playwright.expect(isolated).to_be_disabled()
    playwright.expect(page.locator("#execution-mode-availability")).to_contain_text(api.reason)
    playwright.expect(page.locator("#execution-mode-error")).to_be_empty()
    playwright.expect(retry).to_be_visible()

    api.available = True
    api.reason = None
    retry.click()
    playwright.expect(isolated).to_be_enabled()
    playwright.expect(page.locator("#execution-mode-availability")).to_be_empty()
    playwright.expect(page.locator("#execution-mode-current")).to_have_text(
        "Активный режим: Без изоляции"
    )
    playwright.expect(page.get_by_role("radio", name="Без изоляции", exact=True)).to_be_checked()
    playwright.expect(retry).to_be_hidden()
    assert api.reads == 2
    assert not api.puts


@pytest.mark.parametrize("tasks_only", [False, True])
def test_execution_mode_settings_are_hidden_from_non_admin(execution_page, tasks_only):
    playwright, page, api = execution_page
    api.role = "user"
    api.tasks_only = tasks_only
    page.goto("http://execution.test/#/queue")
    playwright.expect(page.locator(".sidebar-user")).to_be_visible()
    playwright.expect(page.locator("#execution-mode-form")).to_have_count(0)
    playwright.expect(page.locator('[data-route="queue"]')).to_have_count(0)
    assert api.reads == 0
    assert not api.puts


def test_archived_execution_mode_chat_keeps_history_and_disables_composer(execution_page):
    playwright, page, api = execution_page
    api.role = "user"
    api.chat = {
        "id": "chat", "title": "Архивный диалог", "status": "idle",
        "opencode_session_id": "session", "workspace_snapshot": "/workspace",
        "archived_reason": "execution_mode_changed",
    }
    page.goto("http://execution.test/#/dialogs")
    page.locator('[data-chat="chat"]').click()
    playwright.expect(page.locator("#message-list")).to_contain_text("Сохранённая история")
    playwright.expect(page.locator("#chat-mode-hint")).to_have_text(
        "Диалог создан в другом режиме работы. Для продолжения нужен исходный режим."
    )
    playwright.expect(page.locator('#composer textarea')).to_be_disabled()
    playwright.expect(page.locator('#composer button[type="submit"]')).to_be_disabled()
    assert api.reads == 0

    api.chat["archived_reason"] = None
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'direct'
    })})""")
    playwright.expect(page.locator('#composer textarea')).to_be_enabled()
    playwright.expect(page.locator("#chat-mode-hint")).to_have_count(0)


def test_execution_mode_event_updates_model_catalog_without_losing_draft(execution_page):
    playwright, page, api = execution_page
    api.role = "user"
    api.models = [{"id": "old/model", "name": "Исходная модель"}]
    api.defer_options = True
    page.goto("http://execution.test/#/dialogs")
    playwright.expect(page.locator('#new-chat-model')).to_be_disabled()
    page.locator('#new-chat textarea').fill("Черновик нового диалога")
    assert api.pending_options is not None
    old_models = api.models
    api.defer_options = False
    api.models = [{"id": "new/model", "name": "Новая модель"}]
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'podman'
    })})""")
    playwright.expect(page.locator('#new-chat-model option[value="new/model"]')).to_be_attached()
    with page.expect_response("**/api/options") as stale_response:
        api.pending_options.fulfill(json={
            "agents": ["build"], "models": old_models, "default_agent": "build",
        })
    stale_response.value.finished()
    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
    playwright.expect(page.locator('#new-chat-model option[value="new/model"]')).to_be_attached()
    playwright.expect(page.locator('#new-chat-model option[value="old/model"]')).to_have_count(0)
    playwright.expect(page.locator('#new-chat textarea')).to_have_value("Черновик нового диалога")


def test_archived_consultation_preserves_history_and_resumes_after_mode_event(execution_page):
    playwright, page, api = execution_page
    api.role = "user"
    api.tasks_only = True
    api.consultation = {
        "id": "consultation", "title": "Сохранённая консультация", "status": "completed",
        "archived_reason": "execution_mode_changed", "execution_mode": "direct",
        "messages": [{"role": "assistant", "text": "Сохранённый ответ консультанта"}],
    }
    page.goto("http://execution.test/#/consultations/consultation")
    composer = page.locator("#consultation-send")
    hint = page.locator("#consultation-archive")
    playwright.expect(page.locator("#consultation-messages")).to_contain_text(
        "Сохранённый ответ консультанта"
    )
    playwright.expect(hint).to_be_visible()
    playwright.expect(composer.locator("textarea")).to_be_disabled()
    playwright.expect(composer.locator("button")).to_be_disabled()

    api.consultation["archived_reason"] = None
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'direct'
    })})""")
    playwright.expect(composer.locator("textarea")).to_be_enabled()
    playwright.expect(hint).to_be_hidden()
    composer.locator("textarea").fill("Черновик продолжения")

    api.consultation["archived_reason"] = "execution_mode_changed"
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'podman'
    })})""")
    playwright.expect(composer.locator("textarea")).to_be_disabled()
    playwright.expect(composer.locator("textarea")).to_have_value("Черновик продолжения")
    api.consultation["archived_reason"] = None
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'direct'
    })})""")
    playwright.expect(composer.locator("button")).to_be_enabled()
    composer.locator("button").click()
    playwright.expect(composer.locator("textarea")).to_have_value("")
    playwright.expect(composer.locator("button")).to_be_enabled()
    assert api.consultation_sends == [{"message": "Черновик продолжения"}]
    assert not api.puts


@pytest.mark.parametrize("editor", ["admin-tasks", "admin-consultations"])
def test_mode_event_refreshes_admin_catalog_without_resetting_drafts(execution_page, editor):
    playwright, page, api = execution_page
    api.models = [
        {"id": "old/model", "name": "Исходная модель"},
        {"id": "draft/model", "name": "Выбранная модель"},
    ]
    page.goto(f"http://execution.test/#/{editor}")
    task_editor = editor == "admin-tasks"
    form = page.locator("#admin-task-form" if task_editor else "#consultation-settings")
    model = form.locator('[name="model"]')
    playwright.expect(model).to_be_enabled()
    model.select_option("draft/model")
    form.evaluate("form => { form.dataset.retained = 'yes'; }")
    if task_editor:
        page.locator("#task-title").fill("Черновик таски")
        page.locator("#task-content").fill("Несохранённый промпт")
        page.locator("#task-variant").fill("draft-variant")
    else:
        page.locator('#consultation-filter [name="q"]').fill("Несохранённый поиск")
        page.locator("#consultation-queue").select_option("other")

    old_models = api.models
    api.defer_options = True
    with page.expect_request("**/api/options"):
        page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
            type: 'execution_mode.updated', mode: 'podman'
        })})""")
    playwright.expect(model).to_be_disabled()
    assert api.pending_options is not None
    stale_request = api.pending_options
    api.defer_options = False
    api.models = [{"id": "new/model", "name": "Новая модель"}]
    page.evaluate("""queueEvents.onmessage({data: JSON.stringify({
        type: 'execution_mode.updated', mode: 'direct'
    })})""")
    playwright.expect(model.locator('option[value="new/model"]')).to_be_attached()
    playwright.expect(model).to_have_value("draft/model")
    with page.expect_response("**/api/options") as stale_response:
        stale_request.fulfill(json={
            "agents": ["build"], "models": old_models, "default_agent": "build",
        })
    stale_response.value.finished()
    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
    playwright.expect(model.locator('option[value="new/model"]')).to_be_attached()
    playwright.expect(model.locator('option[value="old/model"]')).to_have_count(0)
    playwright.expect(model).to_have_value("draft/model")
    playwright.expect(form).to_have_attribute("data-retained", "yes")
    if task_editor:
        playwright.expect(page.locator("#task-title")).to_have_value("Черновик таски")
        playwright.expect(page.locator("#task-content")).to_have_value("Несохранённый промпт")
        playwright.expect(page.locator("#task-variant")).to_have_value("draft-variant")
        playwright.expect(page.locator("#task-unsaved")).to_be_visible()
    else:
        playwright.expect(page.locator('#consultation-filter [name="q"]')).to_have_value(
            "Несохранённый поиск"
        )
        playwright.expect(page.locator("#consultation-queue")).to_have_value("other")
