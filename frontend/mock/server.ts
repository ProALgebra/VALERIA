// VALERIA API dev mock. Zero dependencies: `~/.bun/bin/bun frontend/mock/server.ts --port 4301 [--speed 2] [--fixed-now]`.
// Shapes follow src/opencode_web/app.py + consultations.py; new routes follow tmp/new-ui/BRIEF.md (§1–9).
// Scenario control: ?as=user|tasks|admin|none&folder=0|1&first=0|1&feed=all|proto (URL, Referer or x-mock-as),
// /__mock/session, /__mock/state, /__mock/reset, /__mock/emit, /__mock/play, /__mock/patch. See SCENARIOS.md.
import * as D from "./data";
import {
  ACTIVE, HttpError, matchesTokens, parseIso, searchTokens, Store, TERMINAL, UNFINISHED,
  type Json, type MockOptions, type Profile,
} from "./store";

const COOKIE = "ocw_session";
const SCENARIO_COOKIE = "mock_scenario";
const SESSION_TTL = 12 * 3600 * 1000;

type Auth = "public" | "user" | "csrf" | "admin_read" | "admin";
interface Ctx {
  req: Request; url: URL; path: string; method: string; params: Record<string, string>;
  body: Json; user: Json; session: { token: string; csrf: string } | null; cookies: string[];
}
type Handler = (c: Ctx) => Response | Json | Promise<Response | Json>;
interface Route { method: string; re: RegExp; keys: string[]; auth: Auth; status: number; handler: Handler }

const json = (status: number, body: Json, headers: Record<string, string> = {}) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status, headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
  });
const fail = (status: number, detail: Json): never => { throw new HttpError(status, detail); };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const token = () => Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) => b.toString(16).padStart(2, "0")).join("");
const sha256 = (s: string) => new Bun.CryptoHasher("sha256").update(s).digest("hex");

// ---------------------------------------------------------------- pydantic-like validation
const missing = (key: string, input: Json) => ({ type: "missing", loc: ["body", key], msg: "Field required", input });
function validate(body: Json, spec: Record<string, string>, opts: { forbidExtra?: boolean } = {}) {
  const errors: Json[] = [];
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    fail(422, [{ type: "model_attributes_type", loc: ["body"], msg: "Input should be a valid dictionary or object to extract fields from", input: body }]);
  }
  for (const [key, rule] of Object.entries(spec)) {
    const required = rule.endsWith("!");
    const kind = rule.replace(/[!?]$/, "");
    const value = body[key];
    if (value === undefined) { if (required) errors.push(missing(key, body)); continue; }
    if (value === null) { if (required) errors.push({ type: `${kind}_type`, loc: ["body", key], msg: `Input should be a valid ${kind}`, input: value }); continue; }
    const ok = kind === "string" ? typeof value === "string" : kind === "int" ? Number.isInteger(value)
      : kind === "bool" ? typeof value === "boolean" : kind === "list" ? Array.isArray(value) : true;
    if (!ok) errors.push({ type: `${kind === "int" ? "int" : kind}_type`, loc: ["body", key], msg: `Input should be a valid ${kind === "int" ? "integer" : kind}`, input: value });
  }
  if (opts.forbidExtra) {
    for (const key of Object.keys(body)) if (!(key in spec)) errors.push({ type: "extra_forbidden", loc: ["body", key], msg: "Extra inputs are not permitted", input: body[key] });
  }
  if (errors.length) fail(422, errors);
}
function minLen(body: Json, key: string, n: number) {
  if (typeof body[key] === "string" && body[key].length < n) {
    fail(422, [{ type: "string_too_short", loc: ["body", key], msg: `String should have at least ${n} character${n === 1 ? "" : "s"}`, input: body[key], ctx: { min_length: n } }]);
  }
}

// ---------------------------------------------------------------- cookies / profile
function parseCookies(header: string | null) {
  const out: Record<string, string> = {};
  for (const part of (header ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}
const sessionCookie = (value: string) => `${COOKIE}=${value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${SESSION_TTL / 1000}`;
const clearSessionCookie = () => `${COOKIE}=""; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`;
const profileCookie = (p: Profile) =>
  `${SCENARIO_COOKIE}=${encodeURIComponent(`as=${p.as}&folder=${p.folder ? 1 : 0}&first=${p.first ? 1 : 0}&feed=${p.feed}`)}; SameSite=Lax; Path=/; Max-Age=31536000`;
function profileFrom(params: URLSearchParams | null, base: Profile): Profile | null {
  if (!params) return null;
  const as = params.get("as");
  if (!as || !["user", "tasks", "admin", "none"].includes(as)) return null;
  return {
    as: as as Profile["as"],
    folder: params.has("folder") ? params.get("folder") !== "0" : base.folder,
    first: params.has("first") ? params.get("first") === "1" : false,
    feed: params.get("feed") === "proto" ? "proto" : "all",
  };
}
const sameProfile = (a: Profile, b: Profile) => a.as === b.as && a.folder === b.folder && a.first === b.first && a.feed === b.feed;

// ---------------------------------------------------------------- server
export function createMockServer(opts: MockOptions & { port?: number; hostname?: string } = {}) {
  const store = new Store(opts);
  const routes: Route[] = [];
  const add = (method: string, pattern: string, auth: Auth, handler: Handler, status = 200) => {
    const keys: string[] = [];
    const re = new RegExp(`^${pattern.replace(/:(\w+)/g, (_m, k) => { keys.push(k); return "([^/]+)"; })}$`);
    routes.push({ method, re, keys, auth, status, handler });
  };

  // -------------------------------------------------------------- helpers
  const nowIso = () => store.nowIso();
  const userBy = (id: string) => store.users.get(id);
  const visibleTask = (taskId: string, userId: string) => {
    const t = store.tasks.get(taskId);
    return !!t && !!t.is_published && (t.visibility === "all" || !!store.taskAudience.get(taskId)?.has(userId));
  };
  const ownedChat = (c: Ctx, chatId: string) => {
    const chat = store.chats.get(chatId);
    if (!chat || chat.kind === "task" || chat.consultation) fail(404, "Chat not found");
    if (c.user.role !== "admin" && (chat.owner_user_id !== c.user.id || chat.workspace_snapshot !== c.user.workspace_path)) fail(404, "Chat not found");
    return chat;
  };
  const ownedConsultation = (c: Ctx, chatId: string, admin = false) => {
    const chat = store.chats.get(chatId);
    if (!chat || !chat.consultation) fail(404, "Диалог не найден");
    if (!admin && (chat.owner_user_id !== c.user.id || chat.workspace_snapshot !== c.user.workspace_path)) fail(404, "Диалог не найден");
    return chat;
  };
  /** app.py visible_item: the caller's own chat, of a kind they may use, in their current workspace (task runs follow the user). */
  const anyChatOfOwner = (c: Ctx, chatId: string) => {
    const chat = store.chats.get(chatId);
    if (!chat || chat.owner_user_id !== c.user.id) fail(404, "Chat not found");
    const kind = store.item(chat).kind;
    if (c.user.tasks_only && kind === "folder") fail(404, "Chat not found");
    if (kind !== "task" && c.user.workspace_path && chat.workspace_snapshot !== c.user.workspace_path) fail(404, "Chat not found");
    return chat;
  };
  const idempotent = (c: Ctx, fn: () => Json) => {
    const key = c.req.headers.get("idempotency-key");
    if (key !== null && (!key.trim() || key.length > 200)) fail(422, "Invalid Idempotency-Key");
    const fingerprint = sha256(JSON.stringify([c.path, c.user.workspace_path ?? null, c.body ?? null]));
    if (key) {
      const prev = store.idem.get(`${c.user.id}\u0000${key}`);
      if (prev) {
        if (prev.fingerprint !== fingerprint) fail(409, "Idempotency-Key was used for another request");
        return prev.body;
      }
    }
    const result = fn();
    if (key) store.idem.set(`${c.user.id}\u0000${key}`, { fingerprint, body: JSON.parse(JSON.stringify(result ?? {})) });
    return result;
  };
  const splitModel = (model: string | null | undefined) => {
    if (!model) return null;
    if (!model.includes("/") || model.split("/", 2).some((p) => !p.trim())) fail(422, "Model must use provider/model format");
    if (!D.MODELS.some((m) => m.id === model)) fail(422, "Model is not allowed");
    return model;
  };
  const checkAgent = (agent: string | null | undefined) => {
    if (agent && !D.AGENTS.includes(agent)) fail(422, "Agent is not allowed");
    return agent || null;
  };
  const resolveWorkspace = (path: string) => (path.startsWith("/") ? path : D.ws(path.replace(/^\.?\/+/, "")));
  const concurrency = (collection: string, current: Json, body: Json) => {
    if (body?.updated_at && body.updated_at !== current.updated_at && parseIso(body.updated_at) !== parseIso(current.updated_at)) {
      fail(409, { code: "conflict", updated_by: store.updatedBy.get(`${collection}:${current.id}`) ?? null, updated_at: current.updated_at });
    }
  };
  const gone = (collection: string, id: string): never =>
    fail(404, { code: "deleted", deleted_by: store.tombstones.get(`${collection}:${id}`) ?? null });
  const live = (job: Json) => { job._live = true; return job; };

  const enqueueMessage = (chat: Json, actor: string, body: Json) => {
    const content = typeof body.message === "string" ? body.message.trim() : "";
    if (!content) return null;
    const pinned = chat.preferred_provider_id && chat.preferred_model_id ? `${chat.preferred_provider_id}/${chat.preferred_model_id}` : null;
    const model = splitModel(body.model) ?? pinned ?? D.DEFAULT_MODEL;
    const agent = checkAgent(body.agent) ?? chat.preferred_agent ?? D.DEFAULT_AGENT;
    const job = live(store.addJob(chat, { content, agent, model, status: "queued", created: nowIso(), actor }));
    job.model_variant = body.model_variant ?? (model === pinned ? chat.preferred_model_variant : null);
    chat.updated_at = job.created_at;
    store.emitJob(job);
    if (chat.title === "Новый диалог") {
      store.after(1500, () => {
        if (!store.chats.has(chat.id) || chat.title !== "Новый диалог") return;
        chat.title = content.replace(/#\w+/g, "").replace(/\s+/g, " ").trim().slice(0, 60) || "Новый диалог";
        store.emitChat(chat);
      });
    }
    store.playFolder(job);
    return store.jobRow(job);
  };

  // -------------------------------------------------------------- auth & profile
  add("GET", "/api/health", "public", () => ({ status: "ok", version: "mock", dispatcher_limit: store.settings.limit }));
  add("POST", "/api/auth/login", "public", async (c) => {
    validate(c.body, { login: "string!", password: "string!" });
    const user = [...store.users.values()].find((u) => u.login === c.body.login);
    if (!user || user.status !== "active" || user.password !== c.body.password) {
      await sleep(200);
      fail(401, "Неверный логин или пароль");
    }
    const session = { token: token(), user_id: user.id, csrf: token(), expires: Date.now() + SESSION_TTL };
    store.sessions.set(session.token, session);
    user.last_login_at = nowIso();
    store.addAudit(user.id, "auth.login", { type: "user", id: user.id });
    store.profile = { ...store.profile, as: "none" };
    c.cookies.push(sessionCookie(session.token), profileCookie(store.profile));
    return { user: store.publicUser(user), csrf_token: session.csrf };
  });
  add("POST", "/api/auth/logout", "csrf", (c) => {
    if (c.session) store.sessions.delete(c.session.token);
    store.profile = { ...store.profile, as: "none" };
    c.cookies.push(clearSessionCookie(), profileCookie(store.profile));
    return undefined;
  }, 204);
  add("GET", "/api/auth/me", "user", (c) => ({ user: store.publicUser(c.user), csrf_token: c.session!.csrf }));
  add("PATCH", "/api/profile", "csrf", (c) => {
    validate(c.body, { mattermost_username: "string?" });
    if (!("mattermost_username" in c.body)) return store.publicUser(c.user);
    const name = (c.body.mattermost_username ?? "").trim().replace(/^@/, "").toLowerCase() || null;
    if (name && [...store.users.values()].some((u) => u.id !== c.user.id && u.mattermost_username === name)) fail(409, "Mattermost user is already assigned");
    c.user.mattermost_username = name;
    c.user.mattermost_user_id = name ? `mm${name.replace(/\W/g, "")}` : null;
    c.user.updated_at = nowIso();
    store.addAudit(c.user.id, "user.profile_update", { type: "user", id: c.user.id, meta: { mattermost_notifications: !!name } });
    return store.publicUser(c.user);
  });
  add("GET", "/api/config/public", "user", (c) => {
    const result: Json = {
      workspace: c.user.workspace_path, role: c.user.role, max_active_sessions: store.settings.limit,
      agents: D.AGENTS, default_agent: D.DEFAULT_AGENT, default_model: D.DEFAULT_MODEL,
    };
    if (c.user.role === "admin") result.max_active_jobs_hard_limit = D.USER_JOBS_HARD_LIMIT;
    return result;
  });
  add("GET", "/api/options", "user", (c) => {
    const models = [...D.MODELS].sort((a, b) => a.provider.localeCompare(b.provider) || a.name.localeCompare(b.name));
    const result: Json = { agents: D.AGENTS, default_agent: D.DEFAULT_AGENT, default_model: D.DEFAULT_MODEL, models };
    const chatId = c.url.searchParams.get("chat_id");
    if (chatId) {
      const chat = ownedChat(c, chatId);
      result.selected_agent = chat.preferred_agent ?? D.DEFAULT_AGENT;
      result.selected_model = chat.preferred_provider_id ? `${chat.preferred_provider_id}/${chat.preferred_model_id}` : D.DEFAULT_MODEL;
      result.selected_model_variant = chat.preferred_model_variant;
    }
    return result;
  });

  // -------------------------------------------------------------- new contract: items + chat actions
  add("GET", "/api/me/items", "user", (c) => {
    const ws = c.user.workspace_path;
    return [...store.chats.values()]
      .filter((chat) => chat.owner_user_id === c.user.id)
      .filter((chat) => chat.kind === "task" || !ws || chat.workspace_snapshot === ws)
      .map((chat) => store.item(chat))
      .filter((item) => !c.user.tasks_only || item.kind !== "folder")
      .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at));
  });
  const chatAction = (action: "archive" | "unarchive" | "snooze" | "unsnooze") => (c: Ctx) => {
    const chat = anyChatOfOwner(c, c.params.id);
    const now = nowIso();
    let meta: Json = {};
    if (action === "snooze") {
      validate(c.body, { until: "string!" });
      const until = parseIso(c.body.until);
      if (!/([+-]\d{2}:?\d{2}|Z)$/.test(c.body.until) || Number.isNaN(until)) fail(422, "until must be an ISO 8601 datetime with an offset");
      if (until <= store.now()) fail(422, "Snooze time must be in the future");
      if (chat.archived_at) fail(409, "Archived chats cannot be snoozed");
      chat.snoozed_until = new Date(until).toISOString().replace(/\.(\d{3})Z$/, ".$1000+00:00");
      meta = { until: chat.snoozed_until };
    } else if (action === "unsnooze") {
      chat.snoozed_until = null;
    } else if (action === "archive") {
      chat.archived_at = chat.archived_at ?? now;
      chat.snoozed_until = null;
    } else {
      if (chat.archived_at && store.item(chat).kind !== "task" && chat.archived_reason) fail(409, "Chat was archived after a workspace change");
      chat.archived_at = null;
      chat.archived_reason = null;
    }
    chat.updated_at = now;
    store.armSnooze(chat);
    store.addAudit(c.user.id, `chat.${action}`, { effective: chat.owner_user_id, type: "chat", id: chat.id, meta });
    store.emitChat(chat);
    return store.item(chat);
  };
  for (const action of ["archive", "unarchive", "snooze", "unsnooze"] as const) add("POST", `/api/chats/:id/${action}`, "csrf", chatAction(action));
  add("DELETE", "/api/chats/:id", "csrf", (c) => {
    const chat = anyChatOfOwner(c, c.params.id);
    for (const job of store.chatJobs(chat.id)) if (UNFINISHED.has(job.status)) store.setStatus(job, "cancelled");
    const row = store.chatEventRow(chat);
    store.addAudit(c.user.id, "chat.delete", { effective: chat.owner_user_id, type: "chat", id: chat.id });
    store.dropChat(chat.id);
    store.tombstones.set(`chats:${chat.id}`, c.user.login);
    const t = store.snoozeTimers.get(chat.id);
    if (t) clearTimeout(t);
    store.publish({ type: "chat.deleted", item: row });
    return undefined;
  }, 204);

  // -------------------------------------------------------------- consultations (questions)
  const settingsOk = () => { if (!store.settings.consultation_model) fail(503, "Администратор ещё не настроил консультации"); };
  add("GET", "/api/consultations", "user", (c) =>
    [...store.chats.values()]
      .filter((chat) => chat.consultation && chat.owner_user_id === c.user.id && chat.workspace_snapshot === c.user.workspace_path)
      .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at))
      .map((chat) => store.consultationSummary(chat)));
  add("POST", "/api/consultations", "csrf", (c) => {
    validate(c.body, { message: "string!" });
    if (!c.user.workspace_path) fail(422, "Рабочая папка не назначена");
    const message = c.body.message.trim();
    if (!message) fail(422, "Введите вопрос");
    settingsOk();
    return idempotent(c, () => {
      const now = nowIso();
      const chat = store.addChat({ id: store.newId("q"), owner: c.user.id, title: message.slice(0, 120), consultation: 1, created: now });
      const job = live(store.addJob(chat, { content: message, status: "queued", created: now, model: store.settings.consultation_model!, agent: "plan" }));
      store.emitJob(job);
      store.playConsultation(job, { clarify: !message.includes("#direct"), fail: message.includes("#fail") });
      return store.consultationDetail(chat);
    });
  }, 201);
  add("GET", "/api/consultations/:id", "user", (c) => store.consultationDetail(ownedConsultation(c, c.params.id)));
  add("POST", "/api/consultations/:id/messages", "csrf", (c) => {
    validate(c.body, { message: "string!" });
    const chat = ownedConsultation(c, c.params.id);
    const message = c.body.message.trim();
    if (!message) fail(422, "Введите вопрос");
    if (chat.archived_reason) fail(409, "Диалог архивирован");
    settingsOk();
    if (store.chatJobs(chat.id).some((j) => j.status === "waiting_question")) fail(409, "Сначала ответьте на уточняющий вопрос");
    return idempotent(c, () => {
      const job = live(store.addJob(chat, { content: message, status: "queued", created: nowIso(), model: store.settings.consultation_model!, agent: "plan" }));
      chat.updated_at = job.created_at;
      store.emitJob(job);
      store.playConsultation(job, { clarify: message.includes("#question"), fail: message.includes("#fail") });
      return { id: job.id, chat_id: job.chat_id, status: job.status, created_at: job.created_at, finished_at: job.finished_at };
    });
  }, 202);
  const publicQuestions = (requests: Json[]) => requests.map((r) => ({
    id: r.id,
    questions: r.questions.map((q: Json) => ({
      question: q.question, multiple: !!q.multiple, custom: q.custom !== false,
      options: (q.options ?? []).map((o: Json) => (typeof o.description === "string" ? { label: o.label, description: o.description } : { label: o.label })),
      ...(typeof q.header === "string" ? { header: q.header } : {}),
    })),
  }));
  const validateAnswers = (request: Json, answers: string[][]) => {
    const qs = request.questions;
    if (!qs.length || answers.length !== qs.length) fail(422, "Ответьте на каждый вопрос");
    qs.forEach((q: Json, i: number) => {
      const chosen = answers[i];
      if (!Array.isArray(chosen) || !chosen.length || chosen.some((v) => typeof v !== "string" || !v.trim() || v.length > 10000)) fail(422, "Выберите вариант или введите ответ");
      if (new Set(chosen).size !== chosen.length) fail(422, "В ответе повторяются варианты");
      if (!q.multiple && chosen.length !== 1) fail(422, "Для этого вопроса можно выбрать только один ответ");
      const labels = new Set((q.options ?? []).map((o: Json) => o.label));
      if (q.custom === false && chosen.some((v) => !labels.has(v))) fail(422, "Можно выбрать только предложенные варианты");
    });
  };
  add("GET", "/api/consultations/:id/questions", "user", (c) => {
    const chat = ownedConsultation(c, c.params.id);
    if (!store.chatJobs(chat.id).some((j) => j.status === "waiting_question")) return [];
    return publicQuestions(store.questions.get(chat.id) ?? []);
  });
  add("POST", "/api/consultations/:id/questions/:qid/reply", "csrf", (c) => {
    validate(c.body, { answers: "list!" }, { forbidExtra: true });
    const chat = ownedConsultation(c, c.params.id);
    const job = store.chatJobs(chat.id).find((j) => j.status === "waiting_question");
    const pending = store.questions.get(chat.id) ?? [];
    const request = pending.find((q) => q.id === c.params.qid);
    if (!job || !request) fail(409, "Вопрос уже закрыт или недоступен");
    validateAnswers(request, c.body.answers);
    store.questions.delete(chat.id);
    store.addAudit(c.user.id, "consultation.question.reply", { effective: chat.owner_user_id, type: "consultation", id: chat.id, meta: { question_id: c.params.qid } });
    store.resumeConsultation(job);
    return { accepted: true };
  });

  // -------------------------------------------------------------- tasks
  add("GET", "/api/tasks", "user", (c) =>
    [...store.tasks.values()].filter((t) => visibleTask(t.id, c.user.id))
      .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at)).map((t) => store.publicTask(t)));
  add("POST", "/api/tasks/:id/runs", "csrf", (c) => {
    validate(c.body, { user_input: "string?" }, { forbidExtra: true });
    if (typeof c.body.user_input === "string" && c.body.user_input.length > 100000) fail(422, [{ type: "string_too_long", loc: ["body", "user_input"], msg: "String should have at most 100000 characters", input: c.body.user_input }]);
    const task = store.tasks.get(c.params.id);
    if (!task || !visibleTask(task.id, c.user.id)) fail(404, "Task not found");
    if (!c.user.workspace_path) fail(422, "No workspace assigned");
    return idempotent(c, () => {
      const input = String(c.body.user_input ?? "");
      const normalized = input.split(/\s+/).filter(Boolean).join(" ");
      const now = nowIso();
      const chat = store.addChat({ id: store.newId("c"), owner: c.user.id, title: normalized ? normalized.slice(0, 120) : task.title, kind: "task", created: now });
      const job = live(store.addJob(chat, { id: store.newId("r"), content: task.content, status: "queued", created: now, task, user_input: input, agent: task.agent }));
      store.addAudit(c.user.id, "task.run", { effective: c.user.id, type: "task_run", id: job.id, meta: { task_id: task.id } });
      store.emitJob(job);
      store.playTask(job);
      return store.publicTaskRun(job);
    });
  }, 202);
  const ownRuns = (userId: string) => [...store.jobs.values()].filter((j) => j.task_id && j.effective_user_id === userId).sort((a, b) => b.sequence - a.sequence);
  add("GET", "/api/task-runs", "user", (c) => ownRuns(c.user.id).map((j) => store.publicTaskRun(j)));
  add("POST", "/api/task-runs/title-sync", "csrf", (c) => ownRuns(c.user.id).map((j) => store.publicTaskRun(j)));
  const ownRun = (c: Ctx, id: string, allowAdmin = false) => {
    const job = store.jobs.get(id);
    if (!job || !job.task_id || (job.effective_user_id !== c.user.id && !(allowAdmin && c.user.role === "admin"))) fail(404, "Task run not found");
    return job;
  };
  add("GET", "/api/task-runs/:id", "user", (c) => store.publicTaskRun(ownRun(c, c.params.id)));
  add("GET", "/api/task-runs/:id/result", "user", (c) => {
    const job = ownRun(c, c.params.id, true);
    if (job.status !== "completed") return { run_id: job.id, status: "not_completed", text: null };
    if (c.user.role === "admin" && job.effective_user_id !== c.user.id) {
      store.addAudit(c.user.id, "task_run.result", { effective: job.effective_user_id, type: "task_run", id: job.id });
    }
    const text = store.taskResults.get(job.id) ?? null;
    return { run_id: job.id, status: text ? "available" : "unavailable", text };
  });
  add("POST", "/api/task-runs/:id/stop", "csrf", (c) => {
    const job = ownRun(c, c.params.id);
    const previous = job.status;
    store.addAudit(c.user.id, "job.stop.request", { effective: c.user.id, type: "job", id: job.id, meta: { previous_status: previous, task_id: job.task_id } });
    store.stopJob(job);
    if (job.status === "cancelled" && previous === "queued") store.addAudit(c.user.id, "job.stop.completed", { effective: c.user.id, type: "job", id: job.id });
    return store.publicTaskRun(job);
  }, 202);

  // -------------------------------------------------------------- templates + folder chats
  const templatesSorted = (published: boolean) => [...store.templates.values()].filter((t) => !published || t.is_published)
    .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at));
  add("GET", "/api/templates", "user", () => templatesSorted(true));
  const folderChats = (ownerId: string | null, workspace: string | null) => [...store.chats.values()]
    .filter((chat) => chat.kind === "chat" && !chat.consultation && (!ownerId || chat.owner_user_id === ownerId) && (!workspace || chat.workspace_snapshot === workspace))
    .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at)).map((chat) => store.chatRowWithOwner(chat));
  add("GET", "/api/chats", "user", (c) => folderChats(c.user.id, c.user.workspace_path));
  add("POST", "/api/chats/title-sync", "csrf", (c) => folderChats(c.user.id, c.user.workspace_path));
  add("POST", "/api/chats", "csrf", (c) => {
    validate(c.body, { message: "string?", agent: "string?", model: "string?", model_variant: "string?", template_id: "string?" });
    if (!c.user.workspace_path) fail(422, "No workspace assigned");
    const agent = checkAgent(c.body.agent);
    const model = splitModel(c.body.model);
    return idempotent(c, () => {
      const chat = store.addChat({ id: store.newId("f"), owner: c.user.id, title: "Новый диалог", created: nowIso(), agent, model });
      chat.preferred_model_variant = c.body.model_variant ?? null;
      store.messages.set(chat.id, []);
      store.mcp.set(chat.id, { servers: D.MCP_SERVERS });
      const job = enqueueMessage(chat, c.user.id, c.body);
      return { chat: store.chatRow(chat), job };
    });
  }, 201);
  add("GET", "/api/chats/:id", "user", (c) => {
    const chat = ownedChat(c, c.params.id);
    if (c.user.role === "admin" && chat.owner_user_id !== c.user.id) {
      store.addAudit(c.user.id, "chat.admin_view", { effective: chat.owner_user_id, type: "chat", id: chat.id });
    }
    return store.chatRow(chat);
  });
  add("GET", "/api/chats/:id/messages", "user", (c) => store.messages.get(ownedChat(c, c.params.id).id) ?? []);
  add("GET", "/api/chats/:id/questions", "user", (c) => {
    const chat = ownedChat(c, c.params.id);
    return publicQuestions(store.questions.get(chat.id) ?? []);
  });
  add("POST", "/api/chats/:id/questions/:qid/reply", "csrf", (c) => {
    validate(c.body, { answers: "list!" }, { forbidExtra: true });
    const chat = ownedChat(c, c.params.id);
    const request = (store.questions.get(chat.id) ?? []).find((q) => q.id === c.params.qid);
    if (!request) fail(409, "Вопрос уже закрыт или недоступен в этом диалоге");
    validateAnswers(request, c.body.answers);
    store.questions.delete(chat.id);
    store.addAudit(c.user.id, "question.reply", { effective: chat.owner_user_id, type: "chat", id: chat.id, meta: { question_id: c.params.qid } });
    store.publish({ type: "chat.questions.updated", workspace: chat.workspace_snapshot, session_id: chat.opencode_session_id });
    const job = store.chatJobs(chat.id).find((j) => j.status === "waiting_question");
    if (job) store.afterFolderQuestion(job);
    return { accepted: true };
  });
  add("GET", "/api/chats/:id/permissions", "user", (c) => store.permissions.get(ownedChat(c, c.params.id).id) ?? []);
  add("POST", "/api/chats/:id/permissions/:pid", "csrf", (c) => {
    validate(c.body, { response: "string!", remember: "bool?" });
    if (!["once", "always", "reject"].includes(c.body.response)) fail(422, "Invalid permission response");
    const chat = ownedChat(c, c.params.id);
    const list = store.permissions.get(chat.id) ?? [];
    const found = list.find((p) => p.id === c.params.pid);
    if (found) {
      store.permissions.set(chat.id, list.filter((p) => p !== found));
      store.emitOpencode(chat, "permission.replied", { sessionID: chat.opencode_session_id, requestID: found.id, reply: c.body.response });
      const job = store.chatJobs(chat.id).find((j) => j.status === "waiting_permission");
      if (job) {
        if (job.task_id) store.setStatus(job, "running");
        else store.afterPermission(job, c.body.response);
      }
    }
    if (c.user.role === "admin") {
      store.addAudit(c.user.id, "permission.admin_response", { effective: chat.owner_user_id, type: "chat", id: chat.id, meta: { response: c.body.response } });
    }
    return { accepted: !!found };
  });
  add("GET", "/api/chats/:id/mcp", "user", (c) => store.mcp.get(ownedChat(c, c.params.id).id) ?? { servers: D.MCP_SERVERS });
  add("POST", "/api/chats/:id/messages", "csrf", (c) => {
    validate(c.body, { message: "string?", agent: "string?", model: "string?", model_variant: "string?" });
    const chat = ownedChat(c, c.params.id);
    return idempotent(c, () => enqueueMessage(chat, c.user.id, c.body) ?? {});
  }, 202);
  add("POST", "/api/chats/:id/abort", "csrf", (c) => {
    const chat = ownedChat(c, c.params.id);
    const active = store.chatJobs(chat.id).find((j) => ACTIVE.has(j.status));
    if (!active) fail(409, "Chat is not running");
    store.stopJob(active);
    return { aborted: true };
  }, 202);

  // -------------------------------------------------------------- queue / jobs
  add("GET", "/api/queue/me", "user", (c) =>
    [...store.jobs.values()].filter((j) => j.effective_user_id === c.user.id)
      // app.py my_queue: job_kind(job) in allowed_item_kinds(user), so a «Задачи и вопросы» user gets no folder jobs.
      .filter((j) => !c.user.tasks_only || j.task_id || store.chats.get(j.chat_id)?.consultation)
      // Folder jobs and questions only from the current workspace, task runs follow the user.
      .filter((j) => j.task_id || store.chats.get(j.chat_id)?.workspace_snapshot === c.user.workspace_path)
      .sort((a, b) => b.sequence - a.sequence).slice(0, 500)
      .map((j) => ({ ...store.jobRow(j), ...(j.task_id ? { chat_title: store.displayTitle(j) } : {}), position: store.position(j) })));
  add("GET", "/api/jobs/:id", "user", (c) => {
    const job = store.jobs.get(c.params.id);
    if (!job || (c.user.role !== "admin" && (job.effective_user_id !== c.user.id || job.task_id))) fail(404, "Job not found");
    const chat = store.chats.get(job.chat_id);
    if (chat?.consultation && c.user.role !== "admin") {
      if (chat.workspace_snapshot !== c.user.workspace_path) fail(404, "Job not found");
      return { id: job.id, chat_id: job.chat_id, status: job.status, created_at: job.created_at, finished_at: job.finished_at };
    }
    return store.jobRow(job);
  });
  add("DELETE", "/api/jobs/:id", "csrf", (c) => {
    const job = store.jobs.get(c.params.id);
    const owner = c.user.role === "admin" ? null : c.user.id;
    if (!job || (owner && (job.effective_user_id !== owner || job.task_id))) fail(404, "Job not found");
    if (job.status !== "queued") fail(409, "Only queued jobs can be cancelled");
    store.setStatus(job, "cancelled");
    return undefined;
  }, 204);

  // -------------------------------------------------------------- admin: users
  const userOr404 = (id: string) => store.users.get(id) ?? fail(404, "User not found");
  /** app.py checked_workspace / batch with ALLOW_SHARED_WORKSPACES off (the default): another active user has the workspace. */
  const workspaceInUse = (path: string, excludeId: string | null = null) =>
    [...store.users.values()].some((u) => u.id !== excludeId && u.status === "active" && u.workspace_path === path);
  add("GET", "/api/admin/users", "admin_read", () =>
    [...store.users.values()].sort((a, b) => a.login.localeCompare(b.login)).map((u) => store.userRow(u)));
  add("POST", "/api/admin/users", "admin", (c) => {
    validate(c.body, { login: "string!", password: "string!", role: "string?", workspace_path: "string?", tasks_only: "bool?", mattermost_username: "string?" });
    minLen(c.body, "login", 1);
    minLen(c.body, "password", 12);
    const role = c.body.role ?? "user";
    if (!["user", "admin"].includes(role)) fail(422, "Role must be 'user' or 'admin'");
    if (role !== "admin" && !c.body.workspace_path) fail(422, "Workspace is required for a user");
    const workspace = c.body.workspace_path ? resolveWorkspace(c.body.workspace_path) : null;
    if (workspace && workspaceInUse(workspace)) fail(409, "Workspace is already assigned");
    const mm = (c.body.mattermost_username ?? "").trim().replace(/^@/, "").toLowerCase() || null;
    if ([...store.users.values()].some((u) => u.login === c.body.login || (mm && u.mattermost_username === mm))) fail(409, "Login or Mattermost user already exists");
    const now = nowIso();
    const user = {
      id: store.newId("u"), login: c.body.login, password: c.body.password, role, tasks_only: c.body.tasks_only ? 1 : 0,
      mattermost_username: mm, mattermost_user_id: mm ? `mm${mm.replace(/\W/g, "")}` : null,
      workspace_path: workspace, status: "active",
      max_active_jobs: null, created_at: now, updated_at: now, last_login_at: null,
    };
    store.users.set(user.id, user);
    store.updatedBy.set(`users:${user.id}`, c.user.login);
    store.addAudit(c.user.id, "user.create", { type: "user", id: user.id });
    return store.publicUser(user);
  }, 201);
  add("PATCH", "/api/admin/users/:id", "admin", (c) => {
    validate(c.body, { role: "string?", workspace_path: "string?", status: "string?", tasks_only: "bool?", max_active_jobs: "int?", mattermost_username: "string?", updated_at: "string?" });
    const current = store.users.get(c.params.id) ?? gone("users", c.params.id);
    concurrency("users", current, c.body);
    const role = c.body.role ?? current.role;
    const path = c.body.workspace_path != null ? c.body.workspace_path : current.workspace_path;
    if (role !== "admin" && !path) fail(422, "Workspace is required for a user");
    const workspace = path ? resolveWorkspace(path) : null;
    if (workspace && workspaceInUse(workspace, current.id)) fail(409, "Workspace is already assigned");
    const changed = workspace !== current.workspace_path;
    if (changed && [...store.jobs.values()].some((j) => j.effective_user_id === current.id && UNFINISHED.has(j.status))) {
      fail(409, "Workspace cannot change while jobs are queued or active");
    }
    if ("max_active_jobs" in c.body) {
      if (c.body.max_active_jobs != null && c.body.max_active_jobs > D.USER_JOBS_HARD_LIMIT) fail(422, `Value exceeds MAX_ACTIVE_SESSIONS_PER_USER_HARD_LIMIT (${D.USER_JOBS_HARD_LIMIT})`);
      current.max_active_jobs = c.body.max_active_jobs;
    }
    if ("mattermost_username" in c.body) {
      const mm = (c.body.mattermost_username ?? "").trim().replace(/^@/, "").toLowerCase() || null;
      if (mm && [...store.users.values()].some((u) => u.id !== current.id && u.mattermost_username === mm)) fail(409, "Mattermost user is already assigned");
      current.mattermost_username = mm;
      current.mattermost_user_id = mm ? `mm${mm.replace(/\W/g, "")}` : null;
    }
    if (c.body.status != null) {
      if (!["active", "disabled"].includes(c.body.status)) fail(422, "Status must be 'active' or 'disabled'");
      current.status = c.body.status;
      if (current.status === "disabled") for (const [k, s] of store.sessions) if (s.user_id === current.id) store.sessions.delete(k);
    }
    if (c.body.tasks_only != null) current.tasks_only = c.body.tasks_only ? 1 : 0;
    current.role = role;
    current.workspace_path = workspace;
    current.updated_at = nowIso();
    // database.py apply_workspace_change: archive the folder chats and questions of other workspaces (not the ones archived
    // by hand), bring back the ones archived earlier for leaving this workspace; task runs stay as they are.
    const moved: Json[] = [];
    if (changed && workspace) {
      const now = nowIso();
      for (const chat of store.chats.values()) {
        if (chat.owner_user_id !== current.id || store.item(chat).kind === "task") continue;
        if (chat.workspace_snapshot !== workspace && !chat.archived_at && !chat.archived_reason) {
          Object.assign(chat, { archived_reason: "workspace_changed", archived_at: now, snoozed_until: null, updated_at: now });
          moved.push(chat);
        } else if (chat.workspace_snapshot === workspace && chat.archived_reason === "workspace_changed") {
          Object.assign(chat, { archived_reason: null, archived_at: null, updated_at: now });
          moved.push(chat);
        }
      }
    }
    store.updatedBy.set(`users:${current.id}`, c.user.login);
    store.addAudit(c.user.id, "user.update", { type: "user", id: current.id });
    for (const chat of moved) { store.armSnooze(chat); store.emitChat(chat); }
    return store.publicUser(current);
  });
  add("POST", "/api/admin/users/batch", "admin", (c) => {
    validate(c.body, { ids: "list!", status: "string!" });
    if (!["active", "blocked"].includes(c.body.status)) fail(422, "Status must be 'active' or 'blocked'");
    const target = c.body.status === "blocked" ? "disabled" : "active";
    const results = (c.body.ids as string[]).map((id) => {
      const user = store.users.get(id);
      if (!user) return { id, login: null, ok: false, skipped: false, reason: "not_found" };
      if (user.id === c.user.id) return { id, login: user.login, ok: false, skipped: true, reason: "self" };
      if (user.status === target) return { id, login: user.login, ok: true, skipped: true, reason: "already" };
      // Same exclusivity rule as the single update: an active user may not share a workspace with another active user.
      if (target === "active" && user.workspace_path && workspaceInUse(user.workspace_path, user.id)) {
        return { id, login: user.login, ok: false, skipped: false, reason: "workspace_in_use" };
      }
      // The backend's only other per-user failure: update_user rejects the change (ValueError).
      if (store.scenario.fail === "batch:" + user.id) return { id, login: user.login, ok: false, skipped: false, reason: "invalid" };
      user.status = target;
      user.updated_at = nowIso();
      store.updatedBy.set(`users:${user.id}`, c.user.login);
      if (target === "disabled") for (const [k, s] of store.sessions) if (s.user_id === user.id) store.sessions.delete(k);
      store.addAudit(c.user.id, "user.update", { type: "user", id: user.id, meta: { status: target } });
      return { id, login: user.login, ok: true, skipped: false, reason: null };
    });
    return { results };
  });
  add("POST", "/api/admin/users/:id/reset-password", "admin", (c) => {
    validate(c.body, { password: "string!" });
    minLen(c.body, "password", 12);
    const user = userOr404(c.params.id);
    user.password = c.body.password;
    for (const [k, s] of store.sessions) if (s.user_id === user.id) store.sessions.delete(k);
    store.addAudit(c.user.id, "user.password_reset", { type: "user", id: user.id });
    return undefined;
  }, 204);
  add("GET", "/api/admin/users/:id/chats", "admin_read", (c) => {
    store.addAudit(c.user.id, "chats.admin_list", { effective: c.params.id, type: "user", id: c.params.id });
    return folderChats(c.params.id, null);
  });
  add("POST", "/api/admin/users/:id/chats/title-sync", "admin", (c) => { userOr404(c.params.id); return folderChats(c.params.id, null); });
  const adminRuns = (userId: string | null) => [...store.jobs.values()].filter((j) => j.task_id && (!userId || j.effective_user_id === userId))
    .sort((a, b) => b.sequence - a.sequence).map((j) => ({ ...store.publicTaskRun(j), owner_login: userBy(j.effective_user_id)?.login ?? null }));
  add("GET", "/api/admin/users/:id/task-runs", "admin_read", (c) => {
    userOr404(c.params.id);
    store.addAudit(c.user.id, "task_runs.admin_list", { effective: c.params.id, type: "user", id: c.params.id });
    return adminRuns(c.params.id);
  });
  add("POST", "/api/admin/users/:id/task-runs/title-sync", "admin", (c) => { userOr404(c.params.id); return adminRuns(c.params.id); });
  const taskAccess = (userId: string) => [...store.tasks.values()].sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at)).map((t) => ({
    id: t.id, title: t.title, description: t.description, is_published: t.is_published, visibility: t.visibility,
    assigned: t.visibility === "all" || !!store.taskAudience.get(t.id)?.has(userId),
  }));
  add("GET", "/api/admin/users/:id/tasks", "admin_read", (c) => {
    userOr404(c.params.id);
    store.addAudit(c.user.id, "task_access.admin_list", { effective: c.params.id, type: "user", id: c.params.id });
    return taskAccess(c.params.id);
  });
  add("PUT", "/api/admin/users/:id/task-access", "admin", (c) => {
    validate(c.body, { task_ids: "list?" });
    const user = userOr404(c.params.id);
    const ids: string[] = [...new Set<string>(c.body.task_ids ?? [])];
    for (const id of ids) if (!store.tasks.has(id)) fail(422, `Unknown task: ${id}`);
    for (const [taskId, audience] of store.taskAudience) {
      if (ids.includes(taskId)) audience.add(user.id); else audience.delete(user.id);
    }
    store.addAudit(c.user.id, "task_access.update", { effective: user.id, type: "user", id: user.id, meta: { task_ids: ids } });
    return taskAccess(user.id);
  });
  add("POST", "/api/admin/chats/:id/messages", "admin", (c) => {
    validate(c.body, { message: "string?", agent: "string?", model: "string?", model_variant: "string?" });
    const chat = ownedChat(c, c.params.id);
    return idempotent(c, () => {
      const job = enqueueMessage(chat, c.user.id, c.body);
      store.addAudit(c.user.id, "message.impersonated", { effective: chat.owner_user_id, type: "job", id: job?.id ?? null, meta: { chat_id: chat.id } });
      return job ?? {};
    });
  }, 202);
  add("POST", "/api/admin/jobs/:id/stop", "admin", (c) => {
    const job = store.jobs.get(c.params.id) ?? fail(404, "Job not found");
    const previous = job.status;
    store.addAudit(c.user.id, "job.stop.request", { effective: job.effective_user_id, type: "job", id: job.id, meta: { previous_status: previous, task_id: job.task_id } });
    store.stopJob(job);
    if (job.status === "cancelled" && previous === "queued") store.addAudit(c.user.id, "job.stop.completed", { effective: job.effective_user_id, type: "job", id: job.id });
    return store.jobRow(job);
  }, 202);

  // -------------------------------------------------------------- admin: statistics
  const zoneOffsetMs = (zone: string, at: number) => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
      .formatToParts(new Date(at)).map((p) => [p.type, p.value]));
    return Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second) - Math.floor(at / 1000) * 1000;
  };
  const allRuns = () => {
    const liveRuns = [...store.jobs.values()].filter((j) => j._live && (j.task_id || store.chats.get(j.chat_id)?.consultation)).map((j) => ({
      id: j.id, kind: j.task_id ? "task" : "question", task_id: j.task_id, task_title: j.task_title, created_at: j.created_at,
      started_at: j.started_at, finished_at: j.finished_at, outcome: TERMINAL.has(j.status) ? j.status : null,
      login: store.users.get(j.effective_user_id)?.login ?? null,
    }));
    return [...store.statRows(), ...liveRuns];
  };
  add("GET", "/api/admin/statistics", "admin_read", (c) => {
    const tz = c.url.searchParams.get("timezone") || "UTC";
    try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); } catch { fail(422, "Unknown timezone"); }
    let day = c.url.searchParams.get("day");
    if (day && !/^\d{4}-\d{2}-\d{2}$/.test(day)) fail(422, [{ type: "date_from_datetime_parsing", loc: ["query", "day"], msg: "Input should be a valid date", input: day }]);
    if (!day) {
      const local = store.now() + zoneOffsetMs(tz, store.now());
      day = new Date(local).toISOString().slice(0, 10);
    }
    const [y, m, d] = day.split("-").map(Number);
    const startGuess = Date.UTC(y, m - 1, d);
    const start = startGuess - zoneOffsetMs(tz, startGuess);
    const endGuess = Date.UTC(y, m - 1, d + 1);
    const end = endGuess - zoneOffsetMs(tz, endGuess);
    const done = allRuns().filter((r) => r.kind === "task" && r.outcome === "completed");
    const inDay = done.filter((r) => { const f = parseIso(r.finished_at); return f >= start && f < end; });
    const exec = inDay.map((r) => (parseIso(r.finished_at) - parseIso(r.started_at)) / 1000).filter((v) => v >= 0);
    const total = inDay.map((r) => (parseIso(r.finished_at) - parseIso(r.created_at)) / 1000).filter((v) => v >= 0);
    const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
    return {
      day, timezone: tz, completed_all_time: done.length, completed_day: inDay.length,
      average_execution_seconds: avg(exec), average_total_seconds: avg(total), execution_samples: exec.length, total_samples: total.length,
    };
  });
  add("GET", "/api/admin/statistics/runs", "admin_read", (c) => {
    const from = parseIso(c.url.searchParams.get("from"));
    const to = parseIso(c.url.searchParams.get("to"));
    const type = c.url.searchParams.get("type");
    const items = allRuns().filter((r) => {
      const t = parseIso(r.created_at);
      if (!Number.isNaN(from) && t < from) return false;
      if (!Number.isNaN(to) && t >= to) return false;
      if (type === "question") return r.kind === "question";
      if (type === "task") return r.kind === "task";
      if (type) return r.task_id === type;
      return true;
    }).sort((a, b) => parseIso(a.created_at) - parseIso(b.created_at));
    return { items, generated_at: nowIso() };
  });

  // -------------------------------------------------------------- admin: queue, queues, settings
  const queueList = () => [...store.queues.values()].sort((a, b) => b.priority - a.priority || parseIso(a.created_at) - parseIso(b.created_at)).map((q) => store.queueRow(q));
  add("GET", "/api/admin/queue", "admin_read", () => {
    const jobs = [...store.jobs.values()].sort((a, b) => b.sequence - a.sequence).slice(0, 500);
    return {
      limit: store.settings.limit, env_limit: D.ENV_LIMIT, hard_limit: D.HARD_LIMIT,
      active: new Set(jobs.filter((j) => ["dispatching", "running", "waiting_permission", "cancelling"].includes(j.status)).map((j) => j.session_id)).size,
      queued: jobs.filter((j) => j.status === "queued").length,
      default_chat_queue_id: store.settings.default_chat_queue, queues: queueList(),
      jobs: jobs.map((j) => {
        const { workspace_snapshot: _w, pipeline_json: _p, resume_content: _r, ...row } = store.jobRow(j);
        row.content = String(row.content ?? "").slice(0, 90);
        if (j.task_id) row.chat_title = store.displayTitle(j);
        return row;
      }),
    };
  });
  add("GET", "/api/admin/queues", "admin_read", () => queueList());
  const queueBody = (c: Ctx) => {
    validate(c.body, { name: "string!", priority: "int?", max_workers: "int!", updated_at: "string?" });
    minLen(c.body, "name", 1);
    if (c.body.max_workers < 1) fail(422, [{ type: "greater_than_equal", loc: ["body", "max_workers"], msg: "Input should be greater than or equal to 1", input: c.body.max_workers, ctx: { ge: 1 } }]);
    if (c.body.max_workers > D.HARD_LIMIT) fail(422, "Value exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT");
  };
  add("POST", "/api/admin/queues", "admin", (c) => {
    queueBody(c);
    const name = c.body.name.trim();
    if ([...store.queues.values()].some((q) => q.name.toLowerCase() === name.toLowerCase())) fail(422, "Queue name already exists");
    const now = nowIso();
    const queue = { id: store.newId("queue"), name, priority: c.body.priority ?? 0, max_workers: c.body.max_workers, status: "active", is_system: 0, created_at: now, updated_at: now };
    store.queues.set(queue.id, queue);
    store.updatedBy.set(`queues:${queue.id}`, c.user.login);
    store.addAudit(c.user.id, "queue.create", { type: "queue", id: queue.id });
    return { ...queue, updated_by: c.user.id };
  }, 201);
  add("PATCH", "/api/admin/queues/:id", "admin", (c) => {
    const queue = store.queues.get(c.params.id) ?? gone("queues", c.params.id);
    concurrency("queues", queue, c.body ?? {});
    queueBody(c);
    const name = c.body.name.trim();
    if ([...store.queues.values()].some((q) => q.id !== queue.id && q.name.toLowerCase() === name.toLowerCase())) fail(422, "Queue name already exists");
    Object.assign(queue, { name, priority: c.body.priority ?? 0, max_workers: c.body.max_workers, updated_at: nowIso() });
    store.updatedBy.set(`queues:${queue.id}`, c.user.login);
    store.addAudit(c.user.id, "queue.update", { type: "queue", id: queue.id });
    return { ...queue, updated_by: c.user.id };
  });
  add("DELETE", "/api/admin/queues/:id", "admin", (c) => {
    const queue = store.queues.get(c.params.id);
    if (!queue || queue.status === "archived") fail(404, "Queue not found");
    if (queue.is_system) fail(409, "The system queue cannot be archived");
    const used = [...store.tasks.values()].some((t) => t.queue_id === queue.id) || store.settings.consultation_queue === queue.id
      || store.settings.default_chat_queue === queue.id || [...store.jobs.values()].some((j) => j.queue_id === queue.id && UNFINISHED.has(j.status));
    if (used) fail(409, "Queue is still assigned or contains unfinished jobs");
    queue.status = "archived";
    queue.updated_at = nowIso();
    store.addAudit(c.user.id, "queue.archive", { type: "queue", id: queue.id });
    return undefined;
  }, 204);
  add("PUT", "/api/admin/settings/default-chat-queue", "admin", (c) => {
    validate(c.body, { queue_id: "string!" });
    const queue = store.queues.get(c.body.queue_id);
    if (!queue || queue.status !== "active") fail(422, "Unknown queue");
    store.settings.default_chat_queue = queue.id;
    store.addAudit(c.user.id, "queue.chat-default.update", { type: "queue", id: queue.id });
    return { queue_id: queue.id };
  });
  add("GET", "/api/admin/mcp", "admin_read", () => {
    const busy = new Set([...store.jobs.values()].filter((j) => UNFINISHED.has(j.status) || j.status === "completed").map((j) => j.workspace_snapshot));
    const workspaces = [...new Set([...store.users.values()].map((u) => u.workspace_path).filter(Boolean))].sort();
    return workspaces.map((w) => (busy.has(w) && [...store.users.values()].some((u) => u.workspace_path === w && u.status === "active")
      ? { workspace: w, servers: D.MCP_SERVERS }
      : { workspace: w, servers: [], idle: true }));
  });
  add("PATCH", "/api/admin/settings/concurrency", "admin", (c) => {
    validate(c.body, { value: "int!" });
    if (c.body.value < 1) fail(422, [{ type: "greater_than_equal", loc: ["body", "value"], msg: "Input should be greater than or equal to 1", input: c.body.value, ctx: { ge: 1 } }]);
    if (c.body.value > D.HARD_LIMIT) fail(422, "Value exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT");
    store.settings.limit = c.body.value;
    store.addAudit(c.user.id, "concurrency.update", { meta: { value: c.body.value } });
    return { value: c.body.value };
  });
  add("DELETE", "/api/admin/settings/concurrency/override", "admin", (c) => {
    store.settings.limit = D.ENV_LIMIT;
    store.addAudit(c.user.id, "concurrency.reset");
    return undefined;
  }, 204);
  add("GET", "/api/admin/settings/consultations", "admin_read", () => ({ model: store.settings.consultation_model, queue_id: store.settings.consultation_queue }));
  add("PUT", "/api/admin/settings/consultations", "admin", (c) => {
    validate(c.body, { model: "string!", queue_id: "string?" });
    const model = c.body.model.trim();
    if (!model) fail(422, "Выберите модель");
    splitModel(model);
    const queueId = c.body.queue_id || store.settings.consultation_queue;
    const queue = store.queues.get(queueId);
    if (!queue || queue.status !== "active") fail(422, "Unknown queue");
    store.settings.consultation_model = model;
    store.settings.consultation_queue = queueId;
    store.addAudit(c.user.id, "consultation.settings.update", { meta: { model, queue_id: queueId } });
    return { model, queue_id: queueId };
  });

  // -------------------------------------------------------------- admin: templates
  add("GET", "/api/admin/templates", "admin_read", () => templatesSorted(false));
  add("POST", "/api/admin/templates", "admin", (c) => {
    validate(c.body, { title: "string!", description: "string?", content: "string!", is_published: "bool?" });
    minLen(c.body, "title", 1);
    minLen(c.body, "content", 1);
    const now = nowIso();
    const tpl = { id: store.newId("tpl"), title: c.body.title, description: c.body.description ?? null, content: c.body.content, is_published: c.body.is_published ? 1 : 0, created_by_admin_id: c.user.id, created_at: now, updated_at: now };
    store.templates.set(tpl.id, tpl);
    store.addAudit(c.user.id, "template.create", { type: "template", id: tpl.id });
    return tpl;
  }, 201);
  add("PATCH", "/api/admin/templates/:id", "admin", (c) => {
    validate(c.body, { title: "string?", description: "string?", content: "string?", is_published: "bool?" });
    const tpl = store.templates.get(c.params.id) ?? fail(404, "Template not found");
    const title = c.body.title ?? tpl.title, content = c.body.content ?? tpl.content;
    if (!String(title).trim() || !String(content).trim()) fail(422, "Title and content are required");
    Object.assign(tpl, { title, content, updated_at: nowIso() });
    if ("description" in c.body) tpl.description = c.body.description;
    if (c.body.is_published != null) tpl.is_published = c.body.is_published ? 1 : 0;
    store.addAudit(c.user.id, "template.update", { type: "template", id: tpl.id });
    return tpl;
  });
  add("DELETE", "/api/admin/templates/:id", "admin", (c) => {
    if (!store.templates.delete(c.params.id)) fail(404, "Template not found");
    store.addAudit(c.user.id, "template.delete", { type: "template", id: c.params.id });
    return undefined;
  }, 204);

  // -------------------------------------------------------------- admin: tasks
  const taskSpec = { title: "string", description: "string", content: "string", agent: "string", model: "string", model_variant: "string", is_published: "bool", plan_first: "bool", visibility: "string", user_ids: "list", next_task_ids: "list", queue_id: "string", input_hint: "string", updated_at: "string" };
  const checkHint = (body: Json) => {
    if (typeof body.input_hint === "string" && body.input_hint.length > 500) {
      fail(422, [{ type: "string_too_long", loc: ["body", "input_hint"], msg: "String should have at most 500 characters", input: body.input_hint, ctx: { max_length: 500 } }]);
    }
  };
  const checkUsers = (ids: string[] | null | undefined) => {
    const out: string[] = [];
    for (const id of new Set(ids ?? [])) { if (!store.users.has(id)) fail(422, `Unknown user: ${id}`); out.push(id); }
    return out;
  };
  const checkVisibility = (v: string) => { if (!["all", "selected"].includes(v)) fail(422, "Visibility must be 'all' or 'selected'"); return v; };
  const checkNext = (taskId: string | null, ids: string[]) => {
    for (const id of ids) { if (!store.tasks.has(id)) fail(422, `Unknown task: ${id}`); if (id === taskId) fail(422, "A task cannot follow itself"); }
    return [...new Set(ids)];
  };
  const checkQueue = (id: string) => { const q = store.queues.get(id); if (!q || q.status !== "active") fail(422, "Unknown queue"); return id; };
  add("GET", "/api/admin/tasks", "admin_read", () =>
    [...store.tasks.values()].sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at)).map((t) => store.adminTask(t)));
  add("POST", "/api/admin/tasks", "admin", (c) => {
    const spec: Record<string, string> = { ...taskSpec, title: "string!", content: "string!", agent: "string!", model: "string!" };
    delete spec.updated_at;
    validate(c.body, spec);
    minLen(c.body, "title", 1);
    minLen(c.body, "content", 1);
    checkHint(c.body);
    checkAgent(c.body.agent);
    const model = splitModel(c.body.model)!;
    const visibility = checkVisibility(c.body.visibility ?? "all");
    const users = checkUsers(c.body.user_ids);
    const queueId = checkQueue(c.body.queue_id ?? "default");
    const next = checkNext(null, c.body.next_task_ids ?? []);
    const [provider_id, model_id] = model.split("/");
    const now = nowIso();
    const task = {
      id: store.newId("t"), title: c.body.title, description: c.body.description ?? null, content: c.body.content, agent: c.body.agent,
      provider_id, model_id, model_variant: c.body.model_variant ?? null, queue_id: queueId, is_published: c.body.is_published ? 1 : 0,
      visibility, created_by_admin_id: c.user.id, created_at: now, updated_at: now, deleted_at: null, plan_first: c.body.plan_first ? 1 : 0,
      input_hint: c.body.input_hint ?? null,
    };
    store.tasks.set(task.id, task);
    store.taskAudience.set(task.id, new Set(visibility === "selected" ? users : []));
    store.taskNext.set(task.id, next);
    store.updatedBy.set(`tasks:${task.id}`, c.user.login);
    store.addAudit(c.user.id, "task.create", { type: "task", id: task.id });
    return store.adminTask(task);
  }, 201);
  add("PATCH", "/api/admin/tasks/:id", "admin", (c) => {
    validate(c.body, taskSpec);
    const task = store.tasks.get(c.params.id) ?? gone("tasks", c.params.id);
    concurrency("tasks", task, c.body);
    for (const key of ["title", "content", "agent", "model", "is_published", "plan_first", "queue_id"]) {
      if (key in c.body && c.body[key] === null) fail(422, `${key} cannot be null`);
    }
    checkHint(c.body);
    if (c.body.visibility != null) checkVisibility(c.body.visibility);
    const users = c.body.user_ids != null ? checkUsers(c.body.user_ids) : null;
    checkAgent(c.body.agent);
    const model = c.body.model ? splitModel(c.body.model)! : `${task.provider_id}/${task.model_id}`;
    if (c.body.queue_id) checkQueue(c.body.queue_id);
    for (const key of ["title", "description", "content", "agent", "model_variant", "queue_id", "input_hint"]) if (key in c.body) task[key] = c.body[key];
    if ("is_published" in c.body) task.is_published = c.body.is_published ? 1 : 0;
    if ("plan_first" in c.body) task.plan_first = c.body.plan_first ? 1 : 0;
    [task.provider_id, task.model_id] = model.split("/");
    if ("next_task_ids" in c.body) store.taskNext.set(task.id, checkNext(task.id, c.body.next_task_ids));
    if (c.body.visibility != null || users) {
      task.visibility = c.body.visibility ?? task.visibility;
      store.taskAudience.set(task.id, new Set(task.visibility === "selected" ? users ?? [...(store.taskAudience.get(task.id) ?? [])] : []));
    }
    task.updated_at = nowIso();
    store.updatedBy.set(`tasks:${task.id}`, c.user.login);
    store.addAudit(c.user.id, "task.update", { type: "task", id: task.id });
    return store.adminTask(task);
  });
  add("PUT", "/api/admin/tasks/:id/audience", "admin", (c) => {
    validate(c.body, { visibility: "string!", user_ids: "list?" });
    const task = store.tasks.get(c.params.id) ?? fail(404, "Task not found");
    const visibility = checkVisibility(c.body.visibility);
    const users = checkUsers(c.body.user_ids);
    task.visibility = visibility;
    task.updated_at = nowIso();
    store.taskAudience.set(task.id, new Set(visibility === "selected" ? users : []));
    store.updatedBy.set(`tasks:${task.id}`, c.user.login);
    store.addAudit(c.user.id, "task.audience.update", { type: "task", id: task.id, meta: { visibility, user_ids: users } });
    return store.adminTask(task);
  });
  add("DELETE", "/api/admin/tasks/:id", "admin", (c) => {
    if (!store.tasks.delete(c.params.id)) fail(404, "Task not found");
    store.taskAudience.delete(c.params.id);
    for (const next of store.taskNext.values()) { const i = next.indexOf(c.params.id); if (i >= 0) next.splice(i, 1); }
    store.tombstones.set(`tasks:${c.params.id}`, c.user.login);
    store.addAudit(c.user.id, "task.delete", { type: "task", id: c.params.id });
    return undefined;
  }, 204);
  add("GET", "/api/admin/task-runs", "admin_read", () => adminRuns(null));
  add("POST", "/api/admin/task-runs/title-sync", "admin", () => adminRuns(null));
  add("GET", "/api/admin/task-runs/:id/messages", "admin_read", (c) => {
    const job = store.jobs.get(c.params.id);
    if (!job || !job.task_id) fail(404, "Task run not found");
    store.addAudit(c.user.id, "task_run.messages", { effective: job.effective_user_id, type: "task_run", id: job.id });
    return store.runMessages(job);
  });

  // -------------------------------------------------------------- admin: consultations, requests, audit
  add("GET", "/api/admin/consultations", "admin_read", (c) => {
    const owner = c.url.searchParams.get("owner_id");
    const tokens = searchTokens(c.url.searchParams.get("q"));
    return [...store.chats.values()]
      .filter((chat) => chat.consultation && (!owner || chat.owner_user_id === owner))
      .filter((chat) => matchesTokens(tokens, [chat.title, userBy(chat.owner_user_id)?.login]))
      .sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at))
      .map((chat) => store.consultationSummary(chat, true));
  });
  add("GET", "/api/admin/consultations/:id", "admin_read", (c) => {
    const chat = ownedConsultation(c, c.params.id, true);
    store.addAudit(c.user.id, "consultation.admin_view", { effective: chat.owner_user_id, type: "chat", id: chat.id });
    return store.consultationDetail(chat, true);
  });
  add("GET", "/api/admin/requests", "admin_read", (c) => {
    const p = c.url.searchParams;
    const users = (p.get("user") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const types = (p.get("type") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const from = parseIso(p.get("from")), to = parseIso(p.get("to"));
    const taskId = p.get("task");
    const tokens = searchTokens(p.get("q"));
    return [...store.chats.values()].map((chat) => {
      const item = store.item(chat);
      const owner = userBy(chat.owner_user_id);
      return {
        kind: item.kind, id: item.id, chat_id: chat.id, title: item.title, status: item.status,
        user: { id: owner?.id ?? chat.owner_user_id, login: owner?.login ?? null }, task_id: item.task_id,
        task_title: item.task_id ? store.chatJobs(chat.id).find((j) => j.task_id)?.task_title ?? null : null,
        created_at: item.created_at, updated_at: item.updated_at,
      };
    }).filter((r) => {
      if (users.length && !users.includes(r.user.login ?? "") && !users.includes(r.user.id)) return false;
      if (types.length && !types.includes(r.kind)) return false;
      if (taskId && r.task_id !== taskId) return false;
      const t = parseIso(r.created_at);
      if (!Number.isNaN(from) && t < from) return false;
      if (!Number.isNaN(to) && t >= to) return false;
      return matchesTokens(tokens, [r.title, r.user.login, r.task_title]);
    }).sort((a, b) => parseIso(b.updated_at) - parseIso(a.updated_at));
  });
  add("GET", "/api/admin/audit", "admin_read", (c) => {
    const p = c.url.searchParams;
    const from = parseIso(p.get("from")), to = parseIso(p.get("to"));
    const logins = (p.get("user") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const actions = (p.get("action") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    const tokens = searchTokens(p.get("q"));
    const limit = Math.min(Math.max(parseInt(p.get("limit") ?? "100", 10) || 100, 1), 500);
    const cursor = p.get("cursor");
    const cursorId = cursor ? parseInt(cursor, 10) : null;
    if (cursor && (cursorId === null || Number.isNaN(cursorId))) fail(422, "Invalid cursor");
    const rows = [...store.audit].sort((a, b) => b.id - a.id).map((r) => store.auditRow(r)).filter((r) => {
      if (cursorId !== null && r.id >= cursorId) return false;
      const t = parseIso(r.created_at);
      if (!Number.isNaN(from) && t < from) return false;
      if (!Number.isNaN(to) && t >= to) return false;
      if (logins.length && !logins.includes(r.actor_login ?? "") && !logins.includes(r.effective_login ?? "")) return false;
      if (actions.length && !actions.some((a) => (a.endsWith(".") || a.endsWith("*") ? r.action.startsWith(a.replace(/\*$/, "")) : r.action === a))) return false;
      return matchesTokens(tokens, [r.action, r.object_type, r.object_id, r.metadata_json, r.actor_login, r.effective_login]);
    });
    const items = rows.slice(0, limit);
    return { items, next_cursor: rows.length > limit ? String(items[items.length - 1].id) : null };
  });

  // -------------------------------------------------------------- SSE
  const sse = (c: Ctx) => {
    const sessionToken = c.session!.token;
    let cleanup = () => {};
    const stream = new ReadableStream({
      start(controller) {
        const enc = new TextEncoder();
        const send = (s: string) => { try { controller.enqueue(enc.encode(s)); } catch { cleanup(); } };
        send(": heartbeat\n\n");
        const unsubscribe = store.subscribe((event) => {
          const session = store.sessions.get(sessionToken);
          const user = session && store.users.get(session.user_id);
          if (!user || user.status !== "active") { cleanup(); try { controller.close(); } catch { /* closed */ } return; }
          const visible = store.eventForUser(user, event);
          if (visible) send(`data: ${JSON.stringify(visible)}\n\n`);
        });
        const hb = setInterval(() => send(": heartbeat\n\n"), 20000);
        cleanup = () => { unsubscribe(); clearInterval(hb); };
        c.req.signal.addEventListener("abort", () => cleanup());
      },
      cancel() { cleanup(); },
    });
    return new Response(stream, { headers: { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" } });
  };
  add("GET", "/api/events", "user", sse);

  // -------------------------------------------------------------- mock control
  const stateSummary = () => ({
    profile: store.profile, scenario: store.scenario, speed: store.speed, fixed_now: store.fixedNow, now: nowIso(),
    proto_shift_ms: store.shift, accounts: D.ACCOUNT, password: D.DEMO_PASSWORD,
    users: [...store.users.values()].map((u) => ({ id: u.id, login: u.login, role: u.role, tasks_only: !!u.tasks_only, status: u.status, workspace_path: u.workspace_path })),
    items: [...store.chats.values()].map((chat) => ({ ...store.item(chat), owner: userBy(chat.owner_user_id)?.login })),
    queue: [...store.jobs.values()].filter((j) => UNFINISHED.has(j.status)).map((j) => ({ id: j.id, chat_id: j.chat_id, status: j.status, queue_id: j.queue_id, position: store.position(j) })),
    tasks: [...store.tasks.values()].map((t) => ({ id: t.id, title: t.title, is_published: t.is_published, updated_at: t.updated_at })),
    tombstones: Object.fromEntries(store.tombstones), sessions: store.sessions.size, timers: store.timers.size,
  });
  const applyScenario = (body: Json) => {
    const s = store.scenario;
    if ("delay" in body) s.delay = Math.max(0, Number(body.delay) || 0);
    if ("delay_path" in body) s.delay_path = body.delay_path || null;
    if ("fail" in body) s.fail = body.fail || null;
    if ("fail_status" in body) s.fail_status = Number(body.fail_status) || 503;
    if ("fail_times" in body) s.fail_times = body.fail_times == null ? null : Number(body.fail_times);
  };
  const switchPersona = (c: Ctx, profile: Profile) => {
    store.applyProfile(profile);
    c.cookies.push(profileCookie(store.profile));
    if (profile.as === "none") {
      if (c.session) store.sessions.delete(c.session.token);
      c.cookies.push(clearSessionCookie());
      return null;
    }
    const session = { token: token(), user_id: D.ACCOUNT[profile.as], csrf: token(), expires: Date.now() + SESSION_TTL };
    store.sessions.set(session.token, session);
    c.cookies.push(sessionCookie(session.token));
    return session;
  };
  const control = async (c: Ctx): Promise<Response | Json> => {
    const { method, path } = c;
    if (path === "/__mock/state" && method === "GET") return stateSummary();
    if (path === "/__mock/session") {
      const params = method === "GET" ? c.url.searchParams : new URLSearchParams(Object.entries(c.body ?? {}).filter(([, v]) => typeof v !== "object").map(([k, v]) => [k, String(v)]));
      const profile = profileFrom(params, store.profile);
      if (method === "POST") applyScenario(c.body ?? {});
      if (method === "GET") applyScenario(Object.fromEntries([...params].filter(([k]) => ["delay", "delay_path", "fail", "fail_status", "fail_times"].includes(k))));
      let session = null;
      if (profile) session = switchPersona(c, profile);
      const redirect = c.url.searchParams.get("redirect");
      if (method === "GET" && redirect) {
        // Cookies are added once by finish() in handle().
        return new Response(null, { status: 302, headers: { location: redirect.startsWith("/") ? redirect : "/" } });
      }
      return { ...stateSummary(), csrf_token: session?.csrf ?? null };
    }
    if (path === "/__mock/reset" && method === "POST") {
      const profile = store.profile;
      store.reset();
      store.scenario = { delay: 0, delay_path: null, fail: null, fail_status: 503, fail_times: null };
      // A reset with a persona starts from the default profile (folder assigned) unless the body says otherwise.
      const next = profileFrom(new URLSearchParams(Object.entries(c.body ?? {}).map(([k, v]) => [k, String(v)])), { ...profile, folder: true }) ?? profile;
      const session = switchPersona(c, next);
      return { ...stateSummary(), csrf_token: session?.csrf ?? null };
    }
    if (path === "/__mock/emit" && method === "POST") {
      const ev = c.body ?? {};
      if (!ev.type) fail(422, "type is required");
      if (["chat.updated", "chat.deleted", "task.title.updated"].includes(ev.type) && !ev.item) {
        // Chat events go through items.py chat_event_for_user, so they are published as {type, item}.
        const chat = store.chats.get(ev.chat_id) ?? fail(404, "Chat not found");
        Object.assign(chat, Object.fromEntries(Object.entries(ev).filter(([k]) => ["snoozed_until", "archived_at", "title"].includes(k))));
        store.armSnooze(chat);
        store.emitChat(chat, ev.type);
      } else if (ev.type === "job.updated" && typeof ev.job === "string") {
        const job = store.jobs.get(ev.job) ?? fail(404, "Job not found");
        if (ev.status) store.setStatus(job, ev.status); else store.emitJob(job);
      } else if (ev.type === "opencode.disconnected" && !ev.workspace) {
        // The backend publishes it per OpenCode instance, always with its workspace (opencode_client.py),
        // and delivers it only to users of that workspace (app.py can_receive_event): fan out per workspace.
        const workspaces = new Set([...store.users.values()].map((u) => u.workspace_path).filter(Boolean));
        for (const workspace of workspaces) store.publish({ ...ev, workspace });
      } else {
        store.publish(ev);
      }
      return { published: true };
    }
    if (path === "/__mock/play" && method === "POST") {
      const job = store.play(String(c.body?.id ?? ""));
      if (!job) fail(404, "Nothing to play for this id (unknown or already finished)");
      return { playing: job.id, status: job.status };
    }
    if (path === "/__mock/patch" && method === "POST") {
      const { collection, id, set = {}, by } = c.body ?? {};
      const map = ({ users: store.users, tasks: store.tasks, queues: store.queues, chats: store.chats, jobs: store.jobs, templates: store.templates } as Record<string, Map<string, Json>>)[collection];
      if (!map) fail(422, "collection must be users|tasks|queues|chats|jobs|templates");
      const row = map.get(id) ?? fail(404, `${collection}/${id} not found`);
      if (c.body.delete) {
        map.delete(id);
        store.tombstones.set(`${collection}:${id}`, by ?? null);
        return { deleted: id };
      }
      Object.assign(row, set);
      if (["users", "tasks", "queues", "templates"].includes(collection) && !("updated_at" in set)) row.updated_at = nowIso();
      if (by) store.updatedBy.set(`${collection}:${id}`, by);
      if (collection === "jobs" && set.status) store.emitJob(row);
      if (collection === "chats") { store.armSnooze(row); store.emitChat(row); }
      return row;
    }
    fail(404, "Not Found");
  };

  // -------------------------------------------------------------- dispatch
  const tasksOnlyAllowed = (method: string, path: string) =>
    path.startsWith("/api/auth/") || ["/api/config/public", "/api/events", "/api/profile", "/api/tasks", "/api/consultations", "/api/me/items", "/api/queue/me"].includes(path)
    || path.startsWith("/api/tasks/") || path.startsWith("/api/task-runs") || path.startsWith("/api/consultations/")
    || (method === "POST" && /^\/api\/chats\/[^/]+\/(archive|unarchive|snooze|unsnooze)$/.test(path))
    || (method === "DELETE" && /^\/api\/chats\/[^/]+$/.test(path));

  const log403 = (c: Ctx, detail: Json) => {
    console.log(`[mock 403] ${c.method} ${c.path} user=${c.user?.login ?? "-"} detail=${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  };

  async function handle(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const c: Ctx = { req, url, path: url.pathname, method: req.method, params: {}, body: undefined, user: null, session: null, cookies: [] };
    const finish = (res: Response) => {
      for (const ck of c.cookies) res.headers.append("set-cookie", ck);
      return res;
    };
    try {
      if (!["GET", "HEAD", "DELETE"].includes(req.method)) {
        const text = await req.text();
        if (text) {
          try { c.body = JSON.parse(text); } catch {
            fail(422, [{ type: "json_invalid", loc: ["body", 0], msg: "JSON decode error", input: {}, ctx: { error: "Expecting value" } }]);
          }
        } else c.body = {};
      }
      if (c.path.startsWith("/__mock/")) {
        const out = await control(c);
        return finish(out instanceof Response ? out : json(200, out));
      }

      // scenario: ?as= on the request, then Referer query, then x-mock-as header, then cookie
      const cookies = parseCookies(req.headers.get("cookie"));
      let referer: URL | null = null;
      try { referer = req.headers.get("referer") ? new URL(req.headers.get("referer")!) : null; } catch { referer = null; }
      const header = req.headers.get("x-mock-as");
      const explicit = profileFrom(url.searchParams, store.profile) ?? profileFrom(referer?.searchParams ?? null, store.profile)
        ?? (header ? profileFrom(new URLSearchParams({ as: header }), store.profile) : null);
      const cookieProfile = cookies[SCENARIO_COOKIE] ? profileFrom(new URLSearchParams(cookies[SCENARIO_COOKIE]), store.profile) : null;
      let sessionToken = cookies[COOKIE];
      let session = sessionToken ? store.sessions.get(sessionToken) : undefined;
      if (explicit) {
        const wanted = explicit.as === "none" ? null : D.ACCOUNT[explicit.as];
        if (!sameProfile(explicit, store.profile) || (wanted && session?.user_id !== wanted) || (!wanted && session)) {
          const s = switchPersona(c, explicit);
          session = s ?? undefined;
          sessionToken = s?.token ?? "";
        }
      } else if (cookieProfile) {
        if (!sameProfile(cookieProfile, store.profile)) store.applyProfile(cookieProfile);
        if (cookieProfile.as !== "none" && (!session || session.expires < Date.now())) {
          const s = switchPersona(c, cookieProfile);
          session = s ?? undefined;
          sessionToken = s?.token ?? "";
        }
      }

      // delay / forced errors (never on SSE)
      if (c.path !== "/api/events") {
        const delay = Number(url.searchParams.get("delay") ?? req.headers.get("x-mock-delay") ?? referer?.searchParams.get("delay") ?? NaN);
        const scenarioDelay = store.scenario.delay && (!store.scenario.delay_path || c.path.includes(store.scenario.delay_path)) ? store.scenario.delay : 0;
        const wait = Number.isFinite(delay) ? delay : scenarioDelay;
        if (wait > 0) await sleep(Math.min(wait, 60000));
        const forced = req.headers.get("x-mock-fail");
        if (forced) return finish(json(Number(forced) || 503, { detail: "Mock forced error" }));
        const s = store.scenario;
        if (s.fail && (s.fail === "*" || c.path.includes(s.fail)) && !s.fail.startsWith("batch:") && (s.fail_times === null || s.fail_times > 0)) {
          if (s.fail_times !== null) s.fail_times -= 1;
          return finish(json(s.fail_status, { detail: "Mock forced error" }));
        }
      }

      const candidates = routes.filter((r) => r.re.test(c.path));
      if (!candidates.length) fail(404, "Not Found");
      const route = candidates.find((r) => r.method === req.method || (req.method === "HEAD" && r.method === "GET"));
      if (!route) fail(405, "Method Not Allowed");
      const m = route!.re.exec(c.path)!;
      route!.keys.forEach((k, i) => { c.params[k] = decodeURIComponent(m[i + 1]); });

      if (route!.auth !== "public") {
        if (!sessionToken) fail(401, "Authentication required");
        if (!session || session.expires < Date.now()) fail(401, "Session expired");
        const user = store.users.get(session!.user_id);
        if (!user || user.status !== "active") fail(401, "Session expired");
        c.user = user;
        c.session = { token: session!.token, csrf: session!.csrf };
        if (user.tasks_only && !tasksOnlyAllowed(req.method, c.path)) fail(403, "This account can only use tasks and consultations");
        if (route!.auth === "csrf" || route!.auth === "admin") {
          if (req.headers.get("x-csrf-token") !== session!.csrf) fail(403, "CSRF check failed");
        }
        if ((route!.auth === "admin" || route!.auth === "admin_read") && user.role !== "admin") fail(403, "Administrator role required");
      }
      const out = await route!.handler(c);
      if (out instanceof Response) return finish(out);
      return finish(json(route!.status, route!.status === 204 ? undefined : out));
    } catch (e) {
      if (e instanceof HttpError) {
        if (e.status === 403) log403(c, e.detail);
        return finish(json(e.status, { detail: e.detail }));
      }
      console.error("[mock 500]", c.method, c.path, e);
      return finish(json(500, { detail: "Internal Server Error" }));
    }
  }

  const server = Bun.serve({ port: opts.port ?? 4301, hostname: opts.hostname ?? "127.0.0.1", idleTimeout: 255, fetch: handle });
  return {
    server, store, url: `http://${server.hostname}:${server.port}`,
    stop() {
      for (const t of store.timers) clearTimeout(t);
      for (const t of store.snoozeTimers.values()) clearTimeout(t);
      store.listeners.clear();
      server.stop(true);
    },
  };
}

if (import.meta.main) {
  const args = Bun.argv.slice(2);
  const arg = (name: string) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
  const port = Number(arg("--port") ?? 4301);
  const speed = Number(arg("--speed") ?? 1);
  const mock = createMockServer({ port, speed, fixedNow: args.includes("--fixed-now"), hostname: arg("--host") });
  console.log(`[mock] VALERIA API mock on ${mock.url} (speed ×${speed}${args.includes("--fixed-now") ? ", fixed now = 2026-10-24 14:30 local" : ""})`);
  console.log(`[mock] open the UI with ?as=user|tasks|admin (&folder=0, &first=1, &feed=proto, &delay=ms) — see frontend/mock/SCENARIOS.md`);
}
