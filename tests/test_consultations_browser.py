from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse

import pytest


def multipart_form(request):
    """Parse a multipart form the same way the consultation server does."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return request.post_data_json
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + (request.post_data_buffer or b"")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    fields = {}
    files = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8")
        elif filename:
            files.append(
                {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "data": payload,
                }
            )
    if files:
        fields["files"] = files
    return fields


def test_consultation_deep_link_survives_login_and_back_navigation():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    authenticated = False
    requested = []

    def route_handler(route):
        nonlocal authenticated
        path = urlparse(route.request.url).path
        requested.append(path)
        if path == "/" or path.startswith("/static/"):
            file = static / (path.removeprefix("/static/") if path != "/" else "index.html")
            mime = {".js": "text/javascript", ".mjs": "text/javascript",
                    ".css": "text/css", ".html": "text/html"}
            route.fulfill(body=file.read_text(), content_type=mime[file.suffix])
            return
        user = {"id": "worker", "login": "worker", "role": "user",
                "tasks_only": False, "workspace_path": "/workspace"}
        if path == "/api/auth/me" and not authenticated:
            route.fulfill(status=401, json={"detail": "login"})
            return
        if path == "/api/auth/login":
            authenticated = True
            payload = {"user": user, "csrf_token": "csrf"}
        elif path == "/api/auth/me":
            payload = {"user": user, "csrf_token": "csrf"}
        elif path == "/api/consultations/consultation-1":
            payload = {"id": "consultation-1", "title": "Deep link", "status": "completed",
                       "messages": [{"role": "assistant", "text": "Готовый ответ"}]}
        elif path == "/api/consultations/consultation-1/questions":
            payload = []
        elif path == "/api/consultations":
            payload = [{"id": "consultation-1", "title": "Deep link", "status": "completed",
                        "updated_at": "2026-10-09T00:00:00Z"}]
        else:
            payload = []
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.add_init_script("""
            window.EventSource = class { close() {} };
            window.fallbackCopiedAnswer = null;
            Object.defineProperty(navigator, "clipboard", {configurable: true, value: undefined});
            document.execCommand = command => {
                if (command !== "copy") return false;
                window.fallbackCopiedAnswer = document.activeElement.value;
                return true;
            };
        """)
        page.route("**/*", route_handler)
        page.goto("http://consultation.test/#/consultations/consultation-1")
        playwright.expect(page.locator("#login-form")).to_be_visible()
        page.locator("#login").fill("worker")
        page.locator("#password").fill("password")
        page.locator("#login-form button").click()
        playwright.expect(page.locator("#consultation-messages")).to_contain_text("Готовый ответ")
        page.locator("#consultation-messages .message-copy").click()
        assert page.evaluate("window.fallbackCopiedAnswer") == "Готовый ответ"
        assert page.url.endswith("#/consultations/consultation-1")
        assert "/api/consultations" not in requested
        page.locator("#consultation-back").click()
        playwright.expect(page.locator("#consultation-list")).to_be_visible()
        assert page.url.endswith("#/consultations")
        page.go_back()
        playwright.expect(page.locator("#consultation-messages")).to_contain_text("Готовый ответ")
        browser.close()


@pytest.mark.parametrize("admin,tasks_only", [(False, False), (True, False), (False, True)])
def test_consultation_ui_and_live_draft(admin, tasks_only):
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    chat = {
        "id": "consultation-1",
        "title": "Первый вопрос",
        "owner_login": "worker",
        "status": "completed",
        "updated_at": "2026-10-04T10:00:00Z",
        "messages": [
            {"id": "1", "role": "user", "text": "Первый вопрос", "status": "completed"},
            {"id": "2", "role": "assistant", "text": "**Итоговый ответ**"},
        ],
        "jobs": [{"id": "job", "model": "test/coder", "agent": "build", "status": "completed"}],
    }
    sent = []
    settings = {"model": "test/coder", "queue_id": "default"}

    def route_handler(route):
        path = urlparse(route.request.url).path
        if path == "/" or path.startswith("/static/"):
            file = static / (path.removeprefix("/static/") if path != "/" else "index.html")
            mime = {
                ".js": "text/javascript",
                ".mjs": "text/javascript",
                ".css": "text/css",
                ".html": "text/html",
            }
            route.fulfill(body=file.read_text(), content_type=mime[file.suffix])
            return
        payload = []
        if path == "/api/auth/me":
            payload = {
                "user": {
                    "id": "worker",
                    "login": "worker",
                    "role": "admin" if admin else "user",
                    "tasks_only": tasks_only,
                    "workspace_path": "/workspace",
                },
                "csrf_token": "csrf",
            }
        elif path == "/api/options":
            payload = {
                "models": [{"id": "test/coder", "name": "Coder"}, {"id": "test/new", "name": "New"}]
            }
        elif path == "/api/admin/users":
            payload = [{"id": "worker", "login": "worker"}]
        elif path == "/api/admin/queues":
            payload = [{"id": "default", "name": "Основная", "status": "active"}]
        elif path == "/api/admin/settings/consultations":
            if route.request.method == "PUT":
                settings.update(route.request.post_data_json)
            payload = settings
        elif path.endswith("/consultations"):
            if route.request.method == "POST":
                sent.append(multipart_form(route.request))
                payload = chat
            else:
                payload = [chat]
        elif path.endswith("/consultation-1"):
            payload = chat
        elif path.endswith("/messages"):
            sent.append(multipart_form(route.request))
            payload = {"id": "job", "status": "queued"}
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.EventSource = class {
                constructor() { window.consultationEvents = this; }
                close() {}
            };
            window.copiedConsultationAnswers = [];
            Object.defineProperty(navigator, "clipboard", {
                configurable: true,
                value: {writeText: async value => window.copiedConsultationAnswers.push(value)},
            });
        """)
        page.route("**/*", route_handler)
        page.goto("http://consultation.test/")
        if tasks_only:
            playwright.expect(page.locator('[data-route="dialogs"]')).to_have_count(0)
            playwright.expect(page.locator('[data-route="templates"]')).to_have_count(0)
            playwright.expect(page.locator('[data-route="tasks"]')).to_be_visible()
        page.locator(
            f'[data-route="{"admin-consultations" if admin else "consultations"}"]'
        ).click()
        if admin:
            playwright.expect(page.locator("#consultation-queue")).to_have_value("default")
            page.locator("#consultation-model").select_option("test/new")
            page.locator("#consultation-settings button").click()
            playwright.expect(page.locator("#toast")).to_have_text(
                "Настройки консультаций сохранены"
            )
            assert settings["model"] == "test/new"
            page.locator('[data-consultation="consultation-1"]').click()
            playwright.expect(page.locator("#consultation-send")).to_have_count(0)
            playwright.expect(page.locator("#consultation-questions")).to_have_count(0)
            playwright.expect(page.locator("#consultation-jobs")).to_contain_text("test/coder")
        else:
            playwright.expect(page.locator("#consultation-model")).to_have_count(0)
            page.locator("#consultation-question").fill("Первый вопрос")
            page.locator("#consultation-new-files").set_input_files(
                {"name": "question.txt", "mimeType": "text/plain", "buffer": b"details"}
            )
            page.locator("#consultation-new button").click()
            playwright.expect(page.locator("#consultation-send")).to_be_visible()
            assert sent == [
                {
                    "message": "Первый вопрос",
                    "files": [
                        {
                            "filename": "question.txt",
                            "content_type": "text/plain",
                            "data": b"details",
                        }
                    ],
                }
            ]
            page.locator("#consultation-send textarea").fill("Черновик")
            page.evaluate(
                """consultationEvents.onmessage({data: JSON.stringify({
                    type: 'consultation.updated', job: {chat_id: 'consultation-1'}
                })})"""
            )
            playwright.expect(page.locator("#consultation-send textarea")).to_have_value("Черновик")
            page.locator("#consultation-send button").click()
            playwright.expect(page.locator("#consultation-send textarea")).to_have_value("")
            assert sent[-1] == {"message": "Черновик"}
            playwright.expect(page.locator("#consultation-jobs")).to_have_count(0)
        playwright.expect(page.locator("#consultation-messages strong")).to_have_text(
            "Итоговый ответ"
        )
        playwright.expect(page.locator("#consultation-messages .message-copy")).to_have_count(1)
        page.locator("#consultation-messages .message-copy").click()
        playwright.expect(page.locator("#toast")).to_have_text("Ответ скопирован")
        assert page.evaluate("window.copiedConsultationAnswers") == ["**Итоговый ответ**"]
        playwright.expect(page.locator(".runtime-card, .reasoning-card, .tool-card")).to_have_count(
            0
        )
        if tasks_only:
            page.locator('[data-route="tasks"]').click()
            page.locator('[data-route="consultations"]').click()
            page.locator('[data-consultation="consultation-1"]').click()
            playwright.expect(page.locator("#consultation-send")).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert errors == []
        browser.close()


def test_consultation_question_blocks_composer_until_answered():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    chat = {
        "id": "consultation-1",
        "title": "Нужен выбор",
        "status": "waiting_question",
        "updated_at": "2026-10-07T10:00:00Z",
        "messages": [{
            "id": "1", "role": "user", "text": "Предложи архитектуру",
            "status": "waiting_question",
        }],
    }
    questions = [{
        "id": "question-1",
        "questions": [{
            "header": "Подход",
            "question": "Какой вариант выбрать?",
            "multiple": False,
            "custom": True,
            "options": [{"label": "Надёжный", "description": "Больше проверок"}],
        }],
    }]
    replies = []

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
        if path == "/api/auth/me":
            payload = {
                "user": {"id": "worker", "login": "worker", "role": "user",
                         "tasks_only": False, "workspace_path": "/workspace"},
                "csrf_token": "csrf",
            }
        elif path == "/api/consultations":
            payload = [chat]
        elif path == "/api/consultations/consultation-1":
            payload = chat
        elif path == "/api/consultations/consultation-1/questions":
            payload = questions
        elif path.endswith("/questions/question-1/reply"):
            replies.append(route.request.post_data_json)
            questions.clear()
            chat["status"] = "queued"
            payload = {"accepted": True}
        else:
            payload = []
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.add_init_script("window.EventSource = class { close() {} };")
        page.route("**/*", route_handler)
        page.goto("http://consultation.test/")
        page.locator('[data-route="consultations"]').click()
        page.locator('[data-consultation="consultation-1"]').click()
        playwright.expect(page.locator("#consultation-status")).to_have_text(
            "Ожидает вашего ответа"
        )
        playwright.expect(page.locator(".question-card")).to_be_visible()
        playwright.expect(page.locator("#consultation-send")).to_be_hidden()
        page.get_by_text("Надёжный", exact=True).click()
        page.get_by_role("button", name="Отправить ответ").click()
        playwright.expect(page.locator(".question-card")).to_have_count(0)
        playwright.expect(page.locator("#consultation-send")).to_be_visible()
        assert replies == [{"answers": [["Надёжный"]]}]
        browser.close()
