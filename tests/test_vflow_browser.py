import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

SOURCE = """workflow "demo" {
  version: 1;
  input: json;
  step pass = transform(operations: [{op: "set", path: "$.ok", value: true}]);
  start -> pass -> end
}
"""


def assert_no_horizontal_overflow(page):
    metrics = page.evaluate(
        """() => ({
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
            bodyScrollWidth: document.body.scrollWidth,
            innerWidth: window.innerWidth,
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1, metrics
    assert metrics["bodyScrollWidth"] <= metrics["innerWidth"] + 1, metrics


def test_vflow_pages_render_without_errors():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    pipeline = {
        "id": "demo",
        "slug": "demo",
        "title": "Демо-пайплайн",
        "description": "Проверка интерфейса",
        "version": 1,
        "published_at": "2026-11-20T12:00:00Z",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
        "public_result_enabled": False,
    }
    run = {
        "id": "run-1",
        "pipeline": {"id": "demo", "slug": "demo", "title": "Демо-пайплайн", "version": 1},
        "status": "succeeded",
        "progress": {"points_total": 1, "points_done": 1, "waiting_tasks": 0, "waiting_merges": 0},
        "public_result_enabled": True,
        "error": None,
    }
    admin_run = {
        "id": "run-events",
        "pipeline": {"id": "demo", "slug": "demo", "title": "Демо-пайплайн", "version": 1},
        "status": "succeeded",
        "started_by": {"id": "admin", "login": "admin"},
        "created_at": "2026-11-20T12:00:00Z",
    }
    admin_run_detail = {
        **admin_run,
        "points": [
            {
                "id": "point-1",
                "node_id": "pass",
                "status": "succeeded",
                "visit_seq": 1,
                "branch_index": None,
                "task_run_id": None,
            }
        ],
        "events": [
            {
                "id": "event-1",
                "event_type": "RUN_STARTED",
                "node_id": "pass",
                "point_id": "point-1",
                "payload": {"ok": True},
                "created_at": "2026-11-20T12:00:00Z",
            },
            {
                "id": "event-2",
                "event_type": "NODE_ENTERED",
                "node_id": "pass",
                "point_id": "point-2",
                "payload": {"note": "alpha"},
                "created_at": "2026-11-20T12:00:01Z",
            },
            {
                "id": "event-3",
                "event_type": "RUN_SUCCEEDED",
                "node_id": None,
                "point_id": "point-3",
                "payload": {"result": "beta"},
                "created_at": "2026-11-20T12:00:02Z",
            },
        ],
    }
    validation = {
        "valid": True,
        "diagnostics": [],
        "graph_hash": "hash",
        "required_handlers": [],
    }
    pipeline_runs = [run]
    draft_requests = []

    def created_run():
        return {
            "id": "run-keyboard",
            "pipeline": {
                "id": "demo",
                "slug": "demo",
                "title": "Демо-пайплайн",
                "version": 1,
            },
            "status": "running",
            "progress": {
                "points_total": 1,
                "points_done": 0,
                "waiting_tasks": 1,
                "waiting_merges": 0,
            },
            "public_result_enabled": False,
            "error": None,
        }
    graph = {
        "ir_version": 1,
        "workflow": "demo",
        "entry": "pass",
        "nodes": [
            {
                "id": "pass",
                "type": "core.transform",
                "type_version": "1.0",
                "config": {"operations": [{"op": "set", "path": "$.ok", "value": True}]},
                "ui": {"x": 40, "y": 40},
            }
        ],
        "edges": [
            {"source": "start", "source_port": "out", "target": "pass"},
            {"source": "pass", "source_port": "out", "target": "end"},
        ],
    }
    definition = {
        "id": "demo",
        "slug": "demo",
        "title": "Демо-пайплайн",
        "status": "active",
        "settings": {"cancel_own": True, "limits": {"max_fork_n": 2}},
        "draft": {
            "id": "version-1",
            "version_number": 1,
            "status": "draft",
            "source": SOURCE,
            "graph": graph,
            "input_schema": {},
            "required_handlers": [],
            "etag": '"etag-1"',
        },
        "published": None,
    }

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/" or parsed.path.startswith("/static/"):
            file = static / (
                parsed.path.removeprefix("/static/")
                if parsed.path != "/"
                else "index.html"
            )
            mime = {
                ".js": "text/javascript",
                ".mjs": "text/javascript",
                ".css": "text/css",
                ".html": "text/html",
            }
            route.fulfill(body=file.read_text(), content_type=mime[file.suffix])
            return
        if parsed.path == "/api/auth/me":
            route.fulfill(
                json={
                    "user": {
                        "id": "admin",
                        "login": "admin",
                        "role": "admin",
                        "workspace_path": "/tmp/demo",
                    },
                    "csrf_token": "test",
                }
            )
        elif parsed.path == "/api/config/public":
            route.fulfill(
                json={
                    "agents": ["build"],
                    "default_agent": "build",
                    "workspace": "/tmp/demo",
                }
            )
        elif parsed.path == "/api/options":
            route.fulfill(json={"models": [], "agents": ["build"]})
        elif parsed.path == "/api/pipelines":
            route.fulfill(json=[pipeline])
        elif parsed.path == "/api/pipelines/demo/runs":
            new_run = created_run()
            pipeline_runs.append(new_run)
            route.fulfill(json=new_run)
        elif parsed.path == "/api/pipeline-runs":
            route.fulfill(json=pipeline_runs)
        elif parsed.path == "/api/pipeline-runs/run-1/result":
            route.fulfill(json={"result": {"ok": True}})
        elif parsed.path == "/api/pipeline-runs/run-keyboard/cancel":
            for item in pipeline_runs:
                if item["id"] == "run-keyboard":
                    item["status"] = "cancelled"
            route.fulfill(json={"status": "cancelled"})
        elif parsed.path == "/api/admin/pipelines":
            route.fulfill(json=[definition])
        elif parsed.path == "/api/admin/pipelines/demo/settings":
            body = json.loads(route.request.post_data or "{}")
            definition["settings"] = body.get("settings") or {}
            route.fulfill(json={**definition, "settings": definition["settings"]})
        elif parsed.path == "/api/admin/pipelines/demo/draft":
            draft_requests.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(json=definition["draft"])
        elif parsed.path == "/api/admin/pipelines/demo/audience":
            route.fulfill(json={"mode": "selected", "user_ids": ["u1"]})
        elif parsed.path == "/api/admin/pipelines/demo":
            route.fulfill(json=definition)
        elif parsed.path == "/api/admin/vflow/block-registry":
            route.fulfill(
                json=[
                    {
                        "type": "core.transform",
                        "title": "Transform",
                        "version": 1,
                        "config_schema": {
                            "type": "object",
                            "properties": {
                                "operations": {"type": "array"},
                            },
                            "required": ["operations"],
                        },
                    }
                ]
            )
        elif parsed.path == "/api/admin/vflow/compile":
            route.fulfill(
                json={
                    "graph": graph,
                    "graph_hash": "hash",
                    "required_handlers": [],
                }
            )
        elif parsed.path == "/api/admin/vflow/tasks-catalog":
            route.fulfill(json=[])
        elif parsed.path == "/api/admin/vflow/runs":
            route.fulfill(json=[admin_run])
        elif parsed.path == "/api/admin/vflow/runs/run-events":
            route.fulfill(json=admin_run_detail)
        elif parsed.path == "/api/admin/pipelines/demo/validate":
            route.fulfill(json=validation)
        else:
            route.fulfill(json={})

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(channel="chromium")
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(
            """
            window.EventSource = class {
                constructor() {}
                close() {}
            };
            """
        )
        page.route("**/*", handle)
        page.goto("http://vflow.test/")
        playwright.expect(page.locator("#app")).not_to_have_attribute("aria-live", "polite")

        pipeline_nav = page.locator('[data-route="pipelines"]').first
        pipeline_nav.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#vflow-pipelines-title")).to_be_focused()
        playwright.expect(page.locator(".vflow-card h3")).to_have_text("Демо-пайплайн")
        playwright.expect(page.locator("#pipeline-runs")).not_to_have_attribute(
            "aria-live", "polite"
        )
        playwright.expect(page.locator("#pipeline-runs")).to_have_attribute("aria-busy", "false")

        pipeline_card = page.locator('[data-pipeline="demo"]')
        pipeline_card.focus()
        page.keyboard.press("Enter")
        playwright.expect(pipeline_card).to_be_focused()
        playwright.expect(pipeline_card).to_have_attribute("aria-pressed", "true")
        playwright.expect(page.locator('label[for="pipeline-field-0"]')).to_have_text("topic")

        result_button = page.locator('[data-result-run="run-1"]')
        result_button.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator(".vflow-result")).to_be_visible()
        playwright.expect(page.locator(".vflow-result")).to_contain_text('"ok": true')
        playwright.expect(page.locator(".vflow-result")).to_be_focused()
        playwright.expect(page.locator(".vflow-result")).to_have_attribute("role", "region")
        playwright.expect(page.locator(".vflow-result")).to_have_attribute(
            "aria-label", "Результат запуска"
        )

        pipeline_field = page.locator("#pipeline-field-0")
        pipeline_field.fill("keyboard")
        pipeline_field.press("Tab")
        submit = page.locator("#pipeline-run-form button[type=submit]")
        playwright.expect(submit).to_be_focused()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Пайплайн запущен")
        playwright.expect(page.locator("#toast")).to_have_attribute("role", "status")
        playwright.expect(page.locator("#toast")).to_have_attribute("aria-live", "polite")
        playwright.expect(page.locator("#toast")).to_have_attribute("aria-atomic", "true")

        cancel_button = page.locator('[data-cancel-run="run-keyboard"]')
        playwright.expect(cancel_button).to_be_visible()
        cancel_button.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Отмена запрошена")
        keyboard_run = page.locator('[data-run="run-keyboard"]')
        playwright.expect(keyboard_run).to_be_focused()
        playwright.expect(keyboard_run).to_contain_text("отменён")

        admin_nav = page.locator('[data-route="admin-pipelines"]')
        admin_nav.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#vflow-admin-pipelines-title")).to_be_focused()
        playwright.expect(page.locator("#pipeline-create-form")).to_be_visible()

        edit_button = page.locator('[data-edit-pipeline="demo"]')
        edit_button.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#vflow-editor-title")).to_be_focused()
        playwright.expect(page.locator("#vflow-code")).to_have_value(SOURCE)
        playwright.expect(page.locator("#vflow-undo")).to_be_disabled()
        playwright.expect(page.locator("#vflow-redo")).to_be_disabled()
        page.locator("#vflow-palette-search").fill("transform")
        playwright.expect(page.locator(".vflow-palette .vflow-block:visible")).to_have_count(1)
        page.locator("#vflow-palette-search").fill("zzz")
        playwright.expect(page.locator(".vflow-palette .vflow-block:visible")).to_have_count(0)
        page.locator("#vflow-palette-search").fill("")
        page.locator("#vflow-audience-mode").select_option("selected")
        page.locator("#vflow-audience-users").fill("u1")
        page.locator("#vflow-save-audience").click()
        playwright.expect(page.locator("#toast")).to_contain_text("Доступ обновлён")
        playwright.expect(page.locator("#vflow-policy-cancel-own")).to_be_checked()
        playwright.expect(page.locator("#vflow-policy-limits")).to_have_value(
            re.compile(r'"max_fork_n": 2')
        )
        page.locator("#vflow-policy-cancel-own").uncheck()
        page.locator("#vflow-policy-limits").fill("{")
        page.locator("#vflow-save-policy").focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Некорректный JSON лимитов")
        playwright.expect(page.locator("#toast")).to_have_attribute("role", "alert")
        playwright.expect(page.locator("#toast")).to_have_attribute("aria-live", "assertive")
        playwright.expect(page.locator("#vflow-policy-limits")).to_have_attribute(
            "aria-invalid", "true"
        )
        playwright.expect(page.locator("#vflow-policy-limits")).to_be_focused()
        page.locator("#vflow-policy-limits").fill('{"max_transitions": 5}')
        page.locator("#vflow-save-policy").click()
        playwright.expect(page.locator("#toast")).to_have_text("Политика сохранена")
        playwright.expect(page.locator("#toast")).to_have_attribute("role", "status")
        playwright.expect(page.locator("#vflow-policy-limits")).not_to_have_attribute(
            "aria-invalid", "true"
        )
        page.locator("#vflow-save").focus()
        page.keyboard.press("ArrowRight")
        playwright.expect(page.locator("#vflow-validate")).to_be_focused()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Проверка пройдена")
        playwright.expect(page.locator("#vflow-validate")).to_be_focused()
        playwright.expect(page.locator("#vflow-diag")).to_have_attribute("role", "region")
        playwright.expect(page.locator("#vflow-diag")).not_to_have_attribute("aria-live", "polite")
        page.locator("#vflow-save").focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Черновик сохранён")
        playwright.expect(page.locator("#vflow-save")).to_be_focused()

        page.locator('[data-vflow-tab="runs"]').click()
        playwright.expect(page.locator('[data-vflow-run="run-events"]')).to_be_visible()
        page.locator('[data-vflow-run="run-events"]').click()
        playwright.expect(page.locator("#vflow-event-filter")).to_be_visible()
        playwright.expect(page.locator("#vflow-events .vflow-event")).to_have_count(3)

        page.locator("#vflow-event-filter").fill("NoDe_EnTeReD")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(1)
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_contain_text(
            "NODE_ENTERED"
        )
        page.locator("#vflow-event-filter").fill("point-3")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(1)
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_contain_text(
            "RUN_SUCCEEDED"
        )
        page.locator("#vflow-event-filter").fill("beta")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(1)
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_contain_text(
            "beta"
        )
        page.locator("#vflow-event-filter").fill("pass")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(2)
        page.locator("#vflow-event-filter").fill("zzz")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(0)
        playwright.expect(page.locator("#vflow-event-empty")).to_be_visible()
        playwright.expect(page.locator("#vflow-event-empty")).to_have_text("Нет событий по фильтру")
        page.locator("#vflow-event-filter").fill("")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(3)

        page.locator("#vflow-event-filter").fill("point-2")
        page.locator('[data-vflow-tab="runs"]').focus()
        page.keyboard.press("ArrowLeft")
        playwright.expect(page.locator('[data-vflow-tab="graph"]')).to_have_attribute(
            "aria-selected", "true"
        )
        playwright.expect(page.locator('[data-vflow-tab="graph"]')).to_be_focused()
        playwright.expect(page.locator("#vflow-tab-panel")).to_have_attribute(
            "aria-labelledby", "vflow-tab-graph"
        )
        node_select = page.locator('[data-node-select="pass"]')
        node_select.focus()
        page.keyboard.press("Enter")
        playwright.expect(node_select).to_be_focused()
        playwright.expect(node_select).to_have_attribute("aria-pressed", "true")
        playwright.expect(page.locator('[data-node="pass"]')).to_have_attribute("role", "button")
        playwright.expect(page.locator('[data-node-container="pass"]')).to_have_attribute(
            "role", "group"
        )
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("100%")
        page.locator("[data-zoom-in]").click()
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("110%")
        page.locator('[data-vflow-tab="runs"]').click()
        playwright.expect(page.locator("#vflow-event-filter")).to_have_value("point-2")
        playwright.expect(page.locator("#vflow-events .vflow-event:visible")).to_have_count(1)

        validation.update(
            {
                "valid": False,
                "diagnostics": [
                    {
                        "code": "NODE_INVALID",
                        "message": "Узел pass содержит ошибку",
                        "line": None,
                        "column": None,
                        "node_id": "pass",
                        "edge_id": None,
                    }
                ],
            }
        )
        page.locator("#vflow-validate").focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator("#toast")).to_have_text("Найдены ошибки")
        playwright.expect(page.locator("#toast")).to_have_attribute("role", "alert")
        playwright.expect(page.locator("#vflow-diag")).to_be_focused()
        playwright.expect(page.locator('[data-diagnostic-node="pass"]')).to_be_visible()
        page.locator('[data-diagnostic-node="pass"]').focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator('[data-vflow-tab="graph"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        playwright.expect(page.locator('[data-node="pass"]')).to_have_class(
            re.compile(r"\bselected\b")
        )
        playwright.expect(page.locator('[data-node-select="pass"]')).to_be_focused()
        playwright.expect(page.locator(".vflow-node.invalid")).to_have_count(1)
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("110%")
        page.locator("[data-zoom-reset]").click()
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("100%")

        page.locator('[data-vflow-tab="graph"]').click()
        playwright.expect(page.locator(".vflow-graph.interactive")).to_be_visible()
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("100%")

        page.locator("[data-zoom-in]").click()
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("110%")
        playwright.expect(page.locator("#vflow-stage")).to_have_attribute(
            "style", re.compile(r"scale\(1\.1\)")
        )
        page.locator("[data-zoom-out]").click()
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("100%")
        page.keyboard.press("Shift+=")
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("110%")
        page.keyboard.press("0")
        playwright.expect(page.locator("#vflow-zoom-level")).to_have_text("100%")

        page.locator("[data-zoom-fit]").click()
        playwright.expect(page.locator(".vflow-graph.interactive")).to_be_visible()
        canvas_box = page.locator(".vflow-graph.interactive").bounding_box()
        node_box = page.locator('[data-node="pass"]').bounding_box()
        assert canvas_box and node_box
        assert node_box["x"] >= canvas_box["x"] - 1
        assert node_box["y"] >= canvas_box["y"] - 1
        assert node_box["x"] + node_box["width"] <= canvas_box["x"] + canvas_box["width"] + 1
        assert node_box["y"] + node_box["height"] <= canvas_box["y"] + canvas_box["height"] + 1

        page.locator('[data-node="pass"]').click()
        playwright.expect(page.locator('[data-config-key="operations"]')).to_be_visible()
        page.locator("#vflow-apply-node").click()
        playwright.expect(page.locator("#toast")).to_contain_text("Конфигурация обновлена")

        page.locator('[data-delete-edge="0"]').click()
        playwright.expect(page.locator("[data-delete-edge]")).to_have_count(1)

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('[data-node="pass"]').click()
        page.locator("#vflow-delete-node").click()
        playwright.expect(page.locator(".vflow-node")).to_have_count(0)
        playwright.expect(page.locator(".vflow-canvas")).to_contain_text("Связей пока нет")
        playwright.expect(page.locator("#vflow-graph-canvas")).to_be_focused()

        undo = page.locator("#vflow-undo")
        redo = page.locator("#vflow-redo")
        playwright.expect(undo).to_be_enabled()
        playwright.expect(redo).to_be_disabled()

        undo.click()
        playwright.expect(page.locator('[data-node="pass"]')).to_have_count(1)
        playwright.expect(page.locator("[data-delete-edge]")).to_have_count(1)
        playwright.expect(redo).to_be_enabled()

        redo.click()
        playwright.expect(page.locator(".vflow-node")).to_have_count(0)
        playwright.expect(undo).to_be_enabled()
        playwright.expect(redo).to_be_disabled()

        page.keyboard.press("Control+z")
        playwright.expect(page.locator('[data-node="pass"]')).to_have_count(1)
        page.keyboard.press("Control+Shift+z")
        playwright.expect(page.locator(".vflow-node")).to_have_count(0)

        insert = page.locator('[data-insert="core.transform"]')
        insert.focus()
        page.keyboard.press("Enter")
        playwright.expect(page.locator(".vflow-node")).to_have_count(1)
        playwright.expect(page.locator('[data-node-select="transform_1"]')).to_be_focused()
        playwright.expect(page.locator('[data-node-select="transform_1"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        undo.click()
        playwright.expect(page.locator(".vflow-node")).to_have_count(0)
        redo.click()
        playwright.expect(page.locator(".vflow-node")).to_have_count(1)

        # A node inserted from the palette must use the registry's string
        # version ("1.0"), otherwise saving compiles to an unregistered
        # ``core.transform@1`` handler.
        page.locator("#vflow-save").click()
        playwright.expect(page.locator("#toast")).to_have_text("Черновик сохранён")
        assert draft_requests
        inserted_nodes = [
            node
            for node in draft_requests[-1].get("graph", {}).get("nodes", [])
            if node.get("id") == "transform_1"
        ]
        assert inserted_nodes
        assert inserted_nodes[0]["type_version"] == "1.0"

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile_errors = []
        mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
        mobile.add_init_script(
            """
            window.EventSource = class {
                constructor() {}
                close() {}
            };
            """
        )
        mobile.route("**/*", handle)
        mobile.goto("http://vflow.test/#/pipelines")
        playwright.expect(mobile.locator(".vflow-card h3")).to_have_text("Демо-пайплайн")
        playwright.expect(mobile.locator("#pipeline-run-form")).to_be_visible()
        playwright.expect(mobile.locator("#pipeline-run-status")).to_be_visible()
        playwright.expect(mobile.locator("#refresh-pipelines")).to_be_visible()
        playwright.expect(mobile.locator('[data-pipeline="demo"]')).to_have_attribute(
            "role", "button"
        )
        assert_no_horizontal_overflow(mobile)
        mobile.locator(".mobile-menu").click()
        playwright.expect(mobile.locator(".sidebar")).to_be_visible()
        playwright.expect(mobile.locator('[data-route="dialogs"]').first).to_be_focused()
        playwright.expect(mobile.locator('[data-route="pipelines"]')).to_have_attribute(
            "aria-current", "page"
        )
        mobile.keyboard.press("Escape")
        playwright.expect(mobile.locator(".sidebar")).not_to_be_visible()
        playwright.expect(mobile.locator(".mobile-menu")).to_be_focused()
        playwright.expect(mobile.locator(".mobile-menu")).to_have_attribute(
            "aria-expanded", "false"
        )

        for width in (768, 1024):
            mobile.set_viewport_size({"width": width, "height": 844})
            assert_no_horizontal_overflow(mobile)
            playwright.expect(mobile.locator("#pipeline-run-form")).to_be_visible()
            playwright.expect(mobile.locator("#pipeline-run-status")).to_be_visible()

        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto("http://vflow.test/#/admin-pipelines")
        playwright.expect(mobile.locator("#pipeline-create-form")).to_be_visible()
        assert_no_horizontal_overflow(mobile)
        mobile.locator('[data-edit-pipeline="demo"]').click()
        playwright.expect(mobile.locator("#vflow-code")).to_have_value(SOURCE)
        playwright.expect(mobile.locator("#vflow-palette-search")).to_be_visible()
        playwright.expect(mobile.locator("#vflow-policy-limits")).to_be_visible()
        playwright.expect(mobile.locator("#vflow-save")).to_be_visible()
        playwright.expect(mobile.locator("#vflow-validate")).to_be_visible()
        playwright.expect(mobile.locator("#vflow-undo")).to_be_disabled()

        for width in (390, 768, 1024):
            mobile.set_viewport_size({"width": width, "height": 844})
            assert_no_horizontal_overflow(mobile)
            playwright.expect(mobile.locator("#vflow-code")).to_be_visible()

        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.locator('[data-vflow-tab="runs"]').click()
        playwright.expect(mobile.locator('[data-vflow-run="run-events"]')).to_be_visible()
        mobile.locator('[data-vflow-run="run-events"]').click()
        playwright.expect(mobile.locator("#vflow-event-filter")).to_be_visible()
        playwright.expect(mobile.locator("#vflow-events .vflow-event")).to_have_count(3)
        assert_no_horizontal_overflow(mobile)

        mobile.locator('[data-vflow-tab="graph"]').click()
        playwright.expect(mobile.locator(".vflow-graph.interactive")).to_be_visible()
        playwright.expect(mobile.locator('[data-vflow-tab="graph"]')).to_have_attribute(
            "aria-selected", "true"
        )
        mobile.locator('[data-vflow-tab="graph"]').focus()
        mobile.keyboard.press("ArrowRight")
        playwright.expect(mobile.locator('[data-vflow-tab="runs"]')).to_have_attribute(
            "aria-selected", "true"
        )
        mobile.keyboard.press("ArrowLeft")
        playwright.expect(mobile.locator('[data-vflow-tab="graph"]')).to_have_attribute(
            "aria-selected", "true"
        )

        validation.update(
            {
                "valid": False,
                "diagnostics": [
                    {
                        "code": "EDGE_INVALID",
                        "message": "Связь pass → end содержит ошибку",
                        "line": None,
                        "column": None,
                        "node_id": None,
                        "edge_id": "pass:end",
                    }
                ],
            }
        )
        mobile.locator("#vflow-validate").click()
        playwright.expect(mobile.locator("#toast")).to_have_text("Найдены ошибки")
        playwright.expect(mobile.locator('[data-diagnostic-edge="pass:end"]')).to_be_visible()
        playwright.expect(mobile.locator(".vflow-edge-path.invalid")).to_have_count(1)
        playwright.expect(mobile.locator(".vflow-edge-chip.invalid")).to_have_count(1)
        playwright.expect(mobile.locator(".vflow-edge-chip.invalid")).to_contain_text(
            "pass → end"
        )
        playwright.expect(mobile.locator(".vflow-port.invalid")).to_have_count(2)
        assert_no_horizontal_overflow(mobile)

        mobile.locator('[data-diagnostic-edge="pass:end"]').click()
        playwright.expect(mobile.locator('[data-node="pass"]')).to_have_class(
            re.compile(r"\bselected\b")
        )
        playwright.expect(mobile.locator('[data-vflow-tab="graph"]')).to_have_class(
            re.compile(r"\bactive\b")
        )
        playwright.expect(mobile.locator(".vflow-edge-chip.invalid.focused")).to_have_count(1)
        playwright.expect(mobile.locator(".vflow-edge-chip.invalid.focused")).to_be_focused()
        assert_no_horizontal_overflow(mobile)
        assert not mobile_errors
        mobile.close()

        assert not errors
        browser.close()
