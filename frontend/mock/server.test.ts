import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import * as D from "./data";
import { createMockServer } from "./server";
import { parseIso, Store } from "./store";

const mock = createMockServer({ port: 0, speed: 40, quiet: true });
afterAll(() => mock.stop());

class Client {
  cookies = new Map<string, string>();
  csrf = "";
  async req(method: string, path: string, body?: unknown, headers: Record<string, string> = {}) {
    const h: Record<string, string> = { ...headers };
    if (this.cookies.size) h.cookie = [...this.cookies].map(([k, v]) => `${k}=${v}`).join("; ");
    if (body !== undefined) h["content-type"] = "application/json";
    if (this.csrf && method !== "GET" && !("x-csrf-token" in headers)) h["x-csrf-token"] = this.csrf;
    const res = await fetch(mock.url + path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), redirect: "manual" });
    for (const c of res.headers.getSetCookie()) {
      const [pair] = c.split(";");
      const i = pair.indexOf("=");
      const name = pair.slice(0, i), value = pair.slice(i + 1);
      if (/Max-Age=0/.test(c)) this.cookies.delete(name); else this.cookies.set(name, value);
    }
    const text = await res.text();
    return { status: res.status, body: text ? JSON.parse(text) : null };
  }
  async as(persona: "user" | "tasks" | "admin", extra: Record<string, unknown> = {}) {
    const r = await this.req("POST", "/__mock/session", { as: persona, ...extra });
    this.csrf = r.body.csrf_token;
    return this;
  }
}

async function collectEvents(client: Client, until: (events: any[]) => boolean, timeoutMs = 4000) {
  const controller = new AbortController();
  const res = await fetch(`${mock.url}/api/events`, {
    headers: { cookie: [...client.cookies].map(([k, v]) => `${k}=${v}`).join("; ") }, signal: controller.signal,
  });
  expect(res.headers.get("content-type")).toBe("text/event-stream");
  const reader = res.body!.getReader();
  const events: any[] = [];
  let buffer = "";
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const done = (async () => {
    try {
      while (!until(events)) {
        const { value, done: end } = await reader.read();
        if (end) break;
        buffer += new TextDecoder().decode(value);
        let i: number;
        while ((i = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, i);
          buffer = buffer.slice(i + 2);
          if (frame.startsWith("data: ")) events.push(JSON.parse(frame.slice(6)));
        }
      }
    } catch { /* aborted */ }
  })();
  return {
    events,
    finished: done.then(() => { clearTimeout(timer); controller.abort(); return events; }),
  };
}

/** Seeded jobs may finish early to free a slot for a live one; lifecycle assertions wait for the live job. */
const SEEDED = new Set(new Store({ quiet: true }).jobs.keys());
const isLive = (id: string) => !SEEDED.has(id);

beforeEach(async () => {
  await fetch(`${mock.url}/__mock/reset`, { method: "POST", body: JSON.stringify({ as: "none", folder: 1 }) });
});

describe("auth + CSRF", () => {
  test("logged out → 401, login, me, CSRF on non-GET, logout", async () => {
    const c = new Client();
    expect(await c.req("GET", "/api/auth/me")).toEqual({ status: 401, body: { detail: "Authentication required" } });
    const bad = await c.req("POST", "/api/auth/login", { login: "i.sokolova", password: "nope" });
    expect(bad).toEqual({ status: 401, body: { detail: "Неверный логин или пароль" } });
    const blocked = await c.req("POST", "/api/auth/login", { login: "s.egorova", password: D.DEMO_PASSWORD });
    expect(blocked.status).toBe(401);
    const ok = await c.req("POST", "/api/auth/login", { login: "i.sokolova", password: D.DEMO_PASSWORD });
    expect(ok.status).toBe(200);
    expect(Object.keys(ok.body).sort()).toEqual(["csrf_token", "user"]);
    expect(ok.body.user).toMatchObject({ id: "u-isokolova", login: "i.sokolova", role: "user", tasks_only: false });
    expect(c.cookies.has("ocw_session")).toBe(true);

    const me = await c.req("GET", "/api/auth/me");
    expect(me.body.csrf_token).toBe(ok.body.csrf_token);

    // no / wrong CSRF header → 403
    const noCsrf = await c.req("POST", "/api/chats/q-gcc13/archive", {}, {});
    expect(noCsrf).toEqual({ status: 403, body: { detail: "CSRF check failed" } });
    const wrong = await c.req("POST", "/api/auth/logout", undefined, { "x-csrf-token": "bad" });
    expect(wrong.status).toBe(403);

    c.csrf = ok.body.csrf_token;
    expect((await c.req("POST", "/api/auth/logout")).status).toBe(204);
    expect((await c.req("GET", "/api/auth/me")).status).toBe(401);
  });

  test("admin-only routes → 403 for user", async () => {
    const c = await new Client().as("user");
    expect(await c.req("GET", "/api/admin/users")).toEqual({ status: 403, body: { detail: "Administrator role required" } });
    const admin = await new Client().as("admin");
    expect((await admin.req("GET", "/api/admin/users")).status).toBe(200);
  });
});

describe("tasks_only", () => {
  test("allowlist: tasks, consultations, items, chat actions; everything else 403", async () => {
    const c = await new Client().as("tasks");
    for (const path of ["/api/options", "/api/chats", "/api/templates"]) {
      expect(await c.req("GET", path)).toEqual({ status: 403, body: { detail: "This account can only use tasks and consultations" } });
    }
    for (const path of ["/api/tasks", "/api/consultations", "/api/task-runs", "/api/config/public", "/api/me/items", "/api/queue/me"]) {
      expect((await c.req("GET", path)).status).toBe(200);
    }
    const items = (await c.req("GET", "/api/me/items")).body;
    expect(items.length).toBeGreaterThan(0);
    expect(items.every((i: any) => i.kind !== "folder")).toBe(true);
    expect((await c.req("POST", "/api/chats/q-gcc13/unsnooze", {})).status).toBe(200);
    // folder chats are forbidden even for the owner; chat actions do not see them (app.py visible_item)
    expect((await c.req("POST", "/api/chats/f-planning/messages", { message: "x" })).status).toBe(403);
    expect(mock.store.chats.get("f-planning").owner_user_id).toBe("u-isokolova");
    for (const action of ["archive", "unarchive", "snooze", "unsnooze"]) {
      expect((await c.req("POST", `/api/chats/f-planning/${action}`, { until: new Date(Date.now() + 3600000).toISOString() })).status).toBe(404);
    }
    expect((await c.req("DELETE", "/api/chats/f-planning")).status).toBe(404);
  });

  test("/api/queue/me lists only the kinds the user may use (app.py my_queue: allowed_item_kinds)", async () => {
    const kind = (j: any) => (j.task_id ? "task" : mock.store.chats.get(j.chat_id)?.consultation ? "question" : "folder");
    const user = await new Client().as("user");
    const full = (await user.req("GET", "/api/queue/me")).body;
    expect(full.some((j: any) => kind(j) === "folder")).toBe(true);
    const tasks = await new Client().as("tasks");
    const limited = (await tasks.req("GET", "/api/queue/me")).body;
    expect(limited.length).toBeGreaterThan(0);
    expect(limited.every((j: any) => kind(j) !== "folder")).toBe(true);
    expect(limited.map((j: any) => j.id).sort()).toEqual(full.filter((j: any) => kind(j) !== "folder").map((j: any) => j.id).sort());
  });
});

describe("idempotency", () => {
  test("same key replays, other body → 409, blank key → 422", async () => {
    const c = await new Client().as("user");
    const key = { "Idempotency-Key": "run-1" };
    const first = await c.req("POST", "/api/tasks/t-ci/runs", { user_input: "Pipeline #48211" }, key);
    expect(first.status).toBe(202);
    expect(Object.keys(first.body).sort()).toEqual(["created_at", "error_code", "finished_at", "id", "phase", "plan_first", "started_at", "status", "task_id", "title", "user_input"]);
    expect(first.body.user_input).toBe("Pipeline #48211");
    const again = await c.req("POST", "/api/tasks/t-ci/runs", { user_input: "Pipeline #48211" }, key);
    expect(again.status).toBe(202);
    expect(again.body.id).toBe(first.body.id);
    const other = await c.req("POST", "/api/tasks/t-ci/runs", { user_input: "Pipeline #1" }, key);
    expect(other).toEqual({ status: 409, body: { detail: "Idempotency-Key was used for another request" } });
    const blank = await c.req("POST", "/api/tasks/t-ci/runs", {}, { "Idempotency-Key": "  " });
    expect(blank).toEqual({ status: 422, body: { detail: "Invalid Idempotency-Key" } });
    const extra = await c.req("POST", "/api/tasks/t-ci/runs", { user_input: "", foo: 1 });
    expect(extra.status).toBe(422);
    expect(extra.body.detail[0]).toMatchObject({ type: "extra_forbidden", loc: ["body", "foo"] });
    const runs = (await c.req("GET", "/api/task-runs")).body.filter((r: any) => r.id === first.body.id);
    expect(runs).toHaveLength(1);
  });
});

describe("new contract: tasks", () => {
  test("public_task has input_hint; duplicate data = task_id + user_input; real waiting status", async () => {
    const c = await new Client().as("user");
    const tasks = (await c.req("GET", "/api/tasks")).body;
    expect(Object.keys(tasks[0]).sort()).toEqual(["description", "id", "input_hint", "plan_first", "title"]);
    expect(tasks.find((t: any) => t.id === "t-mr").input_hint).toBe("Ссылка на merge request");
    const runs = (await c.req("GET", "/api/task-runs")).body;
    const dup = runs.filter((r: any) => r.task_id === "t-mr" && r.user_input === D.MR_URL && !["completed", "failed", "cancelled"].includes(r.status));
    expect(dup.map((r: any) => r.id)).toEqual(["r-running"]);
    expect(runs.find((r: any) => r.id === "r-failed")).toMatchObject({ error_code: "assistant_error", status: "failed" });

    const stream = await collectEvents(c, (evs) => evs.some((e) => e.type === "task.updated" && e.task.status === "completed" && isLive(e.task.id)));
    const run = (await c.req("POST", "/api/tasks/t-dup/runs", { user_input: "FW-2401 #permission" })).body;
    const statuses = (await stream.finished).filter((e) => e.type === "task.updated" && e.task.id === run.id).map((e) => e.task.status);
    expect(statuses).toContain("waiting_permission");
  });

  test("admin create/PATCH accept and return input_hint (≤ 500)", async () => {
    const c = await new Client().as("admin");
    const created = await c.req("POST", "/api/admin/tasks", { title: "T", content: "C", agent: "build", model: D.DEFAULT_MODEL, input_hint: "Номер задачи" });
    expect(created.status).toBe(201);
    expect(created.body.input_hint).toBe("Номер задачи");
    const patched = await c.req("PATCH", `/api/admin/tasks/${created.body.id}`, { input_hint: "Ссылка" });
    expect(patched.body.input_hint).toBe("Ссылка");
    expect((await c.req("PATCH", `/api/admin/tasks/${created.body.id}`, { input_hint: "x".repeat(501) })).status).toBe(422);
    expect((await c.req("GET", "/api/admin/tasks")).body.find((t: any) => t.id === "t-ci").input_hint).toBe("Ссылка на пайплайн или job GitLab");
  });
});

describe("SSE lifecycle", () => {
  test("task run: queued → dispatching → running → completed with title update", async () => {
    const c = await new Client().as("user");
    const stream = await collectEvents(c, (evs) => evs.some((e) => e.type === "task.updated" && e.task.status === "completed" && isLive(e.task.id)));
    await Bun.sleep(50);
    const run = await c.req("POST", "/api/tasks/t-mr/runs", { user_input: "!1932 — Поддержка TLS 1.3 в HTTP-клиенте" });
    expect(run.status).toBe(202);
    const events = await stream.finished;
    const statuses = events.filter((e) => e.type === "task.updated" && e.task.id === run.body.id).map((e) => e.task.status);
    expect(statuses).toEqual(["queued", "dispatching", "running", "completed"]);
    const title = events.find((e) => e.type === "task.title.updated");
    expect(title).toMatchObject({ run_id: run.body.id, title: `${"Ревью merge request"}: !1932 — Поддержка TLS 1.3 в HTTP-клиенте` });
    const result = await c.req("GET", `/api/task-runs/${run.body.id}/result`);
    expect(result.body).toMatchObject({ run_id: run.body.id, status: "available" });
  });

  test("folder chat: permission → once → completed; stop → cancelled", async () => {
    const c = await new Client().as("user");
    const stream = await collectEvents(c, (evs) => evs.some((e) => e.type === "opencode.event" && e.event.payload.type === "permission.asked"));
    const created = await c.req("POST", "/api/chats", { message: "Обнови conanfile до Conan 2", agent: "build" });
    expect(created.status).toBe(201);
    expect(Object.keys(created.body).sort()).toEqual(["chat", "job"]);
    await stream.finished;
    const chatId = created.body.chat.id;
    const perms = (await c.req("GET", `/api/chats/${chatId}/permissions`)).body;
    expect(perms).toHaveLength(1);
    expect((await c.req("POST", `/api/chats/${chatId}/permissions/${perms[0].id}`, { response: "maybe" })).body).toEqual({ detail: "Invalid permission response" });
    const done = await collectEvents(c, (evs) => evs.some((e) => e.type === "job.updated" && e.job.status === "completed"));
    expect((await c.req("POST", `/api/chats/${chatId}/permissions/${perms[0].id}`, { response: "once" })).body).toEqual({ accepted: true });
    await done.finished;
    expect((await c.req("POST", `/api/chats/${chatId}/abort`)).body).toEqual({ detail: "Chat is not running" });

    const run = (await c.req("POST", "/api/tasks/t-ci/runs", {})).body;
    const stopped = await collectEvents(c, (evs) => evs.some((e) => e.type === "task.updated" && e.task.status === "cancelled"));
    expect((await c.req("POST", `/api/task-runs/${run.id}/stop`)).status).toBe(202);
    await stopped.finished;
  });
});

describe("chat actions", () => {
  test("snooze validation, archive, unarchive, delete + chat.deleted", async () => {
    const c = await new Client().as("user");
    const past = new Date(Date.now() - 60000).toISOString();
    expect(await c.req("POST", "/api/chats/q-gcc13/snooze", { until: past })).toEqual({ status: 422, body: { detail: "Snooze time must be in the future" } });
    const archived = await c.req("POST", "/api/chats/q-gcc13/archive", {});
    expect(archived.status).toBe(200);
    expect(archived.body).toMatchObject({ kind: "question", id: "q-gcc13", chat_id: "q-gcc13", snoozed_until: null });
    expect(archived.body.archived_at).toBeTruthy();
    const future = new Date(Date.now() + 3600000).toISOString();
    expect((await c.req("POST", "/api/chats/q-gcc13/snooze", { until: future })).status).toBe(409);
    expect((await c.req("POST", "/api/chats/q-gcc13/unarchive", {})).body.archived_at).toBeNull();
    const snoozed = await c.req("POST", "/api/chats/q-gcc13/snooze", { until: future });
    expect(Date.parse(snoozed.body.snoozed_until)).toBe(Date.parse(future));

    // snooze expiry clears snoozed_until and emits chat.updated
    const soon = new Date(Date.now() + 300).toISOString();
    const expiry = await collectEvents(c, (evs) => evs.some((e) => e.type === "chat.updated" && e.chat_id === "q-gcc13" && e.snoozed_until === null));
    await c.req("POST", "/api/chats/q-gcc13/snooze", { until: soon });
    await expiry.finished;
    expect(expiry.events.some((e) => e.type === "chat.updated" && e.snoozed_until === null)).toBe(true);

    const deleted = await collectEvents(c, (evs) => evs.some((e) => e.type === "chat.deleted"));
    expect((await c.req("DELETE", "/api/chats/q-gcc13")).status).toBe(204);
    expect(await deleted.finished).toContainEqual({ type: "chat.deleted", chat_id: "q-gcc13" });
    expect((await c.req("POST", "/api/chats/q-gcc13/archive", {})).status).toBe(404);
    // someone else's chat is 404 for the action
    expect((await c.req("POST", "/api/chats/adm-q/archive", {})).status).toBe(404);
  });
});

describe("admin", () => {
  test("batch block: self / already / not found skipped, others blocked", async () => {
    const c = await new Client().as("admin");
    const r = await c.req("POST", "/api/admin/users/batch", { ids: ["u-dorlov", "u-segorova", "u-pvolkov", "u-missing"], status: "blocked" });
    expect(r.status).toBe(200);
    expect(r.body.results).toEqual([
      { id: "u-dorlov", login: "d.orlov", ok: false, skipped: true, reason: "self" },
      { id: "u-segorova", login: "s.egorova", ok: true, skipped: true, reason: "already" },
      { id: "u-pvolkov", login: "p.volkov", ok: true, skipped: false, reason: null },
      { id: "u-missing", login: null, ok: false, skipped: false, reason: "not_found" },
    ]);
    const users = (await c.req("GET", "/api/admin/users")).body;
    expect(users.find((u: any) => u.id === "u-pvolkov").status).toBe("disabled");
    expect((await c.req("POST", "/api/admin/users/batch", { ids: [], status: "gone" })).status).toBe(422);
  });

  test("create and PATCH: a workspace of another active user is 409 «Workspace is already assigned» (app.py checked_workspace)", async () => {
    const c = await new Client().as("admin");
    const taken = mock.store.users.get("u-pvolkov").workspace_path;
    const conflict = { status: 409, body: { detail: "Workspace is already assigned" } };
    const create = (workspace_path: string) => c.req("POST", "/api/admin/users", { login: "n.new", password: "long-enough-password", workspace_path });
    expect(await create("fw/hal-stm32")).toEqual(conflict);
    expect(await create(taken)).toEqual(conflict);
    const created = await create("new/place");
    expect(created.status).toBe(201);
    const id = created.body.id;
    expect(await c.req("PATCH", `/api/admin/users/${id}`, { workspace_path: taken })).toEqual(conflict);
    expect(mock.store.users.get(id).workspace_path).toBe(created.body.workspace_path);
    // The user's own workspace is not a conflict; a disabled user's workspace is free.
    expect((await c.req("PATCH", "/api/admin/users/u-pvolkov", { tasks_only: true })).status).toBe(200);
    const free = mock.store.users.get("u-segorova").workspace_path;
    expect((await c.req("PATCH", `/api/admin/users/${id}`, { workspace_path: free })).status).toBe(200);
  });

  test("batch unblock: a workspace taken by another active user fails; a rejected update is 'invalid'", async () => {
    const c = await new Client().as("admin");
    mock.store.users.get("u-segorova").workspace_path = mock.store.users.get("u-pvolkov").workspace_path;
    const r = await c.req("POST", "/api/admin/users/batch", { ids: ["u-segorova"], status: "active" });
    expect(r.body.results).toEqual([{ id: "u-segorova", login: "s.egorova", ok: false, skipped: false, reason: "workspace_in_use" }]);
    expect(mock.store.users.get("u-segorova").status).toBe("disabled");

    mock.store.scenario.fail = "batch:u-pvolkov";
    const failed = await c.req("POST", "/api/admin/users/batch", { ids: ["u-pvolkov"], status: "blocked" });
    expect(failed.body.results).toEqual([{ id: "u-pvolkov", login: "p.volkov", ok: false, skipped: false, reason: "invalid" }]);
  });

  test("workspace change archives other workspaces' folder chats and questions, the owner gets chat.deleted", async () => {
    const user = await new Client().as("user");
    const admin = await new Client().as("admin");
    // Without its profile cookie the user tab does not re-apply the seed workspaces on every request.
    user.cookies.delete("mock_scenario");
    const iso = mock.store.users.get("u-isokolova");
    const home = iso.workspace_path;
    // The backend refuses a workspace change while jobs are queued or active.
    const unfinished = ["queued", "dispatching", "running", "waiting_permission", "waiting_question", "cancelling"];
    for (const job of mock.store.jobs.values()) {
      if (job.effective_user_id === "u-isokolova" && unfinished.includes(job.status)) mock.store.setStatus(job, "cancelled");
    }
    const before = (await user.req("GET", "/api/me/items")).body;
    const local = before.filter((i: any) => i.kind !== "task" && !i.archived_at);
    const tasks = before.filter((i: any) => i.kind === "task");
    expect(local.length).toBeGreaterThan(0);
    expect(tasks.length).toBeGreaterThan(0);
    const queueBefore = (await user.req("GET", "/api/queue/me")).body;
    expect(queueBefore.some((j: any) => !j.task_id)).toBe(true);

    const events = await collectEvents(user, (evs) => local.every((i: any) => evs.some((e) => e.type === "chat.deleted" && e.chat_id === i.chat_id)));
    await Bun.sleep(30);
    const moved = await admin.req("PATCH", "/api/admin/users/u-isokolova", { workspace_path: "/srv/elsewhere" });
    expect(moved.status).toBe(200);
    const got = await events.finished;
    for (const i of local) expect(got).toContainEqual({ type: "chat.deleted", chat_id: i.chat_id });
    for (const i of local) {
      const chat = mock.store.chats.get(i.chat_id);
      expect(chat.archived_reason).toBe("workspace_changed");
      expect(chat.archived_at).toBeTruthy();
      expect(chat.snoozed_until).toBeNull();
    }
    for (const t of tasks) expect(mock.store.chats.get(t.chat_id).archived_reason ?? null).toBeNull();
    // Out of reach: not listed, actions 404, the queue keeps only task runs.
    expect((await user.req("GET", "/api/me/items")).body.map((i: any) => i.chat_id).sort()).toEqual(tasks.map((i: any) => i.chat_id).sort());
    expect((await user.req("POST", `/api/chats/${local[0].chat_id}/unarchive`, {})).status).toBe(404);
    expect((await user.req("GET", "/api/queue/me")).body.every((j: any) => j.task_id)).toBe(true);

    // Moving back restores them.
    expect((await admin.req("PATCH", "/api/admin/users/u-isokolova", { workspace_path: home })).status).toBe(200);
    for (const i of local) {
      const chat = mock.store.chats.get(i.chat_id);
      expect(chat.archived_reason).toBeNull();
      expect(chat.archived_at).toBeNull();
    }
    expect((await user.req("GET", "/api/me/items")).body.length).toBe(before.length);
  });

  test("unarchive of a chat archived by a workspace change is 409", async () => {
    const user = await new Client().as("user");
    const chat = mock.store.chats.get("q-gcc13");
    Object.assign(chat, { archived_reason: "workspace_changed", archived_at: new Date().toISOString() });
    expect(await user.req("POST", "/api/chats/q-gcc13/unarchive", {})).toEqual({ status: 409, body: { detail: "Chat was archived after a workspace change" } });
    expect(chat.archived_at).toBeTruthy();
  });

  test("PATCH optimistic concurrency: 409 conflict, 404 deleted", async () => {
    const c = await new Client().as("admin");
    const task = (await c.req("GET", "/api/admin/tasks")).body.find((t: any) => t.id === "t-ci");
    await c.req("POST", "/__mock/patch", { collection: "tasks", id: "t-ci", set: { title: "Разбор CI" }, by: "p.volkov" });
    const conflict = await c.req("PATCH", "/api/admin/tasks/t-ci", { title: "Мой заголовок", updated_at: task.updated_at });
    expect(conflict.status).toBe(409);
    expect(conflict.body.detail).toMatchObject({ code: "conflict", updated_by: "p.volkov" });
    const gone = await c.req("PATCH", "/api/admin/tasks/t-old-notes", { title: "x" });
    expect(gone).toEqual({ status: 404, body: { detail: { code: "deleted", deleted_by: "d.orlov" } } });
  });

  test("audit filters + cursor", async () => {
    const c = await new Client().as("admin");
    const page1 = (await c.req("GET", "/api/admin/audit?limit=5")).body;
    expect(page1.items).toHaveLength(5);
    expect(page1.next_cursor).toBeTruthy();
    const page2 = (await c.req("GET", `/api/admin/audit?limit=5&cursor=${page1.next_cursor}`)).body;
    expect(page2.items[0].id).toBeLessThan(page1.items[4].id);
    const stops = (await c.req("GET", "/api/admin/audit?action=job.stop.")).body.items;
    expect(stops.length).toBeGreaterThan(0);
    expect(stops.every((r: any) => r.action.startsWith("job.stop."))).toBe(true);
    const byUser = (await c.req("GET", "/api/admin/audit?user=s.egorova")).body.items;
    expect(byUser.every((r: any) => r.actor_login === "s.egorova" || r.effective_login === "s.egorova")).toBe(true);
    // Several logins are OR, like the backend's comma list.
    const two = ["s.egorova", "d.orlov"];
    const byUsers = (await c.req("GET", `/api/admin/audit?limit=500&user=${two.join(",")}`)).body.items;
    expect(byUsers.length).toBeGreaterThan(byUser.length);
    expect(byUsers.every((r: any) => two.includes(r.actor_login) || two.includes(r.effective_login))).toBe(true);
    const involves = (r: any, login: string) => r.actor_login === login || r.effective_login === login;
    expect(byUsers.some((r: any) => involves(r, "s.egorova"))).toBe(true);
    expect(byUsers.some((r: any) => involves(r, "d.orlov") && !involves(r, "s.egorova"))).toBe(true);
  });

  test("statistics runs + requests filter", async () => {
    const c = await new Client().as("admin");
    const runs = (await c.req("GET", "/api/admin/statistics/runs?type=task")).body;
    expect(runs.items.length).toBeGreaterThan(100);
    expect(runs.items.every((r: any) => r.kind === "task")).toBe(true);
    const reqs = (await c.req("GET", "/api/admin/requests?type=question&user=i.sokolova")).body;
    expect(reqs.length).toBeGreaterThan(0);
    expect(reqs.every((r: any) => r.kind === "question" && r.user.login === "i.sokolova")).toBe(true);
  });
});

describe("scenario controls", () => {
  test("no folder, delay header, forced error, 404 ids", async () => {
    const c = await new Client().as("user", { folder: 0 });
    expect((await c.req("GET", "/api/auth/me")).body.user.workspace_path).toBeNull();
    expect(await c.req("POST", "/api/chats", { message: "x" })).toEqual({ status: 422, body: { detail: "No workspace assigned" } });
    const t0 = performance.now();
    await c.req("GET", "/api/tasks", undefined, { "x-mock-delay": "300" });
    expect(performance.now() - t0).toBeGreaterThanOrEqual(290);
    expect(await c.req("GET", "/api/tasks", undefined, { "x-mock-fail": "500" })).toEqual({ status: 500, body: { detail: "Mock forced error" } });
    expect((await c.req("GET", "/api/task-runs/r-nope")).body).toEqual({ detail: "Task run not found" });
    // A reset with a persona starts from the default profile: the folder is assigned again.
    await c.req("POST", "/__mock/reset", { as: "user" });
    expect((await c.req("GET", "/api/auth/me")).body.user.workspace_path).not.toBeNull();
  });
});

describe("backend alignment", () => {
  const MIN = 60000;
  const ageMin = (ts: string, now = Date.now()) => (now - parseIso(ts)) / MIN;
  const checkSlots = (q: any) => {
    expect(q.active).toBeLessThanOrEqual(q.limit);
    for (const row of q.queues) expect(row.active).toBeLessThanOrEqual(row.max_workers);
  };

  test("seeded unfinished jobs and stats runs keep their age against the mock clock", async () => {
    const c = await new Client().as("admin");
    const queue = (await c.req("GET", "/api/admin/queue")).body;
    const running = queue.jobs.find((j: any) => j.id === "r-running");
    expect(ageMin(running.started_at)).toBeGreaterThan(30);
    expect(ageMin(running.started_at)).toBeLessThan(32);
    // a server that has been up for 33 h still reports minutes, not hours
    mock.store.now = () => Date.now() + 33 * 3600 * 1000;
    try {
      const later = (await c.req("GET", "/api/admin/queue")).body.jobs.find((j: any) => j.id === "r-running");
      expect(ageMin(later.started_at, mock.store.now())).toBeLessThan(32);
      const stats = (await c.req("GET", "/api/admin/statistics/runs")).body;
      const unfinished = stats.items.filter((r: any) => r.outcome === null);
      expect(unfinished.length).toBeGreaterThan(0);
      for (const r of unfinished) expect(ageMin(r.created_at, parseIso(stats.generated_at))).toBeLessThan(15);
    } finally {
      delete (mock.store as any).now;
    }
    // finished seed jobs keep their timestamps; a job that ends stops sliding
    const done = queue.jobs.find((j: any) => j.id === "r-done");
    expect(done.finished_at).toBeTruthy();
  });

  test("--fixed-now: clock continues from the prototype now; runtime rows follow it", async () => {
    const fixed = createMockServer({ port: 0, speed: 40, quiet: true, fixedNow: true });
    try {
      expect(Math.abs(fixed.store.now() - D.PROTO_NOW)).toBeLessThan(2000);
      await Bun.sleep(120);
      expect(fixed.store.now() - D.PROTO_NOW).toBeGreaterThanOrEqual(100);
      const res = await fetch(`${fixed.url}/__mock/reset`, { method: "POST", body: JSON.stringify({ as: "user" }) });
      const csrf = (await res.json()).csrf_token;
      const cookie = res.headers.getSetCookie().map((x) => x.split(";")[0]).join("; ");
      const run = await (await fetch(`${fixed.url}/api/tasks/t-ci/runs`, {
        method: "POST", headers: { cookie, "x-csrf-token": csrf, "content-type": "application/json" }, body: JSON.stringify({ user_input: "x" }),
      })).json();
      expect(Math.abs(parseIso(run.created_at) - D.PROTO_NOW)).toBeLessThan(2000);
      const runs = await (await fetch(`${fixed.url}/api/task-runs`, { headers: { cookie } })).json();
      const seeded = runs.find((r: any) => r.id === "r-running");
      expect(ageMin(seeded.started_at, fixed.store.now())).toBeCloseTo(31, 0);
    } finally {
      fixed.stop();
    }
  });

  test("active sessions never exceed the limit; a live job waits for a free slot", async () => {
    const admin = await new Client().as("admin");
    const seeded = (await admin.req("GET", "/api/admin/queue")).body;
    checkSlots(seeded);
    expect(seeded.limit).toBe(8); // feed=all: 8 seeded sessions → override raised to fit
    await admin.req("POST", "/__mock/session", { as: "admin", feed: "proto" });
    const proto = (await admin.req("GET", "/api/admin/queue")).body;
    checkSlots(proto);
    expect(proto.limit).toBe(D.CURRENT_LIMIT);

    admin.csrf = (await admin.req("POST", "/__mock/reset", { as: "admin" })).body.csrf_token;
    const user = await new Client().as("user");
    await user.req("POST", "/api/consultations", { message: "Вопрос #direct" });
    await user.req("POST", "/api/tasks/t-ci/runs", { user_input: "a" });
    await user.req("POST", "/api/chats", { message: "Сделай", agent: "plan" });
    for (let i = 0; i < 8; i++) { checkSlots((await admin.req("GET", "/api/admin/queue")).body); await Bun.sleep(40); }

    // only a waiting_permission seed job left in the way: the live job stays queued until it is stopped
    admin.csrf = (await admin.req("POST", "/__mock/reset", { as: "admin" })).body.csrf_token;
    expect((await admin.req("PATCH", "/api/admin/settings/concurrency", { value: 1 })).status).toBe(200);
    const u2 = await new Client().as("user");
    const q = (await u2.req("POST", "/api/consultations", { message: "Ждёт слот #direct" })).body;
    await Bun.sleep(150);
    const waiting = (await admin.req("GET", "/api/admin/queue")).body;
    checkSlots(waiting);
    expect(waiting.jobs.find((j: any) => j.chat_id === q.id).status).toBe("queued");
    expect(waiting.jobs.find((j: any) => j.id === "j-f-permission-2").status).toBe("waiting_permission");
    const started = await collectEvents(u2, (evs) => evs.some((e) => e.type === "consultation.updated" && e.job.chat_id === q.id && e.job.status === "running"));
    await admin.req("POST", "/api/admin/jobs/j-f-permission-2/stop");
    await started.finished;
    expect(started.events.some((e) => e.type === "consultation.updated" && e.job.status === "running")).toBe(true);
  });

  test("chat.updated: backend item shape on folder status changes and the terminal transition", async () => {
    const user = await new Client().as("user");
    const admin = await new Client().as("admin");
    const until = (evs: any[]) => evs.some((e) => e.type === "chat.updated" && e.chat_id === "f-running" && e.status === "idle");
    const us = await collectEvents(user, until);
    const as = await collectEvents(admin, until);
    await Bun.sleep(30);
    await user.req("POST", "/__mock/play", { id: "f-running" });
    // f-running → waiting_permission; answer it → running → completed
    for (let i = 0; i < 40 && !mock.store.permissions.get("f-running")?.length; i++) await Bun.sleep(20);
    const pid = (await user.req("GET", "/api/chats/f-running/permissions")).body[0].id;
    await user.req("POST", `/api/chats/f-running/permissions/${pid}`, { response: "once" });
    const mine = (await us.finished).filter((e) => e.type === "chat.updated" && e.chat_id === "f-running");
    expect(mine.map((e) => e.status)).toEqual(["waiting_permission", "running", "idle"]);
    expect(Object.keys(mine[0]).sort()).toEqual(["archived_at", "chat_id", "created_at", "id", "kind", "phase", "snoozed_until", "status", "task_id", "title", "type", "updated_at"]);
    expect(mine[0]).toMatchObject({ kind: "folder", id: "f-running", phase: null, task_id: null });
    const adminSeen = (await as.finished).filter((e) => e.type === "chat.updated" && e.chat_id === "f-running");
    expect(adminSeen.at(-1)).toMatchObject({ status: "idle", owner_user_id: "u-isokolova" });
    expect(mine.some((e) => "owner_user_id" in e)).toBe(false);
  });

  test("chat events: tasks_only gets task chats, not folders; other users get nothing", async () => {
    const tasks = await new Client().as("tasks"); // i.sokolova with tasks_only
    const admin = await new Client().as("admin");
    const taskChat = mock.store.jobs.get("r-running").chat_id;
    const ts = await collectEvents(tasks, (evs) => evs.some((e) => e.chat_id === taskChat), 1500);
    const nikitina = mock.store.chats.get("f-running");
    await Bun.sleep(30);
    await admin.req("POST", "/__mock/emit", { type: "chat.updated", chat_id: "f-running", title: "Папка" });
    await admin.req("POST", "/__mock/emit", { type: "chat.updated", chat_id: taskChat, title: "Задача" });
    const got = (await ts.finished).filter((e) => e.type === "chat.updated");
    expect(got.map((e) => e.chat_id)).toEqual([taskChat]);
    expect(got[0]).toMatchObject({ kind: "task", id: "r-running" });
    expect(nikitina.owner_user_id).toBe("u-isokolova");
    // a folder event of another owner never reaches i.sokolova
    const user = await new Client().as("user");
    const us = await collectEvents(user, () => false, 400);
    const other = [...mock.store.chats.values()].find((c) => c.kind === "chat" && !c.consultation && c.owner_user_id !== "u-isokolova");
    await Bun.sleep(30);
    await admin.req("POST", "/__mock/emit", { type: "chat.updated", chat_id: other.id, title: "Чужая" });
    expect((await us.finished).some((e) => e.type === "chat.updated")).toBe(false);
  });

  test("chat.questions.updated is {type, workspace, session_id}; never for consultations", async () => {
    const user = await new Client().as("user");
    const s = await collectEvents(user, (evs) => evs.filter((e) => e.type === "chat.questions.updated").length >= 1 && evs.some((e) => e.type === "consultation.updated" && e.job.status === "waiting_question"), 5000);
    await user.req("POST", "/api/consultations", { message: "Уточни" });
    await user.req("POST", "/api/chats", { message: "Сделай #question", agent: "build" });
    const evs = await s.finished;
    const q = evs.filter((e) => e.type === "chat.questions.updated");
    expect(q).toHaveLength(1);
    expect(Object.keys(q[0]).sort()).toEqual(["session_id", "type", "workspace"]);
    const consultations = [...mock.store.chats.values()].filter((c) => c.consultation).map((c) => c.opencode_session_id);
    expect(consultations).not.toContain(q[0].session_id);
  });

  test("opencode.disconnected without workspace reaches every workspace's users", async () => {
    const user = await new Client().as("user");
    const admin = await new Client().as("admin");
    const us = await collectEvents(user, (evs) => evs.some((e) => e.type === "opencode.disconnected"), 1500);
    const as = await collectEvents(admin, (evs) => evs.some((e) => e.type === "opencode.disconnected"), 1500);
    await Bun.sleep(30);
    expect((await admin.req("POST", "/__mock/emit", { type: "opencode.disconnected" })).body).toEqual({ published: true });
    const got = (await us.finished).filter((e) => e.type === "opencode.disconnected");
    expect(got).toEqual([{ type: "opencode.disconnected", workspace: D.ws("qa/firmware-sdk") }]);
    expect((await as.finished).some((e) => e.type === "opencode.disconnected")).toBe(true);
  });

  test("audit objects resolve to fixtures", async () => {
    const c = await new Client().as("admin");
    const audit = (await c.req("GET", "/api/admin/audit?limit=500")).body.items;
    const queue = (await c.req("GET", "/api/admin/queue")).body;
    const requests = (await c.req("GET", "/api/admin/requests")).body;
    for (const row of audit.filter((r: any) => r.object_type === "job")) expect(queue.jobs.some((j: any) => j.id === row.object_id)).toBe(true);
    const runs = audit.filter((r: any) => r.object_type === "task_run");
    expect(runs.length).toBeGreaterThan(0);
    for (const row of runs) expect(requests.find((r: any) => r.id === row.object_id)?.title).toBe("Разбор упавшего CI-пайплайна");
  });

  test("t-conan is published to 3 users (prototype: 5 tasks for i.sokolova)", async () => {
    const user = await new Client().as("user");
    expect((await user.req("GET", "/api/tasks")).body.map((t: any) => t.id).sort()).toEqual(["t-ci", "t-conan", "t-dup", "t-mr", "t-notes"]);
    const admin = await new Client().as("admin");
    const conan = (await admin.req("GET", "/api/admin/tasks")).body.find((t: any) => t.id === "t-conan");
    expect(conan).toMatchObject({ is_published: 1, visibility: "selected", audience_user_ids: ["u-anikitina", "u-isokolova", "u-pvolkov"] });
  });

  test("updated_by (user id) on admin tasks and queues", async () => {
    const c = await new Client().as("admin");
    expect((await c.req("GET", "/api/admin/tasks")).body.find((t: any) => t.id === "t-ci").updated_by).toBe("u-dorlov");
    await c.req("POST", "/__mock/patch", { collection: "tasks", id: "t-ci", set: { title: "Разбор CI" }, by: "p.volkov" });
    expect((await c.req("GET", "/api/admin/tasks")).body.find((t: any) => t.id === "t-ci").updated_by).toBe("u-pvolkov");
    const queues = (await c.req("GET", "/api/admin/queues")).body;
    expect(queues.every((q: any) => "updated_by" in q)).toBe(true);
    const q = queues.find((x: any) => x.id === "long");
    const patched = await c.req("PATCH", "/api/admin/queues/long", { name: q.name, priority: q.priority, max_workers: 2, updated_at: q.updated_at });
    expect(patched.body.updated_by).toBe("u-dorlov");
    expect((await c.req("GET", "/api/admin/queues")).body.find((x: any) => x.id === "long").updated_by).toBe("u-dorlov");
    expect((await c.req("GET", "/api/admin/tasks/t-ci")).status).toBe(405); // the backend has no single-task GET (only PATCH/DELETE on the path)
  });

  test("statistics runs carry login", async () => {
    const c = await new Client().as("admin");
    const user = await new Client().as("user");
    const run = (await user.req("POST", "/api/tasks/t-ci/runs", { user_input: "x" })).body;
    const items = (await c.req("GET", "/api/admin/statistics/runs")).body.items;
    expect(items.every((r: any) => typeof r.login === "string")).toBe(true);
    expect(items.find((r: any) => r.id === run.id).login).toBe("i.sokolova");
    expect(new Set(items.map((r: any) => r.login)).size).toBeGreaterThan(1);
  });

  test("GET /__mock/session?redirect= always answers 302 with the session cookie", async () => {
    for (let i = 0; i < 5; i++) {
      const res = await fetch(`${mock.url}/__mock/session?as=admin&redirect=/admin/queue`, { redirect: "manual" });
      expect(res.status).toBe(302);
      expect(res.headers.get("location")).toBe("/admin/queue");
      const cookies = res.headers.getSetCookie();
      expect(cookies.filter((x) => x.startsWith("ocw_session=")).length).toBe(1);
      const me = await fetch(`${mock.url}/api/auth/me`, { headers: { cookie: cookies.map((x) => x.split(";")[0]).join("; ") } });
      expect((await me.json()).user.login).toBe("d.orlov");
    }
    const json = await fetch(`${mock.url}/__mock/session?as=user`, { redirect: "manual" });
    expect(json.status).toBe(200);
  });
});
