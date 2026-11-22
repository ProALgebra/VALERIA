const JOB_STATUSES = ["queued", "processing", "succeeded", "failed", "cancelled"];
const JOB_SOURCES = ["task", "vflow", "standalone"];
const JOB_METHODS = ["none", "local", "llm"];
const esc = (value = "") => String(value).replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[char]);
const REPAIR_MODES = [
  ["syntax_only", "Только синтаксис"],
  ["schema_safe", "Schema-safe"],
];
const PAGE_SIZE = 25;
const SETTINGS_DEFAULTS = {
  enabled: false,
  base_url: "",
  api_key_ref: "",
  model: "",
  local_repair_enabled: true,
  max_llm_attempts: 2,
  request_timeout_seconds: 45,
  max_input_bytes: 65536,
  max_output_bytes: 65536,
  max_output_tokens: 4096,
  temperature: 0,
  repair_mode: "syntax_only",
  on_processor_failure: "fail",
  concurrency_limit: 4,
  queue_limit: 1000,
  raw_output_retention_days: 30,
  attempt_retention_days: 90,
  version: 1,
};

const JOB_STATUS_LABELS = {
  queued: "В очереди",
  processing: "Обработка",
  succeeded: "Успешно",
  failed: "Ошибка",
  cancelled: "Отменено",
};
const METHOD_LABELS = {none: "Без repair", local: "Локально", llm: "LLM"};
const SOURCE_LABELS = {task: "Task", vflow: "VFlow", standalone: "Standalone"};

function statusBadge(status) {
  const kind = status === "succeeded" ? "green"
    : status === "failed" ? "red"
      : status === "processing" || status === "queued" ? "amber" : "";
  return `<span class="badge ${kind}">${esc(JOB_STATUS_LABELS[status] || status || "—")}</span>`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("ru-RU", {dateStyle: "short", timeStyle: "medium"});
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("ru-RU") : "—";
}

function prettyJson(value) {
  if (value === undefined || value === null) return "—";
  try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

function options(items, selected, labels = {}) {
  return items.map(value => `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(labels[value] || value)}</option>`).join("");
}

function checkboxField(id, name, label, checked, hint = "") {
  return `<div class="field jp-check-field"><label class="jp-check" for="${id}"><input id="${id}" name="${name}" type="checkbox"${checked ? " checked" : ""}> <span>${esc(label)}</span></label>${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>`;
}

function numberField(id, name, label, value, min, max, step = "1", hint = "") {
  return `<div class="field"><label for="${id}">${esc(label)}</label><input class="input" id="${id}" name="${name}" type="number" min="${min}" max="${max}" step="${step}" value="${esc(value)}" required>${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>`;
}

function textField(id, name, label, value, hint = "", type = "text", placeholder = "") {
  return `<div class="field"><label for="${id}">${esc(label)}</label><input class="input" id="${id}" name="${name}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off">${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>`;
}

function selectField(id, name, label, items, selected, labels = {}, hint = "") {
  return `<div class="field"><label for="${id}">${esc(label)}</label><select class="input" id="${id}" name="${name}">${options(items, selected, labels)}</select>${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>`;
}

function metricCard(label, value, hint = "") {
  return `<div class="stat"><span>${esc(label)}</span><strong>${esc(value)}</strong>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
}

export function jsonProcessorUI({api, shell, bindShell, state, app, esc, notify}) {
  let settingsSnapshot = null;
  let filters = {status: "", source_type: "", method: "", error_code: ""};
  let offset = 0;
  let focusAfterRender = null;
  let requestSequence = 0;

  function showMessage(text, kind = "status") {
    const node = document.querySelector("#jp-message");
    if (!node) return;
    node.hidden = !text;
    node.className = `jp-message ${kind === "error" ? "jp-message-error" : "jp-message-ok"}`;
    node.setAttribute("role", kind === "error" ? "alert" : "status");
    node.textContent = text || "";
  }

  function jobsQuery() {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    return params.toString();
  }

  async function load() {
    const [settings, providers, metrics, jobs] = await Promise.all([
      api("/api/admin/json-processor/settings"),
      api("/api/admin/json-processor/providers"),
      api("/api/admin/json-processor/metrics"),
      api(`/api/admin/json-processor/jobs?${jobsQuery()}`),
    ]);
    return {settings, providers, metrics, jobs};
  }

  function renderLoading() {
    app.innerHTML = shell(`<div class="page json-processor-page"><div class="empty" role="status" aria-live="polite">Загружаем JSON-процессор…</div></div>`, "JSON-процессор");
    bindShell();
  }

  function renderError(error) {
    app.innerHTML = shell(`<div class="page json-processor-page">
      <div class="card jp-load-error">
        <h2 id="jp-error-title" tabindex="-1">Не удалось загрузить JSON-процессор</h2>
        <p class="error">${esc(error.message || error)}</p>
        <button class="btn btn-primary" id="jp-retry" type="button">Повторить</button>
      </div>
    </div>`, "JSON-процессор");
    bindShell();
    document.querySelector("#jp-error-title")?.focus();
    document.querySelector("#jp-retry")?.addEventListener("click", () => {
      void render().catch(err => notify(err.message, "error"));
    });
  }

  function settingsForm(settings) {
    return `<form id="jp-settings-form" class="jp-settings-form" novalidate>
      <div class="jp-settings-top">
        <div>
          <h3>Настройки процессора</h3>
          <div class="meta">Версия ${esc(settings.version)} · обновлено ${esc(formatTime(settings.updated_at))}${settings.updated_by ? ` · ${esc(settings.updated_by)}` : ""}</div>
        </div>
        <span class="badge ${settings.enabled ? "green" : "amber"}" id="jp-enabled-badge">${settings.enabled ? "Включён" : "Выключен"}</span>
      </div>
      <div id="jp-message" class="jp-message" role="status" aria-live="polite" hidden></div>

      <fieldset class="jp-fieldset">
        <legend>Статус</legend>
        ${checkboxField("jp-enabled", "enabled", "Процессор включён", settings.enabled, "Выключенный процессор не выполняет repair; настройки остаются доступны администратору.")}
      </fieldset>

      <fieldset class="jp-fieldset">
        <legend>OpenAI-compatible провайдер</legend>
        <div class="jp-form-grid">
          ${textField("jp-base-url", "base_url", "Base URL", settings.base_url || "", "Например, https://api.openai.com или локальный /v1 endpoint. Без query и credentials.", "url", "https://provider.example")}
          ${textField("jp-model", "model", "Модель", settings.model || "", "Идентификатор модели для Chat Completions.", "text", "gpt-4o-mini")}
          ${textField("jp-api-key-ref", "api_key_ref", "Ссылка на API-ключ (api_key_ref)", settings.api_key_ref || "", "Только opaque reference в secret storage. Сам ключ в интерфейсе и настройках не хранится.", "text", "vault://json-processor/openai")}
        </div>
        <div class="jp-inline-actions">
          <button class="btn" id="jp-check-connection" type="button">Проверить соединение</button>
          <span class="hint">Проверка использует значения формы и не сохраняет их.</span>
        </div>
        <div id="jp-connection-result" class="jp-check-result" role="status" aria-live="polite"></div>
      </fieldset>

      <fieldset class="jp-fieldset">
        <legend>Repair и ограничения</legend>
        <div class="jp-form-grid">
          ${selectField("jp-repair-mode", "repair_mode", "Режим repair", REPAIR_MODES.map(item => item[0]), settings.repair_mode, Object.fromEntries(REPAIR_MODES), "schema_safe разрешает только заранее заданные безопасные преобразования.")}
          ${selectField("jp-failure-policy", "on_processor_failure", "Политика ошибки", ["fail"], settings.on_processor_failure, {}, "Поддерживается только fail-closed.")}
          ${numberField("jp-max-attempts", "max_llm_attempts", "Максимум LLM-попыток", settings.max_llm_attempts, 0, 3)}
          ${numberField("jp-timeout", "request_timeout_seconds", "Timeout запроса, секунд", settings.request_timeout_seconds, 1, 300)}
          ${numberField("jp-temperature", "temperature", "Temperature", settings.temperature, 0, 2, "any")}
          ${numberField("jp-max-input", "max_input_bytes", "Максимум входных байт", settings.max_input_bytes, 1, 1048576)}
          ${numberField("jp-max-output", "max_output_bytes", "Максимум выходных байт", settings.max_output_bytes, 1, 1048576)}
          ${numberField("jp-max-tokens", "max_output_tokens", "Максимум output tokens", settings.max_output_tokens, 1, 32768)}
        </div>
        <div class="jp-local-toggle">${checkboxField("jp-local-repair-checkbox", "local_repair_enabled", "Разрешить детерминированный локальный repair", settings.local_repair_enabled, "Trailing commas, markdown fences, безопасные кавычки и value-preserving литералы.")}</div>
      </fieldset>

      <fieldset class="jp-fieldset">
        <legend>Очередь и retention</legend>
        <div class="jp-form-grid">
          ${numberField("jp-concurrency", "concurrency_limit", "Concurrency limit", settings.concurrency_limit, 1, 32)}
          ${numberField("jp-queue-limit", "queue_limit", "Queue limit", settings.queue_limit, 1, 100000)}
          ${numberField("jp-raw-retention", "raw_output_retention_days", "Raw output retention, дней", settings.raw_output_retention_days, 1, 3650)}
          ${numberField("jp-attempt-retention", "attempt_retention_days", "Attempt retention, дней", settings.attempt_retention_days, 1, 3650)}
        </div>
      </fieldset>

      <div class="jp-inline-actions jp-save-row">
        <button class="btn btn-primary" id="jp-save-settings" type="submit">Сохранить настройки</button>
        <span class="hint">Сохранение optimistic: версия ${esc(settings.version)}; при конфликте изменения не применяются.</span>
        <span id="jp-save-status" class="hint" role="status" aria-live="polite"></span>
      </div>
    </form>`;
  }

  function sandboxSection() {
    return `<section class="card jp-card" aria-labelledby="jp-sandbox-title">
      <div class="jp-section-head">
        <div><h3 id="jp-sandbox-title">Sandbox</h3><div class="hint">Проверка raw output без Task/VFlow и без создания repair job.</div></div>
        <span class="badge">Без side effects</span>
      </div>
      <form id="jp-sandbox-form" class="jp-sandbox-form" novalidate>
        <div class="field"><label for="jp-sandbox-raw">Raw output</label><textarea class="input mono" id="jp-sandbox-raw" name="raw_output" rows="8" placeholder='{"ok": true,}'></textarea><span class="hint">Вход не изменяется и не сохраняется.</span></div>
        <div class="field"><label for="jp-sandbox-schema">JSON Schema (необязательно)</label><textarea class="input mono" id="jp-sandbox-schema" name="schema" rows="5" placeholder='{"type":"object","properties":{"ok":{"type":"boolean"}}}'></textarea><span class="hint">Используется безопасное подмножество JSON Schema.</span></div>
        <div class="jp-inline-actions"><button class="btn btn-primary" id="jp-run-sandbox" type="submit">Проверить</button><span class="hint">Максимум попыток и провайдер берутся из сохранённых настроек.</span></div>
      </form>
      <div id="jp-sandbox-result" class="jp-result" role="region" aria-live="polite" aria-label="Результат sandbox"><div class="hint">Результат появится здесь.</div></div>
    </section>`;
  }

  function metricsSection(metrics) {
    const jobs = metrics.jobs || {};
    const attempts = metrics.attempts || {};
    const queue = metrics.queue || {};
    const tokens = attempts.tokens || {};
    const successRate = jobs.success_rate === null || jobs.success_rate === undefined
      ? "—"
      : `${Math.round(Number(jobs.success_rate) * 100)}%`;
    const failures = metrics.failures || {};
    const latency = attempts.latency || {};
    const p50 = latency.p50_ms ?? attempts.p50_latency_ms;
    const p95 = latency.p95_ms ?? attempts.p95_latency_ms;
    const retention = metrics.retention || {};
    return `<section class="card jp-card" aria-labelledby="jp-metrics-title">
      <div class="jp-section-head"><div><h3 id="jp-metrics-title">Метрики</h3><div class="hint">Настройки v${esc(metrics.settings?.version ?? "—")} · ${metrics.settings?.enabled ? "процессор включён" : "процессор выключен"}</div></div></div>
      <div class="stats jp-metrics">
        ${metricCard("Всего jobs", formatNumber(jobs.total), `${formatNumber(jobs.terminal)} завершено`)}
        ${metricCard("В очереди", formatNumber(queue.queued), `${formatNumber(queue.processing)} в обработке`)}
        ${metricCard("Успешно", formatNumber(jobs.succeeded), `за 24 ч: ${formatNumber(metrics.last_24h?.succeeded)}`)}
        ${metricCard("Ошибки", formatNumber(jobs.failed), `за 24 ч: ${formatNumber(metrics.last_24h?.failed)}`)}
        ${metricCard("Успешность", successRate, "от завершённых jobs")}
        ${metricCard("Попытки", formatNumber(attempts.total), `local: ${formatNumber(attempts.by_method?.local)} · llm: ${formatNumber(attempts.by_method?.llm)}`)}
        ${metricCard("Задержка p50 / p95", p50 == null && p95 == null ? "—" : `${p50 == null ? "—" : formatNumber(p50)} / ${p95 == null ? "—" : formatNumber(p95)} мс`, `средняя: ${attempts.avg_latency_ms == null ? "—" : `${formatNumber(attempts.avg_latency_ms)} мс`}`)}
        ${metricCard("Токены", formatNumber(tokens.total_tokens), `prompt: ${formatNumber(tokens.prompt_tokens)} · completion: ${formatNumber(tokens.completion_tokens)}`)}
      </div>
      <div class="hint">Ошибки: parse ${formatNumber(failures.parse)} · schema ${formatNumber(failures.schema)} · content ${formatNumber(failures.content)} · provider ${formatNumber(failures.provider_errors ?? failures.provider)}. За 24 ч создано jobs: ${formatNumber(metrics.last_24h?.jobs_created)}, provider errors: ${formatNumber(metrics.last_24h?.provider_errors)}.</div>
      <div class="jp-settings-top jp-retention-row">
        <div>
          <strong>Retention</strong>
          <div class="hint">Raw/repaired output ${formatNumber(retention.raw_output_retention_days)} дн. · attempts ${formatNumber(retention.attempt_retention_days)} дн. · к удалению: raw ${formatNumber(retention.jobs_with_expired_raw_output)} · attempts ${formatNumber(retention.expired_attempts)}.</div>
        </div>
        <div class="jp-inline-actions">
          <button class="btn" id="jp-purge-retention" type="button">Удалить устаревшие</button>
          <span id="jp-retention-status" class="hint" role="status" aria-live="polite"></span>
        </div>
      </div>
    </section>`;
  }

  function jobsSection(jobs) {
    const rows = jobs.items?.length ? jobs.items.map(job => `<tr>
      <td>${statusBadge(job.status)}</td>
      <td><span class="mono">${esc(job.id)}</span><div class="meta">${esc(SOURCE_LABELS[job.source_type] || job.source_type)}${job.task_run_id ? ` · ${esc(job.task_run_id)}` : ""}${job.vflow_run_id ? ` · ${esc(job.vflow_run_id)}` : ""}</div></td>
      <td>${esc(METHOD_LABELS[job.method] || job.method || "—")}</td>
      <td>${formatNumber(job.attempts)}</td>
      <td>${job.error_code ? `<span class="badge red">${esc(job.error_code)}</span>${job.error_message ? `<div class="meta">${esc(job.error_message)}</div>` : ""}` : `<span class="meta">—</span>`}</td>
      <td class="meta">${esc(formatTime(job.created_at))}</td>
      <td class="meta">${esc(formatTime(job.updated_at))}</td>
      <td><button class="btn" type="button" data-jp-job="${esc(job.id)}">Детали</button></td>
    </tr>`).join("") : `<tr><td colspan="8"><div class="empty">Jobs не найдены</div></td></tr>`;
    const start = jobs.total ? jobs.offset + 1 : 0;
    const end = jobs.offset + (jobs.items?.length || 0);
    return `<section class="card jp-card" aria-labelledby="jp-jobs-title">
      <div class="jp-section-head"><div><h3 id="jp-jobs-title" tabindex="-1">Repair jobs</h3><div class="hint">Список не содержит raw output. Детали доступны администратору.</div></div><span class="badge">${formatNumber(jobs.total)}</span></div>
      <form id="jp-job-filters" class="jp-filters">
        <div class="field"><label for="jp-filter-status">Статус</label><select class="input" id="jp-filter-status" name="status"><option value="">Все</option>${options(JOB_STATUSES, filters.status, JOB_STATUS_LABELS)}</select></div>
        <div class="field"><label for="jp-filter-source">Источник</label><select class="input" id="jp-filter-source" name="source_type"><option value="">Все</option>${options(JOB_SOURCES, filters.source_type, SOURCE_LABELS)}</select></div>
        <div class="field"><label for="jp-filter-method">Метод</label><select class="input" id="jp-filter-method" name="method"><option value="">Все</option>${options(JOB_METHODS, filters.method, METHOD_LABELS)}</select></div>
        <div class="field"><label for="jp-filter-error">Код ошибки</label><input class="input" id="jp-filter-error" name="error_code" value="${esc(filters.error_code)}" placeholder="NOT_REPAIRABLE"></div>
        <div class="jp-filter-actions"><button class="btn btn-primary" type="submit">Применить</button><button class="btn btn-ghost" id="jp-filter-reset" type="button">Сбросить</button></div>
      </form>
      <div class="table-wrap"><table><thead><tr><th scope="col">Статус</th><th scope="col">Job / источник</th><th scope="col">Метод</th><th scope="col">Попытки</th><th scope="col">Ошибка</th><th scope="col">Создан</th><th scope="col">Обновлён</th><th scope="col">Действие</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="jp-pagination"><span class="meta">${formatNumber(start)}–${formatNumber(end)} из ${formatNumber(jobs.total)}</span><span class="spacer"></span><button class="btn" id="jp-page-prev" type="button" ${jobs.offset > 0 ? "" : "disabled"}>Назад</button><button class="btn" id="jp-page-next" type="button" ${jobs.has_more ? "" : "disabled"}>Вперёд</button></div>
      <div id="jp-job-detail" class="jp-job-detail" role="region" aria-live="polite" aria-label="Детали repair job"></div>
    </section>`;
  }

  function renderPage(data) {
    const {settings: rawSettings, providers, metrics, jobs} = data;
    const settings = {...SETTINGS_DEFAULTS, ...(rawSettings || {})};
    settingsSnapshot = settings;
    const providerNote = providers?.current?.provider_configured
      ? `Провайдер настроен: ${providers.current.base_url} · ${providers.current.model}`
      : "Провайдер ещё не настроен.";
    const enabledText = settings.enabled ? "Процессор включён" : "Процессор выключен";
    app.innerHTML = shell(`<div class="page json-processor-page">
      <div class="page-head">
        <h2 id="jp-title" tabindex="-1">JSON-процессор</h2>
        <span class="badge ${settings.enabled ? "green" : "amber"}">${esc(enabledText)}</span>
        <span class="spacer"></span>
        <button class="btn" id="jp-refresh" type="button">Обновить</button>
      </div>
      <div class="hint jp-provider-note">${esc(providerNote)}</div>
      ${settingsForm(settings)}
      ${sandboxSection()}
      ${metricsSection(metrics)}
      ${jobsSection(jobs)}
    </div>`, "JSON-процессор");
    bindShell();
    bindSettings(settings);
    bindSandbox();
    bindJobs();
    bindRetention();
    document.querySelector("#jp-refresh")?.addEventListener("click", () => {
      focusAfterRender = "jp-refresh";
      void render().catch(err => notify(err.message, "error"));
    });
    if (focusAfterRender) {
      const target = document.getElementById(focusAfterRender);
      focusAfterRender = null;
      target?.focus({preventScroll: true});
    }
  }

  function bindSettings(settings) {
    const form = document.querySelector("#jp-settings-form");
    if (!form) return;
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = form.querySelector("#jp-save-settings");
      const status = form.querySelector("#jp-save-status");
      button.disabled = true;
      if (status) status.textContent = "Сохраняем…";
      try {
        const changes = collectSettings(form);
        const updated = await api("/api/admin/json-processor/settings", {
          method: "PATCH",
          body: JSON.stringify({expected_version: settingsSnapshot.version, changes}),
        });
        settingsSnapshot = updated;
        showMessage(`Настройки сохранены · версия ${updated.version}`, "status");
        if (status) status.textContent = "Сохранено";
        notify(`Настройки сохранены · версия ${updated.version}`);
        focusAfterRender = "jp-save-settings";
        await render();
      } catch (err) {
        const message = err.status === 409
          ? `Конфликт версий: настройки уже изменены другим администратором. ${err.message}`
          : err.message;
        showMessage(message, "error");
        if (status) status.textContent = "Не сохранено";
        notify(message, "error");
      } finally {
        button.disabled = false;
      }
    });
    form.querySelector("#jp-check-connection")?.addEventListener("click", async event => {
      const button = event.currentTarget;
      const target = form.querySelector("#jp-connection-result");
      button.disabled = true;
      target.className = "jp-check-result";
      target.innerHTML = `<span class="hint">Проверяем соединение…</span>`;
      try {
        const payload = await api("/api/admin/json-processor/check-connection", {
          method: "POST",
          body: JSON.stringify({
            base_url: form.elements.namedItem("base_url").value,
            api_key_ref: form.elements.namedItem("api_key_ref").value,
            model: form.elements.namedItem("model").value,
          }),
        });
        if (payload.ok) {
          const availability = payload.model_available === true
            ? "модель доступна"
            : payload.model_available === false ? "модель отсутствует в /v1/models" : "список моделей не предоставлен";
          target.className = "jp-check-result jp-check-ok";
          target.innerHTML = `<strong>Соединение установлено</strong><div class="meta">${esc(payload.model)} · HTTP ${esc(payload.status_code)} · ${esc(payload.latency_ms)} мс · ${esc(availability)}</div>`;
        } else {
          const error = payload.error || {};
          target.className = "jp-check-result jp-check-error";
          target.innerHTML = `<strong>Соединение не установлено</strong><div class="meta">${esc(error.message || "Ошибка провайдера")}${error.status_code ? ` · HTTP ${esc(error.status_code)}` : ""}</div>`;
        }
      } catch (err) {
        target.className = "jp-check-result jp-check-error";
        target.innerHTML = `<strong>Проверка не выполнена</strong><div class="meta">${esc(err.message)}</div>`;
        notify(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  function collectSettings(form) {
    const value = name => form.elements.namedItem(name)?.value ?? "";
    const number = name => Number(value(name));
    return {
      enabled: Boolean(form.elements.namedItem("enabled")?.checked),
      base_url: value("base_url").trim(),
      api_key_ref: value("api_key_ref").trim(),
      model: value("model").trim(),
      local_repair_enabled: Boolean(form.elements.namedItem("local_repair_enabled")?.checked),
      max_llm_attempts: number("max_llm_attempts"),
      request_timeout_seconds: number("request_timeout_seconds"),
      max_input_bytes: number("max_input_bytes"),
      max_output_bytes: number("max_output_bytes"),
      max_output_tokens: number("max_output_tokens"),
      temperature: number("temperature"),
      repair_mode: value("repair_mode"),
      on_processor_failure: value("on_processor_failure"),
      concurrency_limit: number("concurrency_limit"),
      queue_limit: number("queue_limit"),
      raw_output_retention_days: number("raw_output_retention_days"),
      attempt_retention_days: number("attempt_retention_days"),
    };
  }

  function bindSandbox() {
    const form = document.querySelector("#jp-sandbox-form");
    if (!form) return;
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector("#jp-run-sandbox");
      const result = document.querySelector("#jp-sandbox-result");
      const schemaField = form.querySelector("#jp-sandbox-schema");
      const rawField = form.querySelector("#jp-sandbox-raw");
      schemaField.removeAttribute("aria-invalid");
      let schema = null;
      if (schemaField.value.trim()) {
        try {
          schema = JSON.parse(schemaField.value);
        } catch (_) {
          schemaField.setAttribute("aria-invalid", "true");
          schemaField.focus();
          result.className = "jp-result jp-result-error";
          result.innerHTML = `<strong>Некорректная JSON Schema</strong><div class="meta">Исправьте JSON в поле схемы.</div>`;
          notify("Некорректная JSON Schema", "error");
          return;
        }
      }
      button.disabled = true;
      result.className = "jp-result";
      result.innerHTML = `<span class="hint">Выполняем repair…</span>`;
      try {
        const payload = await api("/api/admin/json-processor/test", {
          method: "POST",
          body: JSON.stringify({raw_output: rawField.value, schema}),
        });
        renderSandboxResult(payload, result);
      } catch (err) {
        result.className = "jp-result jp-result-error";
        result.innerHTML = `<strong>Sandbox не выполнен</strong><div class="meta">${esc(err.message)}</div>`;
        notify(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  function renderSandboxResult(payload, target) {
    const ok = Boolean(payload.ok);
    const attempts = (payload.attempts || []).map(attempt => `<li>
      <span class="badge ${attempt.ok ? "green" : "red"}">${esc(attempt.phase)}</span>
      ${esc(attempt.strategy || "—")} · кандидатов: ${esc(attempt.candidate_count)}
      ${attempt.schema_valid === false ? `<span class="badge red">schema</span>` : ""}
      ${attempt.normalizations?.length ? `<div class="meta">normalizations: ${esc(attempt.normalizations.join(", "))}</div>` : ""}
      ${attempt.validation_error ? `<div class="meta">${esc(attempt.validation_error)}</div>` : ""}
    </li>`).join("");
    const diagnostics = (payload.diagnostics || []).map(item => `<li><span class="mono">${esc(item.path || "$")}</span> · ${esc(item.code)} · ${esc(item.message)}</li>`).join("");
    let body;
    if (ok) {
      body = `<h4>Валидный JSON</h4><pre class="jp-json">${esc(prettyJson(payload.value))}</pre>`;
    } else {
      const error = payload.error || {};
      body = `<h4>Repair отклонён</h4><div class="jp-error-line"><span class="badge red">${esc(error.code || "ERROR")}</span> <span>${esc(error.message || "Не удалось восстановить JSON")}</span></div>${error.path ? `<div class="meta">Путь: <span class="mono">${esc(error.path)}</span></div>` : ""}${diagnostics ? `<ul class="jp-diagnostics">${diagnostics}</ul>` : ""}`;
    }
    target.className = `jp-result ${ok ? "jp-result-ok" : "jp-result-error"}`;
    target.innerHTML = `${body}
      <div class="jp-result-meta"><span class="badge ${ok ? "green" : "red"}">${ok ? "ok" : "fail"}</span><span class="meta">метод: ${esc(payload.method)} · режим: ${esc(payload.mode)} · вход: ${esc(formatNumber(payload.raw_input_bytes))} байт · settings v${esc(payload.settings_version)} · job не создан</span></div>
      ${attempts ? `<div><strong>Попытки</strong><ul class="jp-attempts">${attempts}</ul></div>` : `<div class="hint">LLM не вызывался: результат получен локально или вход уже валиден.</div>`}`;
  }

  async function showJobDetail(jobId) {
    const target = document.querySelector("#jp-job-detail");
    if (!target) return;
    target.innerHTML = `<div class="empty" role="status">Загружаем детали…</div>`;
    try {
      const job = await api(`/api/admin/json-processor/jobs/${encodeURIComponent(jobId)}`);
      const history = job.attempts_history || (Array.isArray(job.attempts) ? job.attempts : []);
      const attempts = history.length
        ? `<div class="table-wrap"><table><thead><tr><th scope="col">№</th><th scope="col">Метод</th><th scope="col">Провайдер / модель</th><th scope="col">Статус</th><th scope="col">Latency</th><th scope="col">Ошибка</th><th scope="col">Metadata</th></tr></thead><tbody>${history.map(attempt => `<tr>
          <td>${esc(attempt.attempt_no)}</td>
          <td>${esc(METHOD_LABELS[attempt.method] || attempt.method)}</td>
          <td>${esc(attempt.provider || "—")}<div class="meta">${esc(attempt.model || "")}</div></td>
          <td>${statusBadge(attempt.status)}</td>
          <td>${attempt.latency_ms == null ? "—" : `${esc(formatNumber(attempt.latency_ms))} мс`}</td>
          <td class="meta">${esc(attempt.validation_error || "—")}</td>
          <td><details><summary>JSON</summary><pre class="jp-json">${esc(prettyJson(attempt.request_meta))}</pre></details><div class="meta">tokens: ${esc(prettyJson(attempt.token_usage))}</div></td>
        </tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">Попыток не записано</div>`;
      target.innerHTML = `<div class="jp-detail-head"><div><h4>Job <span class="mono">${esc(job.id)}</span></h4><div class="meta">${esc(SOURCE_LABELS[job.source_type] || job.source_type)} · ${esc(job.raw_output_ref)} · settings v${esc(job.settings_version)}</div></div><button class="btn" id="jp-close-detail" type="button">Закрыть</button></div>
        <div class="jp-detail-grid">
          <div><span class="meta">Статус</span><div>${statusBadge(job.status)}</div></div>
          <div><span class="meta">Метод</span><div>${esc(METHOD_LABELS[job.method] || job.method)}</div></div>
          <div><span class="meta">Попытки</span><div>${esc(formatNumber(job.attempts_count ?? history.length))}</div></div>
          <div><span class="meta">Создан</span><div>${esc(formatTime(job.created_at))}</div></div>
          <div><span class="meta">Завершён</span><div>${esc(formatTime(job.finished_at))}</div></div>
          <div><span class="meta">Raw hash</span><div class="mono">${esc(job.raw_hash)}</div></div>
        </div>
        ${job.error_code ? `<div class="jp-error-line"><span class="badge red">${esc(job.error_code)}</span> <span>${esc(job.error_message || "")}</span></div>` : ""}
        <div class="jp-detail-columns">
          <div><h5>Результат</h5><pre class="jp-json">${esc(prettyJson(job.result))}</pre></div>
          <div><h5>Схема</h5><pre class="jp-json">${esc(prettyJson(job.schema))}</pre></div>
        </div>
        <h5>История попыток</h5>${attempts}`;
      document.querySelector("#jp-close-detail")?.addEventListener("click", () => {
        target.innerHTML = "";
        target.removeAttribute("tabindex");
      });
      target.setAttribute("tabindex", "-1");
      target.focus({preventScroll: true});
    } catch (err) {
      target.innerHTML = `<div class="jp-result jp-result-error"><strong>Не удалось загрузить job</strong><div class="meta">${esc(err.message)}</div></div>`;
      notify(err.message, "error");
    }
  }

  function bindJobs() {
    const form = document.querySelector("#jp-job-filters");
    form?.addEventListener("submit", event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      filters = {
        status: values.status || "",
        source_type: values.source_type || "",
        method: values.method || "",
        error_code: (values.error_code || "").trim(),
      };
      offset = 0;
      focusAfterRender = "jp-filter-status";
      void render().catch(err => notify(err.message, "error"));
    });
    document.querySelector("#jp-filter-reset")?.addEventListener("click", () => {
      filters = {status: "", source_type: "", method: "", error_code: ""};
      offset = 0;
      focusAfterRender = "jp-filter-status";
      void render().catch(err => notify(err.message, "error"));
    });
    document.querySelector("#jp-page-prev")?.addEventListener("click", () => {
      offset = Math.max(0, offset - PAGE_SIZE);
      focusAfterRender = "jp-jobs-title";
      void render().catch(err => notify(err.message, "error"));
    });
    document.querySelector("#jp-page-next")?.addEventListener("click", () => {
      offset += PAGE_SIZE;
      focusAfterRender = "jp-jobs-title";
      void render().catch(err => notify(err.message, "error"));
    });
    document.querySelectorAll("[data-jp-job]").forEach(button => {
      button.addEventListener("click", () => void showJobDetail(button.dataset.jpJob));
    });
  }

  function bindRetention() {
    const button = document.querySelector("#jp-purge-retention");
    const status = document.querySelector("#jp-retention-status");
    button?.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "Удалить устаревшие raw/repaired outputs и attempts согласно retention? "
        + "Активные jobs не затрагиваются, метаданные jobs сохраняются.",
      );
      if (!confirmed) return;
      button.disabled = true;
      if (status) status.textContent = "Удаляем…";
      try {
        const payload = await api("/api/admin/json-processor/retention/purge", {
          method: "POST",
          body: JSON.stringify({confirm: true, batch_size: 500}),
        });
        const message = `Удалено outputs: ${formatNumber(payload.jobs_output_purged)} · attempts: ${formatNumber(payload.attempts_deleted)}`;
        if (status) status.textContent = message;
        notify(message);
        focusAfterRender = "jp-purge-retention";
        await render();
      } catch (err) {
        if (status) status.textContent = "Не удалено";
        notify(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  async function render() {
    if (!state.me) return;
    if (state.me.role !== "admin") {
      notify("Требуется роль администратора", "error");
      return;
    }
    const sequence = ++requestSequence;
    const content = document.querySelector(".content");
    const scrollTop = content?.scrollTop || 0;
    if (!document.querySelector("#jp-settings-form")) renderLoading();
    let data;
    try {
      data = await load();
    } catch (err) {
      if (!state.me) return;
      if (state.route !== "admin-json-processor" || sequence !== requestSequence) return;
      renderError(err);
      return;
    }
    if (state.route !== "admin-json-processor" || sequence !== requestSequence) return;
    renderPage(data);
    if (content) content.scrollTop = scrollTop;
  }

  return {render};
}
