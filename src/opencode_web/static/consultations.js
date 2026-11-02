import {renderMarkdown} from "./markdown.js?v=1";
import {questionPanel} from "./questions.js?v=1";

export function consultationUI({api, shell, bindShell, state, app, esc, notify, navigate}) {
  let current = null;
  let currentMessages = [];
  let loading = false;
  let revision = 0;
  let modelRevision = 0;
  let questions = null;
  let questionWaiting = false;
  let currentStatus = "idle";
  let currentArchived = false;
  const isAdmin = () => state.route === "admin-consultations" || state.route === "admin-consultation";
  const status = value => value === "waiting_question"
    ? (isAdmin() ? "Ожидает ответа пользователя" : "Ожидает вашего ответа")
    : ({idle: "", queued: "В очереди", dispatching: "Подготовка окружения", running: "Готовится ответ", waiting_permission: "Готовится ответ", completed: "Ответ готов", failed: "Не удалось подготовить ответ. Попробуйте отправить вопрос ещё раз.", cancelled: "Запрос отменён", cancelling: "Запрос останавливается"})[value] || "";
  const prefix = admin => admin ? "/api/admin/consultations" : "/api/consultations";
  const listRows = (items, admin) => items.length ? items.map(item => `<button class="consultation-row" data-consultation="${esc(item.id)}"><strong>${esc(item.title)}</strong><span class="meta">${admin ? `${esc(item.owner_login)} · ` : ""}${esc(new Date(item.updated_at).toLocaleString())}</span><span class="meta">${esc(status(item.status))}</span></button>`).join("") : `<div class="empty">Диалогов пока нет</div>`;
  const messagesHTML = messages => messages.map((message, index) => `<article class="message ${message.role === "user" ? "user" : "assistant"}"><div class="message-text ${message.role === "assistant" ? "markdown-body" : ""}">${message.role === "assistant" ? renderMarkdown(message.text) : esc(message.text)}</div>${message.role === "assistant" ? `<div class="message-actions"><button class="message-copy" type="button" data-copy-message="${index}" aria-label="Копировать ответ агента">Копировать</button></div>` : message.status !== "completed" ? `<div class="meta" role="status">${esc(status(message.status))}</div>` : ""}</article>`).join("");
  const jobsHTML = jobs => (jobs || []).map(job => `<tr><td class="mono">${esc(job.id)}</td><td>${esc(job.queue || "Основная")}</td><td>${esc(job.model)}</td><td>${esc(job.agent)}</td><td>${esc(status(job.status))}</td></tr>`).join("");

  async function copyText(value) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return;
      } catch (_) { /* Fall back for denied clipboard access. */ }
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.readOnly = true;
    textarea.className = "clipboard-fallback";
    document.body.append(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    let copied = false;
    try { copied = document.execCommand("copy"); }
    finally { textarea.remove(); }
    if (!copied) throw new Error("clipboard_unavailable");
  }

  function bindMessageCopy(target) {
    target.addEventListener("click", async event => {
      const button = event.target.closest("[data-copy-message]");
      if (!button || !target.contains(button)) return;
      const message = currentMessages[Number(button.dataset.copyMessage)];
      if (!message || message.role !== "assistant") return;
      button.disabled = true;
      try {
        await copyText(String(message.text || ""));
        if (button.isConnected) button.textContent = "Скопировано";
        notify("Ответ скопирован");
      } catch (_) {
        notify("Не удалось скопировать ответ");
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
  }

  function bindRows(admin) {
    app.querySelectorAll("[data-consultation]").forEach(button => button.addEventListener("click", () => {
      const prefix = admin ? "admin-consultations" : "consultations";
      void navigate(`/${prefix}/${encodeURIComponent(button.dataset.consultation)}`).catch(error => notify(error.message));
    }));
  }

  function updateComposer() {
    const form = app.querySelector("#consultation-send");
    if (!form) return;
    const waiting = questionWaiting || currentStatus === "waiting_question";
    form.hidden = waiting;
    form.querySelectorAll("textarea, button").forEach(control => { control.disabled = waiting || currentArchived; });
    const hint = app.querySelector("#consultation-archive");
    if (hint) hint.hidden = !currentArchived;
  }

  async function refreshModels() {
    const version = ++modelRevision;
    const select = app.querySelector("#consultation-model");
    if (state.route !== "admin-consultations" || !select) return;
    const form = select.form;
    const button = form.querySelector("button[type=submit], button:not([type])");
    const hint = app.querySelector("#consultation-model-status");
    form.dataset.modelsLoading = "true";
    select.disabled = true;
    button.disabled = true;
    hint.textContent = "Обновляем список моделей…";
    try {
      const options = await api("/api/options");
      if (!select.isConnected || state.route !== "admin-consultations" || version !== modelRevision) return;
      const selected = select.value;
      const models = [...(options.models || [])];
      if (selected && !models.some(model => model.id === selected)) models.push({id: selected, name: selected});
      select.innerHTML = `<option value="">Выберите модель</option>${models.map(model => `<option value="${esc(model.id)}" ${model.id === selected ? "selected" : ""}>${esc(model.name)} · ${esc(model.id)}</option>`).join("")}`;
      hint.textContent = options.opencode_unavailable ? "Не удалось загрузить доступные модели. Повторите проверку после восстановления окружения." : "";
    } catch (_) {
      if (select.isConnected && version === modelRevision) hint.textContent = "Не удалось обновить список моделей.";
    } finally {
      if (select.isConnected && version === modelRevision) {
        delete form.dataset.modelsLoading;
        select.disabled = false;
        button.disabled = form.dataset.saving === "true";
      }
    }
  }

  async function list(admin = false) {
    const version = ++revision;
    const modelVersion = ++modelRevision;
    state.route = admin ? "admin-consultations" : "consultations";
    current = null;
    currentMessages = [];
    questions = null;
    questionWaiting = false;
    const [items, config, options, users, queueItems] = await Promise.all([
      api(prefix(admin)),
      admin ? api("/api/admin/settings/consultations") : null,
      admin ? api("/api/options") : null,
      admin ? api("/api/admin/users") : null,
      admin ? api("/api/admin/queues") : null,
    ]);
    if (version !== revision || state.route !== (admin ? "admin-consultations" : "consultations")) return;
    const models = modelVersion === modelRevision ? [...(options?.models || [])] : [];
    const queues = (queueItems || []).filter(queue => queue.status !== "archived");
    if (!queues.length) queues.push({id: "default", name: "Основная"});
    if (config?.model && !models.some(model => model.id === config.model)) models.push({id: config.model, name: config.model});
    const controls = admin ? `<section class="card"><h2>Настройки консультаций</h2><form id="consultation-settings" class="toolbar"><label for="consultation-model">Модель</label><select class="input" id="consultation-model" name="model" required><option value="">Выберите модель</option>${models.map(model => `<option value="${esc(model.id)}" ${model.id === config.model ? "selected" : ""}>${esc(model.name)} · ${esc(model.id)}</option>`).join("")}</select><span class="hint" id="consultation-model-status" role="status"></span><label for="consultation-queue">Очередь</label><select class="input" id="consultation-queue" name="queue_id" required>${queues.map(queue => `<option value="${esc(queue.id)}" ${queue.id === (config.queue_id || "default") ? "selected" : ""}>${esc(queue.name)}</option>`).join("")}</select><button class="btn btn-primary">Сохранить</button></form><p class="hint">Изменения применяются к следующим вопросам. Режим всегда Build.</p></section><form id="consultation-filter" class="toolbar"><input class="input" name="q" placeholder="Поиск по названию или пользователю" aria-label="Поиск диалогов"><select class="input" name="owner_id" aria-label="Пользователь"><option value="">Все пользователи</option>${users.map(user => `<option value="${esc(user.id)}">${esc(user.login)}</option>`).join("")}</select><button class="btn">Найти</button></form>` : `<section class="card"><h2>Новый диалог</h2><form id="consultation-new"><div class="field"><label for="consultation-question">Ваш вопрос</label><textarea class="input" id="consultation-question" name="message" rows="4" maxlength="100000" required placeholder="Задайте вопрос консультанту"></textarea></div><button class="btn btn-primary" ${state.me.workspace_path ? "" : "disabled"}>Начать диалог</button>${state.me.workspace_path ? "" : `<p class="hint">Для консультаций администратор должен назначить рабочую папку.</p>`}</form></section>`;
    app.innerHTML = shell(`<div class="page">${controls}<section class="card"><h2>${admin ? "Диалоги пользователей" : "Мои диалоги"}</h2><div id="consultation-list">${listRows(items, admin)}</div></section></div>`, admin ? "Админка · Консультации" : "Консультации");
    bindShell();
    bindRows(admin);
    app.querySelector("#consultation-new")?.addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const message = new FormData(form).get("message").trim();
      if (!message) return;
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const result = await api(prefix(false), {method: "POST", body: JSON.stringify({message})});
        if (form.isConnected) await navigate(`/consultations/${encodeURIComponent(result.id)}`);
      } catch (error) { notify(error.message); }
      finally { button.disabled = false; }
    });
    app.querySelector("#consultation-settings")?.addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector("button");
      form.dataset.saving = "true";
      button.disabled = true;
      try {
        await api("/api/admin/settings/consultations", {method: "PUT", body: JSON.stringify(Object.fromEntries(new FormData(form)))});
        notify("Настройки консультаций сохранены");
      } catch (error) { notify(error.message); }
      finally { delete form.dataset.saving; button.disabled = form.dataset.modelsLoading === "true"; }
    });
    app.querySelector("#consultation-filter")?.addEventListener("submit", event => {
      event.preventDefault();
      void refresh().catch(error => notify(error.message));
    });
    if (admin && modelVersion !== modelRevision) void refreshModels();
  }

  async function show(id, admin = false) {
    const version = ++revision;
    state.route = admin ? "admin-consultation" : "consultation";
    current = id;
    questions = null;
    questionWaiting = false;
    const data = await api(`${prefix(admin)}/${encodeURIComponent(id)}`);
    if (version !== revision || current !== id || !state.route.endsWith("consultation")) return;
    currentStatus = data.status;
    currentArchived = data.archived_reason === "execution_mode_changed";
    currentMessages = data.messages;
    const composer = admin ? `<p class="hint">Просмотр диалога пользователя ${esc(data.owner_login)}</p>` : `<p class="hint" id="consultation-archive" hidden>Диалог создан в другом режиме работы. Для продолжения нужен исходный режим.</p><div id="consultation-questions"></div><form id="consultation-send" class="composer"><textarea class="input" name="message" rows="3" maxlength="100000" required aria-label="Сообщение" placeholder="Продолжить диалог"></textarea><button class="btn btn-primary">Отправить</button></form>`;
    app.innerHTML = shell(`<div class="page consultation-page"><div class="toolbar"><button class="btn" id="consultation-back">← Все диалоги</button><span id="consultation-status" role="status">${esc(status(data.status))}</span></div><section class="card consultation-conversation"><div class="messages" id="consultation-messages">${messagesHTML(data.messages)}</div>${composer}</section>${admin ? `<section class="card"><h3>Задания консультации</h3><div class="table-wrap"><table><thead><tr><th>Задание</th><th>Очередь</th><th>Модель</th><th>Режим</th><th>Статус</th></tr></thead><tbody id="consultation-jobs">${jobsHTML(data.jobs)}</tbody></table></div></section>` : ""}</div>`, data.title);
    bindShell();
    app.querySelector("#consultation-back").addEventListener("click", () => {
      void navigate(admin ? "/admin-consultations" : "/consultations").catch(error => notify(error.message));
    });
    const messages = app.querySelector("#consultation-messages");
    bindMessageCopy(messages);
    messages.scrollTop = messages.scrollHeight;
    if (!admin) {
      questions = questionPanel(app.querySelector("#consultation-questions"), {
        fetchQuestions: () => api(`/api/consultations/${encodeURIComponent(id)}/questions`),
        reply: (questionId, answers) => api(`/api/consultations/${encodeURIComponent(id)}/questions/${encodeURIComponent(questionId)}/reply`, {method: "POST", body: JSON.stringify({answers})}),
        onWaiting: waiting => {
          questionWaiting = waiting;
          updateComposer();
          if (!waiting) void refresh().catch(error => notify(error.message));
        },
      });
      updateComposer();
      void questions.refresh();
    }
    app.querySelector("#consultation-send")?.addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const input = form.elements.message;
      const message = input.value.trim();
      if (!message) return;
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        await api(`${prefix(false)}/${encodeURIComponent(id)}/messages`, {method: "POST", body: JSON.stringify({message})});
        if (input.value.trim() === message) input.value = "";
        if (form.isConnected) await refresh();
      } catch (error) { notify(error.message); }
      finally { updateComposer(); }
    });
  }

  async function refresh() {
    if (loading || !["consultations", "consultation", "admin-consultations", "admin-consultation"].includes(state.route)) return;
    loading = true;
    const route = state.route;
    const id = current;
    const version = revision;
    try {
      if (route.endsWith("consultation") && id) {
        const data = await api(`${prefix(isAdmin())}/${encodeURIComponent(id)}`);
        if (route !== state.route || current !== id || version !== revision) return;
        const target = app.querySelector("#consultation-messages");
        if (!target) return;
        const bottom = target.scrollHeight - target.scrollTop - target.clientHeight < 96;
        currentMessages = data.messages;
        const html = messagesHTML(data.messages);
        if (target.innerHTML !== html) target.innerHTML = html;
        if (bottom) target.scrollTop = target.scrollHeight;
        currentStatus = data.status;
        currentArchived = data.archived_reason === "execution_mode_changed";
        app.querySelector("#consultation-status").textContent = status(data.status);
        updateComposer();
        if (!isAdmin()) void questions?.refresh();
        const jobs = app.querySelector("#consultation-jobs");
        if (jobs) jobs.innerHTML = jobsHTML(data.jobs);
      } else {
        const form = app.querySelector("#consultation-filter");
        const query = form ? `?${new URLSearchParams(new FormData(form))}` : "";
        const admin = isAdmin();
        const items = await api(prefix(admin) + query);
        if (route !== state.route || version !== revision) return;
        const target = app.querySelector("#consultation-list");
        if (!target) return;
        target.innerHTML = listRows(items, admin);
        bindRows(admin);
      }
    } finally { loading = false; }
  }
  // Reconcile missed SSE updates after reconnects without replacing a draft.
  setInterval(() => { if (state.me) void refresh().catch(() => {}); }, 5000);
  return {list, show, refresh, refreshModels};
}
