import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


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


def test_json_processor_admin_page():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    settings = {
        "id": 1,
        "enabled": False,
        "base_url": "",
        "api_key_ref": "",
        "model": "",
        "local_repair_enabled": True,
        "max_llm_attempts": 2,
        "request_timeout_seconds": 45,
        "max_input_bytes": 65536,
        "max_output_bytes": 65536,
        "max_output_tokens": 4096,
        "temperature": 0.0,
        "repair_mode": "syntax_only",
        "on_processor_failure": "fail",
        "concurrency_limit": 4,
        "queue_limit": 1000,
        "raw_output_retention_days": 30,
        "attempt_retention_days": 90,
        "version": 1,
        "updated_by": None,
        "created_at": "2026-11-20T12:00:00Z",
        "updated_at": "2026-11-20T12:00:00Z",
        "provider_configured": False,
        "api_key_ref_configured": False,
    }
    jobs = [
        {
            "id": "job-ok",
            "source_type": "standalone",
            "status": "succeeded",
            "method": "local",
            "attempts": 1,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-11-20T12:00:00Z",
            "updated_at": "2026-11-20T12:00:01Z",
        },
        {
            "id": "job-failed",
            "source_type": "task",
            "task_run_id": "run-1",
            "status": "failed",
            "method": "llm",
            "attempts": 1,
            "error_code": "NOT_REPAIRABLE",
            "error_message": "raw output is not valid JSON",
            "created_at": "2026-11-20T12:01:00Z",
            "updated_at": "2026-11-20T12:01:01Z",
        },
    ]
    metrics = {
        "settings": {
            "enabled": False,
            "version": 1,
            "provider_configured": False,
        },
        "queue": {"queued": 0, "processing": 0},
        "jobs": {
            "total": 2,
            "terminal": 2,
            "succeeded": 1,
            "failed": 1,
            "success_rate": 0.5,
            "by_status": {"succeeded": 1, "failed": 1},
            "by_method": {"local": 1, "llm": 1},
        },
        "failures": {
            "parse": 1,
            "schema": 0,
            "content": 0,
            "provider": 0,
            "provider_errors": 0,
            "by_category": {"parse": 1},
            "by_code": {"NOT_REPAIRABLE": 1},
        },
        "attempts": {
            "total": 2,
            "avg_latency_ms": 17,
            "latency": {"samples": 2, "p50_ms": 16, "p95_ms": 17, "max_ms": 17},
            "p50_latency_ms": 16,
            "p95_latency_ms": 17,
            "by_method": {"local": 1, "llm": 1},
            "by_status": {"succeeded": 1, "failed": 1},
            "tokens": {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
        },
        "last_24h": {
            "jobs_created": 2,
            "succeeded": 1,
            "failed": 1,
            "cancelled": 0,
            "attempts": 2,
            "provider_errors": 0,
        },
        "retention": {
            "jobs_with_expired_raw_output": 0,
            "expired_attempts": 0,
            "raw_output_retention_days": 30,
            "attempt_retention_days": 90,
        },
    }
    patch_requests = []
    sandbox_requests = []
    purge_requests = []

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
                    "vflow_enabled": False,
                }
            )
        elif parsed.path == "/api/admin/json-processor/settings":
            if route.request.method == "PATCH":
                body = json.loads(route.request.post_data or "{}")
                patch_requests.append(body)
                settings.update(body.get("changes") or {})
                settings["version"] += 1
                settings["provider_configured"] = bool(
                    settings["base_url"] and settings["model"]
                )
                route.fulfill(json=settings)
            else:
                route.fulfill(json=settings)
        elif parsed.path == "/api/admin/json-processor/providers":
            route.fulfill(
                json={
                    "protocol": "openai-compatible",
                    "auth": {
                        "secret_value_exposed": False,
                        "secret_reference_field": "api_key_ref",
                    },
                    "presets": [],
                    "current": {
                        "provider_configured": False,
                        "base_url": "",
                        "model": "",
                    },
                }
            )
        elif parsed.path == "/api/admin/json-processor/metrics":
            route.fulfill(json=metrics)
        elif parsed.path == "/api/admin/json-processor/jobs":
            query = parse_qs(parsed.query)
            selected = query.get("status", [""])[0]
            items = [item for item in jobs if not selected or item["status"] == selected]
            route.fulfill(
                json={
                    "items": items,
                    "total": len(items),
                    "limit": 25,
                    "offset": 0,
                    "has_more": False,
                }
            )
        elif parsed.path == "/api/admin/json-processor/jobs/job-failed":
            route.fulfill(
                json={
                    **jobs[1],
                    "schema": {"type": "object"},
                    "result": None,
                    "has_result": False,
                    "raw_hash": "abc123",
                    "settings_version": 1,
                    "prompt_version": "v1",
                    "attempts_history": [
                        {
                            "id": "attempt-1",
                            "attempt_no": 1,
                            "method": "llm",
                            "provider": "openai-compatible",
                            "model": "repair-model",
                            "status": "failed",
                            "latency_ms": 23,
                            "validation_error": "JSON does not satisfy the schema",
                            "request_meta": {"phase": "llm"},
                            "token_usage": {"total_tokens": 9},
                        }
                    ],
                }
            )
        elif parsed.path == "/api/admin/json-processor/retention/purge":
            purge_requests.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                json={
                    "ok": True,
                    "dry_run": False,
                    "jobs_output_purged": 0,
                    "attempts_deleted": 0,
                    "has_more": False,
                    "raw_output_cutoff": "2026-10-23T12:00:00+00:00",
                    "attempt_cutoff": "2026-08-24T12:00:00+00:00",
                    "batch_size": 500,
                }
            )
        elif parsed.path == "/api/admin/json-processor/check-connection":
            route.fulfill(
                json={
                    "ok": True,
                    "status_code": 200,
                    "model_available": True,
                    "latency_ms": 7,
                    "model": "repair-model",
                    "settings_version": 1,
                }
            )
        elif parsed.path == "/api/admin/json-processor/test":
            sandbox_requests.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                json={
                    "ok": True,
                    "mode": "syntax_only",
                    "method": "local",
                    "value": {"ok": True},
                    "error": None,
                    "diagnostics": [],
                    "attempts": [
                        {
                            "number": 1,
                            "phase": "local",
                            "mode": "syntax_only",
                            "ok": True,
                            "strategy": "normalized",
                            "normalizations": ["trailing_commas"],
                            "candidate_count": 1,
                            "schema_valid": True,
                            "validation_error": None,
                            "error": None,
                        }
                    ],
                    "settings_version": 1,
                    "raw_input_bytes": 14,
                    "job_created": False,
                }
            )
        else:
            route.fulfill(json={})

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(channel="chromium")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
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
        page.goto("http://json-processor.test/#/admin-json-processor")
        playwright.expect(page.locator("#jp-title")).to_be_visible()
        playwright.expect(page.locator("#jp-settings-form")).to_be_visible()
        playwright.expect(page.locator("#jp-sandbox-form")).to_be_visible()
        playwright.expect(page.locator("#jp-jobs-title")).to_be_visible()
        playwright.expect(page.locator(".jp-metrics")).to_be_visible()

        page.locator("#jp-api-key-ref").fill("vault://json-processor/openai")
        page.locator("#jp-check-connection").click()
        playwright.expect(page.locator("#jp-connection-result")).to_contain_text(
            "Соединение установлено"
        )

        page.locator("#jp-sandbox-raw").fill('{"ok": true,}')
        page.locator("#jp-sandbox-schema").fill('{"type": "object"}')
        page.locator("#jp-run-sandbox").click()
        playwright.expect(page.locator("#jp-sandbox-result")).to_contain_text(
            "Валидный JSON"
        )
        playwright.expect(page.locator("#jp-sandbox-result")).to_contain_text(
            '"ok": true'
        )
        playwright.expect(page.locator("#jp-sandbox-result")).to_contain_text(
            "job не создан"
        )
        assert sandbox_requests and sandbox_requests[0]["raw_output"] == '{"ok": true,}'

        page.locator("#jp-enabled").check()
        page.locator("#jp-base-url").fill("https://provider.example")
        page.locator("#jp-model").fill("repair-model")
        page.locator("#jp-max-attempts").fill("1")
        page.locator("#jp-save-settings").click()
        playwright.expect(page.locator("#toast")).to_contain_text(
            "Настройки сохранены"
        )
        assert patch_requests
        assert patch_requests[0]["expected_version"] == 1
        assert patch_requests[0]["changes"]["enabled"] is True
        assert "api_key" not in patch_requests[0]["changes"]

        page.locator("#jp-filter-status").select_option("failed")
        page.locator("#jp-job-filters button[type=submit]").click()
        playwright.expect(page.locator("#jp-jobs-title")).to_be_visible()
        playwright.expect(page.locator("td:nth-child(5) .badge.red").first).to_contain_text(
            "NOT_REPAIRABLE"
        )
        page.locator('[data-jp-job="job-failed"]').click()
        playwright.expect(page.locator("#jp-job-detail")).to_contain_text(
            "История попыток"
        )
        playwright.expect(page.locator("#jp-job-detail")).to_contain_text(
            "repair-model"
        )
        playwright.expect(page.locator("#jp-job-detail")).not_to_contain_text(
            "Authorization"
        )
        playwright.expect(page.locator("#jp-job-detail")).not_to_contain_text(
            '"raw_output"'
        )

        playwright.expect(page.locator("#jp-purge-retention")).to_be_visible()
        playwright.expect(page.locator("#jp-metrics-title")).to_be_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#jp-purge-retention").click()
        playwright.expect(page.locator("#toast")).to_contain_text("Удалено outputs")
        assert purge_requests == [{"confirm": True, "batch_size": 500}]

        page.set_viewport_size({"width": 390, "height": 844})
        assert_no_horizontal_overflow(page)
        playwright.expect(page.locator("#jp-settings-form")).to_be_visible()
        playwright.expect(page.locator("#jp-sandbox-form")).to_be_visible()
        assert not errors
        browser.close()
