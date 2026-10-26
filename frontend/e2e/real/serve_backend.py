#!/usr/bin/env python3
"""Real VALERIA backend for the Playwright smoke suite (playwright.real.config.ts).

Starts `uv run opencode-web` on 127.0.0.1 with a temp SQLite database, docs and debug off, and the
fake OpenCode server from fake-opencode/. Then configures the consultation model, creates the user
`smoke-user` with a workspace, prints `VALERIA READY` and waits. On SIGTERM/SIGINT it stops the
backend (and its OpenCode children) and removes the temp dir.

Passwords come from E2E_ADMIN_PASSWORD / E2E_USER_PASSWORD (the Playwright config generates them).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fake-opencode"))
os.environ.setdefault("SMOKE_APP_PORT", os.environ.get("E2E_BACKEND_PORT", "8084"))

import smoke_api  # noqa: E402  (reuses its API client and helpers; reads SMOKE_APP_PORT)

HOST, PORT, BASE = smoke_api.HOST, smoke_api.PORT, smoke_api.BASE
OC_START = int(os.environ.get("E2E_OPENCODE_PORT_START", "46120"))
OC_END = OC_START + 10


def main() -> int:
    admin_password = os.environ["E2E_ADMIN_PASSWORD"]
    user_password = os.environ["E2E_USER_PASSWORD"]
    ports = [PORT, *range(OC_START, OC_END + 1)]
    busy = smoke_api.listening_pids(ports)
    if busy:
        print(f"ports already in use: {busy}", flush=True)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="valeria-e2e-")).resolve()
    ws_root = tmp / "ws"
    ws_root.mkdir()
    password_file = tmp / "admin-password"
    password_file.write_text(admin_password, encoding="utf-8")
    password_file.chmod(0o600)
    env = {
        **os.environ,
        "APP_HOST": HOST,
        "APP_PORT": str(PORT),
        "APP_BASE_URL": BASE,
        "APP_DATABASE_PATH": str(tmp / "db.sqlite"),
        "APP_COOKIE_SECURE": "false",
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "BOOTSTRAP_ADMIN_LOGIN": "admin",
        "BOOTSTRAP_ADMIN_PASSWORD_FILE": str(password_file),
        "ALLOWED_WORKSPACE_ROOTS": str(ws_root),
        "ALLOW_SHARED_WORKSPACES": "false",
        "OPENCODE_BIN": str(smoke_api.FAKE),
        "OPENCODE_HOST": HOST,
        "OPENCODE_SERVER_USERNAME": "opencode",
        "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(24),
        "OPENCODE_PORT_RANGE_START": str(OC_START),
        "OPENCODE_PORT_RANGE_END": str(OC_END),
        "OPENCODE_DEFAULT_AGENT": "build",
        "OPENCODE_ALLOWED_AGENTS": "build,plan",
        "OPENCODE_DEFAULT_MODEL": "",
        "OPENCODE_ALLOWED_MODELS": "*",
        "OPENCODE_START_TIMEOUT_SECONDS": "30",
        "MAX_ACTIVE_SESSIONS": "1",
        "MAX_ACTIVE_SESSIONS_PER_USER": "0",
        # cli.py calls load_dotenv(); empty values keep the repo .env from leaking in.
        "MATTERMOST_TOKEN_FILE": "",
        "MATTERMOST_CA_BUNDLE": "",
        "LOG_LEVEL": "WARNING",
        # Explicit, so a developer .env (cli.py calls load_dotenv) cannot turn the API docs on.
        "APP_ENABLE_DOCS": "false",
    }

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log = (tmp / "backend.log").open("wb")
    backend = subprocess.Popen(
        ["uv", "run", "opencode-web"], cwd=smoke_api.REPO, env=env,
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    code = 1
    try:
        def healthy() -> bool:
            if backend.poll() is not None:
                raise smoke_api.StepFailed(f"backend exited with {backend.returncode}")
            try:
                with urllib.request.urlopen(BASE + "/api/health", timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False

        smoke_api.wait_for("backend health", healthy, 120)
        admin = smoke_api.Client()
        admin.login("admin", admin_password)
        admin.call("PUT", "/api/admin/settings/consultations", {"model": smoke_api.MODEL})
        admin.call("POST", "/api/admin/users", {
            "login": "smoke-user", "password": user_password, "role": "user",
            "workspace_path": str(ws_root / "smoke-user"),
        })
        print(f"VALERIA READY on {BASE}", flush=True)
        while not stop.wait(0.5):
            if backend.poll() is not None:
                print(f"backend exited with {backend.returncode}", flush=True)
                return 1
        code = 0
    except Exception as exc:  # noqa: BLE001 - print the reason and the log tail, then clean up
        print(f"setup failed: {exc!r}", flush=True)
        log.flush()
        print((tmp / "backend.log").read_text(errors="replace")[-4000:], flush=True)
    finally:
        if backend.poll() is None:
            os.killpg(backend.pid, signal.SIGTERM)
            try:
                backend.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(backend.pid, signal.SIGKILL)
                backend.wait(10)
        for pid in set(smoke_api.listening_pids(ports).values()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
