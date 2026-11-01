from pathlib import Path
from urllib.parse import urlparse

import pytest


def static_response(route, static: Path, path: str) -> bool:
    if path != "/" and not path.startswith("/static/"):
        return False
    file = static / (path.removeprefix("/static/") if path != "/" else "index.html")
    mime = {
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".html": "text/html",
    }
    route.fulfill(body=file.read_text(), content_type=mime[file.suffix])
    return True


def test_user_updates_mattermost_username_in_profile():
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    user = {
        "id": "worker", "login": "worker", "role": "user", "tasks_only": True,
        "workspace_path": "/workspace", "status": "active", "max_active_jobs": 2,
        "mattermost_username": None,
    }
    updates = []

    def handle(route):
        path = urlparse(route.request.url).path
        if static_response(route, static, path):
            return
        if path == "/api/auth/me":
            payload = {"user": user, "csrf_token": "csrf"}
        elif path == "/api/profile":
            updates.append(route.request.post_data_json)
            user.update(mattermost_username="akhamitsevich", mattermost_user_id="m" * 26)
            payload = user
        else:
            payload = []
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.add_init_script("window.EventSource = class { close() {} };")
        page.route("**/*", handle)
        page.goto("http://users.test/#/profile")
        playwright.expect(page.locator("#profile-workspace-form")).to_have_count(0)
        username = page.locator('#profile-form [name="mattermost_username"]')
        playwright.expect(username).to_be_visible()
        username.fill("akhamitsevich")
        page.get_by_role("button", name="Сохранить профиль").click()
        playwright.expect(username).to_have_value("akhamitsevich")
        assert updates == [{"mattermost_username": "akhamitsevich"}]
        browser.close()


@pytest.mark.parametrize(
    ("existing_mattermost_username", "submitted_mattermost_username"),
    [
        pytest.param("old.user", "akhamitsevich", id="mattermost-linked"),
        pytest.param(None, None, id="mattermost-empty"),
    ],
)
def test_admin_opens_user_card_and_updates_visible_settings(
    existing_mattermost_username, submitted_mattermost_username,
):
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    admin = {"id": "admin", "login": "admin", "role": "admin", "tasks_only": False}
    worker = {
        "id": "worker", "login": "worker", "role": "user", "tasks_only": False,
        "workspace_path": "/workspace", "status": "active", "max_active_jobs": 2,
        "mattermost_username": existing_mattermost_username, "created_at": "2026-10-01",
        "updated_at": "2026-10-02", "last_login_at": "2026-10-03",
    }
    updates = []

    def handle(route):
        path = urlparse(route.request.url).path
        if static_response(route, static, path):
            return
        if path == "/api/auth/me":
            payload = {"user": admin, "csrf_token": "csrf"}
        elif path == "/api/config/public":
            payload = {"max_active_jobs_hard_limit": 5}
        elif path == "/api/admin/users" and route.request.method == "GET":
            payload = [worker]
        elif path == "/api/admin/users/worker" and route.request.method == "PATCH":
            data = route.request.post_data_json
            updates.append(data)
            worker.update(
                role=data["role"], tasks_only=data["tasks_only"],
                workspace_path=data["workspace_path"], status=data["status"],
                mattermost_username=data["mattermost_username"],
                max_active_jobs=data["max_active_jobs"],
            )
            payload = worker
        else:
            payload = []
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.add_init_script("window.EventSource = class { close() {} };")
        page.route("**/*", handle)
        page.goto("http://users.test/#/users")
        page.locator('[data-user-card="worker"]').click()
        form = page.locator("#user-settings-form")
        playwright.expect(form).to_be_visible()
        playwright.expect(form.locator('[name="workspace_path"]')).to_have_value("/workspace")
        playwright.expect(form.locator('[name="mattermost_username"]')).to_have_value(
            existing_mattermost_username or ""
        )
        playwright.expect(form.locator('[name="max_active_jobs"]')).to_have_value("2")
        form.locator('[name="account_type"]').select_option("tasks_only")
        if submitted_mattermost_username:
            form.locator('[name="mattermost_username"]').fill(submitted_mattermost_username)
        form.locator('[name="max_active_jobs"]').fill("3")
        page.get_by_role("button", name="Сохранить настройки").click()
        playwright.expect(form.locator('[name="mattermost_username"]')).to_have_value(
            submitted_mattermost_username or ""
        )
        assert updates == [{
            "role": "user",
            "tasks_only": True,
            "workspace_path": "/workspace",
            "mattermost_username": submitted_mattermost_username,
            "status": "active",
            "max_active_jobs": 3,
        }]
        browser.close()


@pytest.mark.parametrize("settings_page", ["profile", "users"])
def test_admin_assigns_own_workspace_without_reload(settings_page):
    playwright = pytest.importorskip("playwright.sync_api")
    static = Path(__file__).parents[1] / "src/opencode_web/static"
    admin = {
        "id": "admin", "login": "admin", "role": "admin", "tasks_only": False,
        "workspace_path": None, "status": "active", "max_active_jobs": None,
        "mattermost_username": None,
    }
    folder = "/workspaces/admin/project"
    updates = []
    auth_requests = []
    errors = []

    def handle(route):
        path = urlparse(route.request.url).path
        if static_response(route, static, path):
            return
        payload = []
        if path == "/api/auth/me":
            auth_requests.append(path)
            payload = {"user": admin, "csrf_token": "csrf"}
        elif path == "/api/config/public":
            payload = {
                "workspace": admin["workspace_path"], "agents": ["build"],
                "default_agent": "build", "max_active_sessions": 1,
                "max_active_jobs_hard_limit": 5,
            }
        elif path == "/api/options":
            payload = {"agents": ["build"], "default_agent": "build", "models": []}
        elif path == "/api/tasks":
            payload = [{"id": "task", "title": "Тестовая таска", "description": ""}]
        elif path == "/api/admin/users":
            payload = [admin]
        elif path == "/api/admin/users/admin" and route.request.method == "PATCH":
            data = route.request.post_data_json
            updates.append(data)
            assert route.request.headers["x-csrf-token"] == "csrf"
            if settings_page == "profile" and len(updates) == 1:
                route.fulfill(status=409, json={"detail": "Workspace is already assigned"})
                return
            admin["workspace_path"] = folder
            payload = admin
        route.fulfill(json=payload)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("window.EventSource = class { close() {} };")
        page.route("**/*", handle)
        page.goto(f"http://users.test/#/{'dialogs' if settings_page == 'profile' else 'users'}")
        if settings_page == "profile":
            playwright.expect(page.get_by_role("button", name="Создать диалог")).to_be_disabled()
            page.get_by_role("button", name="Назначить рабочую папку").click()
            form = page.locator("#profile-workspace-form")
            form.locator('[name="workspace_path"]').fill("occupied")
            form.get_by_role("button", name="Сохранить папку").click()
            playwright.expect(page.locator("#toast")).to_have_text("Workspace is already assigned")
            playwright.expect(form.locator('[name="workspace_path"]')).to_have_value("occupied")
            playwright.expect(form.get_by_role("button", name="Сохранить папку")).to_be_enabled()
            form.locator('[name="workspace_path"]').fill("  admin/project  ")
            form.get_by_role("button", name="Сохранить папку").click()
            playwright.expect(page.locator("#toast")).to_have_text("Рабочая папка сохранена")
            playwright.expect(form.locator('[name="workspace_path"]')).to_have_value(folder)
            assert updates == [
                {"workspace_path": "occupied"}, {"workspace_path": "admin/project"},
            ]
        else:
            form = page.locator("#user-settings-form")
            form.locator('[name="workspace_path"]').fill("admin/project")
            form.get_by_role("button", name="Сохранить настройки").click()
            playwright.expect(form.locator('[name="workspace_path"]')).to_have_value(folder)
            assert len(updates) == 1
            assert updates[0]["workspace_path"] == "admin/project"

        page.locator('[data-route="tasks"]').click()
        playwright.expect(page.get_by_role("button", name="Запустить таску")).to_be_enabled()
        page.locator('[data-route="consultations"]').click()
        playwright.expect(page.get_by_role("button", name="Начать диалог")).to_be_enabled()
        page.locator('[data-route="dialogs"]').click()
        playwright.expect(page.locator('#new-chat input[disabled]')).to_have_value(folder)
        playwright.expect(page.get_by_role("button", name="Создать диалог")).to_be_enabled()
        shortcut = page.get_by_role("button", name="Назначить рабочую папку")
        playwright.expect(shortcut).to_have_count(0)
        assert len(auth_requests) == 1
        assert not errors
        browser.close()
