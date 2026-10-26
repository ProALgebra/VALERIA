from __future__ import annotations

from dataclasses import replace

import httpx

from opencode_web.app import create_app
from opencode_web.security_headers import CONTENT_SECURITY_POLICY


async def fetch_all(settings, *paths):
    app = create_app(settings, start_dispatcher=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return [await client.get(path) for path in paths]


def directives(policy: str) -> dict[str, list[str]]:
    parsed = {}
    for part in policy.split(";"):
        name, *values = part.split()
        parsed[name] = values
    return parsed


def test_policy_blocks_inline_scripts_plugins_and_framing():
    policy = directives(CONTENT_SECURITY_POLICY)
    assert policy["default-src"] == ["'self'"]
    # 'unsafe-eval' is for Koobiq's DateFormatter (messageformat compiles with `new Function`).
    assert policy["script-src"] == ["'self'", "'unsafe-eval'"]
    assert "'unsafe-inline'" not in policy["script-src"]
    assert policy["object-src"] == ["'none'"]
    assert policy["base-uri"] == ["'self'"]
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["connect-src"] == ["'self'"]


async def test_spa_shell_responses_carry_the_csp(settings):
    paths = ("/", "/questions/123", "/admin/users", "/static/index.html", "/docs")
    responses = await fetch_all(settings, *paths)
    for response in responses:
        assert response.status_code == 200, response.url
        assert response.headers["content-type"].startswith("text/html"), response.url
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY, response.url


async def test_api_and_asset_responses_have_no_csp(settings):
    api, favicon = await fetch_all(settings, "/api/does-not-exist", "/static/favicon.svg")
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}
    assert "content-security-policy" not in api.headers
    assert favicon.status_code == 200 and "content-security-policy" not in favicon.headers


async def test_opt_in_api_docs_pages_are_exempt(settings):
    enabled = replace(settings, enable_docs=True)
    docs, shell = await fetch_all(enabled, "/docs", "/")
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers
    assert shell.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_production_build_has_no_inline_scripts():
    from opencode_web.app import PACKAGE_DIR

    shell = (PACKAGE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert "onload=" not in shell
    assert "<script>" not in shell and "<script " not in shell.replace('<script src="', "")
