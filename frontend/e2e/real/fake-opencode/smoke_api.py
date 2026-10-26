#!/usr/bin/env python3
"""API smoke test: real VALERIA backend + fake OpenCode, consultation round trip.

Run from anywhere:  python3 frontend/e2e/real/fake-opencode/smoke_api.py
Exit code 0 when every step passes. Everything runs on 127.0.0.1 and is removed
at the end (backend, fake OpenCode processes, temp dir with DB and workspaces).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
FAKE = HERE / "opencode"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SMOKE_APP_PORT", "8085"))
OC_START, OC_END = 46100, 46110
BASE = f"http://{HOST}:{PORT}"
MODEL = "fake/fake-model"

results: list[tuple[str, bool, str]] = []


class StepFailed(Exception):
    pass


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}", flush=True)
    if not ok:
        raise StepFailed(name)


class Client:
    def __init__(self) -> None:
        self.cookie: str | None = None
        self.csrf: str | None = None

    def call(self, method: str, path: str, body=None, expect=(200, 201, 202, 204)):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(BASE + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        if self.csrf and method not in {"GET", "HEAD"}:
            request.add_header("X-CSRF-Token", self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw, headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status, raw, headers = exc.code, exc.read(), exc.headers
        for value in headers.get_all("Set-Cookie") or []:
            self.cookie = value.split(";", 1)[0]
        payload = json.loads(raw) if raw else None
        if status not in expect:
            raise StepFailed(f"{method} {path} -> HTTP {status}: {raw[:300]!r}")
        return payload

    def login(self, login: str, password: str) -> dict:
        result = self.call("POST", "/api/auth/login", {"login": login, "password": password})
        self.csrf = result["csrf_token"]
        return result


def listening_pids(ports: list[int]) -> dict[int, int]:
    found: dict[int, int] = {}
    for port in ports:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True,
        ).stdout.split()
        if out:
            found[port] = int(out[0])
    return found


def wait_for(description: str, predicate, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise StepFailed(f"timeout waiting for {description}; last={last!r}")


def wait_status(client: Client, chat_id: str, wanted: str, timeout: float = 90) -> dict:
    seen: list[str] = []

    def probe():
        detail = client.call("GET", f"/api/consultations/{chat_id}")
        if not seen or seen[-1] != detail["status"]:
            seen.append(detail["status"])
        if detail["status"] in {"failed", "cancelled"} and wanted not in {"failed", "cancelled"}:
            raise StepFailed(f"consultation became {detail['status']} (seen {seen})")
        return detail if detail["status"] == wanted else None

    detail = wait_for(f"status {wanted}", probe, timeout)
    detail["_seen"] = seen
    return detail


def main() -> int:
    ports = [PORT, *range(OC_START, OC_END + 1)]
    busy = listening_pids(ports)
    if busy:
        print(f"FAIL  ports already in use: {busy}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="valeria-smoke-")).resolve()
    ws_root = tmp / "ws"
    ws_root.mkdir()
    admin_password = secrets.token_urlsafe(18)
    user_password = secrets.token_urlsafe(18)
    password_file = tmp / "admin-password"
    password_file.write_text(admin_password, encoding="utf-8")
    password_file.chmod(0o600)
    backend_log = tmp / "backend.log"
    fake_log = tmp / "fake-opencode.log"

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
        "OPENCODE_BIN": str(FAKE),
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
        "OPENCODE_IDLE_TIMEOUT_SECONDS": "900",
        "MAX_ACTIVE_SESSIONS": "1",
        "MAX_ACTIVE_SESSIONS_PER_USER": "0",
        # cli.py calls load_dotenv(); empty values keep the repo .env from leaking in.
        "MATTERMOST_TOKEN_FILE": "",
        "MATTERMOST_CA_BUNDLE": "",
        "LOG_LEVEL": "INFO",
        # Explicit, so a developer .env (cli.py calls load_dotenv) cannot turn the API docs on.
        "APP_ENABLE_DOCS": "false",
        "FAKE_OPENCODE_LOG": str(fake_log),
    }

    backend: subprocess.Popen | None = None
    exit_code = 1
    try:
        with backend_log.open("wb") as log_handle:
            backend = subprocess.Popen(
                ["uv", "run", "opencode-web"], cwd=REPO, env=env,
                stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
            )

        def healthy():
            if backend.poll() is not None:
                raise StepFailed(f"backend exited with {backend.returncode}")
            try:
                with urllib.request.urlopen(BASE + "/api/health", timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False

        wait_for("backend health", healthy, 120)
        step(f"backend started on 127.0.0.1:{PORT}", True)
        try:
            docs = urllib.request.urlopen(BASE + "/openapi.json", timeout=5).status
        except urllib.error.HTTPError as exc:
            docs = exc.code
        step("API docs disabled (/openapi.json not served)", docs == 404, f"HTTP {docs}")

        admin = Client()
        admin.login("admin", admin_password)
        step("admin login (cookie + csrf)", bool(admin.cookie and admin.csrf))

        options = admin.call("GET", "/api/options")
        models = [item["id"] for item in options.get("models", [])]
        step("model catalog from fake /provider", MODEL in models and options["agents"] ==
             ["build", "plan"], f"models={models} agents={options['agents']}")

        admin.call("PUT", "/api/admin/settings/consultations", {"model": MODEL})
        settings = admin.call("GET", "/api/admin/settings/consultations")
        step("consultation model configured", settings["model"] == MODEL, json.dumps(settings))

        workspace = ws_root / "smoke-user"
        user_record = admin.call("POST", "/api/admin/users", {
            "login": "smoke-user", "password": user_password, "role": "user",
            "workspace_path": str(workspace),
        })
        step("create user with workspace", user_record.get("workspace_path") == str(workspace),
             str(user_record.get("workspace_path")))

        user = Client()
        user.login("smoke-user", user_password)
        step("user login", bool(user.cookie and user.csrf))

        created = user.call("POST", "/api/consultations", {"message": "Как прошить плату?"})
        chat_id = created["id"]
        step("create consultation", created["status"] in {"queued", "dispatching", "running"},
             f"id={chat_id} status={created['status']}")

        detail = wait_status(user, chat_id, "waiting_question")
        step("consultation waits for question", True, " -> ".join(detail["_seen"]))

        pending = user.call("GET", f"/api/consultations/{chat_id}/questions")
        question = pending[0]["questions"][0] if pending else {}
        labels = [option["label"] for option in question.get("options", [])]
        step("question visible via API",
             len(pending) == 1 and question.get("header") == "Уточнение"
             and question.get("question") == "Для какой платы?"
             and labels == ["Плата A", "Плата B"],
             f"{question.get('header')}: {question.get('question')} {labels}")

        reply = user.call(
            "POST", f"/api/consultations/{chat_id}/questions/{pending[0]['id']}/reply",
            {"answers": [["Плата A"]]},
        )
        step("reply to question", reply == {"accepted": True}, json.dumps(reply))

        detail = wait_status(user, chat_id, "completed")
        answers = [m["text"] for m in detail["messages"] if m["role"] == "assistant"]
        step("consultation completed with answer",
             answers == ["Ответ для: Плата A"],
             f"{' -> '.join(detail['_seen'])}; answer={answers}")

        direct = user.call("POST", "/api/consultations", {"message": "#direct Сколько плат?"})
        detail = wait_status(user, direct["id"], "completed")
        answers = [m["text"] for m in detail["messages"] if m["role"] == "assistant"]
        step("#direct consultation answered without question",
             "waiting_question" not in detail["_seen"]
             and answers == ["Прямой ответ на: #direct Сколько плат?"],
             f"{' -> '.join(detail['_seen'])}; answer={answers}")
        exit_code = 0
    except StepFailed as exc:
        if not results or results[-1][1]:
            print(f"FAIL  {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 - report any unexpected error as a failed step
        print(f"FAIL  unexpected error: {exc!r}", flush=True)
    finally:
        if exit_code:
            for name, path in (("backend log", backend_log), ("fake log", fake_log)):
                if path.exists():
                    print(f"----- {name} (tail) -----")
                    print("\n".join(path.read_text(errors="replace").splitlines()[-40:]))
        stopped = True
        if backend and backend.poll() is None:
            os.killpg(backend.pid, signal.SIGTERM)
            try:
                backend.wait(30)
            except subprocess.TimeoutExpired:
                stopped = False
                os.killpg(backend.pid, signal.SIGKILL)
                backend.wait(10)
        try:
            wait_for("ports to close", lambda: not listening_pids(ports), 15)
            leftovers: dict[int, int] = {}
        except StepFailed:
            leftovers = listening_pids(ports)
            for pid in set(leftovers.values()):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        shutil.rmtree(tmp, ignore_errors=True)
        clean = stopped and not leftovers and not tmp.exists()
        print(f"{'PASS' if clean else 'FAIL'}  cleanup (backend stopped gracefully={stopped}, "
              f"leftover listeners={leftovers or 'none'}, temp removed={not tmp.exists()})")
        if not clean:
            exit_code = 1
    passed = sum(ok for _, ok, _ in results)
    print(f"{'PASS' if exit_code == 0 else 'FAIL'}  overall: {passed}/{len(results)} steps passed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
