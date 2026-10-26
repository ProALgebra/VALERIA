// In-memory state of the VALERIA mock: seed, row shapes (as in the backend code),
// event fan-out and timed lifecycles.
import * as D from "./data";
import { generateStats, statTime, STAT_TASK_ID, type StatRun } from "./stats";

// deno-lint-ignore no-explicit-any
export type Json = any;

export interface MockOptions { speed?: number; fixedNow?: boolean; quiet?: boolean }

export class HttpError extends Error {
  constructor(public status: number, public detail: Json) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

const JOB_COLUMNS = [
  "id", "sequence", "chat_id", "session_id", "effective_user_id", "actor_user_id", "workspace_snapshot",
  "message_id", "content", "agent", "provider_id", "model_id", "model_variant", "queue_id", "task_id",
  "task_title", "task_initial_title", "user_input", "status", "created_at", "started_at", "finished_at",
  "error_code", "pipeline_json", "parent_job_id", "session_pending", "plan_first", "phase2_message_id",
  "resume_content", "resume_from_message_id", "resume_monitor",
];
const CHAT_COLUMNS = [
  "id", "owner_user_id", "opencode_session_id", "title", "workspace_snapshot", "status", "kind",
  "archived_reason", "preferred_agent", "preferred_provider_id", "preferred_model_id",
  "preferred_model_variant", "created_at", "updated_at", "consultation", "archived_at", "snoozed_until",
];
export const ACTIVE = new Set(["dispatching", "running", "waiting_permission", "waiting_question", "cancelling"]);
export const UNFINISHED = new Set([...ACTIVE, "queued"]);
export const TERMINAL = new Set(["completed", "failed", "cancelled"]);
/** Statuses that hold a dispatcher slot (database.claim_next_job); waiting_question does not. */
export const SLOT = new Set(["dispatching", "running", "waiting_permission", "cancelling"]);
const CHAT_EVENTS = new Set(["chat.updated", "chat.deleted", "task.title.updated"]);

export const iso = (ms: number) => new Date(ms).toISOString().replace(/\.(\d{3})Z$/, ".$1000+00:00");
export const parseIso = (s: string | null | undefined) =>
  s ? Date.parse(String(s).replace(/(\.\d{3})\d+/, "$1")) : NaN;

const pick = (row: Json, cols: string[]) => Object.fromEntries(cols.map((c) => [c, row[c] ?? null]));

export interface Session { token: string; user_id: string; csrf: string; expires: number }
export interface Profile { as: "user" | "tasks" | "admin" | "none"; folder: boolean; first: boolean; feed: "all" | "proto" }
export interface Scenario {
  delay: number; delay_path: string | null;
  fail: string | null; fail_status: number; fail_times: number | null;
}

/** Demo entities that exist only to show one prototype state; dropped by feed=proto. */
export const STATE_EXTRAS = ["q-answer", "q-running", "q-queued", "q-failed", "f-planning", "f-plan", "f-running", "f-done", "f-denied", "r-plan"];

export class Store {
  speed: number;
  fixedNow: boolean;
  quiet: boolean;
  shift = 0;
  epoch = Date.now();
  seq = 0;
  idCounter = 0;
  users = new Map<string, Json>();
  sessions = new Map<string, Session>();
  queues = new Map<string, Json>();
  tasks = new Map<string, Json>();
  taskAudience = new Map<string, Set<string>>();
  taskNext = new Map<string, string[]>();
  templates = new Map<string, Json>();
  chats = new Map<string, Json>();
  jobs = new Map<string, Json>();
  messages = new Map<string, Json[]>(); // chat_id -> OpenCode messages
  questions = new Map<string, Json[]>(); // chat_id -> OpenCode question requests
  permissions = new Map<string, Json[]>(); // chat_id -> OpenCode permission requests
  mcp = new Map<string, Json>();
  taskResults = new Map<string, string | null>();
  answers = new Map<string, string>(); // consultation job id -> answer text
  audit: Json[] = [];
  auditSeq = 0;
  idem = new Map<string, { fingerprint: string; body: Json }>();
  tombstones = new Map<string, string | null>(); // "tasks:<id>" -> deleted_by login
  updatedBy = new Map<string, string>(); // "tasks:<id>" -> login
  settings = {
    limit: D.CURRENT_LIMIT, consultation_model: D.DEFAULT_MODEL as string | null,
    consultation_queue: D.CONSULTATION_QUEUE, default_chat_queue: D.DEFAULT_CHAT_QUEUE,
  };
  stats: { id: string; run: StatRun }[] = [];
  listeners = new Set<(event: Json) => void>();
  timers = new Set<ReturnType<typeof setTimeout>>();
  snoozeTimers = new Map<string, ReturnType<typeof setTimeout>>();
  profile: Profile = { as: "none", folder: true, first: false, feed: "all" };
  scenario: Scenario = { delay: 0, delay_path: null, fail: null, fail_status: 503, fail_times: null };

  constructor(opts: MockOptions = {}) {
    this.speed = opts.speed && opts.speed > 0 ? opts.speed : 1;
    this.fixedNow = !!opts.fixedNow;
    this.quiet = !!opts.quiet;
    this.reset();
  }

  // ------------------------------------------------------------ time
  /** Mock clock: the real clock, or with --fixed-now the prototype "now" plus the real time since the last reset. */
  now() { return this.fixedNow ? D.PROTO_NOW + (Date.now() - this.epoch) : Date.now(); }
  nowIso() { return iso(this.now()); }
  at(protoMs: number) { return iso(protoMs + this.shift); }
  newId(prefix: string) { return `${prefix}-${(++this.idCounter).toString(36)}${Math.random().toString(36).slice(2, 7)}`; }

  after(ms: number, fn: () => void) {
    const t = setTimeout(() => { this.timers.delete(t); try { fn(); } catch (e) { console.error("[mock lifecycle]", e); } }, ms / this.speed);
    this.timers.add(t);
  }

  log(...args: unknown[]) { if (!this.quiet) console.log(...args); }

  /** Make seeded timestamps keep their age against the mock clock (a write turns them back into plain values). */
  slide(obj: Json, fields: string[]) {
    const anchor = D.PROTO_NOW + this.shift;
    for (const f of fields) {
      const value = obj[f];
      if (!value) continue;
      const age = anchor - parseIso(value);
      Object.defineProperty(obj, f, {
        enumerable: true, configurable: true,
        get: () => iso(this.now() - age),
        set: (v) => Object.defineProperty(obj, f, { value: v, writable: true, enumerable: true, configurable: true }),
      });
    }
  }
  /** Pin sliding timestamps at their current value. */
  freeze(obj: Json) {
    for (const [f, d] of Object.entries(Object.getOwnPropertyDescriptors(obj))) {
      if (d.get) Object.defineProperty(obj, f, { value: obj[f], writable: true, enumerable: true, configurable: true });
    }
  }

  // ------------------------------------------------------------ reset / profile
  reset(profile?: Partial<Profile>) {
    for (const t of this.timers) clearTimeout(t);
    for (const t of this.snoozeTimers.values()) clearTimeout(t);
    this.timers.clear();
    this.snoozeTimers.clear();
    this.epoch = Date.now();
    this.shift = this.fixedNow ? 0 : this.epoch - D.PROTO_NOW;
    for (const m of [this.users, this.queues, this.tasks, this.taskAudience, this.taskNext, this.templates,
      this.chats, this.jobs, this.messages, this.questions, this.permissions, this.mcp, this.taskResults,
      this.answers, this.idem, this.tombstones, this.updatedBy]) m.clear();
    this.sessions.clear();
    this.audit = [];
    this.auditSeq = 0;
    this.seq = 0;
    this.settings = {
      limit: D.CURRENT_LIMIT, consultation_model: D.DEFAULT_MODEL,
      consultation_queue: D.CONSULTATION_QUEUE, default_chat_queue: D.DEFAULT_CHAT_QUEUE,
    };
    this.seed();
    this.fitSeedLimit();
    this.profile = { as: "none", folder: true, first: false, feed: "all" };
    if (profile) this.applyProfile({ ...this.profile, ...profile });
  }

  /** Apply ?as / folder / first / feed. first and feed rebuild the seed. */
  applyProfile(next: Profile) {
    const cur = this.profile;
    if (next.first !== cur.first || next.feed !== cur.feed) {
      const keepSessions = new Map(this.sessions);
      this.reset();
      this.sessions = keepSessions;
      if (next.feed === "proto") for (const id of STATE_EXTRAS) this.dropEntity(id);
      if (next.first && next.as !== "none") {
        const uid = D.ACCOUNT[next.as];
        for (const chat of [...this.chats.values()]) if (chat.owner_user_id === uid) this.dropChat(chat.id);
      }
      this.fitSeedLimit();
    }
    const iso = this.users.get("u-isokolova");
    if (iso) iso.tasks_only = next.as === "tasks" ? 1 : 0;
    for (const seed of D.USERS) {
      const user = this.users.get(seed.id);
      if (user) user.workspace_path = seed.workspace ? D.ws(seed.workspace) : null;
    }
    if (!next.folder && next.as !== "none") {
      const user = this.users.get(D.ACCOUNT[next.as]);
      if (user) user.workspace_path = null;
    }
    this.profile = { ...next };
  }

  /**
   * The seeded override is the prototype's 4, raised only as far as the seed needs so that slot-holding jobs
   * never exceed it (feed=all holds 8 sessions at once; feed=proto holds 4). The audit row follows the value.
   */
  fitSeedLimit() {
    this.settings.limit = Math.min(D.HARD_LIMIT, Math.max(D.CURRENT_LIMIT, this.activeSessions()));
    const row = this.audit.find((a) => a.action === "concurrency.update");
    if (row) row.metadata_json = JSON.stringify({ value: this.settings.limit });
  }

  dropEntity(id: string) {
    const job = this.jobs.get(id);
    if (job?.task_id) return this.dropChat(job.chat_id);
    if (this.chats.has(id)) this.dropChat(id);
  }
  dropChat(chatId: string) {
    this.chats.delete(chatId);
    for (const [id, job] of this.jobs) if (job.chat_id === chatId) this.jobs.delete(id);
    this.messages.delete(chatId);
    this.questions.delete(chatId);
    this.permissions.delete(chatId);
  }

  // ------------------------------------------------------------ events
  publish(event: Json) { for (const l of this.listeners) l(event); }
  subscribe(fn: (event: Json) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }

  /** Port of app.py event_for_user + can_receive_event, plus the new chat.* events. */
  eventForUser(user: Json, event: Json): Json | null {
    const job = event.job || {};
    if (event.type === "job.updated" && job.chat_id) {
      const chat = this.chats.get(job.chat_id);
      if (chat && chat.consultation) {
        if (user.role !== "admin" && (chat.owner_user_id !== user.id || chat.workspace_snapshot !== user.workspace_path)) return null;
        return { type: "consultation.updated", job: pick(job, ["id", "chat_id", "status", "created_at", "finished_at"]) };
      }
    }
    if (CHAT_EVENTS.has(event.type)) return this.chatEventForUser(user, event);
    if (event.session_id && [...this.chats.values()].some((c) => c.consultation && c.opencode_session_id === event.session_id)) return null;
    if (!this.canReceive(user, event)) return null;
    if (event.type === "job.updated" && job.task_id) {
      const visible: Json = { type: "task.updated", task: this.publicTaskRun(job) };
      if (user.role === "admin") visible.owner_user_id = job.effective_user_id;
      return visible;
    }
    return event;
  }
  canReceive(user: Json, event: Json) {
    if (user.role === "admin") return true;
    if (event.type === "job.updated") {
      const job = event.job || {};
      return job.effective_user_id === user.id && (!user.tasks_only || !!job.task_id);
    }
    if (user.tasks_only) return false;
    if (!["opencode.event", "opencode.disconnected", "chat.questions.updated"].includes(event.type)) return false;
    if (event.workspace !== user.workspace_path) return false;
    if (event.session_id) {
      return [...this.chats.values()].some((c) => c.opencode_session_id === event.session_id && c.owner_user_id === user.id && c.workspace_snapshot === user.workspace_path);
    }
    const payload = event.event?.payload || {};
    return payload.type === "server.connected" || event.type === "opencode.disconnected";
  }

  /** Port of items.py chat_event_for_user: owner in the scope where they can open the chat, or an admin. */
  chatEventForUser(user: Json, event: Json): Json | null {
    const row = event.item || {};
    const admin = user.role === "admin";
    if (!admin) {
      const kinds = user.tasks_only ? ["question", "task"] : ["question", "task", "folder"];
      if (row.owner_user_id !== user.id || !kinds.includes(row.kind)) return null;
      if (row.kind !== "task" && user.workspace_path && row.workspace_snapshot !== user.workspace_path) {
        // Archived because the owner moved to another workspace: out of reach now, so it leaves the owner's list.
        if (event.type === "chat.updated" && row.archived_reason === "workspace_changed") return { type: "chat.deleted", chat_id: row.chat_id };
        return null;
      }
    }
    const { owner_user_id: _o, workspace_snapshot: _w, archived_reason: _r, ...item } = row;
    let visible: Json;
    if (event.type === "chat.deleted") visible = { type: "chat.deleted", chat_id: item.chat_id };
    else if (event.type === "task.title.updated") {
      visible = { type: "task.title.updated", run_id: item.id, chat_id: item.chat_id, title: item.title, task: { id: item.id, title: item.title } };
    } else visible = { type: "chat.updated", ...item };
    if (admin) visible.owner_user_id = row.owner_user_id;
    return visible;
  }

  emitJob(job: Json) { this.publish({ type: "job.updated", job: this.jobRow(job) }); }
  /** items.py chat_item_row: the public item plus the columns the fan-out filters on. */
  chatEventRow(chat: Json) {
    return { ...this.item(chat), owner_user_id: chat.owner_user_id, workspace_snapshot: chat.workspace_snapshot, archived_reason: chat.archived_reason ?? null };
  }
  /** items.py publish_chat_updated / publish_chat_deleted (+ task.title.updated for task runs). */
  emitChat(chat: Json, type: "chat.updated" | "chat.deleted" | "task.title.updated" = "chat.updated") {
    this.publish({ type, item: this.chatEventRow(chat) });
  }
  emitOpencode(chat: Json, type: string, properties: Json) {
    this.publish({
      type: "opencode.event", workspace: chat.workspace_snapshot, session_id: chat.opencode_session_id,
      event: { directory: chat.workspace_snapshot, payload: { type, properties } },
    });
  }

  // ------------------------------------------------------------ shapes
  publicUser(u: Json) {
    return {
      id: u.id, login: u.login, role: u.role, tasks_only: !!u.tasks_only, mattermost_username: u.mattermost_username,
      mattermost_user_id: u.mattermost_user_id, workspace_path: u.workspace_path, status: u.status,
      max_active_jobs: u.max_active_jobs, created_at: u.created_at, updated_at: u.updated_at, last_login_at: u.last_login_at,
    };
  }
  /** database.list_users row: raw columns (tasks_only stays 0/1), no password hash. */
  userRow(u: Json) { return { ...this.publicUser(u), tasks_only: u.tasks_only, auth_version: 1 }; }

  jobRow(job: Json) {
    const row: Json = pick(job, JOB_COLUMNS);
    const chat = this.chats.get(job.chat_id);
    const queue = this.queues.get(job.queue_id);
    row.effective_login = this.users.get(job.effective_user_id)?.login ?? null;
    row.actor_login = this.users.get(job.actor_user_id)?.login ?? null;
    row.chat_title = chat?.title ?? null;
    row.consultation = chat?.consultation ?? 0;
    row.queue_name = queue?.name ?? null;
    row.queue_priority = queue?.priority ?? null;
    return row;
  }
  displayTitle(job: Json) {
    const template = job.task_title;
    const title = this.chats.get(job.chat_id)?.title || template;
    if (title === template || title.startsWith(`${template}: `)) return title;
    return `${template}: ${title}`;
  }
  publicTaskRun(job: Json) {
    return {
      id: job.id, task_id: job.task_id, plan_first: !!job.plan_first,
      phase: job.plan_first ? (job.phase2_message_id ? "build" : "plan") : "build",
      // New contract (BRIEF §7): real waiting_* statuses, plus user_input / error_code / started_at.
      title: this.displayTitle(job), status: job.status, user_input: job.user_input ?? null, error_code: job.error_code ?? null,
      created_at: job.created_at, started_at: job.started_at, finished_at: job.finished_at,
    };
  }
  publicTask(t: Json) { return { id: t.id, title: t.title, description: t.description, plan_first: t.plan_first, input_hint: t.input_hint ?? null }; }
  /** `updated_by` column (a user id) from the last editor's login. */
  updatedById(collection: string, id: string) {
    const login = this.updatedBy.get(`${collection}:${id}`);
    return login ? [...this.users.values()].find((u) => u.login === login)?.id ?? null : null;
  }
  adminTask(t: Json) {
    return {
      ...t, updated_by: this.updatedById("tasks", t.id), model: `${t.provider_id}/${t.model_id}`,
      audience_user_ids: [...(this.taskAudience.get(t.id) ?? [])].sort(),
      next_task_ids: this.taskNext.get(t.id) ?? [],
    };
  }
  chatRow(chat: Json) { return pick(chat, CHAT_COLUMNS); }
  chatRowWithOwner(chat: Json) { return { ...this.chatRow(chat), owner_login: this.users.get(chat.owner_user_id)?.login ?? null }; }
  queueRow(q: Json) {
    const jobs = [...this.jobs.values()].filter((j) => j.queue_id === q.id);
    return { ...q, updated_by: this.updatedById("queues", q.id), active: this.activeSessions((j) => j.queue_id === q.id), queued: jobs.filter((j) => j.status === "queued").length };
  }
  /** COUNT(DISTINCT session_id) of slot-holding jobs, optionally filtered. */
  activeSessions(filter: (job: Json) => boolean = () => true) {
    return new Set([...this.jobs.values()].filter((j) => SLOT.has(j.status) && filter(j)).map((j) => j.session_id)).size;
  }

  chatJobs(chatId: string) { return [...this.jobs.values()].filter((j) => j.chat_id === chatId).sort((a, b) => a.sequence - b.sequence); }
  /** database.consultation_status: active first, then queued, then latest; no jobs = idle. */
  chatStatus(chatId: string) {
    const jobs = this.chatJobs(chatId);
    if (!jobs.length) return "idle";
    const rank = (s: string) => (ACTIVE.has(s) ? 0 : s === "queued" ? 1 : 2);
    return [...jobs].sort((a, b) => rank(a.status) - rank(b.status) || b.sequence - a.sequence)[0].status;
  }
  itemKind(chat: Json): "question" | "task" | "folder" {
    return chat.consultation ? "question" : chat.kind === "task" ? "task" : "folder";
  }
  item(chat: Json) {
    const kind = this.itemKind(chat);
    let id = chat.id, status = this.chatStatus(chat.id), phase: string | null = null, task_id: string | null = null;
    let title = chat.title;
    if (kind === "task") {
      const job = this.chatJobs(chat.id).find((j) => j.task_id);
      if (job) {
        const run = this.publicTaskRun(job);
        id = job.id; status = job.status; phase = run.phase; task_id = job.task_id; title = run.title;
      }
    } else if (kind === "folder" && !UNFINISHED.has(status)) {
      status = "idle"; // items.py item_status
    }
    return {
      kind, id, chat_id: chat.id, title, status, phase, task_id, created_at: chat.created_at,
      updated_at: chat.updated_at, archived_at: chat.archived_at ?? null, snoozed_until: chat.snoozed_until ?? null,
    };
  }
  consultationSummary(chat: Json, admin = false) {
    const result: Json = { id: chat.id, title: chat.title, created_at: chat.created_at, updated_at: chat.updated_at, status: this.chatStatus(chat.id) };
    if (admin) { result.owner_user_id = chat.owner_user_id; result.owner_login = this.users.get(chat.owner_user_id)?.login ?? null; }
    return result;
  }
  consultationDetail(chat: Json, admin = false) {
    const result = this.consultationSummary(chat, admin);
    const jobs = this.chatJobs(chat.id);
    const messages: Json[] = [];
    for (const job of jobs) {
      messages.push({ id: job.id, role: "user", text: job.content, status: job.status });
      const answer = this.answers.get(job.id);
      if (job.status === "completed" && answer) messages.push({ id: `${job.id}-answer`, role: "assistant", text: answer });
    }
    result.messages = messages;
    if (admin) {
      result.jobs = jobs.map((j) => ({
        id: j.id, chat_id: j.chat_id, status: j.status, created_at: j.created_at, finished_at: j.finished_at,
        model: `${j.provider_id}/${j.model_id}`, agent: j.agent, queue: this.queues.get(j.queue_id)?.name ?? j.queue_id,
      }));
    }
    return result;
  }
  /** Jobs ahead in the same queue + 1 (seeded demo jobs may carry a fixed bias). */
  position(job: Json) {
    if (job.status !== "queued") return null;
    const ahead = [...this.jobs.values()].filter((j) => j.queue_id === job.queue_id && j.status === "queued" && j.sequence < job.sequence).length;
    return ahead + (job._ahead ?? 0) + 1;
  }

  // ------------------------------------------------------------ audit
  addAudit(actor: string | null, action: string, o: { effective?: string | null; type?: string | null; id?: string | null; meta?: Json; at?: string } = {}) {
    const row = {
      id: ++this.auditSeq, actor_user_id: actor, effective_user_id: o.effective ?? null, action,
      object_type: o.type ?? null, object_id: o.id ?? null, metadata_json: JSON.stringify(o.meta ?? {}),
      created_at: o.at ?? this.nowIso(),
    };
    this.audit.push(row);
    return row;
  }
  auditRow(row: Json) {
    return { ...row, actor_login: this.users.get(row.actor_user_id)?.login ?? null, effective_login: this.users.get(row.effective_user_id)?.login ?? null };
  }

  // ------------------------------------------------------------ OpenCode message builders
  userMessage(chat: Json, id: string, text: string, created: string, agent = "build", model = D.DEFAULT_MODEL) {
    const [providerID, modelID] = model.split("/");
    return {
      info: { id, sessionID: chat.opencode_session_id, role: "user", time: { created: parseIso(created) }, agent, model: { providerID, modelID } },
      parts: [{ id: `${id}-p1`, sessionID: chat.opencode_session_id, messageID: id, type: "text", text }],
    };
  }
  assistantMessage(chat: Json, id: string, parentID: string, created: string, agent: string, completed: string | null, parts: Json[], model = D.DEFAULT_MODEL) {
    const [providerID, modelID] = model.split("/");
    const sid = chat.opencode_session_id;
    const info: Json = {
      id, sessionID: sid, role: "assistant", parentID, time: { created: parseIso(created), ...(completed ? { completed: parseIso(completed) } : {}) },
      modelID, providerID, mode: agent, agent, path: { cwd: chat.workspace_snapshot, root: chat.workspace_snapshot },
      cost: 0.0123, tokens: { input: 18234, output: 912, reasoning: 0, cache: { read: 12000, write: 0 } },
    };
    if (completed) info.finish = "stop";
    const all = [{ type: "step-start", snapshot: "4b825dc6" }, ...parts, ...(completed ? [{ type: "step-finish", reason: "stop", cost: 0.0123, tokens: info.tokens }] : [])];
    return { info, parts: all.map((p, i) => ({ id: `${id}-p${i + 1}`, sessionID: sid, messageID: id, ...p })) };
  }
  toolPart(tool: string, input: Json, status: "completed" | "running", title: string, output = "", metadata: Json = {}, start = 0, end = 0) {
    return {
      type: "tool", callID: `call_${tool}_${(++this.idCounter).toString(36)}`, tool,
      state: { status, input, ...(status === "completed" ? { output, title, metadata, time: { start, end } } : { title, metadata, time: { start } }) },
    };
  }
  readParts(t: number, status: "completed" | "running" = "completed") {
    const files = ["conanfile.py", "CMakeLists.txt", "profiles/armv8-gcc12", "profiles/x86_64-gcc13", "test_package/conanfile.py", ".gitlab-ci.yml"];
    return [
      ...files.map((f) => this.toolPart("read", { filePath: f }, status, f, "", {}, t, t + 400)),
      this.toolPart("grep", { pattern: "conans" }, status, "conans", "", { matches: 6 }, t, t + 300),
      this.toolPart("grep", { pattern: "conan_basic_setup" }, status, "conan_basic_setup", "", { matches: 3 }, t, t + 300),
    ];
  }
  editParts(t: number) {
    const e = (filePath: string, additions: number, deletions: number) =>
      this.toolPart("edit", { filePath }, "completed", filePath, "", { filediff: { file: filePath, additions, deletions } }, t, t + 800);
    return [e("conanfile.py", 48, 31), e("profiles/armv8-gcc12", 7, 5), e("profiles/x86_64-gcc13", 7, 5), e(".gitlab-ci.yml", 6, 4)];
  }
  bashPart(t: number) {
    return this.toolPart("bash", { command: "conan create . -pr:h=profiles/armv8-gcc12", description: D.PERMISSION_WHY }, "completed",
      "conan create . -pr:h=profiles/armv8-gcc12", "net-utils/2.0.0: Package created\n37 tests passed", { exit: 0 }, t, t + 134000);
  }
  /** Synthetic session for task runs (admin /task-runs/:id/messages). */
  runMessages(job: Json) {
    const chat = this.chats.get(job.chat_id);
    if (!chat || job.status === "queued") return [];
    const prompt = job.user_input ? `${job.content}\n\n${job.user_input}` : job.content;
    const model = `${job.provider_id}/${job.model_id}`;
    const msgs: Json[] = [this.userMessage(chat, `msg_${job.id}_u`, prompt, job.started_at ?? job.created_at, job.agent, model)];
    if (job.status === "dispatching") return msgs;
    const done = TERMINAL.has(job.status) ? job.finished_at : null;
    const text = this.taskResults.get(job.id);
    msgs.push(this.assistantMessage(chat, `msg_${job.id}_a`, `msg_${job.id}_u`, job.started_at ?? job.created_at, job.agent, done,
      text && job.status === "completed" ? [{ type: "text", text }] : [], model));
    return msgs;
  }

  // ------------------------------------------------------------ creation helpers
  addChat(o: { id: string; owner: string; title: string; kind?: "chat" | "task"; consultation?: 0 | 1; created: string; updated?: string;
    archived_at?: string | null; snoozed_until?: string | null; agent?: string | null; model?: string | null; workspace?: string | null }) {
    const user = this.users.get(o.owner)!;
    const [pp, pm] = o.model ? o.model.split("/") : [null, null];
    const chat = {
      id: o.id, owner_user_id: o.owner, opencode_session_id: `ses_${o.id.replace(/[^a-zA-Z0-9]/g, "")}`, title: o.title,
      workspace_snapshot: o.workspace ?? user.workspace_path, status: "idle", kind: o.kind ?? "chat", archived_reason: null,
      preferred_agent: o.agent ?? null, preferred_provider_id: pp, preferred_model_id: pm, preferred_model_variant: null,
      created_at: o.created, updated_at: o.updated ?? o.created, consultation: o.consultation ?? 0,
      archived_at: o.archived_at ?? null, snoozed_until: o.snoozed_until ?? null,
    };
    this.chats.set(chat.id, chat);
    if (chat.snoozed_until) this.armSnooze(chat);
    return chat;
  }
  addJob(chat: Json, o: { id?: string; content: string; status: string; created: string; started?: string | null; finished?: string | null;
    agent?: string; model?: string; queue_id?: string; task?: Json; user_input?: string | null; error_code?: string | null;
    actor?: string; plan_first?: 0 | 1; phase2?: boolean; ahead?: number }) {
    const model = o.model ?? (o.task ? `${o.task.provider_id}/${o.task.model_id}` : D.DEFAULT_MODEL);
    const [provider_id, model_id] = model.split("/");
    const id = o.id ?? this.newId("job");
    const job: Json = {
      id, sequence: ++this.seq, chat_id: chat.id, session_id: chat.opencode_session_id, effective_user_id: chat.owner_user_id,
      actor_user_id: o.actor ?? chat.owner_user_id, workspace_snapshot: chat.workspace_snapshot, message_id: `msg_${id}`,
      content: o.content, agent: o.agent ?? "build", provider_id, model_id, model_variant: null,
      queue_id: o.queue_id ?? (o.task ? o.task.queue_id : chat.consultation ? this.settings.consultation_queue : this.settings.default_chat_queue),
      task_id: o.task?.id ?? null, task_title: o.task?.title ?? null, task_initial_title: o.task ? chat.title : null,
      user_input: o.user_input ?? (o.task ? "" : null), status: o.status, created_at: o.created, started_at: o.started ?? null,
      finished_at: o.finished ?? null, error_code: o.error_code ?? null, pipeline_json: null, parent_job_id: null, session_pending: 0,
      plan_first: o.plan_first ?? (o.task?.plan_first ? 1 : 0), phase2_message_id: o.phase2 ? `msg_${id}_phase2` : null,
      resume_content: null, resume_from_message_id: null, resume_monitor: 0,
    };
    if (o.ahead) job._ahead = o.ahead;
    this.jobs.set(id, job);
    chat.status = UNFINISHED.has(o.status) ? o.status : "idle";
    return job;
  }

  // ------------------------------------------------------------ seed
  seed() {
    const at = (ms: number) => this.at(ms);
    const T = (d: number, hm: string) => at(D.protoTime(d, hm));
    const DT = (m: number, d: number, hm = "10:00") => at(D.protoDate(m, d, hm));

    for (const s of D.USERS) {
      const updated = s.id === "u-isokolova" ? D.protoTime(0, "12:10") : s.id === "u-segorova" ? D.protoDate(10, 19, "17:05") : s.created_at;
      this.users.set(s.id, {
        id: s.id, login: s.login, password: D.DEMO_PASSWORD, role: s.role, tasks_only: s.tasks_only,
        mattermost_username: s.mattermost_username, mattermost_user_id: s.mattermost_username ? `mm${s.id.replace(/\W/g, "")}` : null,
        workspace_path: s.workspace ? D.ws(s.workspace) : null, status: s.status, max_active_jobs: s.max_active_jobs,
        created_at: at(s.created_at), updated_at: at(updated), last_login_at: s.last_login_at ? at(s.last_login_at) : null,
      });
    }
    for (const q of D.QUEUES) {
      const updated = q.id === "questions" ? D.protoTime(0, "11:30") : q.created_at;
      this.queues.set(q.id, { id: q.id, name: q.name, priority: q.priority, max_workers: q.max_workers, status: "active", is_system: q.is_system, created_at: at(q.created_at), updated_at: at(updated) });
    }
    for (const t of D.TASKS) {
      const [provider_id, model_id] = t.model.split("/");
      this.tasks.set(t.id, {
        id: t.id, title: t.title, description: t.description, content: t.content, agent: "build", provider_id, model_id,
        model_variant: null, queue_id: t.queue_id, is_published: t.is_published, visibility: t.visibility,
        created_by_admin_id: "u-dorlov", created_at: at(t.created_at), updated_at: at(t.updated_at), deleted_at: null, plan_first: t.plan_first,
        input_hint: t.hint ?? null,
      });
      this.taskAudience.set(t.id, new Set(t.audience));
      this.taskNext.set(t.id, [...t.next]);
      this.updatedBy.set(`tasks:${t.id}`, "d.orlov");
    }
    this.tombstones.set("tasks:t-old-notes", "d.orlov");
    D.TEMPLATES.forEach((tpl, i) => this.templates.set(tpl.id, {
      id: tpl.id, title: tpl.title, description: null, content: tpl.content, is_published: 1, created_by_admin_id: "u-dorlov",
      created_at: DT(9, 10 + i), updated_at: DT(9, 10 + i),
    }));

    const task = (id: string) => this.tasks.get(id)!;
    const IS = "u-isokolova";

    // ---- questions (consultations) of i.sokolova
    const question = (id: string, title: string, created: string, status: string, o: { answer?: string; finished?: string; started?: string; archived?: string; snoozed?: string; error?: string; ahead?: number; content?: string; owner?: string } = {}) => {
      const chat = this.addChat({ id, owner: o.owner ?? IS, title, consultation: 1, created, updated: o.finished ?? o.started ?? created, archived_at: o.archived, snoozed_until: o.snoozed });
      const job = this.addJob(chat, { id: `j-${id}`, content: o.content ?? title, status, created, started: o.started ?? (status === "queued" ? null : created), finished: o.finished, error_code: o.error, ahead: o.ahead });
      if (o.answer) this.answers.set(job.id, o.answer);
      return { chat, job };
    };
    const clar = question("q-clarify", "Где в коде обрабатывается таймаут OTA-обновления?", T(0, "14:12"), "waiting_question", { content: D.QUESTION_TEXT, started: T(0, "14:12") });
    clar.chat.updated_at = T(0, "14:13");
    this.questions.set("q-clarify", [{ id: "que_q_clarify", sessionID: clar.chat.opencode_session_id, questions: [D.CLARIFY] }]);
    question("q-answer", "Таймаут OTA-обновления в коде", T(0, "13:40"), "completed", { content: D.QUESTION_TEXT, finished: T(0, "13:43"), answer: D.ANSWER });
    question("q-running", "Таймаут OTA-обновления в коде", T(0, "14:27"), "running", { content: D.QUESTION_TEXT, started: T(0, "14:27") });
    question("q-queued", D.QUESTION_TEXT, T(0, "14:29"), "queued", { ahead: 2 });
    question("q-failed", D.QUESTION_TEXT, T(0, "14:20"), "failed", { finished: T(0, "14:21"), error: "opencode_unavailable" });
    question("q-gcc13", "Почему nightly-сборка firmware-sdk падает только на gcc 13?", T(0, "10:40"), "completed", { finished: T(0, "10:42"), answer: D.R.gcc13, snoozed: T(2, "10:00") });
    question("q-ota-logs", "Как включить отладочные логи в ota-agent?", DT(10, 20, "11:00"), "completed", { finished: DT(10, 20, "11:01"), answer: D.R.otaLogs, archived: DT(10, 21, "09:00") });
    question("q-armv8", "Какой тулчейн нужен для сборки под armv8?", DT(9, 15, "12:00"), "completed", { finished: DT(9, 15, "12:01"), answer: D.R.armv8, archived: DT(9, 16, "09:00") });

    // ---- task runs
    const run = (id: string, owner: string, taskId: string, title: string, created: string, status: string, o: { started?: string; finished?: string; input?: string; result?: string; error?: string; archived?: string; ahead?: number; phase2?: boolean } = {}) => {
      const t = task(taskId);
      const chat = this.addChat({ id: `c-${id}`, owner, title, kind: "task", created, updated: o.finished ?? o.started ?? created, archived_at: o.archived });
      const job = this.addJob(chat, { id, content: t.content, status, created, started: o.started ?? null, finished: o.finished ?? null, task: t, user_input: o.input ?? "", error_code: o.error, ahead: o.ahead, phase2: o.phase2 });
      if (status === "completed") this.taskResults.set(id, o.result ?? null);
      return job;
    };
    run("r-queued", IS, "t-ci", "Разбор упавшего CI-пайплайна", T(0, "14:15"), "queued", { input: D.CI_URL, ahead: 2 });
    run("r-running", IS, "t-mr", "Ревью merge request: !1932 — Поддержка TLS 1.3 в HTTP-клиенте", T(0, "13:58"), "running", { started: T(0, "13:59"), input: D.MR_URL });
    run("r-done", IS, "t-ci", "Разбор упавшего CI-пайплайна: Pipeline #48211 — ошибка линковки libcrypto на armv8", T(0, "10:50"), "completed", { started: T(0, "10:56"), finished: T(0, "11:05"), input: D.CI_URL, result: D.CI_REPORT });
    run("r-failed", IS, "t-notes", "Release notes по задачам версии 4.12", T(-1, "18:02"), "failed", { started: T(-1, "18:03"), finished: T(-1, "18:20"), input: "4.12", error: "assistant_error" });
    run("r-cancelled", IS, "t-dup", "Поиск дубликатов задачи в YouTrack", DT(10, 22, "15:00"), "cancelled", { started: DT(10, 22, "15:01"), finished: DT(10, 22, "15:04") });
    run("r-mr1904", IS, "t-mr", "Ревью merge request: !1904 — Таймауты в MQTT-клиенте", DT(10, 19, "11:00"), "completed", { started: DT(10, 19, "11:01"), finished: DT(10, 19, "11:14"), input: "https://gitlab.example.com/fw/firmware-sdk/-/merge_requests/1904", result: D.R.mr1904, archived: DT(10, 20, "09:00") });
    run("r-ci46102", IS, "t-ci", "Разбор упавшего CI-пайплайна: Pipeline #46102 — флаки-тест в ota-agent", DT(10, 2, "11:00"), "completed", { started: DT(10, 2, "11:01"), finished: DT(10, 2, "11:09"), input: "https://gitlab.example.com/fw/firmware-sdk/-/pipelines/46102", result: D.R.ci46102, archived: DT(10, 3, "09:00") });
    run("r-plan", IS, "t-conan", "Обновление зависимости Conan во всех пакетах прошивки с проверкой сборки на трёх тулчейнах: openssl/3.3.2", T(0, "14:24"), "running", { started: T(0, "14:25"), input: "openssl/3.3.2" });

    // ---- folder chats
    const folder = (id: string, stage: "planning" | "plan" | "running" | "permission" | "done" | "denied", created: number, owner = IS) => {
      const c = (min: number) => at(created + min * 60000);
      const chat = this.addChat({ id, owner, title: "Миграция net-utils на Conan 2", created: c(0), updated: c(stage === "planning" ? 1 : 13), agent: stage === "planning" || stage === "plan" ? "plan" : "build", model: D.DEFAULT_MODEL });
      const ms = (min: number) => parseIso(c(min));
      const u1 = `msg_${id}_u1`, a1 = `msg_${id}_a1`, u2 = `msg_${id}_u2`, a2 = `msg_${id}_a2`;
      const msgs: Json[] = [this.userMessage(chat, u1, D.FOLDER_PROMPT, c(0), "plan")];
      if (stage === "planning") {
        this.addJob(chat, { id: `j-${id}-1`, content: D.FOLDER_PROMPT, agent: "plan", status: "running", created: c(0), started: c(0) });
        msgs.push(this.assistantMessage(chat, a1, u1, c(0), "plan", null, this.readParts(ms(0), "running")));
        this.messages.set(id, msgs);
        return chat;
      }
      this.addJob(chat, { id: `j-${id}-1`, content: D.FOLDER_PROMPT, agent: "plan", status: "completed", created: c(0), started: c(0), finished: c(3) });
      msgs.push(this.assistantMessage(chat, a1, u1, c(0), "plan", c(3), [...this.readParts(ms(1)), { type: "text", text: D.PLAN }]));
      if (stage === "plan") { this.messages.set(id, msgs); return chat; }
      msgs.push(this.userMessage(chat, u2, D.FOLDER_BUILD_PROMPT, c(6), "build"));
      const final = stage === "done" || stage === "denied";
      const status = stage === "running" ? "running" : stage === "permission" ? "waiting_permission" : "completed";
      this.addJob(chat, { id: `j-${id}-2`, content: D.FOLDER_BUILD_PROMPT, agent: "build", status, created: c(6), started: c(6), finished: final ? c(13) : null });
      const parts: Json[] = this.editParts(ms(7));
      if (stage === "done") parts.push(this.bashPart(ms(9)), { type: "text", text: D.DONE_OK });
      if (stage === "denied") parts.push({ type: "text", text: D.DONE_DENIED });
      msgs.push(this.assistantMessage(chat, a2, u2, c(6), "build", final ? c(13) : null, parts));
      this.messages.set(id, msgs);
      if (stage === "permission") {
        this.permissions.set(id, [{
          id: `per_${id.replace(/\W/g, "")}`, sessionID: chat.opencode_session_id, permission: "bash",
          patterns: [D.PERMISSION_COMMAND], always: ["conan create *"],
          metadata: { command: D.PERMISSION_COMMAND, description: D.PERMISSION_WHY }, tool: { messageID: a2, callID: "call_bash_perm" },
        }]);
      }
      return chat;
    };
    folder("f-permission", "permission", D.protoTime(0, "13:52"));
    folder("f-planning", "planning", D.protoTime(0, "14:26"));
    folder("f-plan", "plan", D.protoTime(0, "14:00"));
    folder("f-running", "running", D.protoTime(0, "13:30"));
    folder("f-done", "done", D.protoTime(0, "13:00"));
    folder("f-denied", "denied", D.protoTime(0, "12:30"));
    const simpleFolder = (id: string, owner: string, title: string, created: string, finished: string | null, archived?: string) => {
      const chat = this.addChat({ id, owner, title, created, updated: finished ?? created, archived_at: archived, agent: "build", model: D.DEFAULT_MODEL });
      const status = finished ? "completed" : "running";
      this.addJob(chat, { id: `j-${id}`, content: title, status, created, started: created, finished });
      this.messages.set(id, [
        this.userMessage(chat, `msg_${id}_u1`, title, created),
        this.assistantMessage(chat, `msg_${id}_a1`, `msg_${id}_u1`, created, "build", finished,
          finished ? [this.toolPart("read", { filePath: "README.md" }, "completed", "README.md", "", {}, parseIso(created), parseIso(created) + 400), { type: "text", text: "Готово." }]
            : [this.toolPart("read", { filePath: "README.md" }, "running", "README.md", "", {}, parseIso(created))]),
      ]);
      return chat;
    };
    simpleFolder("f-config", IS, "Рефакторинг парсера конфигурации", T(-1, "15:20"), T(-1, "16:02"));
    simpleFolder("f-conan-profiles", IS, "Обновление профилей Conan для gcc 13", DT(10, 17, "10:00"), DT(10, 17, "10:40"), DT(10, 18, "09:00"));

    // ---- other users (admin requests, admin queue)
    question("adm-q", "Таймаут OTA-обновления в коде", T(0, "09:20"), "completed", { owner: "u-dorlov", content: D.QUESTION_TEXT, finished: T(0, "09:22"), answer: D.ANSWER });
    run("adm-r", "u-dorlov", "t-ci", "Разбор упавшего CI-пайплайна: Pipeline #48211 — ошибка линковки libcrypto на armv8", T(0, "08:40"), "completed", { started: T(0, "08:41"), finished: T(0, "08:49"), input: D.CI_URL, result: D.CI_REPORT });
    folder("adm-f", "done", D.protoTime(-1, "12:00"), "u-dorlov");
    run("r-yt-2410", "u-youtrack", "t-yt", "Ответ на упоминание @Valeria в YouTrack: FW-2410", T(0, "14:29"), "dispatching", { started: T(0, "14:29") });
    const nik = question("q-nikitina-debug", "Как собрать прошивку с отладочными символами?", T(0, "14:03"), "waiting_question", { owner: "u-anikitina", started: T(0, "14:03") });
    this.questions.set(nik.chat.id, [{ id: "que_nikitina", sessionID: nik.chat.opencode_session_id, questions: [{ question: D.R.clarNikitina, multiple: false, custom: true, options: [{ label: "armv8", description: "" }, { label: "x86_64", description: "" }] }] }]);
    run("r-nikitina-dup", "u-anikitina", "t-dup", "Поиск дубликатов задачи в YouTrack: FW-2388", DT(10, 21, "10:00"), "completed", { started: DT(10, 21, "10:01"), finished: DT(10, 21, "10:05"), input: "FW-2388", result: D.R.dup2388 });
    simpleFolder("f-volkov-hal", "u-pvolkov", "Рефакторинг HAL для STM32H7", T(0, "12:48"), null);
    question("q-volkov-hal", "Чем отличается HAL_UART_Transmit_DMA от HAL_UART_Transmit_IT на STM32H7 при высокой нагрузке?", DT(10, 22, "11:00"), "completed", { owner: "u-pvolkov", finished: DT(10, 22, "11:03"), answer: D.R.hal });
    question("q-belov-412", "Какие задачи YouTrack вошли в версию 4.12, но не закрыты?", T(-1, "15:10"), "completed", { owner: "u-mbelov", finished: T(-1, "15:12"), answer: D.R.belov412 });
    run("r-belov-notes", "u-mbelov", "t-notes", "Release notes по задачам версии", T(0, "14:29"), "queued", { input: "4.12" });
    simpleFolder("f-egorova-secureboot", "u-segorova", "Подпись образа загрузчика в secure boot", DT(10, 3, "15:00"), DT(10, 3, "16:00"));

    for (const chat of this.chats.values()) if (!this.mcp.has(chat.id) && !chat.consultation && chat.kind === "chat") this.mcp.set(chat.id, { servers: D.MCP_SERVERS });

    // Unfinished seeded work keeps its age against the mock clock, so "running for N min" stays N min
    // however long the server has been up (and with --fixed-now it continues from the prototype "now").
    for (const job of this.jobs.values()) {
      if (!UNFINISHED.has(job.status)) continue;
      this.slide(job, ["created_at", "started_at"]);
      const chat = this.chats.get(job.chat_id);
      if (!chat) continue;
      this.slide(chat, this.chatJobs(chat.id).every((j) => UNFINISHED.has(j.status)) ? ["created_at", "updated_at"] : ["updated_at"]);
    }

    // ---- audit (prototype AUDIT), oldest first so ids grow with time
    for (const a of [...D.AUDIT].reverse()) this.addAudit(a.who, a.action, { effective: a.as, type: a.type, id: a.obj, meta: a.meta, at: at(a.t) });

    // ---- statistics runs
    this.stats = generateStats().map((r, i) => ({ id: `s-${String(i + 1).padStart(5, "0")}`, run: r }));
  }

  // ------------------------------------------------------------ snooze
  armSnooze(chat: Json) {
    const old = this.snoozeTimers.get(chat.id);
    if (old) clearTimeout(old);
    this.snoozeTimers.delete(chat.id);
    if (!chat.snoozed_until) return;
    const ms = parseIso(chat.snoozed_until) - this.now();
    if (ms > 2 ** 31 - 1) return;
    const t = setTimeout(() => {
      this.snoozeTimers.delete(chat.id);
      const current = this.chats.get(chat.id);
      if (!current || current.snoozed_until !== chat.snoozed_until) return;
      current.snoozed_until = null;
      current.updated_at = this.nowIso();
      this.emitChat(current);
    }, Math.max(0, ms));
    this.snoozeTimers.set(chat.id, t);
  }

  // ------------------------------------------------------------ lifecycle
  setStatus(job: Json, status: string, extra: Json = {}) {
    if (!this.jobs.has(job.id)) return;
    const now = this.nowIso();
    const prev = job.status;
    job.status = status;
    if (!job.started_at && ACTIVE.has(status)) job.started_at = now;
    if (TERMINAL.has(status)) { job.finished_at = now; this.freeze(job); }
    Object.assign(job, extra);
    const chat = this.chats.get(job.chat_id);
    let chatChanged = false;
    if (chat) {
      chat.status = UNFINISHED.has(status) ? status : "idle";
      chat.updated_at = now;
      if (TERMINAL.has(status)) this.freeze(chat);
      // dispatcher.py: a new question / permission ends a snooze (any kind) → chat.updated
      if ((status === "waiting_question" || status === "waiting_permission") && chat.snoozed_until) {
        chat.snoozed_until = null;
        this.armSnooze(chat);
        chatChanged = true;
      }
      // dispatcher.py: a folder chat also gets chat.updated on its run/wait transitions and when the job ends.
      if (this.itemKind(chat) === "folder" && prev !== status
        && (TERMINAL.has(status) ? !TERMINAL.has(prev) : ["running", "waiting_question", "waiting_permission"].includes(status))) chatChanged = true;
    }
    this.emitJob(job);
    if (chat && chatChanged) this.emitChat(chat);
  }

  // -- dispatcher slots (database.claim_next_job): global limit, queue max_workers, user max_active_jobs
  slotBlocker(job: Json): "global" | "queue" | "user" | null {
    if (this.activeSessions() >= this.settings.limit) return "global";
    const queue = this.queues.get(job.queue_id);
    if (queue?.max_workers != null && this.activeSessions((j) => j.queue_id === job.queue_id) >= queue.max_workers) return "queue";
    const user = this.users.get(job.effective_user_id);
    if (user?.max_active_jobs != null && this.activeSessions((j) => j.effective_user_id === job.effective_user_id) >= user.max_active_jobs) return "user";
    return null;
  }
  /**
   * Take a slot for a queued job. Seeded demo jobs are frozen and would block forever, so when they are what
   * stands in the way, the oldest one finishes first (state extras before prototype fixtures), as a real
   * session would. Live jobs are never finished early; then the job waits and retries.
   */
  claimSlot(job: Json) {
    for (let guard = 0; guard < 16; guard++) {
      const blocker = this.slotBlocker(job);
      if (!blocker) return true;
      const extra = (j: Json) => (STATE_EXTRAS.includes(j.id) || STATE_EXTRAS.includes(j.chat_id) ? 0 : 1);
      const victim = [...this.jobs.values()]
        .filter((j) => !j._live && !j._played && (j.status === "running" || j.status === "dispatching")
          && (blocker === "global" || (blocker === "queue" ? j.queue_id === job.queue_id : j.effective_user_id === job.effective_user_id)))
        .sort((a, b) => extra(a) - extra(b) || parseIso(a.started_at ?? a.created_at) - parseIso(b.started_at ?? b.created_at))[0];
      if (!victim) return false;
      this.finishSeeded(victim);
    }
    return false;
  }
  /** queued → dispatching after `ms`, but only through a free slot; otherwise retry. */
  dispatchAfter(job: Json, ms: number, start: () => void) {
    this.step(job, ["queued"], ms, () => {
      if (!this.claimSlot(job)) { this.dispatchAfter(job, 500, start); return; }
      start();
    });
  }
  taskResultText(job: Json) {
    const key = String(job.task_id).replace(/^t-/, "");
    const text: Record<string, string> = { ci: D.CI_REPORT, mr: D.R.mr1904, dup: D.R.dup2388, notes: D.R.belov412, yt: D.R.otaLogs, conan: D.DONE_OK };
    return text[key] ?? D.CI_REPORT;
  }
  /** Complete a frozen seeded job the way its lifecycle would. */
  finishSeeded(job: Json) {
    const chat = this.chats.get(job.chat_id);
    if (job.task_id) { this.taskResults.set(job.id, this.taskResultText(job)); this.setStatus(job, "completed"); }
    else if (chat?.consultation) { this.answers.set(job.id, D.ANSWER); this.setStatus(job, "completed"); }
    else this.folderHooks(job).done(job.agent === "plan" ? D.PLAN : D.DONE_OK);
  }
  /** A step runs only while the job is still in the expected status (stop/delete/reset win). */
  step(job: Json, expect: string[], ms: number, fn: () => void) {
    this.after(ms, () => { if (this.jobs.get(job.id) === job && expect.includes(job.status)) fn(); });
  }

  // -- consultations: queued → dispatching → running → (waiting_question →) completed / failed
  playConsultation(job: Json, opts: { clarify: boolean; fail: boolean }) {
    const chat = this.chats.get(job.chat_id)!;
    const run = () => {
      this.step(job, ["running"], 2200, () => {
        if (opts.fail) return this.setStatus(job, "failed", { error_code: "opencode_unavailable" });
        if (opts.clarify) {
          this.questions.set(chat.id, [{ id: this.newId("que"), sessionID: chat.opencode_session_id, questions: [D.CLARIFY] }]);
          this.setStatus(job, "waiting_question");
          this.publish({ type: "chat.questions.updated", workspace: chat.workspace_snapshot, session_id: chat.opencode_session_id });
          return;
        }
        this.answers.set(job.id, D.ANSWER);
        this.setStatus(job, "completed");
      });
    };
    if (job.status === "queued") {
      this.dispatchAfter(job, 1000, () => { this.setStatus(job, "dispatching"); this.step(job, ["dispatching"], 800, () => { this.setStatus(job, "running"); run(); }); });
    } else if (job.status === "dispatching") {
      this.step(job, ["dispatching"], 800, () => { this.setStatus(job, "running"); run(); });
    } else if (job.status === "running") run();
  }
  resumeConsultation(job: Json) {
    this.setStatus(job, "queued");
    this.dispatchAfter(job, 600, () => {
      this.setStatus(job, "running");
      this.step(job, ["running"], 2000, () => { this.answers.set(job.id, D.ANSWER); this.setStatus(job, "completed"); });
    });
  }

  // -- task runs: queued → dispatching → running [plan → build] → completed / failed
  playTask(job: Json) {
    const input = String(job.user_input ?? "");
    const fail = input.includes("#fail");
    const waitPermission = input.includes("#permission");
    const waitQuestion = input.includes("#question");
    const finish = () => {
      if (fail) return this.setStatus(job, "failed", { error_code: "assistant_error" });
      this.taskResults.set(job.id, this.taskResultText(job));
      this.setStatus(job, "completed");
    };
    const work = () => {
      if (job.plan_first && !job.phase2_message_id) {
        this.step(job, ["running"], 2500, () => {
          job.phase2_message_id = `msg_${job.id}_phase2`;
          this.setStatus(job, "running");
          this.step(job, ["running"], 2500, finish);
        });
        return;
      }
      if (waitPermission || waitQuestion) {
        this.step(job, ["running"], 1500, () => {
          this.setStatus(job, waitPermission ? "waiting_permission" : "waiting_question");
          this.step(job, ["waiting_permission", "waiting_question"], 2500, () => { this.setStatus(job, "running"); this.step(job, ["running"], 1500, finish); });
        });
        return;
      }
      this.step(job, ["running"], 3000, finish);
    };
    const start = () => {
      this.setStatus(job, "running");
      const chat = this.chats.get(job.chat_id);
      // OpenCode summarises the session title once the run starts (backend task-run title sync).
      const clean = input.replace(/#\w+/g, "").split(/\s+/).filter(Boolean).join(" ");
      if (chat && clean) {
        chat.title = clean.slice(0, 80);
        chat.updated_at = this.nowIso();
        this.emitChat(chat);
        this.emitChat(chat, "task.title.updated");
      }
      work();
    };
    if (job.status === "queued") this.dispatchAfter(job, 2000, () => { this.setStatus(job, "dispatching"); this.step(job, ["dispatching"], 1000, start); });
    else if (job.status === "dispatching") this.step(job, ["dispatching"], 1000, start);
    else if (job.status === "running") work();
  }

  // -- folder chats: plan agent → plan text; build agent → edits → permission → bash → done
  playFolder(job: Json) {
    const chat = this.chats.get(job.chat_id)!;
    const msgs = this.messages.get(chat.id) ?? [];
    this.messages.set(chat.id, msgs);
    const userId = `msg_${job.id}_u`, asstId = `msg_${job.id}_a`;
    const text = String(job.content);
    const model = `${job.provider_id}/${job.model_id}`;
    const pushAssistant = (parts: Json[], completed: boolean) => {
      const idx = msgs.findIndex((m) => m.info.id === asstId);
      const msg = this.assistantMessage(chat, asstId, userId, job.started_at ?? this.nowIso(), job.agent, completed ? this.nowIso() : null, parts, model);
      if (idx >= 0) msgs[idx] = msg; else msgs.push(msg);
      this.emitOpencode(chat, "message.updated", { info: msg.info });
      for (const part of msg.parts) this.emitOpencode(chat, "message.part.updated", { part });
    };
    const done = (finalText: string, extra: Json[] = []) => {
      const prev = msgs.find((m) => m.info.id === asstId)?.parts.filter((p: Json) => p.type === "tool") ?? [];
      pushAssistant([...prev.map(({ id: _i, sessionID: _s, messageID: _m, ...p }: Json) => p), ...extra, { type: "text", text: finalText }], true);
      this.setStatus(job, "completed");
      this.emitOpencode(chat, "session.idle", { sessionID: chat.opencode_session_id });
    };
    const begin = () => {
      this.setStatus(job, "running");
      const user = this.userMessage(chat, userId, text, this.nowIso(), job.agent, model);
      if (!msgs.some((m) => m.info.id === userId)) msgs.push(user);
      this.emitOpencode(chat, "message.updated", { info: user.info });
      this.emitOpencode(chat, "session.status", { sessionID: chat.opencode_session_id, status: { type: "busy" } });
      if (text.includes("#fail")) return this.step(job, ["running"], 1500, () => this.setStatus(job, "failed", { error_code: "assistant_error" }));
      if (text.includes("#question")) {
        return this.step(job, ["running"], 1500, () => {
          this.questions.set(chat.id, [{ id: this.newId("que"), sessionID: chat.opencode_session_id, questions: [{ ...D.CLARIFY }] }]);
          this.setStatus(job, "waiting_question");
          this.publish({ type: "chat.questions.updated", workspace: chat.workspace_snapshot, session_id: chat.opencode_session_id });
        });
      }
      this.continueFolder(job);
    };
    (job as Json)._folder = { pushAssistant, done };
    if (job.status === "queued") this.dispatchAfter(job, 800, () => { this.setStatus(job, "dispatching"); this.step(job, ["dispatching"], 700, begin); });
    else if (job.status === "dispatching") this.step(job, ["dispatching"], 700, begin);
    else if (job.status === "running") this.continueFolder(job);
  }
  continueFolder(job: Json) {
    const f = this.folderHooks(job);
    const t = this.now();
    if (job.agent === "plan") {
      f.pushAssistant(this.readParts(t, "running"), false);
      this.step(job, ["running"], 2200, () => f.done(D.PLAN, this.readParts(t)));
      return;
    }
    this.step(job, ["running"], 1100, () => {
      f.pushAssistant(this.editParts(t), false);
      this.step(job, ["running"], 1100, () => {
        const chat = this.chats.get(job.chat_id)!;
        const perm = {
          id: this.newId("per"), sessionID: chat.opencode_session_id, permission: "bash", patterns: [D.PERMISSION_COMMAND],
          always: ["conan create *"], metadata: { command: D.PERMISSION_COMMAND, description: D.PERMISSION_WHY },
          tool: { messageID: `msg_${job.id}_a`, callID: "call_bash_perm" },
        };
        this.permissions.set(chat.id, [perm]);
        this.setStatus(job, "waiting_permission");
        this.emitOpencode(chat, "permission.asked", perm);
      });
    });
  }
  /** Hooks for jobs that were seeded (no playFolder closure yet). */
  folderHooks(job: Json): { pushAssistant: (parts: Json[], completed: boolean) => void; done: (text: string, extra?: Json[]) => void } {
    if (job._folder) return job._folder;
    const chat = this.chats.get(job.chat_id)!;
    const msgs = this.messages.get(chat.id) ?? [];
    this.messages.set(chat.id, msgs);
    const last = [...msgs].reverse().find((m) => m.info.role === "assistant" && !m.info.time.completed);
    const asstId = last?.info.id ?? `msg_${job.id}_a`;
    const parentId = last?.info.parentID ?? [...msgs].reverse().find((m) => m.info.role === "user")?.info.id ?? null;
    const strip = ({ id: _i, sessionID: _s, messageID: _m, ...p }: Json) => p;
    const pushAssistant = (parts: Json[], completed: boolean) => {
      const prev = msgs.find((m) => m.info.id === asstId);
      const created = prev ? iso(prev.info.time.created) : this.nowIso();
      const msg = this.assistantMessage(chat, asstId, parentId, created, job.agent, completed ? this.nowIso() : null, parts);
      const idx = msgs.findIndex((m) => m.info.id === asstId);
      if (idx >= 0) msgs[idx] = msg; else msgs.push(msg);
      this.emitOpencode(chat, "message.updated", { info: msg.info });
      for (const part of msg.parts) this.emitOpencode(chat, "message.part.updated", { part });
    };
    const done = (text: string, extra: Json[] = []) => {
      const prev = (msgs.find((m) => m.info.id === asstId)?.parts ?? []).filter((p: Json) => p.type === "tool").map(strip);
      pushAssistant([...prev, ...extra, { type: "text", text }], true);
      this.setStatus(job, "completed");
      this.emitOpencode(chat, "session.idle", { sessionID: chat.opencode_session_id });
    };
    job._folder = { pushAssistant, done };
    return job._folder;
  }
  afterPermission(job: Json, response: string) {
    const f = this.folderHooks(job);
    this.setStatus(job, "running");
    if (response === "reject") {
      this.step(job, ["running"], 800, () => f.done(D.DONE_DENIED));
      return;
    }
    this.step(job, ["running"], 2200, () => f.done(D.DONE_OK, [this.bashPart(this.now())]));
  }
  afterFolderQuestion(job: Json) {
    this.setStatus(job, "running");
    this.continueFolder(job);
  }

  /** Stop/cancel any unfinished job (queued → cancelled now; active → cancelling → cancelled). */
  stopJob(job: Json) {
    if (job.status === "queued") { this.setStatus(job, "cancelled"); return; }
    if (!ACTIVE.has(job.status) || job.status === "cancelling") return;
    this.setStatus(job, "cancelling");
    this.permissions.delete(job.chat_id);
    this.questions.delete(job.chat_id);
    this.step(job, ["cancelling"], 1000, () => this.setStatus(job, "cancelled"));
  }

  /** __mock/play: run the remaining lifecycle of a seeded (frozen) entity. */
  play(id: string) {
    const job = this.jobs.get(id) ?? [...this.jobs.values()].filter((j) => j.chat_id === id).sort((a, b) => b.sequence - a.sequence)[0];
    if (!job || !UNFINISHED.has(job.status)) return null;
    job._played = true; // never finished early to free a slot
    const chat = this.chats.get(job.chat_id)!;
    if (job.task_id) this.playTask(job);
    else if (chat.consultation) {
      if (job.status === "waiting_question") return job;
      this.playConsultation(job, { clarify: false, fail: false });
    } else if (job.status !== "waiting_permission" && job.status !== "waiting_question") this.playFolder(job);
    return job;
  }

  // ------------------------------------------------------------ stats helpers
  statRows() {
    return this.stats.map(({ id, run }) => {
      const taskId = STAT_TASK_ID[run.task];
      // Unfinished runs keep their age against the mock clock; finished ones stay on the reset-time calendar.
      const created = statTime(run.t) + (run.st === "queued" || run.st === "running" ? this.now() - D.PROTO_NOW : this.shift);
      const started = run.st === "queued" ? null : created + run.wait * 60000;
      const finished = run.st === "queued" || run.st === "running" ? null : (started ?? created) + run.work * 60000;
      const outcome = run.st === "done" ? "completed" : run.st === "failed" ? "failed" : run.st === "cancelled" ? "cancelled" : null;
      return {
        id, kind: run.task === "q" ? "question" : "task", task_id: taskId, task_title: taskId ? this.tasks.get(taskId)?.title ?? null : null,
        created_at: iso(created), started_at: started === null ? null : iso(started), finished_at: finished === null ? null : iso(finished), outcome,
        login: run.who,
      };
    });
  }
}

// ------------------------------------------------------------ search (rules/search-behavior.md)
export function normalizeForSearch(value: string) {
  return value.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLocaleLowerCase();
}
export function searchTokens(query: string | null | undefined): string[] {
  const q = (query ?? "").trim();
  if (!q) return [];
  const tokens: string[] = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(q))) {
    const token = m[1] ?? m[2];
    if (token) tokens.push(normalizeForSearch(token));
  }
  return tokens;
}
export function matchesTokens(tokens: string[], fields: (string | null | undefined)[]) {
  if (!tokens.length) return true;
  const norm = fields.filter(Boolean).map((f) => normalizeForSearch(String(f)));
  return tokens.every((t) => norm.some((f) => f.includes(t)));
}
