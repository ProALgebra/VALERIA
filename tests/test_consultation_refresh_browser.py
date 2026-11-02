from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.mark.parametrize("admin", [False, True], ids=["user", "admin"])
@pytest.mark.parametrize("selected_role", ["user", "assistant"], ids=["question", "answer"])
def test_consultation_refresh_preserves_selection_messages_and_draft(admin, selected_role):
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    answer = (
        '**Ответ "консультанта"**\n\nЗначение `config["mode"]` и '
        '[описание](https://example.com/guide?value="one"&mode=test).'
    )
    chat = {
        "id": "consultation-1",
        "title": "Сохранение выделения",
        "owner_login": "worker",
        "status": "running",
        "messages": [
            {
                "id": "question-1",
                "role": "user",
                "text": 'Почему "config" содержит <значение> & другие символы?',
                "status": "running",
            },
            {"id": "answer-1", "role": "assistant", "text": answer},
        ],
        "jobs": [],
    }
    prefix = "/api/admin/consultations" if admin else "/api/consultations"
    detail_path = f"{prefix}/consultation-1"
    question_path = "/api/consultations/consultation-1/questions"
    reads = {"detail": 0, "questions": 0}

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
        if path == "/api/auth/me":
            payload = {
                "user": {
                    "id": "worker",
                    "login": "worker",
                    "role": "admin" if admin else "user",
                    "tasks_only": False,
                    "workspace_path": "/workspace",
                },
                "csrf_token": "csrf",
            }
        elif path == detail_path:
            reads["detail"] += 1
            payload = chat
        elif path == question_path:
            reads["questions"] += 1
            payload = []
        else:
            payload = []
        route.fulfill(json=payload)

    def selection_and_messages_unchanged(page):
        return page.evaluate("""() => {
            const selection = window.getSelection();
            const saved = window.savedConsultationSelection;
            const messages = document.querySelectorAll('#consultation-messages .message');
            return selection.toString() === saved.text
                && selection.anchorNode === saved.anchorNode
                && selection.anchorOffset === saved.anchorOffset
                && selection.focusNode === saved.focusNode
                && selection.focusOffset === saved.focusOffset
                && window.originalConsultationMessages.every((original, index) =>
                    original.article === messages[index]
                    && original.text === messages[index].querySelector('.message-text'));
        }""")

    def emit_update(page):
        with page.expect_response(lambda response: urlparse(response.url).path == detail_path):
            page.evaluate("""consultationEvents.onmessage({data: JSON.stringify({
                type: 'consultation.updated', job: {chat_id: 'consultation-1'}
            })})""")
        # Let question refreshes settle; a recursive refresh must not create extra GETs.
        page.wait_for_timeout(200)

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
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: {writeText: async value => window.copiedConsultationAnswers.push(value)},
            });
        """)
        page.route("**/*", route_handler)
        route_name = "admin-consultations" if admin else "consultations"
        page.goto(f"http://consultation.test/#/{route_name}/consultation-1")
        messages = page.locator("#consultation-messages .message")
        playwright.expect(messages).to_have_count(2)
        playwright.expect(page.locator("#consultation-messages strong")).to_have_text(
            'Ответ "консультанта"'
        )
        if not admin:
            page.locator("#consultation-send textarea").fill('Черновик "следующего" вопроса')
        page.wait_for_timeout(200)
        assert reads == {"detail": 1, "questions": 0 if admin else 1}
        page.evaluate("""role => {
            const messages = [...document.querySelectorAll('#consultation-messages .message')];
            window.originalConsultationMessages = messages.map(article => ({
                article, text: article.querySelector('.message-text'),
            }));
            const text = document.querySelector(`#consultation-messages .${role} .message-text`);
            const node = document.createTreeWalker(text, NodeFilter.SHOW_TEXT).nextNode();
            const range = document.createRange();
            range.setStart(node, 1);
            range.setEnd(node, node.length - 1);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            window.savedConsultationSelection = {
                text: selection.toString(),
                anchorNode: selection.anchorNode, anchorOffset: selection.anchorOffset,
                focusNode: selection.focusNode, focusOffset: selection.focusOffset,
            };
        }""", selected_role)
        assert page.evaluate("savedConsultationSelection.text.length") > 5

        emit_update(page)
        assert reads == {"detail": 2, "questions": 0 if admin else 2}
        assert selection_and_messages_unchanged(page)

        # Wait for the actual five-second reconciliation timer, independently of SSE.
        with page.expect_response(
            lambda response: urlparse(response.url).path == detail_path, timeout=6500
        ):
            pass
        page.wait_for_timeout(200)
        assert reads == {"detail": 3, "questions": 0 if admin else 3}
        assert selection_and_messages_unchanged(page)

        chat["status"] = "completed"
        chat["messages"][0]["status"] = "completed"
        chat["messages"].append(
            {"id": "answer-2", "role": "assistant", "text": "Новый **ответ**"}
        )
        emit_update(page)
        playwright.expect(messages).to_have_count(3)
        playwright.expect(page.locator("#consultation-status")).to_have_text("Ответ готов")
        playwright.expect(messages.nth(0).locator('[role="status"]')).to_have_count(0)
        assert reads == {"detail": 4, "questions": 0 if admin else 4}
        assert selection_and_messages_unchanged(page)
        if not admin:
            playwright.expect(page.locator("#consultation-send textarea")).to_have_value(
                'Черновик "следующего" вопроса'
            )

        copy_buttons = page.locator("#consultation-messages .message-copy")
        playwright.expect(copy_buttons).to_have_count(2)
        copy_buttons.nth(0).click()
        playwright.expect(copy_buttons.nth(0)).to_have_text("Скопировано")
        copy_buttons.nth(1).click()
        playwright.expect(copy_buttons.nth(1)).to_have_text("Скопировано")
        assert page.evaluate("copiedConsultationAnswers") == [answer, "Новый **ответ**"]
        assert errors == []
        browser.close()
