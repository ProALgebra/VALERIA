function parseJson(value, fallback) {
  if (value == null || value === "") return fallback;
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch (_) { return fallback; }
}

function statusLabel(status) {
  return ({
    created: "создан", running: "выполняется", succeeded: "завершён",
    failed: "ошибка", cancelled: "отменён", cancel_requested: "отмена…",
  })[status] || status;
}

function statusClass(status) {
  if (status === "succeeded") return "badge green";
  if (status === "failed") return "badge red";
  if (status === "running" || status === "cancel_requested") return "badge amber";
  return "badge";
}

function edgeKey(edge) {
  if (!edge) return "";
  if (edge.id != null && edge.id !== "") return String(edge.id);
  return `${edge.source || ""}:${edge.target || ""}`;
}

// Diagnostics use the compiler's "source:target" edge id. Accept a few common
// spellings and optional explicit ids, but only match exact endpoint metadata
// so a stale or ambiguous diagnostic never highlights an unrelated edge.
function edgeMatchesDiagnostic(edge, edgeId) {
  if (!edge || edgeId == null || edgeId === "") return false;
  const wanted = String(edgeId).trim();
  if (!wanted) return false;
  const source = String(edge.source || "");
  const target = String(edge.target || "");
  const port = edge.source_port || "out";
  const candidates = new Set([
    edgeKey(edge),
    `${source}:${target}`,
    `${source}->${target}`,
    `${source}→${target}`,
  ]);
  if (port !== "out") {
    candidates.add(`${source}.${port}->${target}`);
    candidates.add(`${source}.${port}:${target}`);
    candidates.add(`${source}:${port}:${target}`);
  }
  if (candidates.has(wanted)) return true;
  const parts = wanted.split(":").filter(Boolean);
  if (parts.length < 2) return false;
  return parts[0] === source && parts[parts.length - 1] === target;
}

function fieldHtml(name, schema, required, index) {
  const id = `pipeline-field-${index}`;
  const label = `${name}${required ? " *" : ""}`;
  const type = schema?.type;
  const hintId = `${id}-hint`;
  const hint = schema?.description ? `<div class="hint" id="${hintId}">${esc(schema.description)}</div>` : "";
  const describedBy = hint ? ` aria-describedby="${hintId}"` : "";
  if (Array.isArray(schema?.enum)) {
    const options = schema.enum.map(value => `<option value="${esc(String(value))}">${esc(String(value))}</option>`).join("");
    return `<div class="field"><label for="${id}">${label}</label><select class="input" id="${id}" name="${index}" ${required ? "required" : ""}${describedBy}>${options}</select>${hint}</div>`;
  }
  if (type === "boolean") {
    return `<div class="vflow-field"><label class="vflow-check" for="${id}"><input id="${id}" name="${index}" type="checkbox"${describedBy}> ${label}</label>${hint}</div>`;
  }
  if (type === "number" || type === "integer") {
    const step = schema?.multipleOf !== undefined
      ? schema.multipleOf
      : (type === "integer" ? "1" : "any");
    const attrs = [
      `step="${esc(String(step))}"`,
      schema?.minimum !== undefined ? `min="${esc(String(schema.minimum))}"` : "",
      schema?.maximum !== undefined ? `max="${esc(String(schema.maximum))}"` : "",
    ].filter(Boolean).join(" ");
    return `<div class="field"><label for="${id}">${label}</label><input class="input" id="${id}" name="${index}" type="number" ${attrs} ${required ? "required" : ""}${describedBy}>${hint}</div>`;
  }
  if (type === "object" || type === "array") {
    return `<div class="field"><label for="${id}">${label} (JSON)</label><textarea class="input" id="${id}" name="${index}" rows="4" placeholder="${type === "array" ? "[]" : "{}"}" ${required ? "required" : ""}${describedBy}></textarea>${hint}</div>`;
  }
  const attrs = [
    schema?.minLength !== undefined ? `minlength="${esc(String(schema.minLength))}"` : "",
    schema?.maxLength !== undefined ? `maxlength="${esc(String(schema.maxLength))}"` : "",
    schema?.pattern ? `pattern="${esc(schema.pattern)}"` : "",
  ].filter(Boolean).join(" ");
  return `<div class="field"><label for="${id}">${label}</label><input class="input" id="${id}" name="${index}" ${attrs} ${required ? "required" : ""}${describedBy}>${hint}</div>`;
}

function collectInput(form, properties, required) {
  if (!properties || !Object.keys(properties).length) {
    const raw = form.querySelector("[name=pipeline-json-input]")?.value || "{}";
    return parseJson(raw, {});
  }
  const result = {};
  Object.entries(properties).forEach(([name, schema], index) => {
    const field = form.elements.namedItem(String(index));
    if (!field) return;
    if (Array.isArray(schema?.enum)) {
      const raw = field.value;
      result[name] = schema.enum.find(item => String(item) === raw) ?? raw;
      return;
    }
    if (schema?.type === "boolean") {
      result[name] = Boolean(field.checked);
      return;
    }
    const value = field.value;
    if (schema?.type === "number" || schema?.type === "integer") {
      if (value !== "") result[name] = Number(value);
      return;
    }
    if (schema?.type === "object" || schema?.type === "array") {
      if (value !== "") result[name] = parseJson(value, schema.type === "array" ? [] : {});
      return;
    }
    if (value !== "" || required.has(name)) result[name] = value;
  });
  return result;
}

const PIPELINE_RUN_PAGE_SIZE = 50;
const PIPELINE_RUN_STATUSES = [
  "created", "running", "cancel_requested", "succeeded", "failed", "cancelled",
];
const ACTIVE_RUN_STATUSES = ["created", "running", "cancel_requested"];

export function vflowUI({api, shell, bindShell, state, app, esc, notify, navigate}) {
  let pollTimer = null;
  let codeHistoryBase = null;
  let codeHistoryTimer = null;
  let historyBusy = false;
  const GRAPH_MIN_ZOOM = 0.4;
  const GRAPH_MAX_ZOOM = 2;
  const GRAPH_ZOOM_STEP = 1.1;
  const HISTORY_LIMIT = 50;
  const CODE_HISTORY_DELAY = 400;
  let pendingFocus = null;

  function queueFocus(spec) {
    pendingFocus = spec || null;
  }

  function resolveFocus(spec) {
    if (!spec) return null;
    if (spec.id) return document.getElementById(spec.id);
    if (spec.selector) return document.querySelector(spec.selector);
    if (spec.attr) {
      return [...document.querySelectorAll(`[${spec.attr}]`)]
        .find(element => element.getAttribute(spec.attr) === String(spec.value)) || null;
    }
    return null;
  }

  function restoreFocus() {
    const spec = pendingFocus;
    pendingFocus = null;
    if (!spec) return false;
    const target = resolveFocus(spec) || (spec.fallback ? resolveFocus(spec.fallback) : null);
    if (!target || typeof target.focus !== "function") return false;
    target.focus({preventScroll: true});
    if (spec.scrollIntoView) {
      target.scrollIntoView({
        block: "nearest",
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth",
      });
    }
    return true;
  }

  function focusPageHeading(id, alreadyRendered) {
    if (restoreFocus()) return;
    if (alreadyRendered) return;
    document.getElementById(id)?.focus({preventScroll: true});
  }

  function markInvalid(selector, invalid = true) {
    document.querySelectorAll(selector).forEach(element => {
      if (invalid) element.setAttribute("aria-invalid", "true");
      else element.removeAttribute("aria-invalid");
    });
  }

  function bindToolbarNavigation() {
    document.querySelectorAll('[role="toolbar"]').forEach(toolbar => {
      toolbar.addEventListener("keydown", event => {
        if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
        const buttons = [...toolbar.querySelectorAll("button")].filter(button => !button.disabled);
        if (!buttons.length) return;
        const current = buttons.indexOf(event.target.closest("button"));
        if (current < 0) return;
        let next = null;
        if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
        else if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = buttons.length - 1;
        if (next == null) return;
        event.preventDefault();
        buttons[next].focus({preventScroll: true});
      });
    });
  }

  function loadingHtml(message) {
    return `<div class="page vflow-page"><div class="vflow-loading card" role="status" aria-live="polite" aria-atomic="true" aria-busy="true">
      <span class="vflow-spinner" aria-hidden="true"></span>
      <div><strong>${esc(message)}</strong><div class="hint">Подготавливаем данные интерфейса.</div></div>
    </div></div>`;
  }

  function showLoading(title, message) {
    app.innerHTML = shell(loadingHtml(message), title);
    bindShell();
  }

  function showError(title, message, error, retry) {
    app.innerHTML = shell(`<div class="page vflow-page"><div class="vflow-error" role="alert" aria-atomic="true">
      <h2 id="vflow-error-title">${esc(message)}</h2>
      <p>${esc(error?.message || error || "Неизвестная ошибка")}</p>
      <button class="btn btn-primary" id="vflow-retry" type="button">Повторить</button>
    </div></div>`, title);
    bindShell();
    document.querySelector("#vflow-retry")?.focus({preventScroll: true});
    document.querySelector("#vflow-retry")?.addEventListener("click", event => {
      event.currentTarget.disabled = true;
      Promise.resolve(retry()).catch(err => {
        event.currentTarget.disabled = false;
        notify(err.message, "error");
      });
    });
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function clampGraphZoom(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 1;
    return Math.min(GRAPH_MAX_ZOOM, Math.max(GRAPH_MIN_ZOOM, number));
  }

  function graphOffset(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function applyGraphViewport(viewport) {
    const stage = viewport?.querySelector("#vflow-stage");
    if (!stage) return;
    const zoom = clampGraphZoom(state.vflowZoom);
    const panX = graphOffset(state.vflowPanX);
    const panY = graphOffset(state.vflowPanY);
    state.vflowZoom = zoom;
    state.vflowPanX = panX;
    state.vflowPanY = panY;
    stage.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    const level = viewport.querySelector("#vflow-zoom-level");
    if (level) level.textContent = `${Math.round(zoom * 100)}%`;
  }

  function zoomGraph(factor, origin = null) {
    const viewport = document.querySelector("#vflow-graph-canvas");
    if (!viewport) return;
    const current = clampGraphZoom(state.vflowZoom);
    const next = clampGraphZoom(current * factor);
    if (next === current) return;
    const rect = viewport.getBoundingClientRect();
    const point = origin || {x: rect.width / 2, y: rect.height / 2};
    const ratio = next / current;
    state.vflowPanX = point.x - (point.x - graphOffset(state.vflowPanX)) * ratio;
    state.vflowPanY = point.y - (point.y - graphOffset(state.vflowPanY)) * ratio;
    state.vflowZoom = next;
    applyGraphViewport(viewport);
  }

  function resetGraphView() {
    state.vflowZoom = 1;
    state.vflowPanX = 0;
    state.vflowPanY = 0;
    applyGraphViewport(document.querySelector("#vflow-graph-canvas"));
  }

  function fitGraphView() {
    const viewport = document.querySelector("#vflow-graph-canvas");
    const stage = viewport?.querySelector("#vflow-stage");
    if (!viewport || !stage) return;
    const items = [...stage.querySelectorAll(".vflow-node, .vflow-terminal")];
    if (!items.length) return;
    const bounds = items.reduce((result, element) => {
      const left = element.offsetLeft;
      const top = element.offsetTop;
      result.minX = Math.min(result.minX, left);
      result.minY = Math.min(result.minY, top);
      result.maxX = Math.max(result.maxX, left + element.offsetWidth);
      result.maxY = Math.max(result.maxY, top + element.offsetHeight);
      return result;
    }, {minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity});
    const width = viewport.clientWidth;
    const height = viewport.clientHeight;
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
    if (![bounds.minX, bounds.minY, bounds.maxX, bounds.maxY].every(Number.isFinite)) return;
    const padding = 40;
    const contentWidth = Math.max(1, bounds.maxX - bounds.minX);
    const contentHeight = Math.max(1, bounds.maxY - bounds.minY);
    const zoom = clampGraphZoom(Math.min(
      Math.max(1, width - padding * 2) / contentWidth,
      Math.max(1, height - padding * 2) / contentHeight,
    ));
    state.vflowZoom = zoom;
    state.vflowPanX = (width - contentWidth * zoom) / 2 - bounds.minX * zoom;
    state.vflowPanY = (height - contentHeight * zoom) / 2 - bounds.minY * zoom;
    applyGraphViewport(viewport);
  }

  function bindGraphControls() {
    const viewport = document.querySelector("#vflow-graph-canvas");
    if (!viewport) return;
    applyGraphViewport(viewport);
    document.querySelector("[data-zoom-in]")?.addEventListener("click", () => zoomGraph(GRAPH_ZOOM_STEP));
    document.querySelector("[data-zoom-out]")?.addEventListener("click", () => zoomGraph(1 / GRAPH_ZOOM_STEP));
    document.querySelector("[data-zoom-reset]")?.addEventListener("click", () => resetGraphView());
    document.querySelector("[data-zoom-fit]")?.addEventListener("click", () => fitGraphView());

    let pan = null;
    viewport.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      if (event.target.closest(".vflow-node, .vflow-terminal, .vflow-port, .vflow-zoom-controls, button")) return;
      pan = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        panX: graphOffset(state.vflowPanX),
        panY: graphOffset(state.vflowPanY),
      };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("panning");
      event.preventDefault();
    });
    viewport.addEventListener("pointermove", event => {
      if (!pan || event.pointerId !== pan.pointerId) return;
      state.vflowPanX = pan.panX + (event.clientX - pan.x);
      state.vflowPanY = pan.panY + (event.clientY - pan.y);
      applyGraphViewport(viewport);
    });
    const endPan = event => {
      if (!pan || event.pointerId !== pan.pointerId) return;
      if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
      pan = null;
      viewport.classList.remove("panning");
    };
    viewport.addEventListener("pointerup", endPan);
    viewport.addEventListener("pointercancel", endPan);
    viewport.addEventListener("wheel", event => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      const rect = viewport.getBoundingClientRect();
      zoomGraph(event.deltaY < 0 ? GRAPH_ZOOM_STEP : 1 / GRAPH_ZOOM_STEP, {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    }, {passive: false});
  }

  function handleGraphKeydown(event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (state.route !== "admin-pipeline-editor" || state.vflowTab !== "graph") return;
    const target = event.target;
    const tag = target?.tagName?.toLowerCase();
    if (target?.isContentEditable || ["input", "textarea", "select"].includes(tag)) return;
    if (!document.querySelector("#vflow-graph-canvas")) return;
    const key = event.key;
    const code = event.code || "";
    if (key === "+" || key === "=" || code === "NumpadAdd") zoomGraph(GRAPH_ZOOM_STEP);
    else if (key === "-" || key === "_" || code === "NumpadSubtract") zoomGraph(1 / GRAPH_ZOOM_STEP);
    else if (key === "0" || code === "Numpad0") resetGraphView();
    else if (key === "f" || key === "F" || code === "KeyF") fitGraphView();
    else return;
    event.preventDefault();
  }

  function pipelineRunsQuery(offset) {
    const params = new URLSearchParams({
      limit: String(PIPELINE_RUN_PAGE_SIZE),
      offset: String(offset),
    });
    if (state.pipelineRunStatus) params.set("status", state.pipelineRunStatus);
    return `/api/pipeline-runs?${params.toString()}`;
  }

  function hasActiveRuns(runs) {
    return runs.some(run => ACTIVE_RUN_STATUSES.includes(run.status));
  }

  function schedulePolling(runs) {
    stopPolling();
    if (hasActiveRuns(runs)) pollTimer = setTimeout(refreshPipelineRuns, 2000);
  }

  function runHtml(run) {
    const progress = run.progress || {};
    const total = progress.points_total || 0;
    const done = progress.points_done || 0;
    const percent = total ? Math.round((done / total) * 100) : 0;
    const title = run.pipeline?.title || "Пайплайн";
    const status = statusLabel(run.status);
    const cancel = ACTIVE_RUN_STATUSES.includes(run.status)
      ? `<button class="btn btn-small" data-cancel-run="${esc(run.id)}" type="button" aria-label="Отменить запуск ${esc(title)}">Отменить</button>` : "";
    const result = run.public_result_enabled && run.status === "succeeded"
      ? `<button class="btn btn-small" data-result-run="${esc(run.id)}" type="button" aria-label="Показать результат запуска ${esc(title)}">Результат</button>` : "";
    return `<article class="vflow-run" data-run="${esc(run.id)}" tabindex="-1" aria-label="Запуск ${esc(title)}, версия ${esc(run.pipeline?.version ?? "")}, статус: ${esc(status)}">
      <div class="vflow-run-head"><strong>${esc(title)} · v${esc(run.pipeline?.version ?? "")}</strong><span class="${statusClass(run.status)}">${esc(status)}</span>${cancel}${result}</div>
      <div class="meta">Точек: ${done}/${total} · задач в ожидании: ${progress.waiting_tasks || 0} · merge: ${progress.waiting_merges || 0}</div>
      <div class="vflow-progress" role="progressbar" aria-label="Прогресс запуска" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%" aria-hidden="true"></span></div>
      ${run.error?.message ? `<div class="error">${esc(run.error.message)}</div>` : ""}
      ${result ? `<pre class="vflow-result" data-result-box="${esc(run.id)}" tabindex="0" role="region" aria-label="Результат запуска" hidden></pre>` : ""}
    </article>`;
  }

  function runsHtml(runs) {
    if (state.pipelineRunsLoading) {
      return `<div class="vflow-empty" role="status" aria-live="polite" aria-atomic="true">Загружаем запуски…</div>`;
    }
    const list = runs.length
      ? `<div class="vflow-runs">${runs.map(runHtml).join("")}</div>`
      : `<div class="empty">${state.pipelineRunStatus ? "Запусков с выбранным статусом нет" : "Запусков пока нет"}</div>`;
    const more = state.pipelineRunHasMore
      ? `<div style="margin-top:12px"><button class="btn" id="pipeline-runs-more" type="button">Показать ещё</button></div>`
      : "";
    return list + more;
  }

  function renderRuns() {
    const container = document.querySelector("#pipeline-runs");
    if (!container) return;
    const focused = document.activeElement;
    let fallbackFocus = null;
    if (focused && container.contains(focused)) {
      const runId = focused.closest?.("[data-run]")?.getAttribute("data-run") || null;
      const attr = ["data-cancel-run", "data-result-run", "data-run", "data-result-box"]
        .find(name => focused.hasAttribute?.(name));
      if (attr) {
        fallbackFocus = {attr, value: focused.getAttribute(attr)};
        if (runId) fallbackFocus.fallback = {attr: "data-run", value: runId};
      } else if (focused.id) {
        fallbackFocus = {id: focused.id};
      }
    }
    container.setAttribute("aria-busy", state.pipelineRunsLoading ? "true" : "false");
    container.innerHTML = runsHtml(state.pipelineRuns || []);
    bindRunActions();
    document.querySelector("#pipeline-runs-more")?.addEventListener("click", event => {
      const button = event.currentTarget;
      button.disabled = true;
      void loadMoreRuns().catch(err => {
        button.disabled = false;
        notify(err.message, "error");
      });
    });
    if (!restoreFocus() && fallbackFocus) {
      const target = resolveFocus(fallbackFocus)
        || (fallbackFocus.fallback ? resolveFocus(fallbackFocus.fallback) : null);
      target?.focus?.({preventScroll: true});
    }
  }

  async function loadMoreRuns() {
    if (state.route !== "pipelines") return;
    const filterStatus = state.pipelineRunStatus;
    const page = await api(pipelineRunsQuery(state.pipelineRunOffset || 0));
    if (state.route !== "pipelines" || state.pipelineRunStatus !== filterStatus) return;
    const seen = new Set((state.pipelineRuns || []).map(run => run.id));
    const added = page.filter(run => !seen.has(run.id));
    state.pipelineRuns = (state.pipelineRuns || []).concat(added);
    state.pipelineRunOffset = state.pipelineRuns.length;
    state.pipelineRunHasMore = page.length >= PIPELINE_RUN_PAGE_SIZE;
    queueFocus(added.length
      ? {attr: "data-run", value: added[0].id, fallback: {id: "pipeline-runs-more"}}
      : {id: "pipeline-runs-more"});
    renderRuns();
    schedulePolling(state.pipelineRuns);
  }

  async function refreshPipelineRuns() {
    if (state.route !== "pipelines") return;
    const filterStatus = state.pipelineRunStatus;
    try {
      const page = await api(pipelineRunsQuery(0));
      if (state.route !== "pipelines" || state.pipelineRunStatus !== filterStatus) return;
      const existing = filterStatus
        ? (state.pipelineRuns || []).filter(run => run.status === filterStatus)
        : (state.pipelineRuns || []);
      const freshIds = new Set(page.map(run => run.id));
      state.pipelineRuns = page.concat(existing.filter(run => !freshIds.has(run.id)));
      state.pipelineRunOffset = state.pipelineRuns.length;
      state.pipelineRunHasMore = page.length >= PIPELINE_RUN_PAGE_SIZE;
      renderRuns();
      schedulePolling(state.pipelineRuns);
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function changeRunFilter(value) {
    state.pipelineRunStatus = value;
    state.pipelineRuns = [];
    state.pipelineRunOffset = 0;
    state.pipelineRunHasMore = false;
    state.pipelineRunsLoading = true;
    renderRuns();
    try {
      await refreshPipelineRuns();
    } finally {
      state.pipelineRunsLoading = false;
      if (state.route === "pipelines") renderRuns();
    }
  }

  function bindRunActions() {
    document.querySelectorAll("[data-cancel-run]").forEach(button => {
      button.addEventListener("click", async () => {
        const runId = button.dataset.cancelRun;
        button.disabled = true;
        try {
          await api(`/api/pipeline-runs/${encodeURIComponent(runId)}/cancel`, {method: "POST", body: "{}"});
          notify("Отмена запрошена");
          queueFocus({attr: "data-run", value: runId, fallback: {id: "pipeline-runs-heading"}});
          await refreshPipelineRuns();
        } catch (err) {
          button.disabled = false;
          notify(err.message, "error");
        }
      });
    });
    document.querySelectorAll("[data-result-run]").forEach(button => {
      button.addEventListener("click", async () => {
        const runId = button.dataset.resultRun;
        button.disabled = true;
        try {
          const data = await api(`/api/pipeline-runs/${encodeURIComponent(runId)}/result`);
          const box = document.querySelector(`[data-result-box="${runId}"]`);
          if (box) {
            box.textContent = JSON.stringify(data.result, null, 2);
            box.hidden = false;
            box.focus({preventScroll: true});
            box.scrollIntoView({
              block: "nearest",
              behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth",
            });
          }
        } catch (err) {
          notify(err.message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });
  }

  async function renderPipelines() {
    stopPolling();
    const loaded = Boolean(document.querySelector(".vflow-pipelines-page"));
    if (!loaded) showLoading("Пайплайны", "Загружаем пайплайны…");
    let pipelines;
    let runs;
    try {
      [pipelines, runs] = await Promise.all([
        api("/api/pipelines"),
        api(pipelineRunsQuery(0)),
      ]);
    } catch (err) {
      if (state.route !== "pipelines") return;
      if (loaded) {
        notify(err.message, "error");
        return;
      }
      showError("Пайплайны", "Не удалось загрузить пайплайны", err, renderPipelines);
      return;
    }
    if (state.route !== "pipelines") return;
    state.pipelineRunsLoading = false;
    state.pipelineRuns = runs;
    state.pipelineRunOffset = runs.length;
    state.pipelineRunHasMore = runs.length >= PIPELINE_RUN_PAGE_SIZE;
    const selected = pipelines.find(item => item.id === state.selectedPipeline) || pipelines[0] || null;
    state.selectedPipeline = selected?.id || null;
    const cards = pipelines.length ? pipelines.map(item => `
      <article class="vflow-card vflow-card-click ${selected?.id === item.id ? "selected" : ""}" data-pipeline="${esc(item.id)}" role="button" tabindex="0" aria-pressed="${selected?.id === item.id}" aria-label="Выбрать пайплайн ${esc(item.title)}">
        <h3>${esc(item.title)}</h3>
        <div class="meta">${esc(item.description || "Без описания")}</div>
        <div class="meta">Версия ${esc(item.version)} · ${esc(item.published_at || "")}</div>
        <div class="vflow-card-actions"><span class="badge green">Доступен</span></div>
      </article>`).join("") : `<div class="vflow-empty">Опубликованных пайплайнов пока нет</div>`;

    const schema = selected?.input_schema || {};
    const properties = schema.properties || {};
    const required = new Set(schema.required || []);
    const fields = Object.entries(properties).map(([name, item], index) => fieldHtml(name, item, required.has(name), index)).join("");
    const statusOptions = [
      ["", "Все статусы"],
      ...PIPELINE_RUN_STATUSES.map(item => [item, statusLabel(item)]),
    ].map(([value, label]) => {
      const selectedAttr = state.pipelineRunStatus === value ? " selected" : "";
      return `<option value="${esc(value)}"${selectedAttr}>${esc(label)}</option>`;
    }).join("");
    const inputBlock = selected ? `<form class="card" id="pipeline-run-form" aria-label="Запуск пайплайна ${esc(selected.title)}">
      <h3>${esc(selected.title)}</h3>
      <p class="meta">Версия ${esc(selected.version)}. ${esc(selected.description || "")}</p>
      ${Object.keys(properties).length ? fields : `<div class="field"><label for="pipeline-json-input">Входные данные (JSON)</label><textarea class="input" id="pipeline-json-input" name="pipeline-json-input" rows="5">{}</textarea></div>`}
      <button class="btn btn-primary btn-block" type="submit" ${state.me?.workspace_path ? "" : "disabled"}>Запустить пайплайн</button>
      ${state.me?.workspace_path ? "" : `<p class="hint">Для запуска администратор должен назначить вам рабочую папку.</p>`}
    </form>` : `<div class="vflow-empty">Выберите пайплайн</div>`;

    app.innerHTML = shell(`<div class="page vflow-page vflow-pipelines-page">
      <div class="page-head"><h2 id="vflow-pipelines-title" tabindex="-1">Пайплайны</h2><span class="spacer"></span><button class="btn" id="refresh-pipelines" type="button">Обновить</button></div>
      <div class="vflow-cards" role="group" aria-label="Опубликованные пайплайны">${cards}</div>
      <div class="grid task-grid" style="margin-top:18px"><div>${inputBlock}</div><div class="card"><h3>Как это работает</h3><p class="meta">Пайплайн выполняет задачи VALERIA в заданном порядке. Промежуточные данные и служебные идентификаторы скрыты.</p></div></div>
      <section style="margin-top:18px" aria-labelledby="pipeline-runs-heading">
        <div class="page-head"><h2 id="pipeline-runs-heading" tabindex="-1">Мои запуски</h2><span class="spacer"></span>
          <div class="vflow-filter"><label class="meta" for="pipeline-run-status">Статус</label>
          <select class="input" id="pipeline-run-status">${statusOptions}</select></div>
        </div>
        <div id="pipeline-runs" aria-busy="false"></div>
      </section>
    </div>`, "Пайплайны");
    bindShell();
    bindToolbarNavigation();
    document.querySelector("#refresh-pipelines")?.addEventListener("click", () => {
      queueFocus({id: "refresh-pipelines"});
      void renderPipelines().catch(err => notify(err.message, "error"));
    });
    document.querySelector("#pipeline-run-status")?.addEventListener("change", event => {
      void changeRunFilter(event.currentTarget.value).catch(err => notify(err.message, "error"));
    });
    document.querySelectorAll("[data-pipeline]").forEach(card => {
      const choose = () => {
        state.selectedPipeline = card.dataset.pipeline;
        queueFocus({attr: "data-pipeline", value: card.dataset.pipeline, fallback: {id: "vflow-pipelines-title"}});
        void renderPipelines().catch(err => notify(err.message, "error"));
      };
      card.addEventListener("click", choose);
      card.addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        choose();
      });
    });
    document.querySelector("#pipeline-run-form")?.addEventListener("submit", async event => {
      event.preventDefault();
      if (!selected) return;
      const button = event.currentTarget.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        const input = collectInput(event.currentTarget, properties, required);
        await api(`/api/pipelines/${encodeURIComponent(selected.id)}/runs`, {
          method: "POST", body: JSON.stringify({input}),
        });
        notify("Пайплайн запущен");
        await refreshPipelineRuns();
      } catch (err) {
        notify(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    renderRuns();
    schedulePolling(runs);
    focusPageHeading("vflow-pipelines-title", loaded);
  }

  async function renderAdminPipelines() {
    const loaded = Boolean(document.querySelector("#pipeline-create-form"));
    if (!loaded) showLoading("Администрирование пайплайнов", "Загружаем пайплайны…");
    let pipelines;
    try {
      pipelines = await api("/api/admin/pipelines");
    } catch (err) {
      if (state.route !== "admin-pipelines") return;
      if (loaded) {
        notify(err.message, "error");
        return;
      }
      showError("Администрирование пайплайнов", "Не удалось загрузить пайплайны", err, renderAdminPipelines);
      return;
    }
    if (state.route !== "admin-pipelines") return;
    const cards = pipelines.length ? pipelines.map(item => `
      <article class="vflow-card">
        <h3>${esc(item.title)}</h3>
        <div class="meta">${esc(item.slug)} · ${esc(item.status)}</div>
        <div class="meta">Опубликована: ${esc(item.published_version_number ?? "—")}${item.draft_version_number ? ` · черновик v${esc(item.draft_version_number)}` : ""}</div>
        <div class="vflow-card-actions"><button class="btn" data-edit-pipeline="${esc(item.id)}" type="button">Открыть редактор</button></div>
      </article>`).join("") : `<div class="vflow-empty">Пайплайнов пока нет</div>`;
    app.innerHTML = shell(`<div class="page vflow-page vflow-admin-pipelines-page">
      <div class="page-head"><h2 id="vflow-admin-pipelines-title" tabindex="-1">Пайплайны</h2><span class="spacer"></span><button class="btn" id="refresh-admin-pipelines" type="button">Обновить</button></div>
      <div class="vflow-cards" role="group" aria-label="Пайплайны администратора">${cards}</div>
      <form class="card" id="pipeline-create-form" style="margin-top:18px">
        <h3>Новый пайплайн</h3>
        <div class="grid vflow-create-grid">
          <div class="field"><label for="pipeline-slug">Slug</label><input class="input" id="pipeline-slug" name="slug" pattern="[a-z0-9-]+" required placeholder="review-changes"></div>
          <div class="field"><label for="pipeline-title">Название</label><input class="input" id="pipeline-title" name="title" required></div>
        </div>
        <div class="field"><label for="pipeline-description">Описание</label><textarea class="input" id="pipeline-description" name="description" rows="2"></textarea></div>
        <button class="btn btn-primary" type="submit">Создать</button>
      </form>
    </div>`, "Администрирование пайплайнов");
    bindShell();
    bindToolbarNavigation();
    document.querySelector("#refresh-admin-pipelines")?.addEventListener("click", () => {
      queueFocus({id: "refresh-admin-pipelines"});
      void renderAdminPipelines().catch(err => notify(err.message, "error"));
    });
    document.querySelectorAll("[data-edit-pipeline]").forEach(button => button.addEventListener("click", () => {
      state.adminPipelineId = button.dataset.editPipeline;
      navigate("admin-pipeline-editor");
    }));
    document.querySelector("#pipeline-create-form")?.addEventListener("submit", async event => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(event.currentTarget));
      try {
        const created = await api("/api/admin/pipelines", {method: "POST", body: JSON.stringify(body)});
        state.adminPipelineId = created.id;
        notify("Пайплайн создан");
        navigate("admin-pipeline-editor");
      } catch (err) {
        notify(err.message, "error");
      }
    });
    focusPageHeading("vflow-admin-pipelines-title", loaded);
  }

  function graphHtml(graph, points = []) {
    const active = state.vflowGraph || graph || {nodes: [], edges: []};
    const nodes = active.nodes || [];
    const edges = active.edges || [];
    const counts = points.reduce((result, point) => {
      result[point.node_id] = (result[point.node_id] || 0) + 1;
      return result;
    }, {});
    const positioned = nodes.map((node, index) => ({
      ...node,
      ui: {
        ...(node.ui || {}),
        x: Number(node.ui?.x ?? 60 + (index % 3) * 240),
        y: Number(node.ui?.y ?? 60 + Math.floor(index / 3) * 150),
      },
    }));
    const byId = Object.fromEntries(positioned.map(node => [node.id, node]));
    const diagnostics = state.vflowDiagnostics || [];
    const invalid = new Set(diagnostics.map(item => item.node_id).filter(Boolean));
    const edgeDiagnostics = diagnostics.filter(item => item.edge_id);
    const invalidEdgeIndexes = new Set();
    const invalidIncoming = new Set();
    const invalidOutgoing = new Map();
    edges.forEach((edge, index) => {
      if (!edgeDiagnostics.some(item => edgeMatchesDiagnostic(edge, item.edge_id))) return;
      invalidEdgeIndexes.add(index);
      invalidIncoming.add(String(edge.target || ""));
      const source = String(edge.source || "");
      const port = edge.source_port || "out";
      if (!invalidOutgoing.has(source)) invalidOutgoing.set(source, new Set());
      invalidOutgoing.get(source).add(port);
    });
    const portInvalid = (nodeId, direction, port) => {
      if (invalid.has(nodeId)) return true;
      if (direction === "in") return invalidIncoming.has(nodeId);
      return Boolean(invalidOutgoing.get(nodeId)?.has(port));
    };
    const maxX = Math.max(720, ...positioned.map(node => node.ui.x + 260));
    const maxY = Math.max(420, ...positioned.map(node => node.ui.y + 180));
    const start = {x: 20, y: 20};
    const end = {x: maxX, y: Math.max(120, maxY / 2)};
    const point = endpoint => {
      if (endpoint === "start") return {x: start.x + 58, y: start.y + 30};
      if (endpoint === "end") return {x: end.x, y: end.y + 18};
      const node = byId[endpoint];
      if (!node) return null;
      return {x: node.ui.x + 176, y: node.ui.y + 54};
    };
    const targetPoint = endpoint => {
      if (endpoint === "start") return {x: start.x + 58, y: start.y + 30};
      if (endpoint === "end") return {x: end.x, y: end.y + 18};
      const node = byId[endpoint];
      return node ? {x: node.ui.x, y: node.ui.y + 54} : null;
    };
    const edgeSvg = edges.map((edge, index) => {
      const from = point(edge.source);
      const to = targetPoint(edge.target);
      if (!from || !to) return "";
      const path = `M ${from.x} ${from.y} C ${from.x + 70} ${from.y}, ${to.x - 70} ${to.y}, ${to.x} ${to.y}`;
      const label = edge.source_port && edge.source_port !== "out" ? edge.source_port : "";
      const bad = invalidEdgeIndexes.has(index);
      return `<path d="${path}" class="vflow-edge-path${bad ? " invalid" : ""}" data-edge-index="${index}" data-edge-key="${esc(edgeKey(edge))}"${bad ? ' data-edge-invalid="true"' : ""} marker-end="url(#vflow-arrow)"></path>${label ? `<text x="${(from.x + to.x) / 2}" y="${(from.y + to.y) / 2 - 6}" class="vflow-edge-label${bad ? " invalid" : ""}">${esc(label)}</text>` : ""}`;
    }).join("");
    const portButton = (nodeId, direction, port, extraClass, label, text = "") => {
      const bad = portInvalid(nodeId, direction, port);
      const data = direction === "in"
        ? `data-target="${esc(nodeId)}"`
        : `data-source="${esc(nodeId)}" data-source-port="${esc(port)}"`;
      const name = `${label}${bad ? ", есть ошибка" : ""}`;
      return `<button class="vflow-port ${direction === "in" ? "in" : "out"}${extraClass ? ` ${extraClass}` : ""}${bad ? " invalid" : ""}" ${data} type="button" title="${esc(name)}" aria-label="${esc(name)}"${bad ? ' aria-invalid="true"' : ""}>${text}</button>`;
    };
    const ports = node => {
      const input = portButton(node.id, "in", "in", "", `Вход узла ${node.id}`);
      if (node.type === "core.if") {
        return `${input}${portButton(node.id, "out", "true", "", `Выход true узла ${node.id}`, "T")}${portButton(node.id, "out", "false", "second", `Выход false узла ${node.id}`, "F")}`;
      }
      if (node.type === "core.fork") {
        const count = Math.max(2, Math.min(32, Number(node.config?.count || 2)));
        return `${input}${Array.from({length: count}, (_, index) => {
          const port = `branch[${index}]`;
          const bad = portInvalid(node.id, "out", port);
          const label = `Ветвь ${index} узла ${node.id}${bad ? ", есть ошибка" : ""}`;
          return `<button class="vflow-port out branch${bad ? " invalid" : ""}" style="top:${28 + index * 18}px" data-source="${esc(node.id)}" data-source-port="${esc(port)}" type="button" title="${esc(label)}" aria-label="${esc(label)}"${bad ? ' aria-invalid="true"' : ""}>${index}</button>`;
        }).join("")}`;
      }
      return `${input}${portButton(node.id, "out", "out", "", `Выход узла ${node.id}`)}`;
    };
    const nodeHtml = positioned.map(node => {
      const selected = state.vflowSelectedNode === node.id;
      const bad = invalid.has(node.id);
      const label = `Узел ${node.id}, тип ${node.type}${bad ? ", есть ошибка" : ""}`;
      return `<div class="vflow-node ${selected ? "selected" : ""} ${bad ? "invalid" : ""}" data-node-container="${esc(node.id)}" role="group" aria-label="${esc(label)}"${bad ? ' aria-invalid="true"' : ""} style="left:${node.ui.x}px;top:${node.ui.y}px">
      <button class="vflow-node-body ${selected ? "selected" : ""}" data-node="${esc(node.id)}" data-node-select="${esc(node.id)}" type="button" role="button" aria-pressed="${selected}" aria-label="${esc(label)}"${bad ? ' aria-invalid="true"' : ""}>
        <strong>${esc(node.id)}</strong><code>${esc(node.type)}</code>
        <small>${esc(JSON.stringify(node.config || {}))}</small>
        ${counts[node.id] ? `<span class="badge">${counts[node.id]} точ.</span>` : ""}
      </button>
      ${ports(node)}
    </div>`;
    }).join("");
    const zoom = clampGraphZoom(state.vflowZoom);
    const panX = graphOffset(state.vflowPanX);
    const panY = graphOffset(state.vflowPanY);
    return `<div class="vflow-graph interactive" id="vflow-graph-canvas" role="region" aria-label="Граф пайплайна" aria-describedby="vflow-graph-help" tabindex="0" style="height:${maxY + 80}px;min-height:520px">
      <p class="vflow-sr-only" id="vflow-graph-help">Клавиши плюс и минус изменяют масштаб, 0 сбрасывает масштаб, F вписывает граф в экран.</p>
      <div class="vflow-zoom-controls" role="toolbar" aria-label="Масштаб графа" aria-orientation="horizontal">
        <button class="btn vflow-zoom-btn" data-zoom-out type="button" title="Уменьшить масштаб (−)" aria-label="Уменьшить масштаб">−</button>
        <span class="vflow-zoom-level" id="vflow-zoom-level" aria-live="polite">${Math.round(zoom * 100)}%</span>
        <button class="btn vflow-zoom-btn" data-zoom-in type="button" title="Увеличить масштаб (+)" aria-label="Увеличить масштаб">+</button>
        <button class="btn vflow-zoom-btn" data-zoom-reset type="button" title="Сбросить масштаб (0)" aria-label="Сбросить масштаб">100%</button>
        <button class="btn vflow-zoom-btn" data-zoom-fit type="button" title="Вписать граф в экран (F)" aria-label="Вписать граф в экран">Вписать</button>
      </div>
      <div class="vflow-stage" id="vflow-stage" style="width:${maxX + 260}px;height:${maxY + 80}px;transform:translate(${panX}px, ${panY}px) scale(${zoom})">
        <svg class="vflow-edges" width="${maxX + 260}" height="${maxY + 80}" aria-hidden="true">
          <defs><marker id="vflow-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="currentColor"></path></marker></defs>
          ${edgeSvg}
        </svg>
        <div class="vflow-terminal" style="left:${start.x}px;top:${start.y}px">start${portButton("start", "out", "out", "", "Выход начала графа")}</div>
        ${nodeHtml}
        <div class="vflow-terminal end" style="left:${end.x}px;top:${end.y}px">end${portButton("end", "in", "in", "", "Вход конца графа")}</div>
      </div>
      ${positioned.length ? "" : `<div class="vflow-graph-empty" role="status">В графе пока нет узлов. Добавьте блок из библиотеки.</div>`}
    </div><div class="vflow-edge-list" role="group" aria-label="Связи графа">${edges.length ? edges.map((edge, index) => {
      const bad = invalidEdgeIndexes.has(index);
      const focused = state.vflowDiagnosticEdge != null && Number(state.vflowDiagnosticEdge) === index;
      const text = `${edge.source}${edge.source_port && edge.source_port !== "out" ? `.${edge.source_port}` : ""} → ${edge.target}`;
      return `<button class="vflow-edge-chip${bad ? " invalid" : ""}${focused ? " focused" : ""}" data-delete-edge="${index}" data-edge-key="${esc(edgeKey(edge))}"${bad ? ' data-edge-invalid="true" aria-invalid="true"' : ""} type="button" title="${bad ? "Связь с ошибкой. Удалить связь" : "Удалить связь"}" aria-label="${esc(`Связь ${text}${bad ? ". Есть ошибка" : ""}. Удалить связь`)}">${esc(text)} <b aria-hidden="true">×</b></button>`;
    }).join("") : `<span class="hint">Связей пока нет</span>`}</div>`;
  }

  function cloneGraph(graph) {
    return JSON.parse(JSON.stringify(graph));
  }

  function emptyGraph(workflow) {
    return {ir_version: 1, workflow: workflow || "workflow", entry: "", nodes: [], edges: []};
  }

  function captureHistoryEntry() {
    return {
      source: state.vflowSource || "",
      graph: cloneGraph(state.vflowGraph || emptyGraph()),
      graphDirty: Boolean(state.vflowGraphDirty),
    };
  }

  function normalizeHistoryEntry(entry = null) {
    const snapshot = entry || captureHistoryEntry();
    return {
      source: snapshot.source || "",
      graph: cloneGraph(snapshot.graph || emptyGraph()),
      graphDirty: Boolean(snapshot.graphDirty),
    };
  }

  function historyEntryKey(entry) {
    return JSON.stringify([entry.source || "", entry.graph || null]);
  }

  function updateHistoryButtons() {
    const undo = document.querySelector("#vflow-undo");
    if (undo) undo.disabled = !((state.vflowUndoStack || []).length || codeHistoryBase);
    const redo = document.querySelector("#vflow-redo");
    if (redo) redo.disabled = !(state.vflowRedoStack || []).length || Boolean(codeHistoryBase);
  }

  function resetVFlowHistory() {
    state.vflowUndoStack = [];
    state.vflowRedoStack = [];
    if (codeHistoryTimer) clearTimeout(codeHistoryTimer);
    codeHistoryTimer = null;
    codeHistoryBase = null;
    updateHistoryButtons();
  }

  function pushVFlowHistory(entry = null) {
    if (!Array.isArray(state.vflowUndoStack)) state.vflowUndoStack = [];
    if (!Array.isArray(state.vflowRedoStack)) state.vflowRedoStack = [];
    const snapshot = normalizeHistoryEntry(entry);
    const top = state.vflowUndoStack[state.vflowUndoStack.length - 1];
    if (top && historyEntryKey(top) === historyEntryKey(snapshot)) {
      state.vflowRedoStack = [];
      updateHistoryButtons();
      return false;
    }
    state.vflowUndoStack.push(snapshot);
    while (state.vflowUndoStack.length > HISTORY_LIMIT) state.vflowUndoStack.shift();
    state.vflowRedoStack = [];
    updateHistoryButtons();
    return true;
  }

  function scheduleCodeHistory() {
    if (!codeHistoryBase) codeHistoryBase = captureHistoryEntry();
    const definitionId = state.vflowDefinitionId;
    if (codeHistoryTimer) clearTimeout(codeHistoryTimer);
    codeHistoryTimer = setTimeout(() => {
      codeHistoryTimer = null;
      const base = codeHistoryBase;
      codeHistoryBase = null;
      if (!base || state.route !== "admin-pipeline-editor" || state.vflowDefinitionId !== definitionId) return;
      if (historyEntryKey(base) === historyEntryKey(captureHistoryEntry())) return;
      pushVFlowHistory(base);
    }, CODE_HISTORY_DELAY);
  }

  function flushCodeHistory() {
    if (codeHistoryTimer) clearTimeout(codeHistoryTimer);
    codeHistoryTimer = null;
    const base = codeHistoryBase;
    codeHistoryBase = null;
    if (!base) return false;
    if (historyEntryKey(base) === historyEntryKey(captureHistoryEntry())) return false;
    return pushVFlowHistory(base);
  }

  function restoreHistoryEntry(entry) {
    state.vflowSource = entry.source || "";
    state.vflowGraph = cloneGraph(entry.graph || emptyGraph());
    state.vflowGraphDirty = Boolean(entry.graphDirty);
    state.vflowDirty = true;
    state.vflowConnecting = null;
    state.vflowDiagnosticEdge = null;
    if (state.vflowSelectedNode && !(state.vflowGraph.nodes || []).some(node => node.id === state.vflowSelectedNode)) {
      state.vflowSelectedNode = null;
    }
  }

  async function undoVFlowEdit() {
    if (historyBusy || state.route !== "admin-pipeline-editor") return;
    flushCodeHistory();
    const undoStack = state.vflowUndoStack || [];
    if (!undoStack.length) {
      updateHistoryButtons();
      return;
    }
    historyBusy = true;
    try {
      const entry = undoStack.pop();
      if (!Array.isArray(state.vflowRedoStack)) state.vflowRedoStack = [];
      state.vflowRedoStack.push(captureHistoryEntry());
      while (state.vflowRedoStack.length > HISTORY_LIMIT) state.vflowRedoStack.shift();
      restoreHistoryEntry(entry);
      queueFocus({id: "vflow-undo"});
      await renderAdminPipelineEditor();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      historyBusy = false;
      updateHistoryButtons();
    }
  }

  async function redoVFlowEdit() {
    if (historyBusy || state.route !== "admin-pipeline-editor") return;
    flushCodeHistory();
    const redoStack = state.vflowRedoStack || [];
    if (!redoStack.length) {
      updateHistoryButtons();
      return;
    }
    historyBusy = true;
    try {
      const entry = redoStack.pop();
      if (!Array.isArray(state.vflowUndoStack)) state.vflowUndoStack = [];
      state.vflowUndoStack.push(captureHistoryEntry());
      while (state.vflowUndoStack.length > HISTORY_LIMIT) state.vflowUndoStack.shift();
      restoreHistoryEntry(entry);
      queueFocus({id: "vflow-redo"});
      await renderAdminPipelineEditor();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      historyBusy = false;
      updateHistoryButtons();
    }
  }

  function handleEditorHistoryKeydown(event) {
    if (event.defaultPrevented || state.route !== "admin-pipeline-editor") return;
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const target = event.target;
    const tag = target?.tagName?.toLowerCase();
    if (target?.isContentEditable || ["input", "textarea", "select"].includes(tag)) return;
    if (!document.querySelector(".vflow-editor")) return;
    const key = (event.key || "").toLowerCase();
    const code = event.code || "";
    if (key === "z" || code === "KeyZ") {
      event.preventDefault();
      if (event.shiftKey) void redoVFlowEdit();
      else void undoVFlowEdit();
    } else if ((key === "y" || code === "KeyY") && !event.shiftKey) {
      event.preventDefault();
      void redoVFlowEdit();
    }
  }

  function defaultConfig(type, taskId = "") {
    if (type === "valeria.task") return {task_id: taskId, input_path: "$"};
    if (type === "core.if") return {path: "$.ok", operator: "eq", value: true, missing: "error"};
    if (type === "core.fork") return {count: 2, join: "merge_1"};
    if (type === "core.merge") return {mode: "all"};
    return {operations: [{op: "set", path: "$.ok", value: true}]};
  }

  function eventSearchText(event) {
    const payload = event?.payload;
    return [
      event?.event_type,
      event?.node_id,
      event?.point_id,
      payload == null ? "" : JSON.stringify(payload),
    ].filter(value => value != null && value !== "").join(" ").toLowerCase();
  }

  function applyEventFilter(value) {
    state.vflowEventFilter = String(value || "");
    const query = state.vflowEventFilter.trim().toLowerCase();
    const rows = [...document.querySelectorAll("#vflow-events .vflow-event")];
    let visible = 0;
    rows.forEach(row => {
      const match = !query || (row.dataset.eventSearch || "").includes(query);
      row.hidden = !match;
      if (match) visible += 1;
    });
    const empty = document.querySelector("#vflow-event-empty");
    if (empty) {
      empty.hidden = visible > 0;
      empty.textContent = rows.length ? "Нет событий по фильтру" : "Нет событий";
    }
  }

  function runsPanelHtml(runs, detail) {
    const list = runs.length
      ? runs.map(run => `<button class="vflow-run ${state.vflowRunId === run.id ? "selected" : ""}" data-vflow-run="${esc(run.id)}" type="button" aria-pressed="${state.vflowRunId === run.id}" aria-label="Запуск ${esc(run.id.slice(0, 8))}, статус: ${esc(statusLabel(run.status))}">
          <div class="vflow-run-head"><strong>${esc(run.status)}</strong><span class="${statusClass(run.status)}">${esc(run.id.slice(0, 8))}</span></div>
          <div class="meta">${esc(run.started_by?.login || "—")} · ${esc(run.created_at || "")}</div>
        </button>`).join("")
      : `<div class="vflow-empty">Запусков пока нет</div>`;
    const points = (detail?.points || []).map(point => `<tr><td><code>${esc(point.node_id)}</code></td><td>${esc(point.status)}</td><td>${esc(point.visit_seq)}</td><td>${esc(point.branch_index ?? "—")}</td><td>${esc(point.task_run_id || "—")}</td></tr>`).join("");
    const eventFilter = String(state.vflowEventFilter || "");
    const eventQuery = eventFilter.trim().toLowerCase();
    const detailEvents = detail?.events || [];
    const events = detailEvents.map(event => {
      const search = eventSearchText(event);
      const hidden = eventQuery && !search.includes(eventQuery);
      return `<div class="vflow-event" role="listitem" data-event-search="${esc(search)}"${hidden ? " hidden" : ""}>
        <span class="meta">${esc(event.created_at)}${event.point_id ? `<small class="vflow-event-point">${esc(event.point_id)}</small>` : ""}</span>
        <strong>${esc(event.event_type)}</strong>
        <span>${event.node_id ? `<code>${esc(event.node_id)}</code> ` : ""}<small>${esc(JSON.stringify(event.payload || {}))}</small></span>
      </div>`;
    }).join("");
    const anyMatch = detailEvents.some(event => !eventQuery || eventSearchText(event).includes(eventQuery));
    const eventEmptyText = detailEvents.length ? "Нет событий по фильтру" : "Нет событий";
    return `<div class="vflow-runs-panel">
      <div class="vflow-run-list" role="group" aria-label="Запуски VFlow">${list}</div>
      <div class="vflow-run-detail">${detail ? `<div class="vflow-run-head"><strong>${esc(statusLabel(detail.status))}</strong><span class="spacer"></span><button class="btn" id="vflow-run-cancel" type="button" aria-label="Отменить выбранный запуск">Отменить</button></div>
        <h4 class="vflow-panel-title" id="vflow-points-heading">Точки исполнения</h4>
        <div class="vflow-table-wrap" tabindex="0" role="region" aria-labelledby="vflow-points-heading"><table class="vflow-table"><caption class="vflow-sr-only">Точки исполнения запуска</caption><thead><tr><th>Узел</th><th>Статус</th><th>Визит</th><th>Ветка</th><th>Task Run</th></tr></thead><tbody>${points || `<tr><td colspan="5">Нет точек</td></tr>`}</tbody></table></div>
        <h4 class="vflow-panel-title" id="vflow-events-heading">Журнал переходов</h4>
        <div class="vflow-event-filter-row"><label class="meta" for="vflow-event-filter">Фильтр</label><input class="input vflow-event-filter" id="vflow-event-filter" type="search" placeholder="Тип, узел, точка или payload" value="${esc(eventFilter)}" autocomplete="off" aria-label="Фильтр событий"></div>
        <div class="vflow-events" id="vflow-events" role="list" aria-labelledby="vflow-events-heading">${events}</div>
        <div class="vflow-empty" id="vflow-event-empty" role="status" aria-live="polite"${anyMatch ? " hidden" : ""}>${esc(eventEmptyText)}</div>` : `<div class="vflow-empty">Выберите запуск</div>`}</div>
    </div>`;
  }

  async function renderAdminPipelineEditor() {
    if (!state.adminPipelineId) {
      navigate("admin-pipelines");
      return;
    }
    const loaded = Boolean(document.querySelector(".vflow-editor"));
    if (!loaded) showLoading("Редактор пайплайна", "Загружаем редактор пайплайна…");
    let definition;
    let registry;
    let tasks;
    let runs;
    try {
      [definition, registry, tasks, runs] = await Promise.all([
        api(`/api/admin/pipelines/${encodeURIComponent(state.adminPipelineId)}`),
        api("/api/admin/vflow/block-registry"),
        api("/api/admin/vflow/tasks-catalog"),
        api(`/api/admin/vflow/runs?definition_id=${encodeURIComponent(state.adminPipelineId)}`),
      ]);
    } catch (err) {
      if (state.route !== "admin-pipeline-editor") return;
      if (loaded) {
        notify(err.message, "error");
        return;
      }
      showError("Редактор пайплайна", "Не удалось загрузить редактор", err, renderAdminPipelineEditor);
      return;
    }
    if (state.route !== "admin-pipeline-editor") return;
    const version = definition.draft || definition.published;
    if (!state.vflowSourceInitialized || state.vflowDefinitionId !== definition.id) {
      resetVFlowHistory();
      state.vflowSource = version?.source || (version ? "" : "");
      state.vflowEtag = version?.etag || null;
      state.vflowDefinitionId = definition.id;
      state.vflowSourceInitialized = true;
      state.vflowCompile = version?.graph ? {graph: version.graph} : null;
      state.vflowGraph = version?.graph
        ? cloneGraph(version.graph)
        : emptyGraph(definition.title);
      state.vflowGraphDirty = false;
      state.vflowConnecting = null;
      state.vflowPublicSchema = version?.public_output_schema || null;
      state.vflowInputSchema = version?.input_schema || null;
      state.vflowDiagnostics = [];
      state.vflowDiagnosticEdge = null;
      state.vflowTab = "code";
      state.vflowSelectedNode = null;
      state.vflowRunId = null;
      state.vflowZoom = 1;
      state.vflowPanX = 0;
      state.vflowPanY = 0;
    }
    let runDetail = null;
    if (state.vflowRunId) {
      try {
        runDetail = await api(`/api/admin/vflow/runs/${encodeURIComponent(state.vflowRunId)}`);
      } catch {
        state.vflowRunId = null;
      }
    }
    const graph = state.vflowGraph || state.vflowCompile?.graph || version?.graph || null;
    if (!state.vflowGraph && graph) state.vflowGraph = cloneGraph(graph);
    if (!state.vflowGraph) state.vflowGraph = emptyGraph(definition.title);
    const blocks = registry.map(item => `<button class="vflow-block" data-insert="${esc(item.type)}" data-search="${esc(`${item.title} ${item.type} ${item.description || ""}`.toLowerCase())}" type="button" aria-label="Добавить блок ${esc(item.title)} · ${esc(item.type)}"><strong>${esc(item.title)}</strong><small>${esc(item.type)}</small></button>`).join("");
    const taskBlocks = tasks.map(item => `<button class="vflow-block" data-insert-task="${esc(item.id)}" data-search="${esc(`${item.title} ${item.id} ${item.description || ""}`.toLowerCase())}" type="button" aria-label="Добавить задачу ${esc(item.title)} · ${esc(item.id)}"><strong>${esc(item.title)}</strong><small>${esc(item.id)} · ${esc(item.queue_id || "default")}</small></button>`).join("");
    const selectedNode = (state.vflowGraph?.nodes || graph?.nodes || []).find(node => node.id === state.vflowSelectedNode);
    const diagnostics = (state.vflowDiagnostics || []).map(item => {
      const node = item.node_id
        ? `<button class="vflow-diag-node" data-diagnostic-node="${esc(item.node_id)}" type="button" title="Перейти к узлу ${esc(item.node_id)}" aria-label="Перейти к узлу ${esc(item.node_id)}">${esc(item.node_id)}</button>`
        : "";
      const edge = item.edge_id
        ? `<button class="vflow-diag-edge" data-diagnostic-edge="${esc(item.edge_id)}" type="button" title="Показать связь ${esc(item.edge_id)}" aria-label="Показать связь ${esc(item.edge_id)}">Связь ${esc(item.edge_id)}</button>`
        : "";
      return `<div class="vflow-diag-item"><span class="error">${esc(item.message || item.code)}${item.line ? ` · строка ${esc(item.line)}` : ""}</span>${node}${edge}</div>`;
    }).join("");
    const palette = `<label class="vflow-search-label" for="vflow-palette-search">Поиск по библиотеке</label><input class="input vflow-palette-search" id="vflow-palette-search" type="search" placeholder="Поиск блоков и задач" value="${esc(state.vflowPaletteQuery || "")}" autocomplete="off" aria-label="Поиск блоков и задач">${blocks ? `<div class="vflow-palette-group"><h3 class="vflow-panel-title">Системные блоки · ${registry.length}</h3>${blocks}</div>` : ""}<div class="vflow-palette-group"><h3 class="vflow-panel-title">Задачи VALERIA · ${tasks.length}</h3>${taskBlocks || `<div class="hint">Нет доступных задач</div>`}</div><div class="hint" id="vflow-palette-empty" role="status" hidden>Ничего не найдено</div><button class="btn" data-route="admin-tasks" type="button">Создать задачу</button>`;
    const selectedSpec = selectedNode ? registry.find(item => item.type === selectedNode.type) : null;
    const configSchema = selectedSpec?.config_schema || {};
    const requiredKeys = new Set(configSchema.required || []);
    const configValue = (key, fallback) => selectedNode?.config?.[key] ?? fallback;
    const fieldHtml = (key, schema) => {
      const value = configValue(key, schema.default);
      const fieldName = schema.title || key;
      const label = `${esc(fieldName)}${requiredKeys.has(key) ? " *" : ""}`;
      let control = "";
      if (schema.type === "boolean") {
        control = `<input type="checkbox" data-config-key="${esc(key)}" data-config-type="boolean" aria-label="${label}" ${value ? "checked" : ""}>`;
      } else if (schema.enum) {
        const options = [...new Set([...(schema.enum || []), ...(value === undefined ? [] : [value])])];
        control = `<select data-config-key="${esc(key)}" data-config-type="string" aria-label="${label}">${options.map(option => `<option value="${esc(option)}" ${String(option) === String(value ?? "") ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`;
      } else if (key === "task_id" && tasks.length) {
        control = `<select data-config-key="${esc(key)}" data-config-type="string" aria-label="${label}"><option value="">— выберите задачу —</option>${tasks.map(task => `<option value="${esc(task.id)}" ${task.id === value ? "selected" : ""}>${esc(task.title)} · ${esc(task.id)}</option>`).join("")}</select>`;
      } else if (schema.type === "integer" || schema.type === "number") {
        control = `<input type="number" step="${schema.type === "integer" ? "1" : "any"}" data-config-key="${esc(key)}" data-config-type="number" value="${value === undefined ? "" : esc(value)}" aria-label="${label}">`;
      } else if (schema.type === "object" || schema.type === "array" || schema.type === undefined) {
        control = `<textarea class="vflow-node-config compact" data-config-key="${esc(key)}" data-config-type="json" spellcheck="false" aria-label="${label}">${value === undefined ? "" : esc(JSON.stringify(value, null, 2))}</textarea>`;
      } else {
        control = `<input type="text" data-config-key="${esc(key)}" data-config-type="string" value="${value === undefined ? "" : esc(value)}" aria-label="${label}">`;
      }
      return `<label class="vflow-field"><span>${label}</span>${control}${schema.description ? `<small>${esc(schema.description)}</small>` : ""}</label>`;
    };
    const schemaFields = Object.entries(configSchema.properties || {}).map(([key, schema]) => fieldHtml(key, schema)).join("");
    const center = state.vflowTab === "graph"
      ? graphHtml(graph, runDetail?.points || [])
      : state.vflowTab === "runs"
        ? runsPanelHtml(runs, runDetail)
        : `<textarea class="vflow-code" id="vflow-code" spellcheck="false" aria-label="Код VFlow">${esc(state.vflowSource)}</textarea>`;
    const audience = definition.audience || {mode: "all", user_ids: []};
    const audienceIds = Array.isArray(audience.user_ids) ? audience.user_ids.join(", ") : "";
    const audiencePanel = `<details class="vflow-json-fallback" style="margin-bottom:16px" open><summary>Доступ к пайплайну</summary><label class="vflow-field"><span>Режим</span><select id="vflow-audience-mode" aria-label="Режим доступа"><option value="all" ${audience.mode === "all" ? "selected" : ""}>Все пользователи</option><option value="selected" ${audience.mode === "selected" ? "selected" : ""}>Выбранные пользователи</option></select></label><label class="vflow-field"><span>ID пользователей</span><input id="vflow-audience-users" value="${esc(audienceIds)}" placeholder="id1, id2" aria-label="ID пользователей с доступом"></label><button class="btn" id="vflow-save-audience" type="button">Сохранить доступ</button><div class="hint">Для режима «Выбранные» укажите ID через запятую. Пайплайн видят только пользователи из аудитории.</div></details>`;
    const policySettings = definition.settings && typeof definition.settings === "object" ? definition.settings : {};
    const policyLimits = policySettings.limits && typeof policySettings.limits === "object" && !Array.isArray(policySettings.limits) ? policySettings.limits : null;
    const policyLimitsText = policyLimits && Object.keys(policyLimits).length ? JSON.stringify(policyLimits, null, 2) : "";
    const policyPanel = `<details class="vflow-json-fallback" style="margin-bottom:16px" open><summary>Политика</summary><label class="vflow-field"><span>Разрешить отмену запуска пользователем</span><input type="checkbox" id="vflow-policy-cancel-own" aria-label="Разрешить отмену запуска пользователем" ${policySettings.cancel_own !== false ? "checked" : ""}></label><label class="vflow-field"><span>Лимиты (JSON)</span><textarea class="vflow-node-config compact" id="vflow-policy-limits" spellcheck="false" aria-label="Лимиты пайплайна (JSON)" placeholder='{"max_fork_n": 8}'>${esc(policyLimitsText)}</textarea></label><button class="btn" id="vflow-save-policy" type="button">Сохранить политику</button><div class="hint">Пусто — наследовать глобальные лимиты. Политика может только ужесточать: max_fork_n, max_active_points, max_total_points, max_transitions, max_json_bytes, max_json_depth, max_run_seconds.</div></details>`;
    const inputSchema = `<details class="vflow-json-fallback" style="margin-bottom:16px"><summary>Входные данные</summary><textarea class="vflow-node-config compact" id="vflow-input-schema" spellcheck="false" aria-label="Схема входных данных">${esc(state.vflowInputSchema ? JSON.stringify(state.vflowInputSchema, null, 2) : "")}</textarea><div class="hint">JSON-схема полей формы запуска. Пусто — форма без полей.</div></details>`;
    const publicSchema = `<details class="vflow-json-fallback" style="margin-bottom:16px"><summary>Публичный результат</summary><textarea class="vflow-node-config compact" id="vflow-public-schema" spellcheck="false" aria-label="Схема публичного результата">${esc(state.vflowPublicSchema ? JSON.stringify(state.vflowPublicSchema, null, 2) : "")}</textarea><div class="hint">JSON-схема разрешённых полей результата. Пусто — результат недоступен.</div></details>`;
    const inspector = audiencePanel + policyPanel + inputSchema + publicSchema + (selectedNode ? `<h3 class="vflow-node-title">${esc(selectedNode.id)}</h3><div class="meta">${esc(selectedNode.type)} · v${esc(selectedNode.type_version || 1)}</div><h4 class="vflow-panel-title" style="margin-top:16px">Конфигурация</h4>${schemaFields || `<div class="hint">Для этого типа нет схемы полей.</div>`}<button class="btn btn-primary" id="vflow-apply-node" type="button" style="margin-top:8px">Применить поля</button><details class="vflow-json-fallback"><summary>JSON конфигурации</summary><textarea class="vflow-node-config" id="vflow-node-config" spellcheck="false" aria-label="JSON конфигурации узла">${esc(JSON.stringify(selectedNode.config || {}, null, 2))}</textarea><button class="btn" id="vflow-apply-json" type="button">Применить JSON</button></details><button class="btn danger" id="vflow-delete-node" type="button" style="margin-top:12px">Удалить узел</button><div class="hint">После изменения нажмите «Сохранить».</div>` : `<div class="vflow-empty">Выберите узел в графе</div>`);
    const activeTabLabel = state.vflowTab === "graph" ? "Граф" : state.vflowTab === "runs" ? "Запуски" : "Код";
    app.innerHTML = shell(`<div class="vflow-editor">
      <aside class="vflow-palette" aria-label="Библиотека блоков"><h3 class="vflow-panel-title">VFlow · библиотека</h3>${palette}<p class="hint">Только задачи VALERIA. Новые типы блоков добавляются разработчиком.</p></aside>
      <main class="vflow-canvas">
        <h2 class="vflow-sr-only" id="vflow-editor-title" tabindex="-1">Редактор пайплайна ${esc(definition.title)}</h2>
        <div class="vflow-toolbar" role="toolbar" aria-label="Действия с пайплайном" aria-orientation="horizontal"><strong class="vflow-name">${esc(definition.title)}</strong><span class="badge">v${esc(version?.version_number || 1)}</span><span class="badge">${esc(version?.status || "draft")}</span><span class="spacer"></span><button class="btn" id="vflow-undo" type="button" title="Отменить (Ctrl+Z)" aria-label="Отменить" aria-keyshortcuts="Control+Z Meta+Z" disabled>↶ Отменить</button><button class="btn" id="vflow-redo" type="button" title="Повторить (Ctrl+Shift+Z)" aria-label="Повторить" aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z" disabled>↷ Повторить</button><button class="btn" id="vflow-save" type="button">Сохранить</button><button class="btn" id="vflow-validate" type="button">Проверить</button><button class="btn btn-primary" id="vflow-publish" type="button">Опубликовать</button><button class="btn" id="vflow-run-now" type="button">Запустить</button></div>
        <div class="vflow-tabs" role="tablist" aria-label="Разделы редактора"><button id="vflow-tab-code" class="${state.vflowTab === "code" ? "active" : ""}" data-vflow-tab="code" type="button" role="tab" aria-selected="${state.vflowTab === "code"}" aria-controls="vflow-tab-panel" tabindex="${state.vflowTab === "code" ? "0" : "-1"}">Код</button><button id="vflow-tab-graph" class="${state.vflowTab === "graph" ? "active" : ""}" data-vflow-tab="graph" type="button" role="tab" aria-selected="${state.vflowTab === "graph"}" aria-controls="vflow-tab-panel" tabindex="${state.vflowTab === "graph" ? "0" : "-1"}">Граф</button><button id="vflow-tab-runs" class="${state.vflowTab === "runs" ? "active" : ""}" data-vflow-tab="runs" type="button" role="tab" aria-selected="${state.vflowTab === "runs"}" aria-controls="vflow-tab-panel" tabindex="${state.vflowTab === "runs" ? "0" : "-1"}">Запуски</button></div>
        <div class="vflow-tab-panel" id="vflow-tab-panel" role="tabpanel" aria-labelledby="vflow-tab-${state.vflowTab}" aria-label="${esc(activeTabLabel)}">${center}</div>
        <div class="vflow-diag" id="vflow-diag" role="region" tabindex="-1" aria-label="Диагностика">${diagnostics || `<span>Проверка ещё не запускалась</span>`}</div>
      </main>
      <aside class="vflow-inspector" id="vflow-inspector" aria-label="Инспектор"><h3 class="vflow-panel-title">Инспектор</h3>${inspector}</aside>
    </div>`, "Редактор пайплайна");
    bindShell();
    bindToolbarNavigation();
    bindGraphControls();
    const eventFilterInput = document.querySelector("#vflow-event-filter");
    if (eventFilterInput) {
      eventFilterInput.addEventListener("input", event => applyEventFilter(event.currentTarget.value));
      applyEventFilter(state.vflowEventFilter || "");
    }
    document.querySelectorAll("[data-diagnostic-node]").forEach(button => {
      button.addEventListener("click", () => {
        const nodeId = button.dataset.diagnosticNode || "";
        const exists = (state.vflowGraph?.nodes || []).some(node => node.id === nodeId);
        if (!nodeId || !exists) {
          notify("Узел из диагностики больше не существует в графе", "error");
          return;
        }
        flushCodeHistory();
        state.vflowSelectedNode = nodeId;
        state.vflowDiagnosticEdge = null;
        state.vflowTab = "graph";
        queueFocus({attr: "data-node-select", value: nodeId, fallback: {id: "vflow-graph-canvas"}});
        void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
      });
    });
    document.querySelectorAll("[data-diagnostic-edge]").forEach(button => {
      button.addEventListener("click", () => {
        const edgeId = button.dataset.diagnosticEdge || "";
        const edges = state.vflowGraph?.edges || [];
        const index = edges.findIndex(edge => edgeMatchesDiagnostic(edge, edgeId));
        if (index < 0) {
          notify("Связь из диагностики больше не существует в графе", "error");
          return;
        }
        flushCodeHistory();
        const edge = edges[index];
        const nodeIds = new Set((state.vflowGraph?.nodes || []).map(node => node.id));
        const preferred = [edge.source, edge.target].find(id => nodeIds.has(id));
        if (preferred) state.vflowSelectedNode = preferred;
        state.vflowDiagnosticEdge = index;
        state.vflowTab = "graph";
        void renderAdminPipelineEditor().then(() => {
          const chips = [...document.querySelectorAll(".vflow-edge-chip")];
          const chip = chips[index] || chips.find(item => item.dataset.edgeKey === edgeId);
          if (!chip) return;
          chip.focus({preventScroll: true});
          chip.scrollIntoView({
            block: "nearest",
            behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth",
          });
        }).catch(err => notify(err.message, "error"));
      });
    });
    document.querySelector("#vflow-undo")?.addEventListener("click", () => void undoVFlowEdit());
    document.querySelector("#vflow-redo")?.addEventListener("click", () => void redoVFlowEdit());
    updateHistoryButtons();
    const tabs = [...document.querySelectorAll("[data-vflow-tab]")];
    tabs.forEach((button, index) => {
      button.addEventListener("click", () => {
        void switchVFlowTab(button.dataset.vflowTab);
      });
      button.addEventListener("keydown", event => {
        let nextIndex = null;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") nextIndex = 0;
        else if (event.key === "End") nextIndex = tabs.length - 1;
        if (nextIndex == null) return;
        event.preventDefault();
        const next = tabs[nextIndex];
        next.focus({preventScroll: true});
        void switchVFlowTab(next.dataset.vflowTab);
      });
    });
    document.querySelector("#vflow-code")?.addEventListener("input", event => {
      markInvalid("#vflow-code", false);
      scheduleCodeHistory();
      state.vflowSource = event.target.value;
      state.vflowDirty = true;
      updateHistoryButtons();
    });
    document.querySelector("#vflow-input-schema")?.addEventListener("input", () => {
      state.vflowDirty = true;
    });
    document.querySelector("#vflow-public-schema")?.addEventListener("input", () => {
      state.vflowDirty = true;
    });
    const applyPaletteFilter = value => {
      state.vflowPaletteQuery = String(value || "");
      const query = state.vflowPaletteQuery.trim().toLowerCase();
      const blocksInPalette = [...document.querySelectorAll(".vflow-palette .vflow-block")];
      let visible = 0;
      blocksInPalette.forEach(button => {
        button.hidden = Boolean(query) && !(button.dataset.search || "").includes(query);
        if (!button.hidden) visible += 1;
      });
      const empty = document.querySelector("#vflow-palette-empty");
      if (empty) empty.hidden = !query || visible > 0;
    };
    const paletteSearch = document.querySelector("#vflow-palette-search");
    if (paletteSearch) {
      paletteSearch.addEventListener("input", event => applyPaletteFilter(event.target.value));
      applyPaletteFilter(state.vflowPaletteQuery || "");
    }
    document.querySelector("#vflow-save-audience")?.addEventListener("click", async () => {
      const mode = document.querySelector("#vflow-audience-mode")?.value || "all";
      const usersField = document.querySelector("#vflow-audience-users");
      const ids = (usersField?.value || "")
        .split(",")
        .map(item => item.trim())
        .filter(Boolean);
      if (mode === "selected" && !ids.length) {
        markInvalid("#vflow-audience-users");
        usersField?.focus({preventScroll: true});
        notify("Укажите хотя бы одного пользователя", "error");
        return;
      }
      markInvalid("#vflow-audience-users", false);
      try {
        await api(`/api/admin/pipelines/${encodeURIComponent(definition.id)}/audience`, {
          method: "PUT",
          body: JSON.stringify({mode, user_ids: ids}),
        });
        definition.audience = {mode, user_ids: mode === "selected" ? ids : []};
        notify("Доступ обновлён");
      } catch (err) {
        notify(err.message, "error");
      }
    });
    document.querySelector("#vflow-save-policy")?.addEventListener("click", async () => {
      const cancelOwn = Boolean(document.querySelector("#vflow-policy-cancel-own")?.checked);
      const limitsField = document.querySelector("#vflow-policy-limits");
      const limitsText = (limitsField?.value || "").trim();
      const settings = {cancel_own: cancelOwn};
      if (limitsText) {
        let limits;
        try {
          limits = JSON.parse(limitsText);
        } catch {
          markInvalid("#vflow-policy-limits");
          limitsField?.focus({preventScroll: true});
          notify("Некорректный JSON лимитов", "error");
          return;
        }
        if (!limits || typeof limits !== "object" || Array.isArray(limits)) {
          markInvalid("#vflow-policy-limits");
          limitsField?.focus({preventScroll: true});
          notify("Лимиты должны быть JSON-объектом", "error");
          return;
        }
        markInvalid("#vflow-policy-limits", false);
        if (Object.keys(limits).length) settings.limits = limits;
      } else {
        markInvalid("#vflow-policy-limits", false);
      }
      try {
        const saved = await api(`/api/admin/pipelines/${encodeURIComponent(definition.id)}/settings`, {
          method: "PUT",
          body: JSON.stringify({settings}),
        });
        definition.settings = saved.settings || settings;
        notify("Политика сохранена");
      } catch (err) {
        notify(err.message, "error");
      }
    });
    document.querySelectorAll("[data-node-container]").forEach(button => {
      const nodeId = button.dataset.nodeContainer;
      const selectNode = () => {
        state.vflowSelectedNode = nodeId;
        state.vflowDiagnosticEdge = null;
        queueFocus({attr: "data-node-select", value: nodeId, fallback: {id: "vflow-graph-canvas"}});
        void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
      };
      button.addEventListener("click", selectNode);
      let drag = null;
      button.addEventListener("pointerdown", event => {
        if (event.target.closest(".vflow-port")) return;
        drag = {
          x: event.clientX,
          y: event.clientY,
          nodeX: button.offsetLeft,
          nodeY: button.offsetTop,
          moved: false,
          history: captureHistoryEntry(),
        };
        button.setPointerCapture(event.pointerId);
      });
      button.addEventListener("pointermove", event => {
        if (!drag) return;
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        if (!drag.moved) return;
        const node = state.vflowGraph.nodes.find(item => item.id === nodeId);
        if (!node) return;
        const zoom = clampGraphZoom(state.vflowZoom);
        node.ui = {...(node.ui || {}), x: Math.max(0, drag.nodeX + dx / zoom), y: Math.max(0, drag.nodeY + dy / zoom)};
        button.style.left = `${node.ui.x}px`;
        button.style.top = `${node.ui.y}px`;
        state.vflowGraphDirty = true;
      });
      button.addEventListener("pointerup", () => {
        if (drag?.moved) {
          if (drag.history && historyEntryKey(drag.history) !== historyEntryKey(captureHistoryEntry())) {
            pushVFlowHistory(drag.history);
          }
          state.vflowDirty = true;
          queueFocus({attr: "data-node-select", value: nodeId, fallback: {id: "vflow-graph-canvas"}});
          void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
        }
        drag = null;
      });
    });
    document.querySelectorAll("[data-source]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      state.vflowConnecting = {
        source: button.dataset.source,
        source_port: button.dataset.sourcePort || "out",
      };
      notify("Выберите вход целевого узла");
    }));
    document.querySelectorAll("[data-target]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      if (!state.vflowConnecting) return;
      const target = button.dataset.target;
      const {source, source_port: sourcePort} = state.vflowConnecting;
      if (!target || target === source) return;
      if (target === "start") return;
      const exists = state.vflowGraph.edges.some(edge =>
        edge.source === source && edge.source_port === sourcePort && edge.target === target);
      if (!exists) {
        pushVFlowHistory();
        state.vflowGraph.edges.push({source, source_port: sourcePort, target});
        if (source === "start") state.vflowGraph.entry = target;
      }
      state.vflowConnecting = null;
      state.vflowGraphDirty = true;
      state.vflowDirty = true;
      queueFocus({
        attr: "data-edge-key",
        value: edgeKey({source, source_port: sourcePort, target}),
        fallback: {attr: "data-node-select", value: target},
      });
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    }));
    const commitNodeConfig = (config, focusSpec = {id: "vflow-apply-node"}) => {
      const node = state.vflowGraph.nodes.find(item => item.id === state.vflowSelectedNode);
      if (!node) return;
      if (JSON.stringify(node.config || {}) !== JSON.stringify(config)) pushVFlowHistory();
      node.config = config;
      state.vflowGraphDirty = true;
      state.vflowDirty = true;
      notify("Конфигурация обновлена");
      queueFocus(focusSpec);
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    };
    document.querySelector("#vflow-apply-node")?.addEventListener("click", () => {
      const node = state.vflowGraph.nodes.find(item => item.id === state.vflowSelectedNode);
      if (!node) return;
      const config = { ...(node.config || {}) };
      try {
        document.querySelectorAll("#vflow-inspector [data-config-key]").forEach(field => {
          field.removeAttribute("aria-invalid");
          const key = field.dataset.configKey;
          const type = field.dataset.configType;
          if (type === "boolean") {
            config[key] = field.checked;
          } else if (type === "number") {
            if (field.value === "") delete config[key];
            else {
              const value = Number(field.value);
              const bounds = configSchema.properties?.[key] || {};
              const invalid = !Number.isFinite(value)
                || (bounds.minimum !== undefined && value < bounds.minimum)
                || (bounds.maximum !== undefined && value > bounds.maximum);
              if (invalid) {
                field.setAttribute("aria-invalid", "true");
                throw {field};
              }
              config[key] = value;
            }
          } else if (type === "json") {
            if (field.value.trim() === "") delete config[key];
            else {
              try {
                config[key] = JSON.parse(field.value);
              } catch (error) {
                field.setAttribute("aria-invalid", "true");
                throw {field, cause: error};
              }
              if (["json_schema", "output_schema"].includes(key) && (
                !config[key] || typeof config[key] !== "object" || Array.isArray(config[key])
              )) {
                field.setAttribute("aria-invalid", "true");
                throw {field};
              }
            }
          } else if (field.value === "" && !["task_id"].includes(key)) {
            delete config[key];
          } else {
            config[key] = field.value;
          }
        });
        commitNodeConfig(config);
      } catch (error) {
        const invalid = error?.field;
        invalid?.focus?.({preventScroll: true});
        notify("Проверьте числовые и JSON-поля конфигурации", "error");
      }
    });
    document.querySelector("#vflow-apply-json")?.addEventListener("click", () => {
      const field = document.querySelector("#vflow-node-config");
      try {
        commitNodeConfig(JSON.parse(field.value), {
          attr: "data-node-select",
          value: state.vflowSelectedNode,
          fallback: {id: "vflow-apply-node"},
        });
      } catch {
        markInvalid("#vflow-node-config");
        field?.focus({preventScroll: true});
        notify("Некорректный JSON конфигурации", "error");
      }
    });
    document.querySelector("#vflow-delete-node")?.addEventListener("click", () => {
      const nodeId = state.vflowSelectedNode;
      if (!nodeId || !confirm(`Удалить узел ${nodeId}?`)) return;
      pushVFlowHistory();
      state.vflowGraph.nodes = state.vflowGraph.nodes.filter(node => node.id !== nodeId);
      state.vflowGraph.edges = state.vflowGraph.edges.filter(edge => edge.source !== nodeId && edge.target !== nodeId);
      if (state.vflowGraph.entry === nodeId) state.vflowGraph.entry = "";
      state.vflowSelectedNode = null;
      state.vflowConnecting = null;
      state.vflowGraphDirty = true;
      state.vflowDirty = true;
      queueFocus({id: "vflow-graph-canvas", fallback: {id: "vflow-palette-search"}});
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    });
    document.querySelectorAll("[data-delete-edge]").forEach(button => button.addEventListener("click", () => {
      const edgeIndex = Number(button.dataset.deleteEdge);
      pushVFlowHistory();
      state.vflowGraph.edges.splice(edgeIndex, 1);
      state.vflowDiagnosticEdge = null;
      state.vflowGraphDirty = true;
      state.vflowDirty = true;
      const nextEdge = state.vflowGraph.edges[edgeIndex] || state.vflowGraph.edges[edgeIndex - 1];
      queueFocus(nextEdge
        ? {attr: "data-edge-key", value: edgeKey(nextEdge)}
        : {id: "vflow-graph-canvas", fallback: {id: "vflow-palette-search"}});
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    }));
    const insert = text => {
      const area = document.querySelector("#vflow-code");
      if (!area) return;
      const start = area.selectionStart;
      area.value = area.value.slice(0, start) + text + area.value.slice(area.selectionEnd);
      area.dispatchEvent(new Event("input"));
      area.focus();
    };
    const addNode = (type, taskId = "") => {
      const base = type === "valeria.task" ? "task" : type.split(".").pop();
      let index = 1;
      while (state.vflowGraph.nodes.some(node => node.id === `${base}_${index}`)) index += 1;
      const id = `${base}_${index}`;
      pushVFlowHistory();
      state.vflowGraph.nodes.push({
        id,
        type,
        type_version: "1.0",
        config: defaultConfig(type, taskId),
        ui: {x: 80 + state.vflowGraph.nodes.length * 30, y: 80 + state.vflowGraph.nodes.length * 24},
      });
      state.vflowSelectedNode = id;
      state.vflowDiagnosticEdge = null;
      state.vflowGraphDirty = true;
      state.vflowDirty = true;
      notify(`Добавлен узел ${id}`);
      queueFocus({attr: "data-node-select", value: id, fallback: {id: "vflow-graph-canvas"}});
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    };
    document.querySelectorAll("[data-insert]").forEach(button => button.addEventListener("click", () => {
      if (state.vflowTab === "graph") {
        addNode(button.dataset.insert);
        return;
      }
      const snippets = {
        "core.transform": `step transform_1 = transform(operations: [{op: "set", path: "$.ok", value: true}]);\n`,
        "core.if": `step decision = if(path: "$.ok", operator: "eq", value: true, missing: "error");\n`,
        "core.fork": `step split = fork(count: 2, join: merge_1);\n`,
        "core.merge": `step merge_1 = merge();\n`,
      };
      insert(snippets[button.dataset.insert] || "");
    }));
    document.querySelectorAll("[data-insert-task]").forEach(button => button.addEventListener("click", () => {
      if (state.vflowTab === "graph") {
        addNode("valeria.task", button.dataset.insertTask);
        return;
      }
      insert(`step task_1 = task.run(task_id: "${button.dataset.insertTask}", input_path: "$");\n`);
    }));

    async function switchVFlowTab(next) {
      state.vflowSource = document.querySelector("#vflow-code")?.value ?? state.vflowSource;
      flushCodeHistory();
      if (next === "graph" && state.vflowSource.trim() && (state.vflowDirty || !state.vflowGraph)) {
        try {
          const compiled = await api("/api/admin/vflow/compile", {
            method: "POST",
            body: JSON.stringify({source: state.vflowSource}),
          });
          if (JSON.stringify(state.vflowGraph || null) !== JSON.stringify(compiled.graph || null)) {
            pushVFlowHistory();
          }
          state.vflowCompile = compiled;
          state.vflowGraph = cloneGraph(compiled.graph);
          state.vflowDirty = false;
          state.vflowGraphDirty = false;
        } catch (err) {
          markInvalid("#vflow-code");
          document.querySelector("#vflow-code")?.focus({preventScroll: true});
          notify(err.message, "error");
          return;
        }
      }
      if (next === "code" && state.vflowGraphDirty) {
        try {
          const formatted = await api("/api/admin/vflow/format", {
            method: "POST",
            body: JSON.stringify({graph: state.vflowGraph}),
          });
          if ((formatted.source || "") !== (state.vflowSource || "")) pushVFlowHistory();
          state.vflowSource = formatted.source;
          state.vflowGraphDirty = false;
          state.vflowDirty = false;
        } catch (err) {
          notify(err.message, "error");
          return;
        }
      }
      state.vflowTab = next;
      state.vflowDiagnosticEdge = null;
      await renderAdminPipelineEditor();
      document.querySelector(`[data-vflow-tab="${next}"]`)?.focus({preventScroll: true});
    }
    document.querySelector("#vflow-save")?.addEventListener("click", () => void saveDraft());
    document.querySelector("#vflow-validate")?.addEventListener("click", () => void validateDraft());
    document.querySelector("#vflow-publish")?.addEventListener("click", () => void publishDraft());
    document.querySelectorAll("[data-vflow-run]").forEach(button => button.addEventListener("click", () => {
      state.vflowRunId = button.dataset.vflowRun;
      state.vflowTab = "runs";
      queueFocus({attr: "data-vflow-run", value: button.dataset.vflowRun, fallback: {id: "vflow-tab-runs"}});
      void renderAdminPipelineEditor().catch(err => notify(err.message, "error"));
    }));
    document.querySelector("#vflow-run-cancel")?.addEventListener("click", async () => {
      try {
        await api(`/api/admin/vflow/runs/${encodeURIComponent(state.vflowRunId)}/cancel`, {method: "POST"});
        notify("Запуск отменён");
        queueFocus({attr: "data-vflow-run", value: state.vflowRunId, fallback: {id: "vflow-tab-runs"}});
        await renderAdminPipelineEditor();
      } catch (err) {
        notify(err.message, "error");
      }
    });
    document.querySelector("#vflow-run-now")?.addEventListener("click", async () => {
      try {
        const run = await api(`/api/pipelines/${encodeURIComponent(definition.slug)}/runs`, {
          method: "POST",
          body: JSON.stringify({input: {}}),
        });
        state.vflowRunId = run.id;
        state.vflowTab = "runs";
        notify("Запуск создан");
        queueFocus({attr: "data-vflow-run", value: run.id, fallback: {id: "vflow-tab-runs"}});
        await renderAdminPipelineEditor();
      } catch (err) {
        notify(err.message, "error");
      }
    });

    async function saveDraft() {
      state.vflowSource = document.querySelector("#vflow-code")?.value ?? state.vflowSource;
      flushCodeHistory();
      const inputField = document.querySelector("#vflow-input-schema");
      let inputSchema = state.vflowInputSchema;
      const inputText = inputField?.value.trim();
      if (inputText) {
        try {
          inputSchema = JSON.parse(inputText);
          markInvalid("#vflow-input-schema", false);
        } catch {
          markInvalid("#vflow-input-schema");
          inputField?.focus({preventScroll: true});
          notify("Некорректная JSON-схема входных данных", "error");
          return;
        }
      } else {
        inputSchema = {};
        markInvalid("#vflow-input-schema", false);
      }
      const publicField = document.querySelector("#vflow-public-schema");
      let publicSchema = state.vflowPublicSchema;
      const publicText = publicField?.value.trim();
      if (publicText) {
        try {
          publicSchema = JSON.parse(publicText);
          markInvalid("#vflow-public-schema", false);
        } catch {
          markInvalid("#vflow-public-schema");
          publicField?.focus({preventScroll: true});
          notify("Некорректная JSON-схема публичного результата", "error");
          return;
        }
      } else {
        publicSchema = null;
        markInvalid("#vflow-public-schema", false);
      }
      try {
        const headers = state.vflowEtag ? {"If-Match": state.vflowEtag} : {};
        const body = state.vflowGraphDirty
          ? {graph: state.vflowGraph}
          : {source: state.vflowSource};
        body.input_schema = inputSchema;
        body.public_output_schema = publicSchema;
        const saved = await api(`/api/admin/pipelines/${encodeURIComponent(definition.id)}/draft`, {
          method: "PUT", headers, body: JSON.stringify(body),
        });
        state.vflowEtag = saved.etag;
        state.vflowInputSchema = saved.input_schema ?? null;
        state.vflowPublicSchema = saved.public_output_schema ?? null;
        state.vflowSource = saved.source ?? state.vflowSource;
        state.vflowGraph = saved.graph ? cloneGraph(saved.graph) : state.vflowGraph;
        state.vflowCompile = saved.graph ? {graph: saved.graph} : state.vflowCompile;
        state.vflowDirty = false;
        state.vflowGraphDirty = false;
        notify("Черновик сохранён");
        queueFocus({id: "vflow-save", fallback: {id: `vflow-tab-${state.vflowTab}`}});
        await renderAdminPipelineEditor();
      } catch (err) {
        notify(err.message, "error");
      }
    }

    async function validateDraft() {
      try {
        const report = await api(`/api/admin/pipelines/${encodeURIComponent(definition.id)}/validate`, {method: "POST", body: "{}"});
        state.vflowDiagnostics = report.diagnostics || [];
        state.vflowDiagnosticEdge = null;
        state.vflowCompile = report.graph ? {graph: report.graph} : state.vflowCompile;
        notify(report.valid ? "Проверка пройдена" : "Найдены ошибки", report.valid ? "status" : "error");
        queueFocus(report.valid
          ? {id: "vflow-validate"}
          : {id: "vflow-diag", fallback: {id: "vflow-validate"}});
        await renderAdminPipelineEditor();
      } catch (err) {
        notify(err.message, "error");
      }
    }

    async function publishDraft() {
      try {
        const headers = state.vflowEtag ? {"If-Match": state.vflowEtag} : {};
        await api(`/api/admin/pipelines/${encodeURIComponent(definition.id)}/publish`, {method: "POST", headers, body: "{}"});
        notify("Пайплайн опубликован");
        queueFocus({id: "vflow-publish", fallback: {id: `vflow-tab-${state.vflowTab}`}});
        await renderAdminPipelineEditor();
      } catch (err) {
        notify(err.message, "error");
      }
    }

    focusPageHeading("vflow-editor-title", loaded);
  }

  document.addEventListener("keydown", handleGraphKeydown);
  document.addEventListener("keydown", handleEditorHistoryKeydown);

  return {renderPipelines, renderAdminPipelines, renderAdminPipelineEditor};
}
