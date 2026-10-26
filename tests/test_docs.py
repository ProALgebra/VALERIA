from __future__ import annotations

from dataclasses import replace

import httpx

from opencode_web.app import PACKAGE_DIR, create_app
from opencode_web.config import Settings

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
INDEX = (PACKAGE_DIR / "static" / "index.html").read_bytes()


async def fetch(settings, path):
    app = create_app(settings, start_dispatcher=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


def test_docs_setting_reads_app_enable_docs(monkeypatch, workspace):
    monkeypatch.setenv("ALLOWED_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.delenv("APP_ENABLE_DOCS", raising=False)
    assert Settings.from_env().enable_docs is False
    monkeypatch.setenv("APP_ENABLE_DOCS", "true")
    assert Settings.from_env().enable_docs is True


async def test_api_docs_are_off_by_default(settings):
    assert settings.enable_docs is False
    for path in ("/docs", "/redoc"):
        response = await fetch(settings, path)
        # The page URL falls through to the app shell; no schema is exposed.
        assert response.content == INDEX, path
    # The schema URL names a file, so it is a plain 404 rather than the app shell.
    schema = await fetch(settings, "/openapi.json")
    assert schema.status_code == 404 and schema.json() == {"detail": "Not Found"}
    assert (await fetch(settings, "/docs/oauth2-redirect")).content == INDEX


async def test_api_docs_can_be_enabled(settings):
    enabled = replace(settings, enable_docs=True)
    schema = await fetch(enabled, "/openapi.json")
    assert schema.status_code == 200 and "/api/health" in schema.json()["paths"]
    for path in ("/docs", "/redoc"):
        response = await fetch(enabled, path)
        assert response.status_code == 200 and response.content != INDEX, path
