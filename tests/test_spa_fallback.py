from __future__ import annotations

import re

from test_chat_actions import ui_app as ui_app

from opencode_web.app import PACKAGE_DIR

INDEX = (PACKAGE_DIR / "static" / "index.html").read_bytes()


async def test_page_urls_serve_the_single_page_app(ui_app):
    t = ui_app
    t.client.cookies.clear()  # pages load before signing in
    for path in (
        "/", "/questions/chat-1", "/tasks/runs/run-1", "/folder/chat-2",
        "/admin/users?status=blocked", "/apiary", "/staticky/page",
        # A dot in a middle segment or in the query is still a page URL.
        "/admin/users/i.sokolova/edit", "/admin/users?q=i.sokolova", "/folder/chat-2?file=a.py",
        "/admin/users/",
    ):
        response = await t.client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.content == INDEX, path


async def test_unknown_api_and_static_paths_stay_not_found(ui_app):
    t = ui_app
    for method, path in (
        ("GET", "/api/nope"), ("GET", "/api"), ("GET", "/api/"), ("POST", "/api/nope"),
        ("DELETE", "/api/admin/nope"), ("GET", "/static/"),
    ):
        response = await t.client.request(method, path)
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "Not Found"}, (method, path)
    response = await t.client.get("/static/missing.js")
    assert response.status_code == 404 and response.content != INDEX
    # Real routes keep their own answers.
    assert (await t.client.get("/api/health")).json()["status"] == "ok"
    assert (await t.client.post("/api/health")).status_code == 405


async def test_file_like_paths_are_plain_not_found(ui_app):
    t = ui_app
    t.client.cookies.clear()
    for path in (
        "/favicon.ico", "/robots.txt", "/main.js", "/assets/app.css", "/admin/users/i.sokolova",
        "/.env", "/folder/report.pdf?download=1", "/favicon.ico/", "/admin/users/i.sokolova/",
    ):
        response = await t.client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}, path
    # Real static files are still served.
    response = await t.client.get("/static/index.html")
    assert response.status_code == 200 and response.content == INDEX


async def test_built_ui_assets_are_served_from_static(ui_app):
    t = ui_app
    t.client.cookies.clear()
    html = INDEX.decode()
    # The production build references its favicon and hashed assets under /static/.
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in html
    favicon = await t.client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        response = await t.client.get(src)
        assert response.status_code == 200, src
    # The old UI is gone.
    for old in ("/static/app.js", "/static/styles.css", "/static/vendor/markdown-it.esm.min.mjs"):
        assert (await t.client.get(old)).status_code == 404, old
