from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.opencode_client import OpenCodeError


class CatalogClient:
    async def providers(self) -> dict[str, Any]:
        return {
            "connected": ["configured"],
            "all": [
                {
                    "id": "configured",
                    "apiKey": "secret",
                    "models": {
                        "coder": {"name": "Coder"},
                        "other": {"name": "Other"},
                    },
                },
                {"id": "unconfigured", "models": {"unused": {"name": "Unavailable"}}},
            ],
        }


class CatalogManager:
    def __init__(self, fail: bool = False):
        self.workspaces: list[str] = []
        self.fail = fail

    async def ensure(self, workspace: str) -> CatalogClient:
        assert Path(workspace).is_dir()
        self.workspaces.append(workspace)
        if self.fail:
            raise OpenCodeError("unavailable")
        return CatalogClient()


@pytest.mark.parametrize("workspace_assigned", [False, True])
async def test_admin_catalog_uses_connected_opencode_models(
    settings, workspace, workspace_assigned
):
    app = create_app(
        replace(settings, allowed_models=("configured/coder",)), start_dispatcher=False
    )
    async with app.router.lifespan_context(app):
        db = app.state.database
        admin = db.get_user_by_login("admin")
        if workspace_assigned:
            own_workspace = workspace / "admin"
            own_workspace.mkdir()
            admin = db.update_user(admin["id"], workspace_path=str(own_workspace))
        else:
            own_workspace = workspace / ".opencode-web-catalog"
        db.create_auth_session(admin, "admin-token", "csrf", 1)
        manager = CatalogManager()
        app.state.manager = manager
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("ocw_session", "admin-token")
            response = await client.get("/api/options")
            assert response.status_code == 200
            assert response.json()["models"] == [
                {"id": "configured/coder", "name": "Coder", "provider": "configured"}
            ]
            assert "secret" not in response.text
            assert manager.workspaces == [str(own_workspace)]
            assert db.get_user(admin["id"])["workspace_path"] == admin["workspace_path"]
            assert db.list_chats() == []
            assert db.list_jobs() == []
            # The catalog is the same endpoint used by ordinary chat owners.
            user_workspace = workspace / "worker"
            user_workspace.mkdir()
            user = db.create_user("worker", "long-user-password", "user", str(user_workspace))
            db.create_auth_session(user, "worker-token", "csrf", 1)
            client.cookies.set("ocw_session", "worker-token")
            user_response = await client.get("/api/options")
            assert user_response.json()["models"] == response.json()["models"]
            assert manager.workspaces[-1] == str(user_workspace)


@pytest.mark.parametrize("failure", ["opencode", "directory"])
async def test_admin_catalog_reports_unavailability_without_manual_models(
    settings, workspace, failure
):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db = app.state.database
        admin = db.get_user_by_login("admin")
        db.create_auth_session(admin, "admin-token", "csrf", 1)
        if failure == "directory":
            (workspace / ".opencode-web-catalog").write_text("not a directory")
        app.state.manager = CatalogManager(fail=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("ocw_session", "admin-token")
            response = await client.get("/api/options")
        assert response.status_code == 200
        assert response.json()["models"] == []
        assert response.json()["opencode_unavailable"] is True
