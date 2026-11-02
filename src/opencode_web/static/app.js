import {consultationUI} from "./consultations.js?v=7";
import {questionPanel} from "./questions.js?v=1";
import {renderMarkdown} from "./markdown.js?v=1";

const app = document.querySelector("#app");
const toast = document.querySelector("#toast");

const state = {
  me: null,
  csrf: null,
  route: "dialogs",
  selectedChat: null,
  selectedTemplate: null,
  draftTemplate: null,
  options: null,
  adminContext: false,
  eventSource: null,
  refreshTimer: null,
  questions: null,
  waitingQuestion: false,
  eventLog: [],
  templateDirty: false,
  titleSyncs: new Set(),
  titleSyncStartedAt: new Map(),
  adminChatOwner: null,
  adminTaskOwner: null,
  adminTaskAccessOwner: null,
  selectedAdminUser: null,
  taskRunOrigin: null,
  selectedTask: null,
  selectedAdminTask: null,
  taskDrafts: new Map(),
  taskAdminDirty: false,
  stoppingJobs: new Set(),
  statisticsDay: null,
  statisticsRequest: 0,
};

const TITLE_SYNC_INTERVAL_MS = 5000;

const esc = (value = "") => String(value).replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[char]);

const consultations = consultationUI({api, shell, bindShell, state, app, esc, notify, navigate});

function notify(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { toast.hidden = true; }, 3500);
}

// Keep the same request key after an uncertain network failure; share in-flight clicks.
const pendingSubmissions = new Map();
async function api(path, options = {}) {
  const queued = options.method === "POST" && (
    path === "/api/chats" || path === "/api/consultations" ||
    /^\/api\/tasks\/[^/]+\/runs$/.test(path) ||
    /^\/api\/(?:admin\/)?(?:chats|consultations)\/[^/]+\/messages$/.test(path)
  );
  if (!queued) return apiRequest(path, options);
  const identity = JSON.stringify([state.me?.id, path, options.body]);
  let pending = pendingSubmissions.get(identity);
  if (pending?.promise) return pending.promise;
  if (!pending) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    pending = {key: Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("")};
    pendingSubmissions.set(identity, pending);
  }
  const headers = new Headers(options.headers || {});
  headers.set("idempotency-key", pending.key);
  pending.promise = apiRequest(path, {...options, headers}).then(result => {
    pendingSubmissions.delete(identity);
    return result;
  }).catch(error => {
    pending.promise = null;
    if (error.status && error.status < 500) pendingSubmissions.delete(identity);
    throw error;
  });
  return pending.promise;
}

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (state.csrf && !["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
    headers.set("x-csrf-token", state.csrf);
  }
  const response = await fetch(path, {...options, headers});
  if (response.status === 401 && path !== "/api/auth/login") {
    state.me = null; state.csrf = null; renderLogin();
    throw new Error("Сессия истекла");
  }
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* empty */ }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function renderLogin(error = "") {
  state.selectedTask = null;
  state.selectedAdminTask = null;
  state.taskDrafts.clear();
  state.taskAdminDirty = false;
  app.innerHTML = `
    <section class="login-page">
      <form class="login-card" id="login-form">
        <div class="brand"><span class="brand-mark">⌘</span> V.A.L.E.R.I.A.</div>
        <h1>Вход в систему</h1>
        <div class="field"><label for="login">Логин</label><input class="input" id="login" name="login" autocomplete="username" required autofocus></div>
        <div class="field"><label for="password">Пароль</label><input class="input" id="password" name="password" type="password" autocomplete="current-password" required></div>
        <div class="error" id="login-error">${esc(error)}</div>
        <button class="btn btn-primary btn-block" type="submit">Войти</button>
        <p class="hint">Доступ предоставляется администратором</p>
      </form>
    </section>`;
  document.querySelector("#login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      const result = await api("/api/auth/login", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget)))});
      state.me = result.user; state.csrf = result.csrf_token; state.options = null; startEvents(); await renderLocation();
    } catch (err) {
      renderLogin(err.message);
    }
  });
}

function navButton(route, label) {
  return `<button data-route="${route}" class="${state.route === route ? "active" : ""}">${label}</button>`;
}

function defaultRoute() {
  return state.me?.tasks_only ? "tasks" : "dialogs";
}

const routeNames = new Set(["dialogs", "consultations", "templates", "tasks", "profile", "statistics", "admin-consultations", "admin-tasks", "task-access", "users", "queue", "audit"]);

function locationTarget() {
  const raw = location.hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean).map(part => {
    try { return decodeURIComponent(part); } catch (_) { return ""; }
  });
  if (parts[0] === "consultations" && parts[1]) return {route: "consultation", id: parts[1]};
  if (parts[0] === "admin-consultations" && parts[1]) return {route: "admin-consultation", id: parts[1]};
  if (parts[0] === "tasks" && parts[1] === "runs" && parts[2]) return {route: "tasks", runId: parts[2]};
  return {route: routeNames.has(parts[0]) ? parts[0] : defaultRoute()};
}

async function navigate(path, replace = false) {
  const hash = `#/${path.replace(/^\/+/, "")}`;
  if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
  await renderLocation();
}

async function renderLocation() {
  if (!state.me) return;
  const target = locationTarget();
  if (!location.hash) history.replaceState(null, "", `#/${target.route}`);
  if (target.route === "consultation") return consultations.show(target.id, false);
  if (target.route === "admin-consultation" && state.me.role === "admin") return consultations.show(target.id, true);
  if (target.route === "tasks" && target.runId) {
    state.route = "tasks";
    return renderTasks(state.selectedTask, target.runId);
  }
  return renderShell(target.route);
}

function shell(content, title = "V.A.L.E.R.I.A.") {
  const admin = state.me.role === "admin";
  return `<div class="shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">⌘</span> V.A.L.E.R.I.A. </div>
      <nav class="nav">
        ${state.me.tasks_only ? "" : navButton("dialogs", "▣ Диалоги")}
        ${navButton("consultations", "◌ Консультации")}
        ${state.me.tasks_only ? "" : navButton("templates", "▱ Шаблоны")}
        ${navButton("tasks", "✓ Таски")}
        ${admin ? navButton("statistics", "▥ Статистика") : ""}
        ${admin ? `<div class="hint" style="padding:12px 12px 3px">АДМИНИСТРИРОВАНИЕ</div>${navButton("admin-consultations", "◌ Консультации")}${navButton("admin-tasks", "⚙ Настройка тасок")}${navButton("task-access", "⊞ Доступ к таскам")}${navButton("users", "♙ Пользователи")}${navButton("queue", "≡ Очередь")}${navButton("audit", "⌁ Аудит")}` : ""}
      </nav>
      <div class="sidebar-user">
        <div class="user-row"><span class="avatar">${esc(state.me.login[0]?.toUpperCase())}</span><div><strong>${esc(state.me.login)}</strong><div class="meta">${esc(state.me.role)}</div></div></div>
        <button class="btn btn-ghost" data-route="profile">Профиль</button>
        <button class="btn btn-ghost" id="logout">Выйти</button>
      </div>
    </aside>
    <section class="content">
      <header class="topbar"><button class="btn mobile-menu" aria-label="Открыть меню" aria-expanded="false">☰</button><h1 data-shell-title>${esc(title)}</h1><span class="spacer"></span><span class="badge ${admin ? "amber" : "green"}">${admin ? "Администратор" : state.me.tasks_only ? "Таски и консультации" : "Пользователь"}</span></header>
      ${content}
    </section>
  </div>`;
}

function bindShell() {
  document.querySelectorAll("[data-route]").forEach(button => button.addEventListener("click", () => {
    if (state.route === "templates" && state.templateDirty && !confirm("Отменить несохранённые изменения шаблона?")) return;
    if (state.route === "admin-tasks" && state.taskAdminDirty && !confirm("Отменить несохранённые изменения таски?")) return;
    state.templateDirty = false;
    state.taskAdminDirty = false;
    void navigate(`/${button.dataset.route}`).catch(err => notify(err.message));
  }));
  document.querySelector(".mobile-menu")?.addEventListener("click", event => {
    const expanded = document.querySelector(".shell").classList.toggle("menu-open");
    event.currentTarget.setAttribute("aria-expanded", String(expanded));
  });
  document.querySelector("#logout")?.addEventListener("click", async () => {
    try { await api("/api/auth/logout", {method: "POST"}); } finally {
      state.eventSource?.close(); state.eventSource = null;
      state.me = null; state.csrf = null; state.options = null; renderLogin();
    }
  });
}

async function renderShell(route) {
  if (state.me.tasks_only && !["tasks", "consultations", "profile"].includes(route)) route = "tasks";
  state.route = route;
  try {
    if (route === "dialogs") {
      app.innerHTML = shell(`<div class="page"><div class="empty">Загружаем диалоги…</div></div>`, "Диалоги");
      bindShell();
      return await renderDialogs();
    }
    if (route === "consultations") return await consultations.list();
    if (route === "admin-consultations" && state.me.role === "admin") return await consultations.list(true);
    if (route === "templates") return await renderTemplates();
    if (route === "tasks") return await renderTasks();
    if (route === "profile") return await renderProfile();
    if (route === "statistics" && state.me.role === "admin") return await renderStatistics();
    if (route === "admin-tasks" && state.me.role === "admin") return await renderAdminTasks();
    if (route === "task-access" && state.me.role === "admin") return await renderTaskAccess();
    if (route === "users" && state.me.role === "admin") return await renderUsers();
    if (route === "queue" && state.me.role === "admin") return await renderQueue();
    if (route === "audit" && state.me.role === "admin") return await renderAudit();
    return await renderDialogs();
  } catch (err) {
    app.innerHTML = shell(`<div class="page"><div class="empty">${esc(err.message)}</div></div>`);
    bindShell();
  }
}

async function renderDialogs() {
  const [chats, templates, config] = await Promise.all([
    api("/api/chats"), api("/api/templates"), api("/api/config/public")
  ]);
  const initialOptions = state.options || {
    agents: config.agents,
    default_agent: config.default_agent,
    default_model: config.default_model,
    models: [],
  };
  const models = flattenModels(initialOptions.models);
  const modelsLoading = !state.options;
  const content = `<div class="page">
    <div class="page-head"><h2>Диалоги</h2><span class="spacer"></span><span class="badge green">Лимит: ${config.max_active_sessions}</span></div>
    <div class="grid">
      <section><div class="toolbar"><input class="input" id="chat-search" placeholder="Поиск диалогов..."></div>
        <div class="list" id="chat-list">${chats.length ? chats.map(chat => `<button class="list-item" data-chat="${chat.id}"><div class="list-item-head"><strong data-chat-title="${esc(chat.id)}">${esc(chat.title)}</strong>${jobBadge(chat.status)}</div><div class="meta mono">${esc(chat.workspace_snapshot)}</div></button>`).join("") : `<div class="empty">Диалогов пока нет</div>`}</div>
      </section>
      <form class="card" id="new-chat"><h3>Новый диалог</h3>
        <div class="field"><label>Рабочая папка</label><input class="input mono" value="${esc(config.workspace || "Не назначена")}" disabled></div>
        ${!config.workspace && state.me.role === "admin" ? `<button class="btn" type="button" data-route="profile">Назначить рабочую папку</button>` : ""}
        <div class="selectors"><div class="field" style="flex:1"><label>Режим</label><select class="input" name="agent" id="new-chat-agent">${initialOptions.agents.map(a => `<option value="${esc(a)}" ${a === initialOptions.default_agent ? "selected" : ""}>${esc(a[0].toUpperCase() + a.slice(1))}</option>`).join("")}</select></div>
        <div class="field" style="flex:2"><label>Модель</label><select class="input" name="model" id="new-chat-model" ${modelsLoading ? "disabled" : ""}>${modelsLoading ? `<option value="">Модели OpenCode загружаются…</option>` : modelOptions(models, initialOptions.default_model)}</select><span class="hint" id="model-load-status">${modelsLoading ? "Интерфейс уже доступен; каталог загрузится отдельно." : `${models.length} моделей доступно`}</span></div></div>
        <div class="field"><label>Шаблон сообщения</label><select class="input" id="template"><option value="">Без шаблона</option>${templates.map(t => `<option value="${t.id}" ${state.draftTemplate?.id === t.id ? "selected" : ""}>${esc(t.title)}</option>`).join("")}</select></div>
        <div class="field"><label>Первая задача</label><textarea class="input" name="message" placeholder="Опишите задачу агенту...">${esc(state.draftTemplate?.content || "")}</textarea><span class="hint">Текст шаблона скопирован в черновик и доступен для изменения.</span></div>
        <button class="btn btn-primary btn-block" type="submit" ${config.workspace ? "" : "disabled"}>Создать диалог</button>
      </form>
    </div></div>`;
  app.innerHTML = shell(content, "Диалоги"); bindShell();
  document.querySelectorAll("[data-chat]").forEach(el => el.addEventListener("click", () => renderChat(el.dataset.chat)));
  document.querySelector("#template").addEventListener("change", event => {
    const template = templates.find(item => item.id === event.target.value);
    state.draftTemplate = template || null;
    document.querySelector("#new-chat textarea").value = template?.content || "";
  });
  document.querySelector("#new-chat").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    const values = Object.fromEntries(new FormData(form));
    try {
      const result = await api("/api/chats", {method: "POST", body: JSON.stringify(values)});
      state.draftTemplate = null;
      notify(result.job ? "Диалог создан, сообщение поставлено в очередь" : "Диалог создан");
      await renderChat(result.chat.id);
    } catch (err) { notify(err.message); }
    finally { button.disabled = false; }
  });
  void hydrateChatTitles(
    "/api/chats/title-sync",
    () => state.route === "dialogs"
  );
  void hydrateDialogOptions();
}

function patchChatTitles(updates) {
  const titles = new Map(updates.map(item => [item.id, item.title]));
  document.querySelectorAll("[data-chat-title]").forEach(target => {
    const title = titles.get(target.dataset.chatTitle);
    if (title && target.textContent !== title) target.textContent = title;
  });
}

async function hydrateChatTitles(path, isCurrentView, patchTitles = patchChatTitles) {
  const now = Date.now();
  const lastStartedAt = state.titleSyncStartedAt.get(path) || 0;
  if (state.titleSyncs.has(path) || now - lastStartedAt < TITLE_SYNC_INTERVAL_MS) return;
  state.titleSyncs.add(path);
  state.titleSyncStartedAt.set(path, now);
  try {
    const updates = await api(path, {method: "POST"});
    if (isCurrentView() && Array.isArray(updates)) patchTitles(updates);
  } catch (_) {
    // The cached list remains usable while OpenCode is starting or unavailable.
  } finally {
    state.titleSyncs.delete(path);
  }
}

async function hydrateDialogOptions() {
  const modelSelect = document.querySelector("#new-chat-model");
  const agentSelect = document.querySelector("#new-chat-agent");
  const status = document.querySelector("#model-load-status");
  if (!modelSelect || !agentSelect || !status) return;
  try {
    const options = await api("/api/options");
    if (state.route !== "dialogs" || !modelSelect.isConnected) return;
    state.options = options;
    const models = flattenModels(options.models);
    agentSelect.innerHTML = options.agents.map(a => `<option value="${esc(a)}" ${a === options.default_agent ? "selected" : ""}>${esc(a[0].toUpperCase() + a.slice(1))}</option>`).join("");
    modelSelect.innerHTML = modelOptions(models, options.default_model);
    modelSelect.disabled = false;
    const providers = new Set(models.map(item => item.provider).filter(Boolean));
    status.textContent = options.opencode_unavailable
      ? "OpenCode сейчас недоступен; будет использована модель по умолчанию."
      : `Доступно моделей: ${models.length} · провайдеров: ${providers.size}`;
  } catch (err) {
    if (modelSelect.isConnected) {
      modelSelect.innerHTML = `<option value="">Модель OpenCode по умолчанию</option>`;
      modelSelect.disabled = false;
      status.textContent = `Не удалось загрузить модели: ${err.message}`;
    }
  }
}

function flattenModels(payload) {
  if (Array.isArray(payload) && payload.every(item => item && item.id && !item.models)) return payload;
  const source = payload?.all || payload?.providers || payload || [];
  const result = [];
  const providers = Array.isArray(source) ? source : Object.values(source);
  for (const provider of providers) {
    const providerId = provider.id || provider.providerID || provider.name;
    const models = provider.models || {};
    for (const [id, model] of Object.entries(models)) result.push({id: `${providerId}/${id}`, name: model.name || id});
  }
  return result;
}

function modelOptions(models, selected) {
  const values = [...models];
  if (selected && !values.some(item => item.id === selected)) values.unshift({id: selected, name: selected});
  if (!values.length) return `<option value="">Модель OpenCode по умолчанию</option>`;
  return `<option value="">По умолчанию</option>${values.map(item => `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""}>${esc(item.name)} · ${esc(item.id)}</option>`).join("")}`;
}

function jobBadge(status) {
  const style = ["running"].includes(status) ? "green" : ["queued", "dispatching", "waiting_permission", "waiting_question", "cancelling"].includes(status) ? "amber" : status === "failed" ? "red" : "";
  const labels = {running:"Работает", queued:"В очереди", dispatching:"Подготовка окружения", waiting_permission:"Ожидает разрешения", waiting_question:"Ожидает ответа пользователя", cancelling:"Останавливается…", cancelled:"Отменено", completed:"Завершено", failed:"Ошибка", idle:"Завершён"};
  return `<span class="badge ${style}">${esc(labels[status] || status)}</span>`;
}

function messageText(item) {
  const parts = item.parts || [];
  return parts.filter(p => p.type === "text").map(p => p.text).join("\n") || JSON.stringify(parts, null, 2);
}

function renderPart(part, role) {
  const runtime = `<details class="runtime-card"><summary>Runtime-поля</summary><pre>${esc(JSON.stringify(part, null, 2))}</pre></details>`;
  if (part.type === "text") {
    const text = role === "assistant" ? renderMarkdown(part.text) : esc(part.text || "");
    return `<div class="message-text ${role === "assistant" ? "markdown-body" : ""}">${text}</div>${runtime}`;
  }
  if (part.type === "reasoning") return `<details class="reasoning-card" open><summary>Reasoning</summary><pre>${esc(part.text || JSON.stringify(part, null, 2))}</pre></details>${runtime}`;
  if (part.type === "tool") {
    const toolState = part.state || {};
    return `<details class="tool-card" ${toolState.status === "running" ? "open" : ""}><summary>${esc(part.tool || "Инструмент")} · ${esc(toolState.status || "")}</summary><pre>${esc(JSON.stringify(part, null, 2))}</pre></details>`;
  }
  return `<details class="tool-card"><summary>${esc(part.type || "Часть сообщения")}</summary><pre>${esc(JSON.stringify(part, null, 2))}</pre></details>`;
}

function renderMessages(messages) {
  if (!messages.length) return `<div class="empty">История пока пуста</div>`;
  return messages.map(item => {
    const role = item.info?.role || item.role || "assistant";
    const parts = (item.parts || []).map(part => renderPart(part, role)).join("");
    const metadata = `<details class="runtime-card"><summary>Метаданные сообщения</summary><pre>${esc(JSON.stringify(item.info || item, null, 2))}</pre></details>`;
    const fallback = role === "assistant"
      ? `<div class="message-text markdown-body">${renderMarkdown(messageText(item))}</div>`
      : esc(messageText(item));
    return `<article class="message ${role === "user" ? "user" : "assistant"}">${parts || fallback}${item.submission_status ? jobBadge(item.submission_status) : ""}${metadata}</article>`;
  }).join("");
}

function isNearMessageBottom(target) {
  return target.scrollHeight - target.scrollTop - target.clientHeight <= 96;
}

function updateScrollToBottomButton() {
  const target = document.querySelector("#message-list");
  const button = document.querySelector("#scroll-to-bottom");
  if (!target || !button) return;
  button.hidden = isNearMessageBottom(target);
}

function scrollMessagesToBottom(behavior = "smooth") {
  const target = document.querySelector("#message-list");
  if (!target) return;
  if (behavior === "smooth") target.scrollTo({top: target.scrollHeight, behavior});
  else target.scrollTop = target.scrollHeight;
  updateScrollToBottomButton();
}

function bindMessageScroller() {
  const target = document.querySelector("#message-list");
  const button = document.querySelector("#scroll-to-bottom");
  if (!target || !button) return;
  target.addEventListener("scroll", updateScrollToBottomButton, {passive: true});
  button.addEventListener("click", () => scrollMessagesToBottom());
  updateScrollToBottomButton();
}

function renderPermissions(permissions) {
  return permissions.map(item => {
    const patterns = (item.patterns || []).join("\n");
    return `<section class="permission-card"><strong>Требуется разрешение: ${esc(item.permission || "действие")}</strong><pre>${esc(patterns || JSON.stringify(item.metadata || {}, null, 2))}</pre><div class="actions"><button class="btn btn-primary" data-permission="${esc(item.id)}" data-response="once">Разрешить один раз</button>${item.always?.length ? `<button class="btn" data-permission="${esc(item.id)}" data-response="always">Всегда разрешать</button>` : ""}<button class="btn btn-danger" data-permission="${esc(item.id)}" data-response="reject">Отклонить</button></div></section>`;
  }).join("");
}

function bindPermissionActions(chatId) {
  document.querySelectorAll("[data-permission]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/chats/${chatId}/permissions/${encodeURIComponent(button.dataset.permission)}`, {method:"POST", body:JSON.stringify({response:button.dataset.response, remember:button.dataset.response === "always"})});
      await refreshChatLive(chatId);
    } catch (err) { button.disabled = false; notify(err.message); }
  }));
}

async function hydrateChatMcp(chatId) {
  const target = document.querySelector("#chat-mcp");
  if (!target) return;
  try {
    const result = await api(`/api/chats/${chatId}/mcp`);
    target.innerHTML = result.pending ? `<span class="hint">Окружение ещё не запущено</span>` : result.servers.length ? result.servers.map(server => `<div class="mcp-row"><span>${esc(server.name)}</span><span class="badge ${server.status === "connected" ? "green" : "amber"}">${esc(server.status)}</span></div>`).join("") : `<span class="hint">MCP не подключены</span>`;
  } catch (err) { target.innerHTML = `<span class="error">${esc(err.message)}</span>`; }
}

function chatStatusText(status) {
  return ({queued: "В очереди", dispatching: "Подготовка окружения"})[status] || status || "";
}

function updateChatStatus() {
  const waiting = state.waitingQuestion;
  document.querySelector("#chat-status")?.replaceChildren(document.createTextNode(waiting ? "Ожидает вашего ответа" : chatStatusText(state.selectedChat?.status)));
  const badge = document.querySelector("#question-waiting");
  if (badge) badge.hidden = !waiting;
}

async function refreshChatLive(chatId) {
  if (state.route !== "chat" || state.selectedChat?.id !== chatId) return;
  try {
    const [chat, messages, jobs, permissions] = await Promise.all([
      api(`/api/chats/${chatId}`), api(`/api/chats/${chatId}/messages`),
      state.adminContext ? api("/api/admin/queue").then(x => x.jobs.filter(j => j.chat_id === chatId)) : api("/api/queue/me").then(x => x.filter(j => j.chat_id === chatId)),
      api(`/api/chats/${chatId}/permissions`)
    ]);
    if (state.route !== "chat" || state.selectedChat?.id !== chatId) return;
    state.selectedChat = chat;
    const messagesTarget = document.querySelector("#message-list");
    const permissionsTarget = document.querySelector("#permission-list");
    const followLatestMessage = messagesTarget ? isNearMessageBottom(messagesTarget) : true;
    if (messagesTarget) {
      messagesTarget.innerHTML = renderMessages(messages);
      if (followLatestMessage) scrollMessagesToBottom("auto");
      else updateScrollToBottomButton();
    }
    if (permissionsTarget) permissionsTarget.innerHTML = renderPermissions(permissions);
    document.querySelector("[data-shell-title]")?.replaceChildren(document.createTextNode(chat.title));
    updateChatStatus();
    void state.questions?.refresh();
    const latestTarget = document.querySelector("#latest-job-status");
    if (latestTarget) latestTarget.innerHTML = jobs[0] ? jobBadge(jobs[0].status) : "";
    bindPermissionActions(chatId);
  } catch (err) { notify(err.message); }
}

async function renderChat(chatId, adminContext = false) {
  state.route = "chat";
  state.questions = null;
  state.waitingQuestion = false;
  state.adminContext = adminContext;
  if (state.selectedChat?.id !== chatId) state.eventLog = [];
  const [chat, messages, jobs, options, permissions] = await Promise.all([
    api(`/api/chats/${chatId}`), api(`/api/chats/${chatId}/messages`),
    adminContext ? api("/api/admin/queue").then(x => x.jobs.filter(j => j.chat_id === chatId)) : api("/api/queue/me").then(x => x.filter(j => j.chat_id === chatId)),
    api(`/api/options?chat_id=${encodeURIComponent(chatId)}`), api(`/api/chats/${chatId}/permissions`)
  ]);
  state.selectedChat = chat;
  const models = flattenModels(options.models);
  const selectedAgent = options.selected_agent || options.default_agent;
  const selectedModel = options.selected_model || options.default_model;
  const latest = jobs[0];
  const content = `<div class="chat-layout"><section class="chat-main">
    ${adminContext ? `<div class="card" style="margin:12px;border-color:#815f1d;background:#2a210f">Вы просматриваете диалог пользователя. Сообщение будет считаться отправленным владельцем диалога.</div>` : ""}
    <div class="message-scroll"><div class="messages" id="message-list">${renderMessages(messages)}</div><button class="scroll-to-bottom" id="scroll-to-bottom" type="button" aria-label="Прокрутить диалог до конца" title="Прокрутить до конца" hidden>↓</button></div><div id="permission-list">${renderPermissions(permissions)}</div><div id="question-list" aria-label="Вопросы OpenCode"></div>
    <form class="composer" id="composer"><div class="selectors"><select class="input" name="agent">${options.agents.map(a => `<option value="${esc(a)}" ${a === selectedAgent ? "selected" : ""}>${esc(a)}</option>`).join("")}</select><select class="input" name="model">${modelOptions(models, selectedModel)}</select><span class="badge amber" id="question-waiting" hidden>Ожидает вашего ответа</span><span id="latest-job-status">${latest ? jobBadge(latest.status) : ""}</span></div><div class="composer-row"><textarea class="input" name="message" placeholder="${adminContext ? "Сообщение от имени пользователя..." : "Сообщение агенту..."}" required></textarea><button class="btn btn-primary" type="submit">➤</button></div></form>
  </section><aside class="chat-side"><section><h3>Состояние</h3><span class="badge" id="chat-status">${esc(chatStatusText(chat.status))}</span></section><section><h3>Рабочая папка</h3><div class="mono meta">${esc(chat.workspace_snapshot)}</div></section><section><h3>Очередь этого диалога</h3><div>${jobs.length}</div></section><section><h3>Подключённые MCP</h3><div id="chat-mcp"><span class="hint">Загрузка…</span></div></section><section><h3>Поток событий OpenCode</h3><div id="event-log" class="event-log">${renderEventLog()}</div></section></aside></div>`;
  app.innerHTML = shell(content, chat.title); bindShell();
  bindMessageScroller();
  scrollMessagesToBottom("auto");
  bindPermissionActions(chatId);
  hydrateChatMcp(chatId);
  const questionTarget = document.querySelector("#question-list");
  state.questions = questionPanel(questionTarget, {
    fetchQuestions: () => api(`/api/chats/${chatId}/questions`),
    reply: (id, answers) => api(`/api/chats/${chatId}/questions/${encodeURIComponent(id)}/reply`, {method: "POST", body: JSON.stringify({answers})}),
    onWaiting: waiting => {
      if (!questionTarget.isConnected || state.selectedChat?.id !== chatId) return;
      state.waitingQuestion = waiting;
      updateChatStatus();
    },
  });
  void state.questions.refresh();
  document.querySelector("#composer").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    const values = Object.fromEntries(new FormData(form));
    const endpoint = adminContext ? `/api/admin/chats/${chatId}/messages` : `/api/chats/${chatId}/messages`;
    try { await api(endpoint, {method:"POST", body:JSON.stringify(values)}); form.reset(); notify("Сообщение поставлено в очередь"); await refreshChatLive(chatId); }
    catch (err) { notify(err.message); }
    finally { button.disabled = false; }
  });
}

function taskBadge(status, run = null) {
  if (status === "running" && run?.plan_first) return `<span class="badge amber">${run.phase === "plan" ? "Планирование" : "Выполнение"}</span>`;
  const labels = {queued: "В очереди", dispatching: "Подготовка окружения", running: "В работе", cancelling: "Останавливается…", completed: "Завершена", failed: "Завершена с ошибкой", cancelled: "Отменена"};
  return `<span class="badge ${status === "failed" ? "red" : ["running", "completed"].includes(status) ? "green" : "amber"}">${esc(labels[status] || labels.running)}</span>`;
}

function taskRunOverview(run, admin = false) {
  return `<div class="list-item-head"><strong data-task-run-title="${esc(run.id)}">${esc(run.title)}</strong>${taskBadge(run.status, run)}</div><div class="meta">${admin && (run.effective_login || run.owner_login) ? `${esc(run.effective_login || run.owner_login)} · ` : ""}${esc(run.created_at)}${run.finished_at ? ` · Завершена: ${esc(run.finished_at)}` : ""}</div>${admin ? "" : taskRunAction(run)}`;
}

function taskResultContent(run) {
  if (run.status !== "completed") return "";
  return `<details class="task-result" data-task-result="${esc(run.id)}"><summary>Итоги от Valeria</summary><div class="task-result-body" data-task-result-body aria-live="polite"></div></details>`;
}

function taskRunContent(run, admin = false) {
  return `<div data-task-run-overview>${taskRunOverview(run, admin)}</div>${taskResultContent(run)}`;
}

async function loadTaskResult(details) {
  if (details.dataset.loading || details.dataset.loaded) return;
  details.dataset.loading = "true";
  const body = details.querySelector("[data-task-result-body]");
  body.innerHTML = `<p class="hint" role="status">Загрузка итога…</p>`;
  try {
    const result = await api(`/api/task-runs/${encodeURIComponent(details.dataset.taskResult)}/result`);
    if (!details.isConnected) return;
    body.innerHTML = result.status === "available"
      ? `<div class="markdown-body">${renderMarkdown(result.text)}</div>`
      : `<p class="hint">${result.status === "not_completed" ? "Таска ещё не завершена" : "Итоговое сообщение недоступно"}</p>`;
    if (result.status !== "not_completed") details.dataset.loaded = "true";
  } catch {
    if (details.isConnected) body.innerHTML = `<p class="hint">Не удалось загрузить итоговое сообщение.</p><button class="btn" type="button" data-retry-task-result>Повторить</button>`;
  } finally {
    delete details.dataset.loading;
  }
}

function bindTaskResults() {
  const target = document.querySelector("#task-runs");
  // toggle does not bubble; capture also handles rows inserted by SSE.
  target.addEventListener("toggle", event => {
    const details = event.target;
    if (details.matches("[data-task-result]") && details.open) void loadTaskResult(details);
  }, true);
  target.addEventListener("click", event => {
    const retry = event.target.closest("[data-retry-task-result]");
    if (retry) void loadTaskResult(retry.closest("[data-task-result]"));
  });
}

function taskRunAction(run) {
  if (!["queued", "dispatching", "running", "cancelling"].includes(run.status)) return "";
  const stopping = run.status === "cancelling" || state.stoppingJobs.has(run.id);
  return `<div class="actions"><button class="btn btn-danger" type="button" data-stop-task-run="${esc(run.id)}" ${stopping ? "disabled" : ""}>${stopping ? "Останавливается…" : "Отменить"}</button></div>`;
}

function bindTaskRunStop() {
  const target = document.querySelector("#task-runs");
  target.addEventListener("click", async event => {
    const button = event.target.closest("[data-stop-task-run]");
    if (!button || button.disabled) return;
    const runId = button.dataset.stopTaskRun;
    if (state.stoppingJobs.has(runId)) return;
    const title = button.closest("[data-task-run]").querySelector("[data-task-run-title]").textContent;
    if (!confirm(`Отменить запуск таски «${title}»?`)) return;
    state.stoppingJobs.add(runId);
    button.disabled = true;
    button.textContent = "Останавливается…";
    let result;
    try {
      result = await api(`/api/task-runs/${encodeURIComponent(runId)}/stop`, {method: "POST"});
      notify(result.status === "cancelled" ? "Таска отменена" : result.status === "cancelling" ? "Остановка запрошена" : "Таска уже завершена");
    } catch (err) {
      notify(err.message);
    } finally {
      state.stoppingJobs.delete(runId);
      if (state.route === "tasks" && target.isConnected) {
        if (result) patchTaskRun(result);
        else target.querySelectorAll("[data-stop-task-run]").forEach(current => {
          if (current.dataset.stopTaskRun === runId) {
            current.disabled = false;
            current.textContent = "Отменить";
          }
        });
        await refreshTaskRuns();
      }
    }
  });
}

function patchTaskTitles(updates) {
  const titles = new Map(updates.filter(item => item && typeof item.title === "string").map(item => [item.id, item.title]));
  document.querySelectorAll("[data-task-run-title]").forEach(target => {
    const title = titles.get(target.dataset.taskRunTitle);
    if (title && target.textContent !== title) target.textContent = title;
  });
}

function hydrateTaskTitles() {
  const route = state.route;
  if (route === "admin-user-tasks" && state.adminTaskOwner) {
    const userId = state.adminTaskOwner;
    return hydrateChatTitles(
      `/api/admin/users/${encodeURIComponent(userId)}/task-runs/title-sync`,
      () => state.route === "admin-user-tasks" && state.adminTaskOwner === userId,
      patchTaskTitles
    );
  }
  if (!["tasks", "admin-tasks", "queue"].includes(route)) return;
  const path = route === "tasks" ? "/api/task-runs/title-sync" : "/api/admin/task-runs/title-sync";
  return hydrateChatTitles(path, () => state.route === route, patchTaskTitles);
}

function taskRunList(runs, admin = false) {
  return runs.length ? runs.map(run => `<article class="list-item" data-task-run="${esc(run.id)}">${taskRunContent(run, admin)}</article>`).join("") : `<div class="empty" data-task-runs-empty>Запусков пока нет</div>`;
}

function patchTaskRun(run) {
  const target = document.querySelector("#task-runs");
  if (!target || !run?.id) return;
  let row = [...target.querySelectorAll("[data-task-run]")].find(item => item.dataset.taskRun === run.id);
  if (!row) {
    target.querySelector("[data-task-runs-empty]")?.remove();
    row = document.createElement("article");
    row.className = "list-item";
    row.dataset.taskRun = run.id;
    target.prepend(row);
  }
  const admin = ["admin-tasks", "admin-user-tasks"].includes(state.route);
  const overview = row.querySelector("[data-task-run-overview]");
  if (!overview) {
    row.innerHTML = taskRunContent(run, admin);
    return;
  }
  const content = taskRunOverview(run, admin);
  if (overview.innerHTML !== content) overview.innerHTML = content;
  // Preserve the details node, open state, in-flight request and scroll position.
  const details = row.querySelector("[data-task-result]");
  if (run.status === "completed" && !details) row.insertAdjacentHTML("beforeend", taskResultContent(run));
  else if (run.status !== "completed") details?.remove();
}

async function refreshTaskRuns() {
  const route = state.route;
  const target = document.querySelector("#task-runs");
  if (!target || !["tasks", "admin-tasks", "admin-user-tasks"].includes(route)) return;
  const path = route === "tasks"
    ? "/api/task-runs"
    : route === "admin-user-tasks"
      ? `/api/admin/users/${encodeURIComponent(state.adminTaskOwner)}/task-runs`
      : "/api/admin/task-runs";
  try {
    const runs = await api(path);
    if (state.route !== route || !target.isConnected) return;
    // Update history only: selection, focused inputs and unsaved settings stay intact.
    for (const run of runs.slice().reverse()) patchTaskRun(run);
    void hydrateTaskTitles();
  } catch (err) { if (target.isConnected) notify(err.message); }
}

async function renderTasks(selectedId = state.selectedTask, linkedRunId = null) {
  const [tasks, runs] = await Promise.all([api("/api/tasks"), api("/api/task-runs")]);
  if (state.route !== "tasks") return;
  const selected = tasks.find(task => task.id === selectedId) || tasks[0] || null;
  state.selectedTask = selected?.id || null;
  const list = tasks.map(task => `<button class="list-item ${selected?.id === task.id ? "selected" : ""}" data-task="${esc(task.id)}"><div class="list-item-head"><strong>${esc(task.title)}</strong></div><div class="meta">${esc(task.description)}</div></button>`).join("");
  const editor = selected ? `<form class="card" id="task-run-form"><h3>${esc(selected.title)}</h3><p class="task-description">${esc(selected.description)}</p><div class="field"><label for="task-user-input">Ваше дополнение</label><textarea class="input" id="task-user-input" name="user_input" maxlength="100000" placeholder="Добавьте детали, если нужно…">${esc(state.taskDrafts.get(selected.id) || "")}</textarea></div>${state.me.workspace_path ? "" : `<p class="hint">Для запуска администратор должен назначить вам рабочую папку.</p>`}<button class="btn btn-primary btn-block" type="submit" ${state.me.workspace_path ? "" : "disabled"}>Запустить таску</button></form>` : `<div class="empty">Администратор ещё не опубликовал таски</div>`;
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Таски</h2></div><div class="grid task-grid"><div class="list">${list}</div>${editor}</div><section class="task-history"><div class="page-head"><h2>Мои запуски</h2><span class="spacer"></span><button class="btn" id="refresh-task-runs" type="button">Обновить</button></div><div class="list" id="task-runs" aria-live="polite">${taskRunList(runs)}</div></section></div>`, "Таски");
  bindShell();
  document.querySelectorAll("[data-task]").forEach(button => button.addEventListener("click", () => {
    if (location.hash.includes("/tasks/runs/")) history.replaceState(null, "", "#/tasks");
    void renderTasks(button.dataset.task).catch(err => notify(err.message));
  }));
  document.querySelector("#refresh-task-runs").addEventListener("click", refreshTaskRuns);
  bindTaskResults();
  bindTaskRunStop();
  if (linkedRunId) {
    const row = [...document.querySelectorAll("[data-task-run]")].find(
      item => item.dataset.taskRun === linkedRunId
    );
    if (!row) {
      app.innerHTML = shell(`<div class="page"><div class="empty">Запуск таски не найден или недоступен</div></div>`, "Таски");
      bindShell();
      return;
    }
    row.scrollIntoView({block: "center"});
    const details = row.querySelector("[data-task-result]");
    if (details) {
      details.open = true;
      await loadTaskResult(details);
    }
  }
  const form = document.querySelector("#task-run-form");
  form?.addEventListener("input", () => state.taskDrafts.set(selected.id, form.elements.user_input.value));
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    const userInput = form.elements.user_input.value;
    button.disabled = true;
    try {
      const run = await api(`/api/tasks/${encodeURIComponent(selected.id)}/runs`, {method: "POST", body: JSON.stringify({user_input: userInput})});
      if (state.taskDrafts.get(selected.id) === userInput) state.taskDrafts.delete(selected.id);
      if (form.isConnected && form.elements.user_input.value === userInput) form.elements.user_input.value = "";
      patchTaskRun(run);
      void hydrateTaskTitles();
      notify(run.status === "cancelled" ? "Таска отменена: такой запуск уже в очереди или выполняется" : run.status === "queued" ? "Таска поставлена в очередь" : "Таска запущена");
    } catch (err) { notify(err.message); }
    finally { button.disabled = false; }
  });
  void hydrateTaskTitles();
}

async function renderTaskRun(runId, run = null) {
  state.route = "task-run";
  app.innerHTML = shell(`<div class="page"><div class="empty">Загружаем содержимое запуска…</div></div>`, run?.title || "Запуск таски");
  bindShell();
  try {
    const [messages, runs] = await Promise.all([
      api(`/api/admin/task-runs/${encodeURIComponent(runId)}/messages`),
      run ? Promise.resolve(null) : api("/api/admin/task-runs"),
    ]);
    if (state.route !== "task-run") return;
    const meta = run || runs?.find(item => item.id === runId) || {};
    const content = `<div class="page">
      <div class="page-head"><button class="btn" id="task-run-back" type="button">← Назад</button><h2>${esc(meta.title || "Запуск таски")}</h2><span class="spacer"></span>${taskBadge(meta.status || "running", meta)}</div>
      <div class="card" style="margin:12px;border-color:#815f1d;background:#2a210f">Режим отладки: содержимое запуска таски доступно только администратору. Отправка сообщений недоступна.</div>
      <div class="meta" style="margin:0 12px 8px">${meta.owner_login ? `${esc(meta.owner_login)} · ` : ""}${esc(meta.created_at || "")}${meta.finished_at ? ` · Завершена: ${esc(meta.finished_at)}` : ""}</div>
      <div class="message-scroll"><div class="messages" id="message-list">${renderMessages(messages)}</div></div>
    </div>`;
    app.innerHTML = shell(content, meta.title || "Запуск таски");
    bindShell();
    const origin = state.taskRunOrigin || {route: "admin-tasks", owner: null};
    document.querySelector("#task-run-back").addEventListener("click", () => {
      if (origin.route === "admin-user-tasks" && origin.owner) {
        void renderAdminUserTaskRuns(origin.owner).catch(err => notify(err.message));
      } else {
        void renderAdminTasks().catch(err => notify(err.message));
      }
    });
    scrollMessagesToBottom("auto");
  } catch (err) {
    if (state.route !== "task-run") return;
    app.innerHTML = shell(`<div class="page"><div class="empty">${esc(err.message)}</div></div>`, "Запуск таски");
    bindShell();
  }
}

function taskModelOptions(models, value, loading = false) {
  const selected = value || "";
  const unavailable = selected && !models.some(model => model.id === selected);
  return `<option value="">${loading ? "Модели OpenCode загружаются…" : models.length ? "Выберите модель" : "Нет доступных моделей"}</option>${unavailable ? `<option value="${esc(selected)}" selected disabled>${esc(selected)} · ${loading ? "проверяем доступность" : "недоступна"}</option>` : ""}${models.map(model => `<option value="${esc(model.id)}" ${model.id === selected ? "selected" : ""}>${esc(model.name || model.id)} · ${esc(model.id)}</option>`).join("")}`;
}

function taskModelField(value) {
  return `<select class="input" id="task-model" name="model" required disabled>${taskModelOptions([], value, true)}</select>`;
}

function updateTaskSaveState(form) {
  const model = form.elements.model;
  const selected = model.selectedOptions[0];
  form.querySelector("button[type=submit]").disabled = form.dataset.modelsReady !== "true" || form.dataset.saving === "true" || !model.value || !selected || selected.disabled;
}

async function hydrateTaskOptions() {
  const target = document.querySelector("#task-model-field");
  const hint = document.querySelector("#task-model-hint");
  const retry = document.querySelector("#retry-task-models");
  const form = document.querySelector("#admin-task-form");
  if (!target || !hint || !retry || !form) return;
  const current = form.elements.model;
  form.dataset.modelsReady = "false";
  current.disabled = true;
  retry.disabled = true;
  hint.textContent = "Загружаем доступные модели OpenCode…";
  updateTaskSaveState(form);
  try {
    const options = await api("/api/options");
    if (state.route !== "admin-tasks" || !target.isConnected) return;
    state.options = options;
    const models = options.opencode_unavailable ? [] : flattenModels(options.models);
    current.innerHTML = taskModelOptions(models, current.value);
    current.disabled = !models.length;
    form.dataset.modelsReady = String(models.length > 0);
    hint.textContent = options.opencode_unavailable
      ? "OpenCode сейчас недоступен. Повторите загрузку списка моделей."
      : models.length
        ? "Выберите фиксированную модель для всех запусков таски."
        : "Нет доступных моделей. Проверьте подключение провайдера OpenCode и обновите список.";
  } catch (_) {
    if (hint.isConnected) {
      current.innerHTML = taskModelOptions([], current.value);
      hint.textContent = "Не удалось загрузить модели OpenCode. Повторите загрузку списка.";
    }
  } finally {
    if (form.isConnected) {
      retry.disabled = false;
      updateTaskSaveState(form);
    }
  }
}

async function renderAdminTasks(selectedId = state.selectedAdminTask) {
  const [tasks, runs, config, allUsers, queueItems] = await Promise.all([api("/api/admin/tasks"), api("/api/admin/task-runs"), api("/api/config/public"), api("/api/admin/users"), api("/api/admin/queues")]);
  if (state.route !== "admin-tasks") return;
  const selected = tasks.find(task => task.id === selectedId) || null;
  state.selectedAdminTask = selected?.id || null;
  state.taskAdminDirty = false;
  const model = selected?.model || (selected?.provider_id && selected?.model_id ? `${selected.provider_id}/${selected.model_id}` : config.default_model || "");
  const agent = selected?.agent || config.default_agent;
  const agents = config.agents.includes(agent) ? config.agents : [agent, ...config.agents];
  const assignableUsers = allUsers.filter(user => user.role !== "admin");
  const visibility = selected?.visibility === "selected" ? "selected" : "all";
  const audience = new Set(selected?.audience_user_ids || []);
  const successors = new Set(selected?.next_task_ids || []);
  const queues = (queueItems || []).filter(queue => queue.status !== "archived");
  if (!queues.length) queues.push({id: "default", name: "Основная"});
  const selectedQueue = selected?.queue_id || "default";
  const pipelineField = `<div class="field"><label>Запустить после успешного завершения</label><div id="task-successors">${tasks.filter(task => task.id !== selected?.id).map(task => `<label class="toolbar"><input type="checkbox" value="${esc(task.id)}" ${successors.has(task.id) ? "checked" : ""}> ${esc(task.title)}${task.is_published ? "" : " (черновик)"}</label>`).join("") || `<span class="hint">Нет других тасок</span>`}</div><span class="hint">Можно выбрать несколько. Все выбранные таски запускаются от лица исходного пользователя с копией его ввода, независимо от их доступности пользователю. При ошибке или отмене продолжение этой ветки не запускается. Изменения пайплайна действуют для новых запусков.</span></div>`;
  const list = tasks.map(task => `<button class="list-item ${selected?.id === task.id ? "selected" : ""}" data-admin-task="${esc(task.id)}"><div class="list-item-head"><strong>${esc(task.title)}</strong><span class="badge ${task.is_published ? "green" : "amber"}">${task.is_published ? "Опубликована" : "Черновик"}</span></div><div class="meta">${esc(task.description)}</div></button>`).join("");
  const editor = `<form class="card" id="admin-task-form"><div class="page-head"><h3>${selected ? "Настройка таски" : "Новая таска"}</h3><span class="spacer"></span><button class="btn" id="new-task" type="button">+ Новая</button></div><div class="unsaved" id="task-unsaved" hidden>Есть несохранённые изменения</div><div class="field"><label for="task-title">Название</label><input class="input" id="task-title" name="title" value="${esc(selected?.title || "")}" maxlength="120" required></div><div class="field"><label for="task-description">Описание для пользователей</label><textarea class="input" id="task-description" name="description" maxlength="1000">${esc(selected?.description || "")}</textarea></div><div class="field"><label for="task-content">Промпт администратора</label><textarea class="input template-content" id="task-content" name="content" maxlength="100000" required>${esc(selected?.content || "")}</textarea><span class="hint">Скрыт от пользователя. Его дополнение добавляется после этого текста.</span></div><div class="field"><label for="task-agent">Режим</label><select class="input" id="task-agent" name="agent">${agents.map(item => `<option value="${esc(item)}" ${item === agent ? "selected" : ""}>${esc(item)}</option>`).join("")}</select></div><div class="field"><label for="task-model">Модель</label><div id="task-model-field">${taskModelField(model)}</div><span class="hint" id="task-model-hint">Загружаем доступные модели OpenCode…</span><button class="btn" id="retry-task-models" type="button" disabled>Обновить список моделей</button></div><div class="field"><label for="task-variant">Вариант модели (необязательно)</label><input class="input" id="task-variant" name="model_variant" maxlength="100" value="${esc(selected?.model_variant || "")}" placeholder="Например: high"><span class="hint">Идентификатор варианта из конфигурации выбранной модели.</span></div><div class="field"><label for="task-queue">Очередь</label><select class="input" id="task-queue" name="queue_id" required>${queues.map(queue => `<option value="${esc(queue.id)}" ${queue.id === selectedQueue ? "selected" : ""}>${esc(queue.name)}</option>`).join("")}</select><span class="hint">Изменение действует только для новых запусков.</span></div><label class="toolbar"><input type="checkbox" name="plan_first" ${selected?.plan_first ? "checked" : ""} ${agent !== "build" ? "disabled" : ""}> Сначала план, затем автоматическое выполнение</label><label class="toolbar"><input type="checkbox" name="is_published" ${selected?.is_published ? "checked" : ""}> Доступна пользователям</label><div class="field"><label for="task-visibility">Кто видит таску</label><select class="input" id="task-visibility" name="visibility"><option value="all" ${visibility === "all" ? "selected" : ""}>Все пользователи</option><option value="selected" ${visibility === "selected" ? "selected" : ""}>Выбранные пользователи</option></select></div><div class="field" id="task-audience-field" ${visibility === "selected" ? "" : "hidden"}><label>Пользователи с доступом</label><div id="task-audience">${assignableUsers.map(user => `<label class="toolbar"><input type="checkbox" value="${esc(user.id)}" ${audience.has(user.id) ? "checked" : ""}> ${esc(user.login)}</label>`).join("") || `<span class="hint">Нет пользователей</span>`}</div></div>${pipelineField}<div class="actions"><button class="btn btn-primary" type="submit">Сохранить</button>${selected ? `<button class="btn btn-danger" id="delete-task" type="button">Удалить таску</button>` : ""}</div></form>`;
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Настройка тасок</h2></div><div class="grid task-admin-grid"><div class="list">${list || `<div class="empty">Создайте первую таску</div>`}</div>${editor}</div><section class="task-history"><div class="page-head"><h2>Все запуски</h2><span class="spacer"></span><button class="btn" id="refresh-task-runs" type="button">Обновить</button></div><div class="list" id="task-runs">${taskRunList(runs, true)}</div></section></div>`, "Админка · Таски");
  bindShell();
  const changeSelection = id => {
    if (state.taskAdminDirty && !confirm("Отменить несохранённые изменения таски?")) return;
    void renderAdminTasks(id).catch(err => notify(err.message));
  };
  document.querySelectorAll("[data-admin-task]").forEach(button => button.addEventListener("click", () => changeSelection(button.dataset.adminTask)));
  document.querySelector("#new-task").addEventListener("click", () => changeSelection(null));
  document.querySelector("#refresh-task-runs").addEventListener("click", refreshTaskRuns);
  bindTaskResults();
  document.querySelector("#task-runs").addEventListener("click", event => {
    if (event.target.closest("[data-task-result]")) return;
    const row = event.target.closest("[data-task-run]");
    if (!row) return;
    const run = runs.find(item => item.id === row.dataset.taskRun) || null;
    state.taskRunOrigin = {route: "admin-tasks", owner: null};
    void renderTaskRun(row.dataset.taskRun, run).catch(err => notify(err.message));
  });
  document.querySelector("#retry-task-models").addEventListener("click", hydrateTaskOptions);
  document.querySelector("#task-visibility").addEventListener("change", event => {
    const field = document.querySelector("#task-audience-field");
    if (field) field.hidden = event.target.value !== "selected";
  });
  const form = document.querySelector("#admin-task-form");
  form.elements.agent.addEventListener("change", () => {
    form.elements.plan_first.disabled = form.elements.agent.value !== "build";
    if (form.elements.plan_first.disabled) form.elements.plan_first.checked = false;
  });
  form.addEventListener("input", () => {
    state.taskAdminDirty = true;
    document.querySelector("#task-unsaved").hidden = false;
    updateTaskSaveState(form);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    updateTaskSaveState(form);
    const button = form.querySelector("button[type=submit]");
    if (button.disabled) return;
    const data = Object.fromEntries(new FormData(form));
    data.is_published = data.is_published === "on";
    data.plan_first = data.plan_first === "on";
    data.model_variant = data.model_variant.trim() || null;
    data.model = data.model.trim();
    data.next_task_ids = [...form.querySelectorAll("#task-successors input:checked")].map(input => input.value);
    data.visibility = data.visibility === "selected" ? "selected" : "all";
    data.user_ids = data.visibility === "selected"
      ? [...document.querySelectorAll("#task-audience input:checked")].map(input => input.value)
      : [];
    form.dataset.saving = "true";
    updateTaskSaveState(form);
    try {
      const saved = await api(selected ? `/api/admin/tasks/${encodeURIComponent(selected.id)}` : "/api/admin/tasks", {method: selected ? "PATCH" : "POST", body: JSON.stringify(data)});
      state.taskAdminDirty = false;
      notify("Таска сохранена");
      if (form.isConnected) await renderAdminTasks(saved.id);
    } catch (err) { notify(err.message); }
    finally { form.dataset.saving = "false"; updateTaskSaveState(form); }
  });
  document.querySelector("#delete-task")?.addEventListener("click", async () => {
    if (!confirm(`Удалить таску «${selected.title}»? История запусков сохранится.`)) return;
    try {
      await api(`/api/admin/tasks/${encodeURIComponent(selected.id)}`, {method: "DELETE"});
      state.taskAdminDirty = false;
      notify("Таска удалена");
      await renderAdminTasks(null);
    } catch (err) { notify(err.message); }
  });
  void hydrateTaskOptions();
  void hydrateTaskTitles();
}

async function renderTemplates(selectedId = state.selectedTemplate?.id) {
  const admin = state.me.role === "admin";
  const templates = await api(admin ? "/api/admin/templates" : "/api/templates");
  const selected = templates.find(item => item.id === selectedId) || null;
  state.selectedTemplate = selected;
  state.templateDirty = false;
  const list = templates.map(t => `<button class="list-item ${selected?.id === t.id ? "selected" : ""}" data-template="${t.id}"><div class="list-item-head"><strong>${esc(t.title)}</strong>${admin ? `<span class="badge ${t.is_published ? "green" : "amber"}">${t.is_published ? "Опубликован" : "Черновик"}</span>` : ""}</div><div class="meta">${esc(t.description || t.content.slice(0, 100))}</div></button>`).join("");
  const editor = admin ? `<form class="card" id="template-form"><div class="page-head"><h3>${selected ? "Редактирование шаблона" : "Новый шаблон"}</h3><button class="btn" id="new-template" type="button">+ Новый</button></div><div class="unsaved" id="template-unsaved" hidden>Есть несохранённые изменения</div><div class="field"><label>Название</label><input class="input" name="title" value="${esc(selected?.title || "")}" required></div><div class="field"><label>Описание</label><textarea class="input" name="description">${esc(selected?.description || "")}</textarea></div><div class="field"><label>Текст сообщения</label><textarea class="input template-content" name="content" required>${esc(selected?.content || "")}</textarea></div><label class="toolbar"><input type="checkbox" name="is_published" ${selected?.is_published ? "checked" : ""}> Доступен пользователям</label><div class="actions"><button class="btn btn-primary" type="submit">Сохранить</button>${selected ? `<button class="btn btn-danger" id="delete-template" type="button">Удалить шаблон</button>` : ""}</div></form>` : `<div class="card"><h3>Библиотека шаблонов</h3><p class="hint">Выберите шаблон и используйте его как редактируемую основу новой задачи.</p>${selected ? `<button class="btn btn-primary" id="use-template">Использовать «${esc(selected.title)}»</button>` : ""}</div>`;
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Шаблоны сообщений</h2></div><div class="grid"><div class="list">${list || `<div class="empty">Нет доступных шаблонов</div>`}</div>${editor}</div></div>`, "Шаблоны"); bindShell();
  document.querySelectorAll("[data-template]").forEach(button => button.addEventListener("click", () => {
    if (state.templateDirty && !confirm("Отменить несохранённые изменения шаблона?")) return;
    renderTemplates(button.dataset.template);
  }));
  if (!admin) {
    document.querySelector("#use-template")?.addEventListener("click", () => { state.draftTemplate = selected; renderShell("dialogs"); });
    return;
  }
  const form = document.querySelector("#template-form");
  form.addEventListener("input", () => { state.templateDirty = true; document.querySelector("#template-unsaved").hidden = false; });
  document.querySelector("#new-template").addEventListener("click", () => {
    if (state.templateDirty && !confirm("Отменить несохранённые изменения шаблона?")) return;
    renderTemplates(null);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.is_published = data.is_published === "on";
    const path = selected ? `/api/admin/templates/${selected.id}` : "/api/admin/templates";
    try { const saved = await api(path, {method:selected ? "PATCH" : "POST", body:JSON.stringify(data)}); state.templateDirty = false; notify("Шаблон сохранён"); await renderTemplates(saved.id); } catch (err) { notify(err.message); }
  });
  document.querySelector("#delete-template")?.addEventListener("click", async () => {
    if (!confirm(`Удалить шаблон «${selected.title}»?`)) return;
    try { await api(`/api/admin/templates/${selected.id}`, {method:"DELETE"}); state.selectedTemplate = null; notify("Шаблон удалён"); await renderTemplates(null); } catch (err) { notify(err.message); }
  });
}

window.addEventListener("beforeunload", event => {
  if (!state.templateDirty && !state.taskAdminDirty) return;
  event.preventDefault();
});

function mattermostUsernameInput(value = "", name = "mattermost_username") {
  return `<input class="input mono" name="${name}" value="${esc(value || "")}" ` +
    `pattern="@?[a-z][a-z0-9._-]{2,21}" maxlength="23" placeholder="akhamitsevich" ` +
    `autocomplete="off"><span class="hint">Username из Mattermost, без отображаемого имени. Можно с @.</span>`;
}

function updateCurrentUser(user) {
  if (user.id !== state.me?.id) return;
  const workspaceChanged = user.workspace_path !== state.me.workspace_path;
  state.me = user;
  if (workspaceChanged) {
    state.options = null;
  }
}

async function renderProfile() {
  const user = state.me;
  const access = user.role === "admin" ? "Администратор" : user.tasks_only ? "Таски и консультации" : "Пользователь";
  const content = `<div class="page profile-page"><div class="page-head"><h2>Мой профиль</h2></div>
    <div class="card profile-card">
      <div class="profile-summary"><span class="avatar profile-avatar">${esc(user.login[0]?.toUpperCase())}</span><div><h3>${esc(user.login)}</h3><div class="meta">${access}</div></div></div>
      <div class="settings-summary"><div><span>Статус</span><strong>${user.status === "active" ? "Активен" : "Заблокирован"}</strong></div><div><span>Лимит задач</span><strong>${user.max_active_jobs ?? "Общий"}</strong></div></div>
      <form id="profile-form"><div class="field"><label>Mattermost username</label>${mattermostUsernameInput(user.mattermost_username)}</div>
        <p class="hint">${user.mattermost_user_id && !user.mattermost_username ? "Уведомления уже настроены по внутреннему ID. Введите username, чтобы обновить понятную привязку. " : ""}После проверки username вы будете получать личные уведомления о завершении тасок и готовых ответах консультаций.</p>
        <div class="actions"><button class="btn btn-primary" type="submit">Сохранить профиль</button>${user.mattermost_user_id ? `<button class="btn btn-danger" type="button" id="profile-mattermost-clear">Отключить уведомления</button>` : ""}</div>
      </form>
    </div>
    ${user.role === "admin" ? `<div class="card profile-card"><h3>Рабочая папка</h3>
      <form id="profile-workspace-form">
        <div class="field"><label for="profile-workspace-path">Путь к папке</label><input class="input mono" id="profile-workspace-path" name="workspace_path" value="${esc(user.workspace_path || "")}" placeholder="Например: admin/project" required></div>
        <p class="hint">Папка для ваших диалогов, тасок и консультаций. Укажите путь внутри разрешённых каталогов сервера. Если разрешён один каталог, можно указать относительный путь. Недостающая папка будет создана.</p>
        <p class="hint">Сменить папку можно после завершения задач. При смене папки старые диалоги архивируются.</p>
        <button class="btn btn-primary" type="submit">Сохранить папку</button>
      </form>
    </div>` : ""}</div>`;
  app.innerHTML = shell(content, "Профиль"); bindShell();
  document.querySelector("#profile-workspace-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const input = form.elements.workspace_path;
    input.value = input.value.trim();
    if (!form.reportValidity()) return;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      updateCurrentUser(await api(`/api/admin/users/${encodeURIComponent(user.id)}`, {
        method: "PATCH", body: JSON.stringify({workspace_path: input.value}),
      }));
      notify("Рабочая папка сохранена");
      await renderProfile();
    } catch (err) { notify(err.message); }
    finally { button.disabled = false; }
  });
  document.querySelector("#profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      data.mattermost_username = data.mattermost_username.trim() || null;
      if (user.mattermost_user_id && !user.mattermost_username && !data.mattermost_username) delete data.mattermost_username;
      updateCurrentUser(await api("/api/profile", {method: "PATCH", body: JSON.stringify(data)}));
      notify("Профиль сохранён");
      await renderProfile();
    } catch (err) { button.disabled = false; notify(err.message); }
  });
  document.querySelector("#profile-mattermost-clear")?.addEventListener("click", async () => {
    try {
      updateCurrentUser(await api("/api/profile", {method: "PATCH", body: JSON.stringify({mattermost_username: null})}));
      notify("Уведомления Mattermost отключены");
      await renderProfile();
    } catch (err) { notify(err.message); }
  });
}

async function renderUsers() {
  const [users, config] = await Promise.all([api("/api/admin/users"), api("/api/config/public")]);
  const hardLimit = config.max_active_jobs_hard_limit || 10;
  let selected = users.find(user => user.id === state.selectedAdminUser) || users[0] || null;
  state.selectedAdminUser = selected?.id || null;
  const list = users.map(user => `<button class="list-item ${selected?.id === user.id ? "selected" : ""}" data-user-card="${user.id}"><div class="list-item-head"><strong>${esc(user.login)}</strong>${jobBadge(user.status === "active" ? "running" : "failed")}</div><div class="meta">${user.role === "admin" ? "Администратор" : user.tasks_only ? "Таски и консультации" : "Пользователь"} · ${user.mattermost_username ? `@${esc(user.mattermost_username)}` : "Mattermost не настроен"}</div></button>`).join("");
  const accountType = !selected ? "user" : selected.role === "admin" ? "admin" : selected.tasks_only ? "tasks_only" : "user";
  const card = selected ? `<section class="card user-settings-card"><div class="page-head"><div><h3>${esc(selected.login)}</h3><div class="meta mono">${esc(selected.id)}</div></div><span class="spacer"></span>${jobBadge(selected.status === "active" ? "running" : "failed")}</div>
    <form id="user-settings-form">
      <div class="field"><label>Логин</label><input class="input" value="${esc(selected.login)}" disabled></div>
      <div class="field"><label>Тип учётной записи</label><select class="input" name="account_type"><option value="user" ${accountType === "user" ? "selected" : ""}>Пользователь: чаты и таски</option><option value="tasks_only" ${accountType === "tasks_only" ? "selected" : ""}>Таски и консультации</option><option value="admin" ${accountType === "admin" ? "selected" : ""}>Администратор</option></select></div>
      <div class="field"><label>Рабочая папка</label><input class="input mono" name="workspace_path" value="${esc(selected.workspace_path || "")}" placeholder="team/project"></div>
      <div class="field"><label>Статус</label><select class="input" name="status"><option value="active" ${selected.status === "active" ? "selected" : ""}>Активен</option><option value="disabled" ${selected.status === "disabled" ? "selected" : ""}>Заблокирован</option></select></div>
      <div class="field"><label>Mattermost username</label>${mattermostUsernameInput(selected.mattermost_username)}${selected.mattermost_user_id && !selected.mattermost_username ? `<span class="hint">Сейчас уведомления настроены по внутреннему ID; введите username для обновления привязки.</span>` : ""}</div>
      <div class="field"><label>Лимит одновременно выполняемых задач</label><input class="input" name="max_active_jobs" type="number" min="1" max="${hardLimit}" value="${selected.max_active_jobs ?? ""}" placeholder="Общий лимит"><span class="hint">От 1 до ${hardLimit}; пусто — общий лимит системы.</span></div>
      <div class="settings-summary"><div><span>Создан</span><strong>${esc(selected.created_at || "—")}</strong></div><div><span>Изменён</span><strong>${esc(selected.updated_at || "—")}</strong></div><div><span>Последний вход</span><strong>${esc(selected.last_login_at || "—")}</strong></div></div>
      <button class="btn btn-primary btn-block" type="submit">Сохранить настройки</button>${selected.mattermost_user_id ? `<button class="btn btn-danger btn-block" type="button" id="admin-mattermost-clear">Отключить Mattermost-уведомления</button>` : ""}
    </form>
    <div class="actions user-card-actions"><button class="btn" data-user-chats="${selected.id}">Диалоги</button><button class="btn" data-user-task-runs="${selected.id}">Запуски</button><button class="btn" data-user-task-access="${selected.id}">Доступ к таскам</button></div>
    <form id="reset-password-form" class="password-reset"><div class="field"><label>Новый временный пароль</label><input class="input" type="password" name="password" minlength="12" autocomplete="new-password" placeholder="Не менее 12 символов"></div><button class="btn" type="submit">Сбросить пароль</button></form>
  </section>` : `<div class="empty">Пользователей пока нет</div>`;
  const create = `<details class="card user-create-panel"><summary>Создать пользователя</summary><form id="user-form">
    <div class="field"><label>Логин</label><input class="input" name="login" required></div><div class="field"><label>Временный пароль</label><input class="input" type="password" name="password" minlength="12" required></div><div class="field"><label>Рабочая папка</label><input class="input mono" name="workspace_path" placeholder="Например: team/project" required><span class="hint">Относительный путь создаётся внутри каталога данных сервера.</span></div><div class="field"><label>Mattermost username (необязательно)</label>${mattermostUsernameInput()}</div><div class="field"><label>Тип учётной записи</label><select class="input" name="account_type"><option value="user">Пользователь: чаты и таски</option><option value="tasks_only">Таски и консультации</option><option value="admin">Администратор</option></select></div><button class="btn btn-primary btn-block">Создать учётную запись</button></form></details>`;
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Пользователи</h2><span class="spacer"></span><span class="badge">${users.length}</span></div><div class="user-admin-grid"><section><div class="list">${list || `<div class="empty">Пользователей нет</div>`}</div>${create}</section>${card}</div></div>`, "Админка · Пользователи"); bindShell();
  document.querySelectorAll("[data-user-card]").forEach(button => button.addEventListener("click", () => { state.selectedAdminUser = button.dataset.userCard; void renderUsers(); }));
  document.querySelector("#user-form").addEventListener("submit", async event => { event.preventDefault(); if (!event.currentTarget.reportValidity()) return; try { const created = await api("/api/admin/users", {method:"POST", body:JSON.stringify(userFormData(event.currentTarget))}); state.selectedAdminUser = created.id; notify("Пользователь создан"); await renderUsers(); } catch(err) { notify(err.message); } });
  document.querySelector("#user-settings-form")?.addEventListener("submit", async event => {
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    const data = userFormData(event.currentTarget);
    data.status = new FormData(event.currentTarget).get("status");
    const rawLimit = new FormData(event.currentTarget).get("max_active_jobs").trim();
    data.max_active_jobs = rawLimit ? Number(rawLimit) : null;
    if (selected.mattermost_user_id && !selected.mattermost_username && !data.mattermost_username) delete data.mattermost_username;
    try {
      updateCurrentUser(await api(`/api/admin/users/${selected.id}`, {method: "PATCH", body: JSON.stringify(data)}));
      notify("Настройки пользователя сохранены");
      if (state.me.role === "admin") await renderUsers();
      else await navigate(defaultRoute());
    } catch (err) { notify(err.message); }
  });
  document.querySelector("#admin-mattermost-clear")?.addEventListener("click", async () => {
    try { updateCurrentUser(await api(`/api/admin/users/${selected.id}`, {method: "PATCH", body: JSON.stringify({mattermost_username: null})})); notify("Mattermost-уведомления отключены"); await renderUsers(); } catch (err) { notify(err.message); }
  });
  document.querySelector("#reset-password-form")?.addEventListener("submit", async event => {
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try { await api(`/api/admin/users/${selected.id}/reset-password`, {method: "POST", body: JSON.stringify(data)}); event.currentTarget.reset(); notify("Пароль сброшен"); } catch (err) { notify(err.message); }
  });
  document.querySelectorAll("[data-user-chats]").forEach(btn => btn.addEventListener("click", () => renderAdminUserChats(btn.dataset.userChats)));
  document.querySelectorAll("[data-user-task-runs]").forEach(btn => btn.addEventListener("click", () => { void renderAdminUserTaskRuns(btn.dataset.userTaskRuns).catch(err => notify(err.message)); }));
  document.querySelectorAll("[data-user-task-access]").forEach(btn => btn.addEventListener("click", () => { void renderAdminUserTaskAccess(btn.dataset.userTaskAccess).catch(err => notify(err.message)); }));
}

function userFormData(form) {
  const data = Object.fromEntries(new FormData(form));
  data.role = data.account_type === "admin" ? "admin" : "user";
  data.tasks_only = data.account_type === "tasks_only";
  if ("mattermost_username" in data) data.mattermost_username = data.mattermost_username.trim() || null;
  delete data.account_type;
  return data;
}

async function renderAdminUserChats(userId) {
  state.route = "admin-user-chats";
  state.adminChatOwner = userId;
  const chats = await api(`/api/admin/users/${userId}/chats`);
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Диалоги пользователя</h2></div><div class="list">${chats.map(c => `<button class="list-item" data-admin-chat="${c.id}"><div class="list-item-head"><strong data-chat-title="${esc(c.id)}">${esc(c.title)}</strong>${jobBadge(c.status)}</div><div class="meta mono">${esc(c.workspace_snapshot)}</div></button>`).join("") || `<div class="empty">Диалогов нет</div>`}</div></div>`, "Админка · Диалоги"); bindShell();
  document.querySelectorAll("[data-admin-chat]").forEach(btn => btn.addEventListener("click", () => renderChat(btn.dataset.adminChat, true)));
  void hydrateChatTitles(
    `/api/admin/users/${encodeURIComponent(userId)}/chats/title-sync`,
    () => state.route === "admin-user-chats"
  );
}

async function renderAdminUserTaskRuns(userId) {
  state.route = "admin-user-tasks";
  state.adminTaskOwner = userId;
  const runs = await api(`/api/admin/users/${encodeURIComponent(userId)}/task-runs`);
  if (state.route !== "admin-user-tasks" || state.adminTaskOwner !== userId) return;
  const content = `<div class="page"><div class="page-head"><button class="btn" id="user-tasks-back" type="button">← Назад</button><h2>Запуски тасок пользователя</h2><span class="spacer"></span><button class="btn" id="refresh-task-runs" type="button">Обновить</button></div><div class="list" id="task-runs" aria-live="polite">${taskRunList(runs, true)}</div></div>`;
  app.innerHTML = shell(content, "Админка · Таски пользователя");
  bindShell();
  document.querySelector("#user-tasks-back").addEventListener("click", () => {
    void renderUsers().catch(err => notify(err.message));
  });
  document.querySelector("#refresh-task-runs").addEventListener("click", refreshTaskRuns);
  bindTaskResults();
  document.querySelector("#task-runs").addEventListener("click", event => {
    if (event.target.closest("[data-task-result]")) return;
    const row = event.target.closest("[data-task-run]");
    if (!row) return;
    const run = runs.find(item => item.id === row.dataset.taskRun) || null;
    state.taskRunOrigin = {route: "admin-user-tasks", owner: userId};
    void renderTaskRun(row.dataset.taskRun, run).catch(err => notify(err.message));
  });
  void hydrateTaskTitles();
}

async function renderAdminUserTaskAccess(userId) {
  state.route = "admin-user-task-access";
  state.adminTaskAccessOwner = userId;
  const tasks = await api(`/api/admin/users/${encodeURIComponent(userId)}/tasks`);
  if (state.route !== "admin-user-task-access" || state.adminTaskAccessOwner !== userId) return;
  const rows = tasks.map(task => {
    const isPublic = task.visibility === "all";
    const badge = isPublic
      ? `<span class="badge green">Всем</span>`
      : task.is_published ? `<span class="badge">Опубликована</span>` : `<span class="badge amber">Черновик</span>`;
    return `<label class="list-item"><div class="list-item-head"><input type="checkbox" data-access-task="${esc(task.id)}" ${task.assigned ? "checked" : ""} ${isPublic ? "disabled" : ""}><strong>${esc(task.title)}</strong>${badge}</div><div class="meta">${esc(task.description || "")}</div></label>`;
  }).join("");
  const content = `<div class="page"><div class="page-head"><button class="btn" id="user-access-back" type="button">← Назад</button><h2>Доступ к таскам</h2><span class="spacer"></span><button class="btn btn-primary" id="save-user-access" type="button">Сохранить</button></div><p class="hint">Отметьте таски, которые видит пользователь. Таски «Всем» доступны всем и здесь не снимаются — меняйте режим в «Настройке тасок» или в матрице «Доступ к таскам».</p><div class="list">${rows || `<div class="empty">Тасок пока нет</div>`}</div></div>`;
  app.innerHTML = shell(content, "Админка · Доступ к таскам");
  bindShell();
  document.querySelector("#user-access-back").addEventListener("click", () => {
    void renderUsers().catch(err => notify(err.message));
  });
  document.querySelector("#save-user-access").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    const taskIds = [...document.querySelectorAll("[data-access-task]")]
      .filter(input => input.checked)
      .map(input => input.dataset.accessTask);
    try {
      await api(`/api/admin/users/${encodeURIComponent(userId)}/task-access`, {method: "PUT", body: JSON.stringify({task_ids: taskIds})});
      notify("Доступ обновлён");
      await renderAdminUserTaskAccess(userId);
    } catch (err) { button.disabled = false; notify(err.message); }
  });
}

async function renderTaskAccess() {
  state.route = "task-access";
  const [tasks, users] = await Promise.all([api("/api/admin/tasks"), api("/api/admin/users")]);
  if (state.route !== "task-access") return;
  const assignable = users.filter(user => user.role !== "admin");
  const header = assignable.map(user => `<th>${esc(user.login)}</th>`).join("");
  const rows = tasks.map(task => {
    const audience = new Set(task.audience_user_ids || []);
    const isPublic = task.visibility === "all";
    const cells = assignable.map(user => `<td><input type="checkbox" data-matrix-task="${esc(task.id)}" data-matrix-user="${esc(user.id)}" ${isPublic || audience.has(user.id) ? "checked" : ""} ${isPublic ? "disabled" : ""}></td>`).join("");
    return `<tr data-matrix-row="${esc(task.id)}"><td><strong>${esc(task.title)}</strong><div class="meta">${task.is_published ? "Опубликована" : "Черновик"}</div></td><td><label class="toolbar"><input type="checkbox" data-matrix-public="${esc(task.id)}" ${isPublic ? "checked" : ""}> Все</label></td>${cells}</tr>`;
  }).join("");
  const content = `<div class="page"><div class="page-head"><h2>Доступ к таскам</h2><span class="spacer"></span><button class="btn btn-primary" id="save-matrix" type="button">Сохранить</button></div><p class="hint">Строка — таска, столбец — пользователь. «Все» — таска доступна всем; иначе отметьте конкретных пользователей.</p><div class="table-wrap"><table><thead><tr><th>Таска</th><th>Все</th>${header}</tr></thead><tbody>${rows || `<tr><td colspan="${assignable.length + 2}"><div class="empty">Тасок пока нет</div></td></tr>`}</tbody></table></div></div>`;
  app.innerHTML = shell(content, "Админка · Доступ к таскам");
  bindShell();
  document.querySelectorAll("[data-matrix-public]").forEach(box => box.addEventListener("change", () => {
    const taskId = box.dataset.matrixPublic;
    const row = document.querySelector(`tr[data-matrix-row="${taskId}"]`);
    row?.querySelectorAll(`[data-matrix-task="${taskId}"]`).forEach(input => {
      input.disabled = box.checked;
      if (box.checked) input.checked = true;
    });
  }));
  document.querySelector("#save-matrix").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await Promise.all(tasks.map(task => {
        const row = document.querySelector(`tr[data-matrix-row="${task.id}"]`);
        const isPublic = row?.querySelector(`[data-matrix-public="${task.id}"]`)?.checked;
        const userIds = [...(row?.querySelectorAll(`[data-matrix-task="${task.id}"]`) || [])]
          .filter(input => input.checked)
          .map(input => input.dataset.matrixUser);
        return api(`/api/admin/tasks/${encodeURIComponent(task.id)}/audience`, {method: "PUT", body: JSON.stringify({visibility: isPublic ? "all" : "selected", user_ids: isPublic ? [] : userIds})});
      }));
      notify("Доступ обновлён");
      await renderTaskAccess();
    } catch (err) { button.disabled = false; notify(err.message); }
  });
}

function queuedTaskTitle(job) {
  const title = job.task_title || "";
  const sessionTitle = (job.chat_title || "").trim();
  return sessionTitle || title;
}

function queueJobAction(job) {
  if (job.status === "cancelling" || state.stoppingJobs.has(job.id)) {
    return `<button class="btn" disabled>Останавливается…</button>${job.error_code === "stop_unconfirmed" ? `<div class="hint">Остановка пока не подтверждена. Повторяем автоматически.</div>` : ""}`;
  }
  if (!["queued", "dispatching", "running", "waiting_permission", "waiting_question"].includes(job.status)) return "—";
  return `<button class="btn btn-danger" type="button" data-stop-job="${esc(job.id)}">${job.status === "queued" ? "Отменить" : "Остановить"}</button>`;
}

async function renderQueue() {
  const queue = await api("/api/admin/queue");
  const queues = queue.queues || [{id: "default", name: "Основная", priority: 0, max_workers: queue.hard_limit, status: "active", is_system: 1, active: queue.active, queued: queue.queued}];
  const activeQueues = queues.filter(item => item.status === "active");
  const rows = queue.jobs.map(job => `<tr><td>${job.sequence}</td><td>${esc(job.queue_name || "Основная")}</td><td><strong>${esc(job.effective_login)}</strong>${job.actor_login !== job.effective_login ? `<div class="badge amber">Отправил ${esc(job.actor_login)}</div>` : ""}</td><td>${job.task_id ? `<span class="badge">Таска</span> <span data-task-run-title="${esc(job.id)}">${esc(queuedTaskTitle(job))}</span>` : `${job.consultation ? `<span class="badge">Консультация</span> ` : ""}${esc(job.chat_title)}`}</td><td>${esc((job.content || "").slice(0, 90))}</td><td>${esc(job.agent)}</td><td>${esc(job.model_id || "default")}</td><td>${jobBadge(job.status)}</td><td>${queueJobAction(job)}</td></tr>`).join("");
  const queueRows = queues.map(item => `<form class="toolbar queue-settings-row" data-queue-form="${esc(item.id)}"><input class="input" name="name" maxlength="120" value="${esc(item.name)}" ${item.status === "archived" ? "disabled" : ""} required><label>Приоритет <input class="input" name="priority" type="number" min="-1000000" max="1000000" value="${item.priority}" ${item.status === "archived" ? "disabled" : ""}></label><label>Воркеры <input class="input" name="max_workers" type="number" min="1" max="${queue.hard_limit}" value="${item.max_workers || queue.hard_limit}" ${item.status === "archived" ? "disabled" : ""}></label><span class="badge ${item.status === "active" ? "green" : "amber"}">${item.active || 0} активных · ${item.queued || 0} ожидают${item.status === "archived" ? " · архив" : ""}</span>${item.status === "active" ? `<button class="btn" type="submit">Сохранить</button>${item.is_system ? "" : `<button class="btn btn-danger" type="button" data-archive-queue="${esc(item.id)}">Архивировать</button>`}` : ""}</form>`).join("");
  const defaultOptions = activeQueues.map(item => `<option value="${esc(item.id)}" ${item.id === (queue.default_chat_queue_id || "default") ? "selected" : ""}>${esc(item.name)}</option>`).join("");
  app.innerHTML = shell(`<div class="page"><div class="stats"><div class="stat"><span>Активные сессии</span><strong>${queue.active} из ${queue.limit}</strong></div><div class="stat"><span>В очереди</span><strong>${queue.queued}</strong></div><div class="stat"><span>Лимит ENV</span><strong>${queue.env_limit}</strong></div></div><form class="toolbar" id="limit-form"><label>Общий лимит активных сессий</label><input class="input" type="number" name="value" min="1" max="${queue.hard_limit}" value="${queue.limit}"><button class="btn btn-primary">Применить</button><button class="btn" type="button" id="reset-limit">Сбросить к ENV</button></form><section class="card"><h3>Очереди</h3><p class="hint">Сначала заполняется очередь с наибольшим приоритетом. Лимит очереди не отменяет общий лимит и лимиты пользователей.</p><div class="queue-settings">${queueRows}</div><form class="toolbar" id="new-queue-form"><input class="input" name="name" maxlength="120" placeholder="Название очереди" required><label>Приоритет <input class="input" name="priority" type="number" min="-1000000" max="1000000" value="0"></label><label>Воркеры <input class="input" name="max_workers" type="number" min="1" max="${queue.hard_limit}" value="1"></label><button class="btn btn-primary">Добавить очередь</button></form><form class="toolbar" id="default-chat-queue-form"><label>Очередь обычных диалогов</label><select class="input" name="queue_id" required>${defaultOptions}</select><button class="btn">Сохранить</button></form></section><section class="card"><h3>Подключённые MCP по активным экземплярам OpenCode</h3><div id="admin-mcp"><span class="hint">Загрузка…</span></div></section><div class="table-wrap"><table><thead><tr><th>№</th><th>Очередь</th><th>Пользователь</th><th>Диалог / таска</th><th>Сообщение</th><th>Режим</th><th>Модель</th><th>Статус</th><th>Действия</th></tr></thead><tbody>${rows}</tbody></table></div></div>`, "Админка · Очереди"); bindShell();
  document.querySelectorAll("[data-stop-job]").forEach(button => button.addEventListener("click", async () => {
    const job = queue.jobs.find(item => item.id === button.dataset.stopJob);
    if (!job || state.stoppingJobs.has(job.id)) return;
    const title = job.task_id ? queuedTaskTitle(job) : job.chat_title;
    const action = job.status === "queued" ? "Отменить" : "Остановить";
    if (!confirm(`${action} ${job.task_id ? "таску" : "итерацию"} «${title}» пользователя ${job.effective_login}?`)) return;
    state.stoppingJobs.add(job.id);
    button.disabled = true;
    button.textContent = "Останавливается…";
    try {
      const result = await api(`/api/admin/jobs/${encodeURIComponent(job.id)}/stop`, {method: "POST"});
      notify(result.status === "cancelled" ? "Задание отменено" : result.status === "cancelling" ? "Остановка запрошена" : "Задание уже завершено");
    } catch (err) {
      notify(err.message);
    } finally {
      state.stoppingJobs.delete(job.id);
      if (state.route === "queue") {
        try { await renderQueue(); } catch (err) { button.disabled = false; button.textContent = action; notify(err.message); }
      }
    }
  }));
  hydrateAdminMcp();
  void hydrateTaskTitles();
  document.querySelector("#limit-form").addEventListener("submit", async event => { event.preventDefault(); const value = Number(new FormData(event.currentTarget).get("value")); try { await api("/api/admin/settings/concurrency", {method:"PATCH", body:JSON.stringify({value})}); await renderQueue(); } catch(err) { notify(err.message); } });
  document.querySelector("#reset-limit").addEventListener("click", async () => { await api("/api/admin/settings/concurrency/override", {method:"DELETE"}); await renderQueue(); });
  document.querySelectorAll("[data-queue-form]").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    data.priority = Number(data.priority); data.max_workers = Number(data.max_workers);
    try { await api(`/api/admin/queues/${encodeURIComponent(form.dataset.queueForm)}`, {method: "PATCH", body: JSON.stringify(data)}); notify("Очередь сохранена"); await renderQueue(); } catch (err) { notify(err.message); }
  }));
  document.querySelector("#new-queue-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
    data.priority = Number(data.priority); data.max_workers = Number(data.max_workers);
    try { await api("/api/admin/queues", {method: "POST", body: JSON.stringify(data)}); notify("Очередь добавлена"); await renderQueue(); } catch (err) { notify(err.message); }
  });
  document.querySelector("#default-chat-queue-form").addEventListener("submit", async event => {
    event.preventDefault();
    try { await api("/api/admin/settings/default-chat-queue", {method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget)))}); notify("Очередь диалогов сохранена"); await renderQueue(); } catch (err) { notify(err.message); }
  });
  document.querySelectorAll("[data-archive-queue]").forEach(button => button.addEventListener("click", async () => {
    if (!confirm("Архивировать очередь? Сначала снимите назначения с тасок и настроек.")) return;
    try { await api(`/api/admin/queues/${encodeURIComponent(button.dataset.archiveQueue)}`, {method: "DELETE"}); notify("Очередь архивирована"); await renderQueue(); } catch (err) { notify(err.message); }
  }));
}

async function hydrateAdminMcp() {
  const target = document.querySelector("#admin-mcp");
  if (!target) return;
  try {
    const workspaces = await api("/api/admin/mcp");
    target.innerHTML = workspaces.length ? workspaces.map(item => `<div class="mcp-workspace"><div class="mono meta">${esc(item.workspace)}</div>${item.unavailable ? `<span class="error">Статус недоступен</span>` : item.servers.map(server => `<div class="mcp-row"><span>${esc(server.name)}</span><span class="badge ${server.status === "connected" ? "green" : "amber"}">${esc(server.status)}</span></div>`).join("") || `<span class="hint">MCP не подключены</span>`}</div>`).join("") : `<span class="hint">Экземпляры OpenCode ещё не запущены</span>`;
  } catch (err) { target.innerHTML = `<span class="error">${esc(err.message)}</span>`; }
}

function statisticsDuration(seconds) {
  if (seconds == null) return "Нет данных";
  const rounded = Math.round(seconds);
  if (rounded < 60) return `${rounded} с`;
  if (rounded < 3600) return `${Math.floor(rounded / 60)} мин ${rounded % 60} с`;
  return `${Math.floor(rounded / 3600)} ч ${Math.floor((rounded % 3600) / 60)} мин`;
}

async function renderStatistics() {
  const request = ++state.statisticsRequest;
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const params = new URLSearchParams({timezone});
  if (state.statisticsDay) params.set("day", state.statisticsDay);
  const data = await api(`/api/admin/statistics?${params}`);
  if (state.route !== "statistics" || request !== state.statisticsRequest) return;
  app.innerHTML = shell(`<div class="page">
    <form class="toolbar" id="statistics-form">
      <label for="statistics-day">Дата завершения</label>
      <input class="input" type="date" id="statistics-day" name="day" value="${esc(data.day)}" required>
      <button class="btn btn-primary">Обновить</button>
      <button class="btn" type="button" id="statistics-today">Сегодня</button>
    </form>
    <p class="hint">Все пользователи · Часовой пояс: ${esc(data.timezone)}</p>
    <div class="stats statistics-cards">
      <div class="stat"><span>Среднее время без очереди за день</span><strong>${statisticsDuration(data.average_execution_seconds)}</strong><span>От запуска до завершения · ${data.execution_samples} задач</span></div>
      <div class="stat"><span>Среднее время с очередью за день</span><strong>${statisticsDuration(data.average_total_seconds)}</strong><span>От постановки в очередь до завершения · ${data.total_samples} задач</span></div>
      <div class="stat"><span>Решено за день</span><strong>${data.completed_day}</strong></div>
      <div class="stat"><span>Решено за всё время</span><strong>${data.completed_all_time}</strong></div>
    </div>
    <p class="hint">Учитываются успешные запуски тасок по дате завершения. Ошибки, отмены, диалоги и консультации не включены. Повторные запуски и этапы цепочек считаются отдельно. Задачи без корректного времени не входят в соответствующее среднее.</p>
  </div>`, "Статистика");
  bindShell();
  document.querySelector("#statistics-form").addEventListener("submit", async event => {
    event.preventDefault();
    state.statisticsDay = new FormData(event.currentTarget).get("day");
    try { await renderStatistics(); } catch (err) { notify(err.message); }
  });
  document.querySelector("#statistics-today").addEventListener("click", async () => {
    state.statisticsDay = null;
    try { await renderStatistics(); } catch (err) { notify(err.message); }
  });
}

async function renderAudit() {
  const audit = await api("/api/admin/audit");
  app.innerHTML = shell(`<div class="page"><div class="page-head"><h2>Аудит</h2></div><div class="table-wrap"><table><thead><tr><th>Время</th><th>Инициатор</th><th>От имени</th><th>Действие</th><th>Объект</th></tr></thead><tbody>${audit.map(item => `<tr><td>${esc(item.created_at)}</td><td>${esc(item.actor_login || "system")}</td><td>${esc(item.effective_login || "—")}</td><td class="mono">${esc(item.action)}</td><td>${esc(item.object_type || "")} ${esc(item.object_id || "")}</td></tr>`).join("")}</tbody></table></div></div>`, "Админка · Аудит"); bindShell();
}

function scheduleLiveRefresh(event) {
  if (state.route === "statistics" && ["job.updated", "task.updated"].includes(event.type)) {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => {
      if (state.route === "statistics") void renderStatistics().catch(err => notify(err.message));
    }, 250);
  }
  if (event.type === "consultation.updated") {
    void consultations.refresh().catch(err => notify(err.message));
    if (state.route === "queue") {
      clearTimeout(state.refreshTimer);
      state.refreshTimer = setTimeout(renderQueue, 250);
    }
    return;
  }
  if (event.type === "task.title.updated") {
    const ownTask = state.route === "tasks" && (!event.owner_user_id || event.owner_user_id === state.me.id);
    const userTask = state.route === "admin-user-tasks" && event.owner_user_id === state.adminTaskOwner;
    if (["admin-tasks", "queue"].includes(state.route) || ownTask || userTask) {
      patchTaskTitles([event.task]);
    }
    return;
  }
  if (event.type === "task.updated") {
    if (state.route === "tasks" && (!event.owner_user_id || event.owner_user_id === state.me.id)) {
      patchTaskRun(event.task);
      void hydrateTaskTitles();
    }
    if (state.route === "admin-tasks") void refreshTaskRuns();
    if (state.route === "admin-user-tasks" && event.owner_user_id === state.adminTaskOwner) void refreshTaskRuns();
    if (state.route === "queue") {
      clearTimeout(state.refreshTimer);
      state.refreshTimer = setTimeout(renderQueue, 250);
    }
    return;
  }
  if (state.me?.tasks_only) return;
  if (state.route === "chat" && state.selectedChat) {
    const sameJob = event.type === "job.updated" && event.job?.chat_id === state.selectedChat.id;
    const sameSession = event.type === "opencode.event" && event.session_id === state.selectedChat.opencode_session_id;
    const sameQuestion = event.type === "chat.questions.updated" && event.session_id === state.selectedChat.opencode_session_id;
    if (!sameJob && !sameSession && !sameQuestion) return;
    if (sameQuestion || sameSession) void state.questions?.refresh();
    if (sameSession) {
      state.eventLog.push(event.event);
      state.eventLog = state.eventLog.slice(-50);
      const target = document.querySelector("#event-log");
      if (target) target.innerHTML = renderEventLog();
    }
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => refreshChatLive(state.selectedChat.id), 120);
    return;
  }
  if (state.route === "queue" && event.type === "job.updated") {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(renderQueue, 250);
  } else if (state.route === "dialogs" && event.type === "job.updated") {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(renderDialogs, 250);
  } else if (state.route === "dialogs" && event.type === "opencode.event") {
    void hydrateChatTitles("/api/chats/title-sync", () => state.route === "dialogs");
  } else if (
    state.route === "admin-user-chats" &&
    state.adminChatOwner &&
    event.type === "opencode.event"
  ) {
    const userId = state.adminChatOwner;
    void hydrateChatTitles(
      `/api/admin/users/${encodeURIComponent(userId)}/chats/title-sync`,
      () => state.route === "admin-user-chats" && state.adminChatOwner === userId
    );
  }
}

function renderEventLog() {
  if (!state.eventLog.length) return `<span class="hint">Ожидание событий…</span>`;
  return state.eventLog.slice().reverse().map(event => {
    const payload = event.payload || event;
    return `<details><summary>${esc(payload.type || "event")}</summary><pre>${esc(JSON.stringify(event, null, 2))}</pre></details>`;
  }).join("");
}

function startEvents() {
  state.eventSource?.close();
  const source = new EventSource("/api/events");
  state.eventSource = source;
  source.onopen = () => { void refreshTaskRuns(); if (state.route === "chat") void state.questions?.refresh(); };
  source.onmessage = event => {
    try { scheduleLiveRefresh(JSON.parse(event.data)); } catch (_) { /* malformed event */ }
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && state.me) {
      setTimeout(startEvents, 1500);
    }
  };
}

async function boot() {
  try {
    const result = await api("/api/auth/me"); state.me = result.user; state.csrf = result.csrf_token; startEvents(); await renderLocation();
  } catch (_) { renderLogin(); }
}

window.addEventListener("popstate", () => {
  if (state.me) void renderLocation().catch(err => notify(err.message));
});

setInterval(() => {
  if (state.me && state.route === "chat") void state.questions?.refresh();
}, 3000);

boot();
