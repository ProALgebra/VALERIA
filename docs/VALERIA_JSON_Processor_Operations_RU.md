# VALERIA JSON Repair Processor — эксплуатационное руководство

**Статус:** описывает фактическую реализацию M0–M11 в текущем рабочем дереве.
**Дата:** 22.11.2026.
**Аудитория:** администраторы VALERIA, DevOps/сопровождение, разработчики интеграций Task/VFlow.
**Область:** самостоятельный модуль `src/opencode_web/json_processor/`, его admin API/UI и интеграция с VFlow/Task.

Документ отвечает на практические вопросы: как включить процессор, какие настройки задать, как проверить провайдера, что происходит с Task и VFlow, как читать ошибки, метрики и retention, как безопасно внедрить модуль и как откатиться.

**Связанный ADR:** [VALERIA_JSON_Processor_ADR_RU.md](VALERIA_JSON_Processor_ADR_RU.md).
**Эксплуатация VFlow:** [VALERIA_VFlow_Operations_RU.md](VALERIA_VFlow_Operations_RU.md), раздел 7.7.1.

---

## Содержание

1. [Назначение, границы и связь с VFlow](#1-назначение-границы-и-связь-с-vflow)
2. [Архитектура и компоненты](#2-архитектура-и-компоненты)
3. [Состояния, Task и контракт output](#3-состояния-task-и-контракт-output)
4. [Справочник настроек](#4-справочник-настроек)
5. [OpenAI-compatible провайдер](#5-openai-compatible-провайдер)
6. [Административный UI и API](#6-административный-ui-и-api)
7. [Настройка Task и VFlow](#7-настройка-task-и-vflow)
8. [Последовательность обработки](#8-последовательность-обработки)
9. [Безопасность](#9-безопасность)
10. [Retention и метрики](#10-retention-и-метрики)
11. [Очередь, worker, recovery и отмена](#11-очередь-worker-recovery-и-отмена)
12. [Коды ошибок и диагностика](#12-коды-ошибок-и-диагностика)
13. [Миграция с legacy same-chat repair и rollout](#13-миграция-с-legacy-same-chat-repair-и-rollout)
14. [Тестирование](#14-тестирование)
15. [Известные ограничения](#15-известные-ограничения)
16. [Связанные документы и исходники](#16-связанные-документы-и-исходники)

---

## 1. Назначение, границы и связь с VFlow

### 1.1. Назначение

VALERIA получает от OpenCode/Task ответы, которые должны быть JSON. Модель может:

- обернуть JSON в markdown-ограждение;
- добавить пояснение до или после JSON;
- оставить trailing comma;
- использовать одинарные кавычки;
- вернуть Python-литералы `True`/`False`/`None`;
- вернуть несколько JSON-блоков или свободный текст.

JSON Repair Processor — отдельный ограниченный компонент, который:

1. сохраняет исходный ответ и его идентичность;
2. строго разбирает JSON;
3. проверяет безопасное подмножество JSON Schema;
4. при необходимости выполняет консервативный локальный repair;
5. при необходимости делает ограниченное число LLM-вызовов через OpenAI-compatible provider;
6. повторно проверяет JSON, схему и сохранность содержимого;
7. либо отдаёт проверенное значение, либо завершается fail-closed.

Ключевой принцип: **repair не должен незаметно менять смысл ответа**. Исправляется синтаксис и, в разрешённых случаях, заранее перечисленные детерминированные преобразования. Свободное переписывание фактов, чисел, имён и структуры запрещено.

### 1.2. Что входит в область

- Локальный детерминированный repair.
- Проверка JSON Schema в безопасном подмножестве.
- LLM repair через `POST /v1/chat/completions`.
- Сохранение job/attempt metadata, idempotency, lease/CAS.
- Отдельная очередь и worker.
- Admin API и admin UI.
- Метрики, аудит, retention preview/purge.
- Интеграция с VFlow через `output_mode="json"`.
- Fail-closed контракт ошибок.

### 1.3. Что не входит в область (non-goals)

- Процессор **не является агентом**: нет tools, function calling, MCP, shell, filesystem, browser и side effects.
- Процессор **не продолжает OpenCode-сессию** и не получает полный контекст задачи, workspace или секреты.
- Процессор **не исправляет смысл**: отсутствующие поля не изобретаются, неверные факты не «уточняются», типы не подменяются.
- Процессор **не заменяет** Task Manager, OpenCode, очередь `queue_jobs` или VFlow runtime.
- Процессор **не является полноценным JSON Schema validator**: поддерживается только безопасное подмножество.
- Процессор **не управляет** retention внешних `task_run_results` и других таблиц Task/VFlow.
- Процессор **не гарантирует** доступность провайдера и не делает бесконечных retry.
- Процессор **не выполняет** бизнес-валидацию сверх заданной схемы.

### 1.4. Связь с VFlow

VFlow вызывает процессор только в ветке Task с `output_mode="json"`:

```text
Task Run (queue_jobs.status = completed)
        │
        │ task_run_results.text — исходный финальный текст
        ▼
VFlow point (claimed)
        │
        ├── строгий JSON + schema OK ──────────────► advance(out), job не создаётся
        │
        └── JSON/schema invalid, on_json_error=repair
                    │
                    ▼
        json_repair_jobs (source_type='vflow', queued)
                    │
                    ▼
        JsonRepairWorker ─► JsonRepairService ─► parser/schema/local/LLM
                    │
                    ├── succeeded ─► result_json ─► VFlow advance(out)
                    └── failed/cancelled ─► Run fails OUTPUT_INVALID
```

Важные свойства:

- Task Run завершается штатно (`completed`); процессор не перезапускает задачу и не меняет её текст.
- VFlow-точка на время ожидания находится в `processing_output` и хранит `json_processor_job_id`.
- Процессор использует **отдельную** таблицу jobs и **отдельный** worker; это не `queue_jobs` и не слот OpenCode.
- Legacy same-chat repair и новый processor path **не должны работать одновременно** для одного ответа. Это обеспечивается на уровне VFlow runtime и компилятора (см. раздел 7).
- При выключенном или ненастроенном процессоре узел с `on_json_error="repair"` завершает Run ошибкой `JSON_PROCESSOR_NOT_CONFIGURED`, а не пропускает невалидный JSON дальше.

---

## 2. Архитектура и компоненты

### 2.1. Пакет и слои

```text
src/opencode_web/json_processor/
├── models.py       — enum-ы, ProcessorError, ParseResult, RepairResult,
│                     RepairAttempt, error envelope, сравнение JSON-значений
├── parser.py       — строгий json.loads и консервативный локальный repair
├── schema.py       — безопасное подмножество JSON Schema
├── content.py      — сравнение decoded values и ContentAllowance
├── prompt.py       — ограниченный prompt builder
├── settings.py     — JsonProcessorSettings и валидация
├── repository.py   — SQLite: settings, jobs, attempts, lease, idempotency, retention
├── service.py      — pipeline, orchestration, provider/key resolution, finalize
├── provider.py     — OpenAI-compatible HTTP client и SSRF-проверки
├── worker.py       — очередь, concurrency, claim/finalize, recovery
├── integration.py  — VFlow adapter, env api-key resolver, сборка worker
├── api.py          — admin HTTP API
└── __init__.py     — публичные экспорты
```

UI:

- `src/opencode_web/static/json_processor.js`
- `src/opencode_web/static/json_processor.css`
- маршрут `#/admin-json-processor` в `app.js`.

Точка подключения:

- `src/opencode_web/app.py` — lifespan: проверка настроек, сборка worker, recovery при старте, запуск/остановка worker, регистрация API.
- `src/opencode_web/database.py` — таблицы `json_processor_settings`, `json_repair_jobs`, `json_repair_attempts` и миграции.
- `src/opencode_web/vflow/runtime.py` — `processing_output`, создание/опрос processor job, отмена.
- `src/opencode_web/vflow/task_output.py` — строгий parse/schema и конверт `output`/`task.output`.
- `src/opencode_web/vflow/compiler.py` — аргументы `task.run`, запрет двух repair-движков.

### 2.2. Компоненты

| Компонент | Файл | Ответственность |
|---|---|---|
| Parser | `parser.py` | Сначала `json.loads` всего вывода; затем одно markdown-ограждение, trailing commas, безопасные кавычки/литералы, один однозначный object/array из prose. Не изобретает поля. |
| Schema validator | `schema.py` | Проверяет схему до repair и значение после. Стабильные пути `$.field`, `$.items[0]`. Внешние `$ref` запрещены. |
| Content preservation | `content.py` | Сравнивает decoded values до/после. `syntax_only` запрещает любые изменения; `schema_safe` допускает только per-request `ContentAllowance`. |
| Prompt builder | `prompt.py` | Собирает bounded prompt: system + user, последняя диагностика, один последний кандидат. Raw в delimiters. Без tools и истории. |
| Settings | `settings.py` | Валидирует singleton-настройки, не принимает secret-поля, не эхо-выводит значения при ошибке. |
| Repository | `repository.py` | Persistence: settings CAS, jobs, attempts, canonical idempotency, claim/lease/CAS, recovery, retention. Не вызывает provider. |
| Service | `service.py` | Пайплайн: raw load + hash check, parse, schema, local, LLM, content, finalize, cancellation checks, provider/key resolution. |
| Worker | `worker.py` | Отдельный цикл: recovery, claim due, bounded concurrency, вызов service, обработка outcome, failure accounting. |
| Provider | `provider.py` | Один non-streaming Chat Completions запрос, `/v1/models` check, `response_format` fallback, SSRF-валидация, typed errors. |
| Integration | `integration.py` | `JsonProcessorVFlowAdapter`, `resolve_env_api_key_ref`, `build_worker`, загрузка raw из `task_run_results`, отмена по VFlow Run. |
| Admin API | `api.py` | `/api/admin/json-processor/*`: settings, providers, connection check, sandbox, jobs, metrics, retention. |
| Admin UI | `json_processor.js` | Настройки, sandbox, метрики, jobs/detail, retention purge, optimistic version и CSRF через общий API-клиент. |

### 2.3. Персистентность

Три таблицы; VFlow-таблицы не дублируются.

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `json_processor_settings` | Singleton `id=1` с не-secret настройками | `settings_json`, `version`, `updated_by`, `created_at`, `updated_at` |
| `json_repair_jobs` | Одна запись на repair-запрос | `id`, `source_type`, correlation ids, `raw_output_ref`, `raw_hash`, `schema_json`, `schema_hash`, `settings_version`, `prompt_version`, `status`, `method`, `attempts`, `result_json`, `error_code/message`, `idempotency_key`, lease fields, timestamps |
| `json_repair_attempts` | Детальная история local/LLM попыток | `job_id`, `attempt_no`, `method`, `provider`, `model`, `request_meta_json`, `validation_error`, `status`, `latency_ms`, `token_usage_json`, timestamps |

Особенности:

- `json_repair_attempts` связан с job через `ON DELETE CASCADE`; уникальность `(job_id, attempt_no)`.
- Секреты в `request_meta_json` рекурсивно вычищаются (`api_key`, `authorization`, `password`, `access_token`, ...); не-secret `api_key_ref` может сохраняться.
- `raw_output_ref` — opaque reference, а не raw text. В VFlow это обычно `task_run_id`; raw остаётся в `task_run_results`.
- `raw_hash` — SHA-256 исходного текста; worker проверяет его перед обработкой.
- `schema_hash` вычисляется repository из канонического JSON схемы.
- `result_json` — только успешный repaired value; при ошибке его нет.
- `method`: `none`, `local`, `llm`.

---

## 3. Состояния, Task и контракт output

### 3.1. Состояния repair job

```text
                 claim_due (CAS, lease)
queued ─────────────────────────────────► processing
  │                                            │
  │ cancel_job                                 ├── complete_job (lease OK) ──► succeeded
  ▼                                            ├── fail_job (lease OK) ──────► failed
cancelled                                      └── cancel_job (owner/force) ─► cancelled
  ▲                                            │
  │                                            │ lease_until <= now
  └──────────── recover_expired_leases ◄───────┘
                 (status → queued, lease cleared, attempts preserved)
```

Правила:

- `queued` — job создан, ожидает claim. Lease пуст.
- `processing` — job захвачен worker-ом; выставляются `lease_owner`, `lease_until`, `started_at`; `attempts` увеличивается на 1.
- `succeeded` — есть `result_json` и `method`, заполнен `finished_at`.
- `failed` — заполнены `error_code`, `error_message`, `method` (если известен), `finished_at`.
- `cancelled` — терминальный статус с `error_code='CANCELLED'`.
- `succeeded`, `failed`, `cancelled` — терминальные; повторный complete/fail/cancel невозможен (`JOB_STATE_CONFLICT`).
- `complete_job`/`fail_job` требуют `status='processing'`, совпадающего `lease_owner` и неистёкшего `lease_until`; иначе `LEASE_LOST`.
- `attempts` в job — число **claim-ов обработки**, а не число local/LLM попыток. Детальные попытки — в `json_repair_attempts`.
- `started_at` фиксируется при первом claim и не перезаписывается при recovery.
- Recovery возвращает просроченный `processing` в `queued`, очищает lease, но **не сбрасывает** `attempts`.

### 3.2. Состояния точки VFlow

Для Task-узла с `output_mode="json"`:

```text
claimed
   │ Task completed, JSON/schema OK
   ├────────────────────────────────────────────► advance(out)
   │
   │ Task completed, нужен repair
   ▼
processing_output  (json_processor_job_id, next_check_at)
   │ job queued/processing: poll каждые TASK_POLL_SECONDS = 2 с
   │
   ├── job succeeded, повторная schema OK ─────► advance(out)
   ├── job failed/cancelled ──────────────────► Run failed OUTPUT_INVALID
   └── Run cancelled ─────────────────────────► point cancelled
```

- Отдельного статуса `waiting_processor` нет; используется `processing_output`.
- При ожидании lease точки снимается (`lease_owner/token/until = NULL`), чтобы scheduler не держал слот.
- VFlow recovery при старте обновляет `next_check_at` для `processing_output` с `json_processor_job_id`, чтобы точка не зависла.
- Отмена Run отменяет незавершённые `json_repair_jobs` этого Run через `cancel_jobs_for_run(force=True)`.

### 3.3. Task Manager остаётся completed

JSON Processor не меняет Task Run:

- `queue_jobs.status` остаётся `completed`;
- `task_run_results.text` остаётся исходным финальным текстом;
- Task не перезапускается и не создаёт второй Task Run;
- OpenCode-сессия не продолжается;
- VFlow-точка ждёт отдельный processor job и только потом продвигается.

Поэтому в Task Manager пользователь видит завершённую задачу, даже если VFlow ещё находится в `processing_output`. Это нормально: Task завершён, downstream-контракт JSON ещё проверяется.

### 3.4. Контракт `output.text` / `output.json` и legacy `task.output`

Успешный Task в `output_mode="json"` возвращает:

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "output": {
    "text": "{\"ok\":true,}",
    "json": {"ok": true}
  },
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-id",
    "status": "completed",
    "output": {
      "text": "{\"ok\":true,}",
      "json": {"ok": true}
    }
  }
}
```

- `output.text` — **исходный** финальный текст Task. Он может быть синтаксически невалидным JSON, если его исправил процессор. Это сохраняется намеренно: потребитель видит, что именно вернула модель.
- `output.json` — decoded JSON после строгого parse, локального/LLM repair, schema и content preservation.
- `task.output` — тот же объект `{text, json}` для обратной совместимости со старыми пайплайнами.
- `task.status` — `completed`.
- `output.json` не подменяется на `null`, `{}` или error envelope при ошибке. Ошибка вообще не превращается в успешный output: Run падает `OUTPUT_INVALID`.
- Легитимный JSON `null` возможен только если он реально прошёл parse/schema (например, схема `{"type":"null"}`); это не error-подстановка.

Legacy-контракт (`result_mode="auto"`/`"strict_json"`) не содержит верхнеуровневый `output`; там остаётся только `task.output`. Для нового кода предпочтителен `$.output.json...`, для старых пайплайнов сохранён `$.task.output.json...`.

### 3.5. Envelope ошибки

Для библиотечного API и диагностики используется отдельный error envelope:

```json
{
  "valeria_json_processor": {
    "status": "error",
    "code": "SCHEMA_VALIDATION_FAILED",
    "message": "expected boolean, got string",
    "path": "$.ok",
    "diagnostics": [
      {"code": "TYPE_MISMATCH", "message": "expected boolean, got string", "path": "$.ok"}
    ]
  }
}
```

Свойства:

- единственный корневой ключ `valeria_json_processor`;
- `status: "error"`;
- `path` и `diagnostics` могут отсутствовать, если их нет;
- error envelope **не вставляется** в `output.json` или `task.output`; downstream не должен принимать его за task JSON;
- в VFlow ошибка процессора приводит к `OUTPUT_INVALID`, а не к подстановке envelope в output;
- это не HTTP-envelope admin API; admin API использует `{"error": {"code", "message", "details", "request_id"}}` (см. 6.2).

### 3.6. События VFlow

При работе processor path используются события:

- `TASK_OUTPUT_READY` — JSON был валиден сразу, job не создавался;
- `TASK_OUTPUT_PROCESSING` — создан processor job, точка переходит в `processing_output`;
- `TASK_OUTPUT_WAITING` — job ещё `queued`/`processing`;
- `TASK_OUTPUT_REPAIRED` — job успешен, точка продвинута.

События не содержат raw output, prompt или API-ключ; только job id, method/status и безопасные коды.

---

## 4. Справочник настроек

Настройки хранятся в singleton-строке `json_processor_settings` и редактируются через admin API/UI. `JsonProcessorSettings` — единственный источник defaults и правил валидации.

### 4.1. Все поля

| Поле | Default | Допустимые значения | Назначение |
|---|---:|---|---|
| `enabled` | `false` | boolean | Глобальный gate процессора. Включает worker и разрешает создание repair job из VFlow. |
| `base_url` | `""` | пусто или absolute `http(s)` URL, ≤ 2000 символов | OpenAI-compatible endpoint. Без credentials, query и fragment. |
| `api_key_ref` | `""` | opaque reference, ≤ 255 символов | Ссылка на env-переменную с ключом; **не** сам ключ. |
| `model` | `""` | ≤ 200 символов | Идентификатор модели для Chat Completions. Обязателен при LLM repair. |
| `local_repair_enabled` | `true` | boolean | Разрешает детерминированный локальный repair. |
| `max_llm_attempts` | `2` | integer 0..3 | Максимум LLM-repair попыток. `0` — local-only. |
| `request_timeout_seconds` | `45` | integer 1..300 | Таймаут одного provider-запроса. |
| `max_input_bytes` | `65536` | integer 1..1048576 | Максимальный размер raw output. |
| `max_output_bytes` | `65536` | integer 1..1048576 | Максимальный размер ответа LLM при parse. |
| `max_output_tokens` | `4096` | integer 1..32768 | `max_tokens` в Chat Completions. |
| `temperature` | `0.0` | number 0..2 | Temperature provider-запроса. |
| `repair_mode` | `"syntax_only"` | `syntax_only` / `schema_safe` | Политика сохранения содержимого. |
| `on_processor_failure` | `"fail"` | только `fail` | Fail-closed политика; других значений нет. |
| `concurrency_limit` | `4` | integer 1..32 | Максимум in-flight processor callbacks одного worker. |
| `queue_limit` | `1000` | integer 1..100000 | Best-effort admission limit для worker.enqueue. |
| `raw_output_retention_days` | `30` | integer 1..3650 | Retention repaired output (`result_json`) терминальных jobs. |
| `attempt_retention_days` | `90` | integer 1..3650 | Retention attempt metadata. |

### 4.2. Правила валидации

- При `enabled=true` должен быть хотя бы один путь repair: `local_repair_enabled=true` или `max_llm_attempts > 0`.
- Если `enabled=true` и `max_llm_attempts > 0`, обязательны `base_url` и `model`.
- Если `max_llm_attempts=0` и `local_repair_enabled=true`, provider не требуется: это local-only режим.
- `api_key_ref` необязателен: локальный OpenAI-compatible endpoint может не требовать авторизации.
- `base_url` нормализуется (`rstrip("/")`), не должен содержать whitespace, control characters, backslash, credentials, query или fragment.
- Строковые значения обрезаются; control characters запрещены.
- Неизвестные ключи при обновлении отклоняются. Это защищает от случайной записи `api_key` или глобального allowance-списка.
- При чтении повреждённой/legacy-строки неизвестные ключи игнорируются, чтобы случайно записанный секрет не вернулся через API.
- `on_processor_failure` принимает только `fail`; никакой `null`/`{}`-подстановки нет.
- `repair_mode=schema_safe` сам по себе **не** даёт глобального списка разрешённых изменений. Allowances строятся на конкретный запрос вызывающим слоем; в текущей VFlow-интеграции они не передаются.
- Ошибки валидации не содержат отклонённое значение.

### 4.3. Версионирование и конкурентные изменения

- Каждая строка настроек имеет `version`, начиная с 1.
- Обновление выполняется как `UPDATE ... WHERE id=1 AND version=expected_version`.
- При несовпадении возвращается `SETTINGS_VERSION_CONFLICT` (HTTP 409) с `expected_version` и `current_version`.
- При успехе `version + 1`; `updated_by`, `created_at`, `updated_at` возвращаются как audit metadata.
- UI сохраняет `expected_version` и показывает конфликт без потери чужих изменений.
- `SETTINGS_VERSION_CONFLICT` не является ошибкой repair job; это конфликт административного редактирования.

### 4.4. Когда настройки применяются

Важно различать настройки, которые читаются на старте приложения, и настройки, которые читаются на каждый job.

| Что | Когда применяется |
|---|---|
| `enabled`, наличие worker, `concurrency_limit`, `queue_limit` | При старте приложения. Включение/выключение и изменение лимитов требует перезапуска. |
| `base_url`, `model`, `api_key_ref`, `local_repair_enabled`, `max_llm_attempts`, `repair_mode`, `request_timeout_seconds`, `max_input_bytes`, `max_output_bytes`, `max_output_tokens`, `temperature` | Читаются service-слоем при обработке; обычно применяются к следующим job без перезапуска. |
| `raw_output_retention_days`, `attempt_retention_days` | Читаются при retention preview/purge. |
| `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS` | Переменная окружения, читается при старте; требует перезапуска. |

`settings_version` в job — часть canonical identity и audit, но **не** snapshot всех настроек: job обрабатывается с текущими настройками на момент обработки. Если нужна воспроизводимость, меняйте настройки в окно обслуживания или после дренажа очереди.

### 4.5. Пример PATCH

```http
PATCH /api/admin/json-processor/settings
Content-Type: application/json
X-CSRF-Token: <token>

{
  "expected_version": 3,
  "changes": {
    "enabled": true,
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key_ref": "env:DEEPSEEK_API_KEY",
    "max_llm_attempts": 2,
    "concurrency_limit": 4
  }
}
```

Ответ — полный объект настроек с новым `version`, `provider_configured`, `api_key_ref_configured`, `updated_by`, `updated_at`.

---

## 5. OpenAI-compatible провайдер

### 5.1. Поддерживаемый протокол

Поддерживается только не-streaming OpenAI-compatible Chat Completions:

| Операция | Метод и путь | Назначение |
|---|---|---|
| Repair completion | `POST {base_url}/v1/chat/completions` | Один запрос на попытку repair. |
| Model/connection check | `GET {base_url}/v1/models` | Проверка доступности endpoint/модели без completion. |

`base_url` можно задавать как `https://host`, так и `https://host/v1`; клиент не создаёт `/v1/v1`.

Запрос completion содержит только:

```json
{
  "model": "repair-model",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "max_tokens": 4096,
  "temperature": 0.0,
  "stream": false,
  "response_format": {"type": "json_object"}
}
```

Ограничения:

- нет `tools`, `functions`, `tool_choice`;
- нет streaming;
- нет vendor-specific адаптеров;
- нет function calling и MCP;
- нет истории чата и продолжения OpenCode-сессии;
- ответ читается только из `choices[0].message.content`;
- пустой `content` считается invalid response.

### 5.2. `response_format` fallback

- Основной запрос отправляется с `response_format={"type":"json_object"}`.
- Если сервер отвечает `400` или `422` и в теле есть признак неподдерживаемого параметра (`response_format`, `json_object`, `unsupported parameter`, `unknown parameter`, `unrecognized field`, `unsupported value`), делается **ровно один** infrastructure retry без `response_format`.
- Этот retry **не является** repair-попыткой и **не расходует** `max_llm_attempts`.
- В metadata попытки фиксируются `infrastructure_retries` и `response_format_used`.
- Если и fallback завершился ошибкой, возвращается typed provider error.

### 5.3. Presets и рекомендуемые параметры

В admin UI сейчас три preset-кнопки: `OpenAI`, `Локальный OpenAI-compatible`, `Другой OpenAI-compatible`. Ниже — рабочие рецепты для типовых провайдеров; это не отдельные UI-пресеты.

| Провайдер | `base_url` | Пример `model` | `api_key_ref` | Примечание |
|---|---|---|---|---|
| OpenAI | `https://api.openai.com` | `gpt-4o-mini` | `env:OPENAI_API_KEY` | Стандартный `/v1/chat/completions`. |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | `env:DEEPSEEK_API_KEY` | OpenAI-compatible endpoint. |
| Ollama | `http://127.0.0.1:11434` | `qwen2.5-coder:7b` | пусто | Нужен `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS=true`. |
| vLLM | `http://127.0.0.1:8000` | имя served model | пусто или ключ | Нужен private opt-in для loopback. |
| LM Studio | `http://127.0.0.1:1234` | id загруженной модели | пусто | Нужен private opt-in. |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` | `env:OPENROUTER_API_KEY` | Проверьте поддержку JSON mode. |
| Другой OpenAI-compatible | absolute URL без query/fragment | id модели | по необходимости | Должен возвращать OpenAI-совместимый `choices[0].message.content`. |

Особенности:

- Azure OpenAI напрямую не поддерживается, если URL требует `api-version` в query или deployment-path: `base_url` с query запрещён. Используйте OpenAI-compatible gateway/proxy.
- Провайдеры без `response_format` работают через fallback; модели без JSON mode могут вернуть markdown — локальный parser попробует его разобрать.
- Для локальных endpoint-ов сначала включите private networks, затем перезапустите приложение.
- `GET /v1/models` может быть не реализован или не содержать модель; `model_available=false` не всегда означает, что completion не работает. После connection check обязательно выполните sandbox.

### 5.4. Разрешение API-ключа по `api_key_ref`

Поддерживается только environment-variable reference:

```text
NAME
env:NAME
```

- Допустимый шаблон: `^[A-Za-z_][A-Za-z0-9_]*$` после optional `env:`.
- Значение читается из `os.environ` в момент provider-вызова.
- Пустая ссылка, неизвестная переменная, пустое значение или неверный формат дают `None`; запрос уходит без `Authorization`.
- `api_key_ref` — не секрет; он хранится в настройках и может сохраняться в безопасной metadata, но сам ключ не хранится нигде.
- UI placeholder `vault://json-processor/openai` — только иллюстрация. Текущий resolver **не** поддерживает Vault, AWS Secrets Manager и другие хранилища; используйте env или внешний wrapper, который экспортирует env.
- Изменение env-переменной требует перезапуска процесса (или как минимум гарантированного обновления окружения); значение не кэшируется в настройках.

### 5.5. Security-правила provider-слоя

- Только `http`/`https`; URL без credentials, query и fragment.
- `base_url` не должен содержать whitespace, control characters или backslash.
- Metadata hostnames и metadata IP блокируются всегда.
- Link-local, unspecified, multicast, reserved адреса блокируются.
- Loopback/private (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `fc00::/7`, `::1`) разрешены только при `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS=true`.
- Alternate numeric IP-формы (`2130706433`, `127.1`, `0177.0.0.1`) и IPv6 zone identifiers отклоняются.
- IPv4-mapped IPv6 проверяется по вложенному IPv4.
- HTTP-клиент не следует redirects (`follow_redirects=False`) и не использует environment proxies (`trust_env=False`).
- API key передаётся только как `Authorization: Bearer ...`; он не сохраняется в provider, job, attempt, ошибке или логе.
- Ответ провайдера не логируется; в ошибку попадают только status code, безопасный provider code и нормализованная категория.
- Один provider-вызов ограничен `request_timeout_seconds`; service дополнительно оборачивает его в timeout + 1 секунда.

### 5.6. Проверка соединения

`POST /api/admin/json-processor/check-connection`:

- читает сохранённые настройки, но позволяет передать `base_url`, `api_key_ref`, `model` как overrides;
- overrides **не сохраняются**;
- делает `GET /v1/models`;
- возвращает `ok`, `status_code`, `model_available` (`true`/`false`/`null`), `latency_ms`;
- при provider-ошибке возвращает `ok=false` и безопасный `error`;
- не создаёт job и не выполняет completion.

Connection check можно выполнять и при `enabled=false`. Это удобно для первичной настройки, но не доказывает, что completion и JSON mode работают; для этого используйте sandbox.

---

## 6. Административный UI и API

### 6.1. UI

Маршрут: `#/admin-json-processor` (пункт «JSON-процессор» в административном меню).

Доступ:

- фронтенд скрывает маршрут для не-администраторов;
- backend всё равно требует роль `admin` на каждом endpoint;
- mutations требуют CSRF-токен.

Секции страницы:

1. **Настройки процессора** — `enabled`, провайдер (`base_url`, `model`, `api_key_ref`), repair mode, fail policy, лимиты, очередь, retention. Сохранение optimistic по `version`.
2. **Проверка соединения** — использует значения формы, не сохраняет их.
3. **Sandbox** — raw output + необязательная JSON Schema; выполняет полный pipeline, но **не создаёт job**, не трогает Task/VFlow и не сохраняет raw.
4. **Метрики** — jobs, queue, success rate, attempts, latency p50/p95, tokens, failure categories, retention preview, кнопка purge.
5. **Repair jobs** — фильтры, пагинация, detail с attempts/schema/result. Raw output в списке и detail не показывается.

Sandbox:

- `raw_output` ограничен 1 MiB;
- schema — безопасное подмножество;
- использует **сохранённые** настройки, а не значения формы;
- `job_created=false`;
- ответ содержит `ok`, `mode`, `method`, `value` (только при успехе), `error`, `diagnostics`, `attempts`, `settings_version`, `raw_input_bytes`;
- error-ответ не содержит raw;
- при `enabled=false` возвращается `NOT_ENABLED`;
- при LLM без `base_url`/`model` — `NOT_CONFIGURED`.

### 6.2. API endpoints

Префикс: `/api/admin/json-processor`.

| Метод | Путь | Назначение | Роль | CSRF |
|---|---|---|---|---|
| `GET` | `/settings` | Текущие настройки + `provider_configured`, `api_key_ref_configured` | admin | нет |
| `PATCH` | `/settings` | Частичное обновление с `expected_version` | admin | да |
| `GET` | `/providers` | Описание протокола, endpoints, auth, presets, current | admin | нет |
| `POST` | `/check-connection` | `GET /v1/models` с optional overrides | admin | да |
| `POST` | `/test` | Sandbox без job/Task/VFlow | admin | да |
| `GET` | `/jobs` | Список jobs с фильтрами и пагинацией | admin | нет |
| `GET` | `/jobs/{job_id}` | Детали job + attempts | admin | нет |
| `GET` | `/metrics` | Агрегированные метрики | admin | нет |
| `GET` | `/retention` | Dry-run preview | admin | нет |
| `POST` | `/retention/purge` | Подтверждённая очистка | admin | да |

Все ответы используют единый error envelope:

```json
{
  "error": {
    "code": "SETTINGS_VERSION_CONFLICT",
    "message": "settings version conflict: expected 2, current 3",
    "details": {"expected_version": 2, "current_version": 3},
    "request_id": "..."
  }
}
```

`request_id` берётся из `X-Request-ID`, если он не длиннее 200 символов, иначе генерируется. Используйте его при разборе инцидентов.

### 6.3. `PATCH /settings`

Тело:

```json
{
  "expected_version": 3,
  "changes": {"enabled": true}
}
```

Допустимые варианты:

- `expected_version` или legacy-alias `version`;
- `changes` **или** `settings`, но не оба одновременно;
- пустой `changes` допустим и просто возвращает текущие настройки.

Поведение:

- неизвестные поля отклоняются (`SETTINGS_INVALID`);
- невалидный `base_url` отклоняется до записи; в audit пишется `json_processor.settings.rejected`;
- конфликт версии — HTTP 409, audit `update_conflict`;
- успех — HTTP 200, audit `json_processor.settings.update`.

### 6.4. `POST /test` (sandbox)

Тело:

~~~json
{
  "raw_output": "Result:\n```json\n{\"ok\": true,}\n```",
  "schema": {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": false
  }
}
~~~

Поле `schema` — alias; можно передать `schema_definition`. Ответ:

```json
{
  "ok": true,
  "mode": "syntax_only",
  "method": "local",
  "value": {"ok": true},
  "error": null,
  "diagnostics": [],
  "attempts": [
    {
      "number": 1,
      "phase": "local",
      "mode": "syntax_only",
      "ok": true,
      "strategy": "fenced",
      "normalizations": ["trailing_commas"],
      "candidate_count": 1,
      "schema_valid": true,
      "validation_error": null,
      "error": null
    }
  ],
  "settings_version": 3,
  "raw_input_bytes": 37,
  "job_created": false
}
```

Sandbox **не** пишет raw, не создаёт job, не вызывает VFlow/Task и не сохраняет schema.

### 6.5. `GET /jobs` и `/jobs/{job_id}`

Фильтры списка:

- `status`: `queued`, `processing`, `succeeded`, `failed`, `cancelled`;
- `source_type`: `task`, `vflow`, `standalone`;
- `method`: `none`, `local`, `llm`;
- `error_code`;
- `task_run_id`, `vflow_run_id`, `point_id`;
- `created_after`, `created_before` (ISO timestamps);
- `limit` 1..200 (default 50), `offset` ≥ 0.

Ответ списка:

```json
{
  "items": [
    {
      "id": "job-id",
      "source_type": "vflow",
      "task_run_id": "task-run-id",
      "vflow_run_id": "run-id",
      "point_id": "point-id",
      "raw_output_ref": "task-run-id",
      "raw_hash": "sha256...",
      "schema_hash": "sha256...",
      "settings_version": 3,
      "prompt_version": "task-output-v1",
      "status": "succeeded",
      "method": "local",
      "attempts": 1,
      "error_code": null,
      "error_message": null,
      "created_at": "...",
      "updated_at": "...",
      "started_at": "...",
      "finished_at": "..."
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "has_more": false
}
```

Detail дополнительно содержит:

- `schema` — сохранённая схема;
- `result` — repaired value (если есть);
- `has_result`;
- `idempotency_key`;
- `lease_owner`, `lease_until`;
- `attempts_count`;
- `attempts` и `attempts_history` — история без raw.

Detail доступен только администратору. Список и detail **не содержат raw output, prompt или API key**. `request_meta` attempts проходит через scrub секретоподобных ключей.

### 6.6. RBAC, CSRF и аудит

- Все endpoints требуют роль `admin`; отдельной роли «только просмотр» нет.
- GET-запросы используют `admin_read_principal` (session + admin).
- Мутации используют `admin_principal` (session + CSRF + admin).
- Без CSRF возвращается `403 FORBIDDEN` с сообщением про CSRF.
- Аудит пишется в общий `audit` VALERIA:
  - `json_processor.settings.update`;
  - `json_processor.settings.rejected`;
  - `json_processor.settings.update_conflict`;
  - `json_processor.connection_check`;
  - `json_processor.sandbox.test`;
  - `json_processor.retention.purge`.
- Audit metadata не содержит raw, prompt или секретов; ошибки audit не ломают ответ.
- Отдельного admin endpoint для отмены job в текущем API нет. Отмена выполняется через VFlow Run или внутренний repository/service.

---

## 7. Настройка Task и VFlow

### 7.1. Новые поля Task

В админ-API Task и UI доступны поля:

| Поле | Default | Допустимо | Смысл |
|---|---:|---|---|
| `output_mode` | `"text"` | `text` / `json` | `json` включает строгий JSON-контракт и processor path для VFlow. |
| `output_schema` | `null` | JSON object или `null` | Схема для `output_mode=json`; проверяется безопасным валидатором при сохранении Task. |
| `on_json_error` | `"fail"` | `fail` / `repair` | `fail` — завершить ошибкой; `repair` — отправить raw в JSON Processor. |
| `repair_trigger` | `"invalid_json"` | `invalid_json` / `invalid_json_or_schema` | Когда запускать repair. |

Правила:

- `on_json_error="repair"` требует `output_mode="json"`.
- `repair_trigger` допустим только при `on_json_error="repair"`.
- `output_schema` валидируется тем же безопасным подмножеством, что и процессор; слишком большая/глубокая схема или `$ref` отклоняются при сохранении.
- Если `output_schema=null`, проверяется только синтаксис JSON; любое валидное JSON-значение допустимо.
- `output_mode="text"` сбрасывает `on_json_error` в `fail` и `repair_trigger` в `invalid_json`.
- Для старых задач значения по умолчанию сохраняют прежнее поведение.

Важно: в текущей реализации поля Task-записи — это декларация контракта и метаданные Task, доступные в админ-API/UI. Исполнение JSON Processor подключено к VFlow-узлу `task.run`, у которого есть **собственные** аргументы `output_mode`, `output_schema`, `on_json_error`, `repair_trigger`. Аргументы узла имеют приоритет; Task-поля не переопределяют их автоматически и не наследуются узлом. Прямой запуск Task из Task Manager по-прежнему возвращает текст и не вызывает JSON Processor. Для опубликованного пайплайна всегда проверяйте аргументы узла.

### 7.2. Аргументы `task.run` в VFlow

```text
task.run(
  task_id: "code.review",
  output_mode: "json",
  output_schema: {type: "object", required: ["ok"], properties: {ok: {type: "boolean"}}},
  on_json_error: "repair",
  repair_trigger: "invalid_json"
)
```

| Аргумент | Default | Взаимодействие |
|---|---|---|
| `task_id` | обязателен | ID существующей Task. |
| `input` / `input_path` | `$` | Входной JSONPath. |
| `result_mode` | `auto` | Legacy: `auto`, `strict_json`, `none`. Не смешивать с `output_mode=json`. |
| `output_mode` | `text` | `json` включает новый контракт. |
| `output_schema` | нет | Приоритетнее legacy `json_schema`. |
| `on_json_error` | `fail` | `repair` требует `output_mode=json`. |
| `repair_trigger` | `invalid_json` | Только с `repair`; `invalid_json_or_schema` также репарит schema mismatch. |
| `json_schema` | нет | Legacy-схема для `result_mode=strict_json`. |
| `max_json_repairs` | 1 в runtime legacy | Legacy same-chat budget, 0..3. Нельзя вместе с `on_json_error=repair` и `max_json_repairs>0`. |
| `queue_timeout_seconds` | нет | Таймаут ожидания Task. |
| `execution_timeout_seconds` | нет | Таймаут выполнения Task. |
| `retries` | 0 | Дополнительные попытки Task. |

### 7.3. Legacy-поля

| Поле | Где | Смысл |
|---|---|---|
| `result_mode="strict_json"` | VFlow node | Legacy-проверка JSON в том же чате. |
| `json_schema` | VFlow node | Legacy-схема для `strict_json`. |
| `max_json_repairs` | VFlow node | Бюджет follow-up repair в том же chat/session. |

Legacy same-chat repair:

- создаёт follow-up `queue_jobs` с `task_id=NULL`, `job_kind='vflow_repair'`, `pipeline_json=''`;
- продолжает тот же chat/session;
- использует `vflow_outbox.kind='task_json_repair'`;
- имеет собственные события `TASK_JSON_REPAIR_REQUESTED`/`TASK_JSON_REPAIR_COMPLETED`.

Новый processor path:

- создаёт `json_repair_jobs`;
- использует отдельный worker/provider;
- не продолжает OpenCode-сессию;
- не смешивается с `queue_jobs`.

### 7.4. Никогда не запускайте два repair-движка

Одновременный запуск запрещён:

- при `output_mode="json"` runtime идёт в processor path и **не** вызывает legacy `_handle_task_json_repair`;
- компилятор отклоняет `max_json_repairs > 0` вместе с `on_json_error="repair"` кодом `COMPILE_REPAIR_ENGINE_CONFLICT`;
- `result_mode="strict_json"` вместе с `output_mode="json"` не даёт второго repair: processor path имеет приоритет, но такую конфигурацию лучше не создавать;
- при миграции сначала уберите `max_json_repairs`/legacy repair, затем включайте `on_json_error="repair"`.

### 7.5. Примеры

Только проверка, без repair:

```text
task.run(
  task_id: "code.review",
  output_mode: "json",
  output_schema: {type: "object", required: ["ok"], properties: {ok: {type: "boolean"}}},
  on_json_error: "fail"
)
```

Repair только синтаксиса:

```text
task.run(
  task_id: "code.review",
  output_mode: "json",
  output_schema: {type: "object", required: ["ok"], properties: {ok: {type: "boolean"}}},
  on_json_error: "repair",
  repair_trigger: "invalid_json"
)
```

Repair синтаксиса и схемы:

```text
task.run(
  task_id: "code.review",
  output_mode: "json",
  output_schema: {type: "object", required: ["ok"], properties: {ok: {type: "boolean"}}},
  on_json_error: "repair",
  repair_trigger: "invalid_json_or_schema"
)
```

---

## 8. Последовательность обработки

### 8.1. Общий pipeline

```text
1. Raw preservation / identity
2. Strict parse всего raw
3. Schema validation
4. Local repair (если разрешён)
5. LLM attempts (если разрешены и нужны)
6. Revalidation каждого LLM-ответа
7. Content preservation
8. Success или fail-closed
9. Audit/result finalize
```

### 8.2. Шаг 1. Raw preservation

- VFlow adapter получает финальный текст Task из `task_run_results`.
- В job сохраняется `raw_output_ref` (обычно `task_run_id`) и `raw_hash` SHA-256.
- Worker загружает raw по reference и проверяет hash; несовпадение — `INVALID_INPUT`.
- Raw не копируется в job, attempt, audit или admin API.
- В sandbox raw не сохраняется вообще.
- Если настроен внешний `raw_store`, service может сохранить копию, но в текущей VFlow-сборке он не подключён.

### 8.3. Шаг 2. Strict parse

Сначала выполняется `json.loads` **всего** вывода:

- `NaN`, `Infinity`, `-Infinity` отклоняются (`parse_constant`);
- пустой вывод — `EMPTY_OUTPUT`;
- превышение `max_input_bytes` — `RAW_OUTPUT_TOO_LARGE`;
- если весь вывод — валидный JSON (включая скаляр, `null`, строку, число), он принимается как decoded value;
- если весь вывод не JSON, начинается локальный repair.

### 8.4. Шаг 3. Schema validation

- Схема проверяется до repair; ошибка — `SCHEMA_DEFINITION_INVALID`.
- Значение проверяется по безопасному подмножеству.
- Ошибки имеют стабильные пути: `$.field`, `$.items[0]`.
- При `additionalProperties: false` лишнее свойство даёт `ADDITIONAL_PROPERTY` в diagnostics и общий `SCHEMA_VALIDATION_FAILED`; процессор **не удаляет** поле автоматически.
- Если strict JSON валиден и схема проходит, успех возвращается сразу: `method="none"`, attempts пусты, job в VFlow не создаётся.

### 8.5. Шаг 4. Local repair

Локальный repair включается `local_repair_enabled=true` (default). Он консервативен:

1. одно полное markdown-ограждение с языком `json` или без языка;
2. trailing commas перед `}`/`]` вне строк;
3. безопасные одинарные кавычки;
4. value-preserving `True`/`False`/`None` → `true`/`false`/`null`;
5. один однозначный object/array-кандидат из текста с prose.

Запрещено/отклоняется:

- `NaN`, `Infinity`, `undefined`;
- несколько независимых JSON-кандидатов (`AMBIGUOUS_JSON`);
- скаляр, найденный внутри prose (скаляр допускается только как полный strict JSON всего вывода);
- изобретение отсутствующих полей;
- неоднозначные кавычки/escape;
- свободный текст без JSON (`NOT_REPAIRABLE`).

Если локальный кандидат проходит parse, schema и content, результат успешен с `method="local"`. Если нет — ошибка сохраняется и может быть передана в LLM как последняя диагностика.

### 8.6. Шаг 5. LLM repair

LLM используется только если:

- strict/local не дали валидный результат;
- `max_llm_attempts > 0`;
- provider настроен.

На каждую попытку:

- prompt builder получает **последний** кандидат/диагностику, схему и режим;
- полная история attempts, предыдущие ответы LLM и секреты не передаются;
- prompt ограничен: 24 KB всего, 12 KB raw, 6 KB schema, 32 diagnostics по 500 символов;
- raw находится внутри `<|UNTRUSTED_JSON_OUTPUT_BEGIN|>` … `<|UNTRUSTED_JSON_OUTPUT_END|>`;
- delimiter-подобные последовательности нейтрализуются;
- system prompt запрещает tools, команды, внешние сервисы и следование инструкциям из данных;
- запрос non-streaming, без tools/function calling.

Бюджет:

- `max_llm_attempts` — максимум последовательных repair-попыток (0..3, default 2);
- provider error, timeout, network error и `CONTENT_CHANGED` завершают обработку сразу; оставшийся бюджет не используется;
- invalid JSON/schema в ответе LLM считается неудачной repair-попыткой, и цикл может продолжиться;
- infrastructure retry `response_format` не расходует бюджет.

### 8.7. Шаг 6. Revalidation

Каждый ответ LLM проходит заново:

1. strict parse;
2. schema;
3. content preservation;
4. проверку сериализуемости (без NaN/Infinity и не-JSON типов).

Только после этого значение может стать `result_json`.

### 8.8. Шаг 7. Content preservation

Baseline:

- если raw был строгим JSON — baseline = это decoded value;
- иначе baseline = первый локально полученный кандидат.

Сравнение:

- `syntax_only` — decoded value должен быть полностью идентичен; любое изменение значения, типа, ключей или порядка массива → `CONTENT_CHANGED`;
- `schema_safe` — изменение допускается только при точном совпадении пути и операции с per-request `ContentAllowance`;
- глобального allowlist нет;
- отсутствующие поля не изобретаются;
- `additionalProperties: false` не даёт права удалять лишнее поле;
- числа сравниваются по значению, booleans не смешиваются с числами.

В текущей VFlow-интеграции `ContentAllowance` не передаются, поэтому `schema_safe` не расширяет допустимые изменения: фактически действует сохранение содержимого. Включать `schema_safe` без вызывающего слоя, который строит allowances, не имеет смысла.

### 8.9. Шаг 8. Fail-closed

Если результат не получен:

- `RepairResult.ok=false`, `value=None`, типизированный `error`;
- job получает `failed`/`cancelled`;
- VFlow Run падает `OUTPUT_INVALID`;
- downstream не получает `null`, `{}`, пустой объект или error envelope вместо task JSON;
- Task остаётся `completed`, его raw text не меняется.

### 8.10. Шаг 9. Audit/finalize

- local/LLM попытки записываются в `json_repair_attempts` с phase, mode, status, latency, token usage, validation error и безопасным `request_meta`;
- успешный job получает `result_json`, `method`, `finished_at`;
- failed job получает `error_code`, `error_message`, `finished_at`;
- terminal job не перезаписывается;
- service проверяет cancellation/lease перед финализацией; при проигранной гонке возвращается conflict, а не перезапись.

---

## 9. Безопасность

### 9.1. Секреты

- В настройках нет поля для API-ключа; только `api_key_ref`.
- Ключ читается из env в память на один provider-вызов.
- Ключ не попадает в prompt, job, attempt, result, error, metrics, audit или лог.
- Ошибки provider не содержат тело ответа и заголовки; provider code очищается от ключа.
- `request_meta_json` рекурсивно удаляет secret-shaped ключи.
- API не возвращает `api_key`; неизвестные ключи настроек отклоняются.
- Не записывайте ключ в `base_url`, `model`, schema, Task prompt или VFlow source.

### 9.2. SSRF

Проверки описаны в 5.5. Дополнительно:

- metadata hostnames/IP блокируются всегда, даже при private opt-in;
- link-local и reserved блокируются;
- redirects отключены, environment proxies отключены;
- URL с credentials/query/fragment запрещён;
- private/loopback разрешаются только явным env-флагом `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS=true`;
- флаг действует глобально на все base_url, а не на один endpoint;
- после включения флага перезапустите приложение.

### 9.3. Prompt injection

Raw output модели считается недоверенными данными:

- передаётся в prompt только внутри явных delimiters;
- delimiter-подобные последовательности нейтрализуются;
- system prompt прямо запрещает выполнять инструкции из данных, использовать tools, запускать команды и обращаться к внешним сервисам;
- provider payload не содержит `tools`/`functions`/`tool_choice`;
- ответ LLM не исполняется, а только парсится как JSON;
- если модель вернула инструкции вместо JSON, parse/schema/content отклонит результат.

### 9.4. Schema depth/size и внешние ссылки

- Максимальный размер схемы: 16 384 байта.
- Максимальная глубина: 16 уровней.
- Максимум enum-значений: 1000.
- Максимум длины pattern: 1024.
- Максимум diagnostics: 100.
- `$ref` запрещён, включая внешние URL и локальные ссылки.
- Неизвестные keywords запрещены; поддерживаются только `type`, `required`, `properties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `additionalProperties` и аннотации `$schema`/`title`/`description`/`default`/`examples`.
- `additionalProperties` допускается только как boolean; реальный эффект имеет `false`.
- Нет `oneOf`/`anyOf`/`allOf`/`not`/`const`/`format`/`patternProperties`/`dependencies`/tuple-items.
- Схема вне подмножества отклоняется до repair, а не деградирует молча.

### 9.5. Raw data и логи

- Raw output не пишется в лог процессора.
- Logger пишет только job id, факт ошибки и безопасные коды.
- Admin jobs list/detail не содержат raw text; detail может содержать schema и repaired result, но это доступно только admin.
- Sandbox не сохраняет raw и не возвращает его в ошибке.
- Attempt metadata не содержит raw response.
- Исключение: сам Task хранит финальный текст в `task_run_results`; Task run result API может вернуть его владельцу/администратору. Это существующий контракт Task, а не JSON Processor. VFlow user API отдаёт только публичный результат, если он настроен.
- Raw может содержать персональные данные; ограничивайте доступ к БД и настройте retention Task/VFlow отдельно.

### 9.6. RBAC/CSRF и изоляция

- Только `admin` может читать настройки, jobs и метрики.
- Только `admin` + CSRF может менять настройки, запускать sandbox/connection check и purge.
- Процессор не импортирует OpenCode session code и не имеет доступа к workspace.
- Процессор не вызывает shell, filesystem, browser, MCP или tools.
- VFlow adapter выполняет только короткие SQLite-операции; provider-вызов всегда вне транзакции VFlow.

### 9.7. Остаточные риски

- DNS rebinding: hostname не резолвится и не пиннится при валидации (см. 15).
- Compromised provider может вернуть вредный JSON; он пройдёт только schema/content, но не должен интерпретироваться как команда.
- Администратор с доступом к настройкам может направить provider на private endpoint при включённом env-флаге; это осознанное доверие роли admin.
- Raw в `task_run_results` и backup-ах живёт по правилам Task/VFlow, а не процессора.

---

## 10. Retention и метрики

### 10.1. Defaults

| Данные | Default | Настройка | Отсчёт |
|---|---:|---|---|
| Repaired output (`result_json`) | 30 дней | `raw_output_retention_days` | `finished_at`, fallback `created_at` терминального job |
| Attempts и metadata | 90 дней | `attempt_retention_days` | `created_at` попытки |

Оба значения: 1..3650 дней.

### 10.2. Preview

`GET /api/admin/json-processor/retention` — non-destructive dry run:

```json
{
  "dry_run": true,
  "raw_output_cutoff": "2026-10-23T12:00:00+00:00",
  "attempt_cutoff": "2026-08-24T12:00:00+00:00",
  "raw_output_retention_days": 30,
  "attempt_retention_days": 90,
  "jobs_with_expired_raw_output": 12,
  "expired_attempts": 34
}
```

- Считаются только **терминальные** jobs; active/processing не трогаются.
- `jobs_with_expired_raw_output` фактически считает jobs с непустым `result_json`, а не внешний raw.
- Preview ничего не удаляет.

### 10.3. Purge

`POST /api/admin/json-processor/retention/purge`:

```json
{"confirm": true, "batch_size": 500}
```

Поведение:

- без `confirm=true` возвращается 422 и dry-run preview;
- `batch_size` 1..5000 (default 500);
- для терминальных jobs старше raw-cutoff: `result_json = NULL`, `updated_at` обновляется; сама job-строка, schema, hashes, correlation ids, error и timestamps **сохраняются**;
- для attempts старше attempt-cutoff и terminal job: строки удаляются;
- queued/processing jobs и их attempts не затрагиваются, даже если timestamps старые;
- операция идемпотентна; `has_more`/`remaining` показывают, нужен ли ещё batch;
- каждая операция пишет audit `json_processor.retention.purge`.

Что **не** удаляется:

- внешний raw в `task_run_results`;
- Task Run, VFlow Run, VFlow points, task links;
- job rows и error metadata;
- настройки.

### 10.4. Важное про имя `raw_output_retention_days`

Исторически настройка называлась raw retention. В текущей реализации raw text не копируется в процессор (кроме опционального внешнего `raw_store`, который в VFlow-сборке не подключён). Поэтому purge обнуляет **repaired output** (`result_json`) в job, принадлежащем процессору, а не внешний Task raw. Внешний raw удаляется средствами Task/VFlow retention/очистки БД. Это известное ограничение, см. 15.

### 10.5. Метрики

`GET /api/admin/json-processor/metrics` возвращает:

| Группа | Поля |
|---|---|
| `settings` | `enabled`, `version`, `base_url`, `model`, `provider_configured`, `api_key_ref_configured`, `updated_at` |
| `queue` | `queued`, `processing`, `succeeded`, `failed`, `cancelled` |
| `jobs` | `total`, `terminal`, `by_status`, `by_method`, `by_source`, `by_error_code`, `success_rate`, `succeeded`, `failed` |
| `failures` | `parse`, `schema`, `content`, `provider`, `provider_errors`, `cancelled`, `internal`, `other`, `by_category`, `by_code` |
| `attempts` | `total`, `avg_latency_ms`, `latency.samples`, `latency.p50_ms`, `latency.p95_ms`, `latency.max_ms`, aliases `p50_latency_ms`/`p95_latency_ms`, `by_method`, `by_status`, `tokens.prompt_tokens`, `tokens.completion_tokens`, `tokens.total_tokens` |
| `last_24h` | `jobs_created`, `succeeded`, `failed`, `cancelled`, `attempts`, `provider_errors` |
| `retention` | cutoffs, days, `jobs_with_expired_raw_output`, `expired_attempts` |

Детали:

- `success_rate = succeeded / terminal`; `cancelled` входит в terminal.
- `queue` — счётчики processor jobs по статусам, не очередь OpenCode.
- latency p50/p95 считаются nearest-rank по `json_repair_attempts.latency_ms`.
- `by_source` включает `task`, `vflow`, `standalone`; сейчас в VFlow используется `vflow`, sandbox jobs не создаёт.
- `failures.provider` и `provider_errors` — одно и то же; в `last_24h.provider_errors` входят `PROVIDER_ERROR`, `LLM_UNAVAILABLE`, `TIMEOUT`, `NOT_CONFIGURED`, `NOT_ENABLED`.
- Метрики не экспортируются в Prometheus; это admin JSON endpoint.

### 10.6. Рекомендуемые алерты (стартовые пороги)

| Сигнал | Порог-ориентир | Действие |
|---|---|---|
| `success_rate` | < 0.90 за 24 ч | Проверить `failures.by_code`, промпты Task, схему. |
| `provider_errors` за 24 ч | > 5% от jobs или резкий рост | Проверить ключ, лимиты, доступность provider. |
| `queue.queued` | > 80% `queue_limit` или устойчивый рост | Увеличить concurrency, проверить latency, снизить поток. |
| `latency.p95_ms` | > 80% `request_timeout_seconds` | Проверить provider; уменьшить timeout/нагрузку. |
| `failures.content` | > 0 | Модель пытается менять значения; разобрать job и промпт. |
| `queue.processing` | растёт и не снижается > lease 120 с | Проверить worker/логи, возможен stuck lease. |
| retention backlog | `has_more=true` после нескольких purge | Увеличить batch/частоту, проверить объём. |

Пороги — отправная точка, а не SLA.

---

## 11. Очередь, worker, recovery и отмена

### 11.1. Отдельная очередь

- Очередь процессора — `json_repair_jobs` со `status='queued'`.
- Это **не** `queue_jobs` OpenCode и не отдельный scheduler Task.
- Provider-вызов никогда не выполняется в транзакции VFlow и не занимает слот OpenCode.
- Один job соответствует одному repair-запросу; attempts — внутренние local/LLM шаги.

### 11.2. Worker

- Создаётся в lifespan приложения, только если `enabled=true` и настройки валидны.
- Один экземпляр worker имеет `concurrency_limit` (default 4, max 32) in-flight callbacks.
- `run_once`:
  1. `recover_expired_leases(limit=500)`;
  2. claim до свободных слотов;
  3. запускает callbacks;
  4. ждёт завершения batch;
  5. считает succeeded/failed/conflict/cancelled.
- `poll_interval` — 1 секунда.
- `lease_seconds` — 120 секунд (не настройка UI).
- `queue_limit` проверяется best-effort в `worker.enqueue`: `queued >= limit` → `QueueLimitExceeded`.
- VFlow adapter создаёт job через repository напрямую, поэтому `queue_limit` не является жёстким admission limit для VFlow path.

### 11.3. Claim/lease/CAS

- `claim_due(limit, lease_seconds, owner)` выбирает queued jobs в порядке `created_at, id`.
- Claim выполняется под `BEGIN IMMEDIATE` с CAS `WHERE status='queued'`.
- При claim: `processing`, `lease_owner`, `lease_until`, `started_at=COALESCE(started_at, now)`, `attempts += 1`.
- `complete_job`/`fail_job` требуют активный lease и совпадающего owner; иначе `LEASE_LOST`.
- Терминальный job нельзя переписать; иначе `JOB_STATE_CONFLICT`.
- `cancel_job`:
  - queued — отменяется;
  - processing — только owner lease или `force=True` (административная/VFlow-отмена);
  - terminal — `JOB_STATE_CONFLICT`.

### 11.4. Idempotency

Canonical identity:

```text
(source_type, task_run_id, vflow_run_id, point_id,
 raw_hash, schema_hash, settings_version, prompt_version)
```

- Повторный запрос с той же identity возвращает существующий job, даже если передан другой explicit `idempotency_key`.
- Тот же explicit key с другой identity → `IDEMPOTENCY_CONFLICT`.
- `schema_hash` всегда вычисляется repository из канонического JSON.
- NULL correlation ids нормализуются; уникальный индекс защищает от гонки.
- Смена `settings_version` или `prompt_version` создаёт новый job; старый остаётся для аудита.
- `settings_version` по умолчанию — текущая версия settings на момент создания.
- 20 параллельных create с одинаковой identity дают один job (проверяется T19).

### 11.5. Recovery после рестарта

- При старте приложения вызывается `recover_expired_leases` **до** `worker.start()`.
- `recover_expired_leases` возвращает `processing` с `lease_until <= now` (или NULL) в `queued`, очищает lease, сохраняет `attempts`.
- Worker также вызывает recovery в каждом `run_once`.
- VFlow recovery обновляет `next_check_at` для `processing_output` с `json_processor_job_id`, чтобы scheduler снова опросил job.
- Старый worker, потерявший lease, не может финализировать job: complete/fail вернут `LEASE_LOST`/conflict.
- После recovery job может быть обработан снова; `attempts` показывает число claim-ов (T12: 2 после recovery).

### 11.6. Отмена

- `POST /api/pipeline-runs/{id}/cancel` (VFlow) отменяет Run и вызывает `cancel_jobs_for_run(vflow_run_id, force=True)`.
- Отменяются все non-terminal processor jobs этого Run; terminal не трогаются.
- Worker проверяет cancellation:
  - перед local repair;
  - перед каждой LLM-попыткой;
  - после ответа LLM, перед finalize.
- Если job уже `cancelled`, provider не вызывается (T14).
- Если отмена произошла во время provider-вызова, результат не перезаписывает cancelled job: finalize проигрывает CAS.
- Остановка worker (`stop`) отменяет in-flight tasks и **оставляет lease abandoned**: jobs остаются `processing` до истечения lease и затем восстанавливаются. Это сделано, чтобы не переписывать job, пока отменённый callback ещё завершается.

### 11.7. Нагрузка и 100 jobs

T19 проверяет:

- 100 jobs с `concurrency_limit=16`;
- до 20 проходов `run_once`;
- ровно 100 succeeded, 0 failed, у каждого job `attempts=1` и одна attempt-запись;
- 20 параллельных create с одинаковой identity дают один job.

Практические ориентиры:

- default 4 in-flight; max 32 на один worker;
- provider latency и timeout определяют throughput;
- несколько worker-процессов могут делить repository, CAS предотвращает двойной claim, но SQLite write contention и общая БД требуют аккуратного масштабирования;
- держите `request_timeout_seconds` ниже lease 120 с; иначе при нескольких worker-ах возможен claim после истечения lease;
- `queue_limit` — мягкий порог, не гарантия жёсткого backpressure для VFlow path.

### 11.8. Остановка и обслуживание

- Graceful shutdown: worker.stop() отменяет tasks; jobs с lease могут остаться `processing`.
- Не удаляйте `json_repair_jobs` вручную во время обслуживания; дайте recovery/lease истечь.
- Перед изменением `enabled`, `concurrency_limit`, `queue_limit` или `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS`:
  1. остановите новые VFlow Run с repair;
  2. дождитесь дренажа или отмените Run;
  3. измените настройки/env;
  4. перезапустите приложение;
  5. проверьте health/metrics/sandbox.
- Изменение provider/model/attempts/timeout может применяться без рестарта, но для предсказуемости лучше делать в окно обслуживания.

---

## 12. Коды ошибок и диагностика

### 12.1. Коды процессора

| Код | Категория | Смысл | Что делать |
|---|---|---|---|
| `INVALID_INPUT` | input | Raw не строка, нет ref, hash mismatch, неверный аргумент | Проверить источник raw, correlation и целостность `task_run_results`. |
| `NOT_REPAIRABLE` | parse | Нет однозначного JSON | Улучшить prompt Task/агента; это не ошибка процессора. |
| `AMBIGUOUS_JSON` | parse | Несколько независимых JSON-кандидатов | Потребовать один JSON-блок; проверить промпт. |
| `EMPTY_OUTPUT` | parse | Пустой вывод | Проверить Task/агента, таймауты, обрезку ответа. |
| `RAW_OUTPUT_TOO_LARGE` | parse | Raw больше `max_input_bytes` | Увеличить лимит (до 1 MiB) или уменьшить ответ. |
| `SCHEMA_DEFINITION_INVALID` | schema | Схема вне безопасного подмножества/слишком большая | Исправить `output_schema`; убрать `$ref`/неподдерживаемые keywords. |
| `SCHEMA_VALIDATION_FAILED` | schema | Значение не соответствует схеме | Проверить `path`; улучшить промпт/схему. Не ослаблять схему без причины. |
| `CONTENT_CHANGED` | content | Repair изменил decoded value | Модель пытается переписать смысл; остановиться, проверить path/промпт. |
| `LLM_UNAVAILABLE` | provider | 401/403/404 или network error | Проверить ключ, URL, модель, доступность. |
| `NOT_CONFIGURED` | provider | Процессор выключен/нет base_url/model/пути repair | Включить, заполнить настройки, перезапустить. |
| `NOT_ENABLED` | provider | Sandbox при `enabled=false` | Включить процессор. |
| `PROVIDER_ERROR` | provider | 400/429/5xx/invalid response | Проверить provider, лимиты, JSON mode; см. troubleshooting. |
| `TIMEOUT` | provider | Provider timeout | Увеличить timeout (в пределах lease) или ускорить provider. |
| `CANCELLED` | cancelled | Job отменён | Ожидаемо при отмене Run; проверить, что не было side effects. |
| `INTERNAL_ERROR` | internal | Непредвиденная ошибка | Смотреть логи по job id, воспроизвести в sandbox. |
| `JOB_NOT_FOUND` | job | Job отсутствует | Проверить id/БД. |
| `JOB_STATE_CONFLICT` | job | Недопустимый переход/терминальный job | Не перезаписывать; проверить lifecycle. |
| `LEASE_LOST` | job | Lease истёк/не совпал owner | Ждать recovery; проверить несколько worker-ов/долгий provider. |
| `IDEMPOTENCY_CONFLICT` | job | Explicit key переиспользован для другой identity | Использовать новый key или canonical identity. |
| `SETTINGS_INVALID` | settings | Настройки не прошли валидацию | Исправить поля; значения не эхо-выводятся. |
| `SETTINGS_VERSION_CONFLICT` | settings | Optimistic lock conflict | Перечитать настройки и повторить. |
| `SCHEMA_INVALID` | schema | Alias для `SCHEMA_VALIDATION_FAILED` | То же. |

### 12.2. VFlow-коды вокруг процессора

| Код | Смысл | Действие |
|---|---|---|
| `OUTPUT_INVALID` | JSON/schema/repair не дал валидный результат | Смотреть processor job error_code; downstream не запускать. |
| `OUTPUT_CONFIG_INVALID` | Схема узла невалидна на runtime | Исправить `output_schema`/опубликовать версию. |
| `OUTPUT_REPAIR_STALE` | Точка изменилась до enqueue job | Повторить обработку точки; проверить гонку. |
| `JSON_PROCESSOR_NOT_CONFIGURED` | Процессор выключен/не настроен/не создан worker | Включить, настроить, перезапустить. |
| `COMPILE_REPAIR_ENGINE_CONFLICT` | `max_json_repairs>0` вместе с `on_json_error=repair` | Оставить один движок. |
| `COMPILE_INVALID_OUTPUT_MODE` | Неверный `output_mode` | `text`/`json`. |
| `COMPILE_INVALID_ON_JSON_ERROR` | Неверный `on_json_error` | `fail`/`repair`. |
| `COMPILE_INVALID_REPAIR_TRIGGER` | Trigger без repair | Исправить аргументы. |
| `COMPILE_INVALID_JSON_SCHEMA` | Схема узла не прошла валидацию | Исправить схему. |
| `COMPILE_INVALID_MAX_JSON_REPAIRS` | Бюджет вне 0..3 | Исправить. |

### 12.3. HTTP/API-ошибки

| HTTP | Код | Причина |
|---|---|---|
| 401 | `UNAUTHORIZED` | Нет сессии. |
| 403 | `FORBIDDEN` | Не admin или нет CSRF. |
| 404 | `NOT_FOUND` / `JOB_NOT_FOUND` | Нет маршрута/job. |
| 405 | `METHOD_NOT_ALLOWED` | Неверный метод. |
| 409 | `SETTINGS_VERSION_CONFLICT` | Устаревшая версия настроек. |
| 422 | `SETTINGS_INVALID`, `INVALID_INPUT`, `NOT_CONFIGURED`, `NOT_ENABLED` | Ошибка тела/конфигурации. |
| 500 | `INTERNAL_ERROR` | Внутренняя ошибка; искать по `request_id`. |

### 12.4. Typed provider-ошибки

Provider layer нормализует:

| Provider kind | Processor code | Retryable |
|---|---|---|
| `configuration` | `SETTINGS_INVALID` | нет |
| `authentication` / `forbidden` / `not_found` | `LLM_UNAVAILABLE` | нет |
| `bad_request` / `rate_limited` / `server_error` / `provider_error` / `invalid_response` | `PROVIDER_ERROR` | 429/5xx помечены retryable, но service не делает автоматический retry |
| `timeout` | `TIMEOUT` | да (но service завершает) |
| `network` | `LLM_UNAVAILABLE` | да (но service завершает) |

`max_llm_attempts` — бюджет repair-попыток, а не политика retry для 429/5xx/timeout. При transient provider error обработка завершается fail-closed; повторный запуск — отдельный job/Run.

### 12.5. Troubleshooting

| Симптом | Вероятная причина | Действие |
|---|---|---|
| `JSON_PROCESSOR_NOT_CONFIGURED` | `enabled=false`, нет repair path, worker не создан при старте | Включить, настроить, перезапустить приложение. |
| Процессор включён, но jobs не обрабатываются | Worker не был создан на старте (включили позже) | Перезапустить приложение; проверить `queue.processing`. |
| `NOT_REPAIRABLE` | Модель вернула prose без JSON | Улучшить prompt/agent; не считать это багом процессора. |
| `AMBIGUOUS_JSON` | Несколько JSON-блоков | Потребовать один блок, убрать примеры из ответа. |
| `SCHEMA_VALIDATION_FAILED` | Модель не соблюдает схему | Уточнить prompt/схему; проверить `path`; processor не изобретает поля. |
| `CONTENT_CHANGED` | LLM переписала значение | Не включать schema_safe «на всякий случай»; исправить промпт/схему. |
| `RAW_OUTPUT_TOO_LARGE` | Ответ больше 64 KiB (default) | Увеличить `max_input_bytes` до 1 MiB или сократить ответ. |
| `PROVIDER_ERROR` 429/5xx | Лимиты/недоступность provider | Подождать, снизить нагрузку, проверить provider; автоматического retry нет. |
| 401/403 provider | Неверный `api_key_ref`/env/ключ | Проверить env, перезапустить процесс, выполнить connection check. |
| 404 provider | Неверный base_url/model или лишний `/v1` | Проверить URL; connection check. |
| Connection check: private network | `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS=false` | Включить env и перезапустить; понимать риск. |
| `SETTINGS_VERSION_CONFLICT` | Другой админ сохранил настройки | Обновить страницу и повторить. |
| `queue.processing` не снижается | Provider долгий/stuck, lease истёк, worker остановлен | Проверить логи/job detail; перезапустить; recovery вернёт lease. |
| VFlow `processing_output` завис | Processor job не обрабатывается/worker не запущен | Проверить metrics/jobs, `json_processor_job_id`, worker, recovery. |
| `output.json` отсутствует | Узел в `output_mode=text` или legacy `result_mode` | Проверить аргументы узла, не только Task-поля. |
| Raw не удаляется purge | Purge не владеет `task_run_results` | Настроить retention Task/VFlow отдельно. |
| `jobs` detail пуст | Job удалён/неверный id | Проверить id; jobs rows не удаляются retention purge. |

### 12.6. Диагностические команды

Безопасные примеры (подставьте свою БД/сессию):

```bash
# Метрики
curl -s -b cookies.txt \
  http://127.0.0.1:8000/api/admin/json-processor/metrics | jq

# Последние jobs
sqlite3 ./data/opencode-web.db \
  "SELECT id, status, method, attempts, error_code, created_at
   FROM json_repair_jobs ORDER BY created_at DESC LIMIT 20;"

# Зависшие processing jobs
sqlite3 ./data/opencode-web.db \
  "SELECT id, lease_owner, lease_until, started_at, attempts
   FROM json_repair_jobs WHERE status='processing' ORDER BY started_at;"

# Attempts конкретного job
sqlite3 ./data/opencode-web.db \
  "SELECT attempt_no, method, status, latency_ms, validation_error
   FROM json_repair_attempts WHERE job_id='...' ORDER BY attempt_no;"
```

Не выбирайте и не публикуйте raw text без необходимости; `task_run_results` может содержать персональные данные.

---

## 13. Миграция с legacy same-chat repair и rollout

### 13.1. Сравнение путей

| Аспект | Legacy same-chat | JSON Processor |
|---|---|---|
| Включение | `result_mode="strict_json"`, `max_json_repairs` | `output_mode="json"`, `on_json_error="repair"` |
| Где выполняется | Продолжение того же chat/session | Отдельный `json_repair_jobs` + worker |
| Provider | Тот же agent/model Task | Настроенный OpenAI-compatible provider |
| Контекст | История чата | Только bounded prompt: схема, диагностика, один кандидат |
| Бюджет | `max_json_repairs` 0..3 (default 1) | `max_llm_attempts` 0..3 (default 2) |
| Схема | `json_schema` | `output_schema` (приоритетнее legacy) |
| Выход | `task.output` | `output` + `task.output` |
| Ошибка | `TASK_OUTPUT_NOT_JSON`/`TASK_SCHEMA_INVALID` | `OUTPUT_INVALID` + processor code |
| Отмена | Отмена Run/repair job | Отмена Run отменяет processor job |
| Аудит | `task_correlations`/`task_repairs` | `json_repair_jobs`/`json_repair_attempts`, metrics |

### 13.2. Правила миграции

1. **Инвентаризация.** Найдите Task/VFlow-узлы с `result_mode="strict_json"`, `json_schema`, `max_json_repairs`. Зафиксируйте владельцев и критичность.
2. **Схема.** Перенесите `json_schema` в `output_schema`; проверьте безопасное подмножество (без `$ref`, `oneOf`, `format` и т.п.).
3. **Sandbox.** Прогоните реальные ответы через admin sandbox с новой схемой; проверьте `NOT_REPAIRABLE`/`SCHEMA_VALIDATION_FAILED`/`CONTENT_CHANGED`.
4. **Провайдер.** Настройте `base_url`/`model`/`api_key_ref`; выполните connection check и LLM sandbox.
5. **Пилот.** Возьмите одну некритичную Task/пайплайн. Сначала `output_mode="json"` + `on_json_error="fail"` — только измерение валидности без repair.
6. **Canary repair.** Включите `on_json_error="repair"` и `repair_trigger="invalid_json"`; затем при необходимости `invalid_json_or_schema`.
7. **Удалите legacy-настройки.** Уберите `max_json_repairs` (или оставьте 0), `result_mode="strict_json"`/`json_schema` для этого узла. Не допускайте оба движка.
8. **Опубликуйте новую версию.** VFlow-версия неизменяема; старые Run продолжат на старой версии, новые — на новой.
9. **Мониторинг.** 24–72 часа: success_rate, provider_errors, content, queue, p95, жалобы.
10. **Расширение.** Переводите пайплайны партиями; legacy оставьте для отката до отдельного ADR/декомиссии.

### 13.3. Feature gates

В текущей реализации **нет** отдельного `VALERIA_JSON_PROCESSOR_VFLOW_ENABLED` (это было предложение в ADR). Реализованные gates:

- глобальный: `json_processor_settings.enabled` (default `false`);
- инфраструктурный: `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS` (default `false`);
- точечный: `output_mode`/`on_json_error`/`repair_trigger` на VFlow-узле;
- бюджет/режим: `max_llm_attempts`, `repair_mode`.

Это позволяет делать canary на уровне узла/пайплайна, не включая процессор для всех.

### 13.4. Стадии rollout

| Стадия | Gate | Действия | Критерий выхода |
|---|---|---|---|
| 0. Disabled | `enabled=false` | Worker не создаётся; legacy работает как раньше; проверить baseline suites и метрики. | Нет регрессий; настройки доступны admin. |
| 1. Sandbox | `enabled=true`, `max_llm_attempts=0`, `local_repair_enabled=true` | Проверить local sandbox, T01–T07, jobs не создаются. | Local repair стабилен; ложных CONTENT_CHANGED нет. |
| 2. Provider | `max_llm_attempts=1..2`, provider настроен | Connection check, LLM sandbox, проверка секретов/SSRF/таймаутов. | Provider отвечает; ошибки типизированы; ключ не утекает. |
| 3. Canary | Один некритичный Task/узел | Сначала `output_mode=json` + `on_json_error=fail`; затем repair с `invalid_json`. | Нет `OUTPUT_INVALID`-всплеска; downstream получает `output.json`. |
| 4. VFlow | Несколько пайплайнов/команд | Включить `invalid_json`; затем `invalid_json_or_schema`; наблюдать queue/p95. | 24–72 ч без инцидентов; success_rate в норме. |
| 5. Default-on | Политика для новых Task | Новые VFlow-узлы по умолчанию получают `output_mode=json`; в Task-записях фиксируется тот же контракт; legacy только для старых; обучение админов. | Процесс воспроизводим; rollback проверен. |

Важно: `JsonProcessorSettings.enabled` в коде имеет default `false`; «default-on» на стадии 5 — это организационная политика/шаблон, а не изменение кода.

### 13.5. Rollback

| Ситуация | Действие |
|---|---|
| Один узел даёт `OUTPUT_INVALID` | Вернуть `output_mode="text"` или `on_json_error="fail"`, опубликовать новую версию. |
| Один пайплайн критичен | Отключить repair на узле, оставить strict validation или legacy. |
| Provider деградировал | Уменьшить `max_llm_attempts`, поднять timeout (осторожно с lease), переключить provider; при необходимости `on_json_error="fail"`. |
| Массовые ошибки | `enabled=false`, перезапуск; новые repair jobs не создаются, worker останавливается. Убедиться, что узлы не остаются с `repair` (иначе `JSON_PROCESSOR_NOT_CONFIGURED`). |
| Нужно вернуть legacy | Убрать `on_json_error="repair"`/`output_mode="json"`, задать `result_mode="strict_json"`, `json_schema`, `max_json_repairs`; опубликовать версию. |
| Отмена активных Run | `POST /api/pipeline-runs/{id}/cancel`; processor jobs отменяются. |

Правила rollback:

- не удаляйте `json_repair_jobs`/attempts вручную;
- не меняйте терминальные jobs;
- после выключения `enabled` worker останавливается только после перезапуска; in-flight jobs могут завершиться;
- queued jobs останутся в БД и могут быть обработаны после повторного включения;
- проверьте, что ни один узел не остался с `repair` при выключенном процессоре: это fail-closed, Run упадёт `JSON_PROCESSOR_NOT_CONFIGURED`;
- сохраните `request_id`, job id, error_code и timestamps для разбора;
- при возврате в `text`/legacy учтите, что downstream мог читать `$.output.json`: верните его на `$.task.output.json` или оставьте совместимый путь.

### 13.6. Декомиссия legacy

Удаление legacy same-chat repair допускается только после:

- отдельного ADR;
- периода совместимости;
- отсутствия активных пайплайнов с `result_mode="strict_json"`/`max_json_repairs`;
- проверки, что новый путь покрывает все схемы и отчёты;
- успешного rollback-учения.

До этого legacy остаётся доступным, но не смешивается с новым процессором на одном ответе.

---

## 14. Тестирование

### 14.1. Матрица T01–T20

В `tests/test_json_processor_qa.py` явно поименованы T01–T14, T16–T17, T19. Номера T15, T18, T20 в текущем дереве закрыты смежными suite; матрица ниже фиксирует покрытие, не утверждая, что каждая строка имеет одноимённую функцию.

| ID | Проверка | Где покрыто |
|---|---|---|
| T01 | Валидный JSON проходит без LLM и без job | `test_t01_valid_json_succeeds_without_llm_or_job` |
| T02 | Schema mismatch fail-closed | `test_t02_schema_failure_is_fail_closed` |
| T03 | Markdown fence → local repair | `test_t03_markdown_fence_local_repair` |
| T04 | Trailing comma → local repair | `test_t04_trailing_comma_local_repair` |
| T05 | Single quotes/Python literals | `test_t05_single_quotes_and_js_literals` |
| T06 | Свободный текст не repair-ится | `test_t06_free_text_is_not_repairable` |
| T07 | Несколько JSON-блоков → ambiguous | `test_t07_multiple_json_blocks_are_ambiguous` |
| T08 | Невалидный, затем валидный LLM-ответ | `test_t08_invalid_then_valid_llm_attempt` |
| T09 | LLM меняет значение → content changed | `test_t09_llm_changing_a_value_is_content_changed` |
| T10 | Disabled/unconfigured provider fail-closed | `test_t10_disabled_and_unconfigured_providers_fail_closed` |
| T11 | Повторное completion-событие → один job | `test_t11_repeated_completion_event_creates_one_job` |
| T12 | Expired lease → recovery → обработка | `test_t12_expired_lease_is_recovered_and_processed` |
| T13 | 429/5xx/timeout типизированы | `test_t13_provider_429_5xx_and_timeout_are_typed` |
| T14 | Отмена не вызывает provider | `test_t14_cancellation_never_calls_the_provider` |
| T15 | Retention preview/purge: 30/90, non-destructive, terminal only | `test_json_processor_db.py`, `test_json_processor_api.py` (retention tests) |
| T16 | Prompt injection остаётся данными, tools отсутствуют | `test_t16_prompt_injection_stays_untrusted_and_tool_free` |
| T17 | Огромная схема и внешний `$ref` отклоняются | `test_t17_huge_schema_and_external_ref_are_rejected` |
| T18 | RBAC/CSRF, audit без секретов, SSRF-защита | `test_json_processor_api.py` (RBAC/CSRF/audit/SSRF tests) |
| T19 | 100 jobs exactly once + duplicate suppression | `test_t19_100_parallel_outputs_are_processed_exactly_once` |
| T20 | Admin UI: настройки, sandbox, jobs, metrics, retention | `test_json_processor_browser.py` |

### 14.2. Mock provider

LLM-тесты не ходят в сеть:

- `httpx.MockTransport` + `MockOpenAIServer` в QA-тестах;
- провайдер получает transport/client через конструктор;
- ответы 200/400/422/429/5xx и timeout имитируются;
- реальные ключи и внешние endpoint-ы не используются;
- проверяется, что в payload нет `tools`/`functions`/`tool_choice`, а prompt не содержит raw delimiter-спуфа.

### 14.3. Основные suite

| Файл | Что покрывает |
|---|---|
| `tests/test_json_processor_core.py` | parser, schema, content, prompt, models. |
| `tests/test_json_processor_db.py` | settings, jobs, idempotency, lease, recovery, attempts, retention. |
| `tests/test_json_processor_service.py` | pipeline, local/LLM, content, provider failures, cancellation, raw. |
| `tests/test_json_processor_provider.py` | URL/SSRF, auth, response_format fallback, typed errors, `/v1/models`. |
| `tests/test_json_processor_worker.py` | concurrency, claim/finalize, recovery, queue limit, conflicts. |
| `tests/test_json_processor_api.py` | RBAC/CSRF, settings, sandbox, connection, jobs, metrics, retention, SSRF, secrets. |
| `tests/test_json_processor_qa.py` | матрица T01–T14, T16–T17, T19, mock provider. |
| `tests/test_json_processor_browser.py` | admin UI (Playwright, может skip без пакета). |
| `tests/test_vflow_json_processor_integration.py` | `processing_output`, output contract, idempotency, failure/cancel/recovery, legacy isolation. |

### 14.4. Команды

Из корня репозитория:

```bash
# Только JSON Processor + VFlow integration
.venv/bin/pytest -q \
  tests/test_json_processor_core.py \
  tests/test_json_processor_db.py \
  tests/test_json_processor_service.py \
  tests/test_json_processor_provider.py \
  tests/test_json_processor_worker.py \
  tests/test_json_processor_api.py \
  tests/test_json_processor_qa.py \
  tests/test_json_processor_browser.py \
  tests/test_vflow_json_processor_integration.py

# Весь VFlow suite
.venv/bin/pytest -q tests/test_vflow_*.py

# Uploads/artifacts
.venv/bin/pytest -q tests/test_attachments.py tests/test_artifacts.py

# Полный suite
.venv/bin/pytest -q

# Линтер
.venv/bin/ruff check src tests

# Синтаксис frontend
node --check src/opencode_web/static/json_processor.js
node --check src/opencode_web/static/vflow.js
node --check src/opencode_web/static/app.js
```

Ориентир на текущем дереве (M10/M11):

- JSON Processor + VFlow integration: **463 passed**;
- uploads/artifacts: **69 passed**;
- полный suite может быть больше и должен быть зелёным перед релизом.

Browser test:

```bash
.venv/bin/pytest -q tests/test_json_processor_browser.py
.venv/bin/python -m playwright install chromium   # если нужно
```

Без `playwright` тест будет **skipped**, а не failed. Browser test использует замоканный API и реальные static-файлы, а не живой backend.

### 14.5. Что проверять вручную после релиза

1. Admin UI открывается только admin.
2. Sandbox local/LLM работает; job не создаётся.
3. Connection check различает auth/private/model.
4. Canary Task с `output_mode=json` даёт `output.json`.
5. Невалидный JSON с `repair` создаёт job и успешно репарится.
6. Невалидный JSON с `fail` даёт `OUTPUT_INVALID` без job.
7. Task Manager показывает completed.
8. Отмена Run отменяет processor job.
9. Metrics/retention отражают реальные данные.
10. Purge не трогает active jobs и не удаляет job rows.

---

## 15. Известные ограничения

### 15.1. DNS rebinding не закрыт пиннингом IP

- Provider валидирует literal IP, `localhost`, metadata/link-local/private диапазоны.
- Hostname **не резолвится** при валидации; публичное имя может в момент запроса разрешиться в private/metadata адрес (DNS rebinding).
- `follow_redirects=False` и `trust_env=False` снижают риск, но не заменяют egress-контроль.
- Для production: egress firewall/allowlist, DNS-контроль, запрет private networks, отдельный proxy для provider, мониторинг аномальных подключений. Пиннинг resolved IP — вне текущего модуля.

### 15.2. Внешние raw rows не удаляются процессором

- `task_run_results` принадлежит Task/VFlow, не процессору.
- Retention purge обнуляет только `json_repair_jobs.result_json` и удаляет `json_repair_attempts`.
- Raw text в `task_run_results` и backup-ах живёт по правилам Task/VFlow; настройте отдельный retention/очистку.
- Имя `raw_output_retention_days` историческое: оно не удаляет внешний raw.

### 15.3. Нет semantic rewrite

- Процессор не исправляет неверные факты, не добавляет отсутствующие поля, не меняет типы вне allowlist, не «улучшает» текст.
- Если модель вернула правильный JSON, но с неправильным смыслом, процессор это не заметит; нужна бизнес-валидация downstream.
- `schema_safe` не является разрешением на свободное переписывание; в VFlow per-request allowances не передаются.
- `CONTENT_CHANGED` — ожидаемый fail-closed результат, а не баг.

### 15.4. Ограничения JSON Schema

Поддерживается только подмножество:

- `type`, `required`, `properties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `additionalProperties`;
- аннотации `$schema`, `title`, `description`, `default`, `examples`;
- нет `$ref`, `oneOf`/`anyOf`/`allOf`/`not`, `const`, `format`, `patternProperties`, `dependencies`, tuple-items, условных схем;
- размер ≤ 16 KiB, глубина ≤ 16, enum ≤ 1000, pattern ≤ 1024;
- `additionalProperties` только boolean;
- схемы вне подмножества отклоняются, а не упрощаются.

### 15.5. Дополнительные эксплуатационные ограничения

- **Task-поля vs аргументы VFlow-узла.** Task-запись хранит `output_mode`/`output_schema`/`on_json_error`/`repair_trigger` как декларацию контракта, но исполнение в текущей реализации определяется аргументами `task.run`; они не наследуются автоматически. Прямой Task Manager run не вызывает процессор.
- **`queue_limit` не жёсткий.** `worker.enqueue` проверяет queued-depth best-effort; VFlow adapter создаёт job напрямую через repository. Жёсткий backpressure нужно строить на уровне API/очереди.
- **Нет admin cancel endpoint.** Отмена — через VFlow Run или внутренний repository/service.
- **Нет streaming/tools/function calling.** Провайдер должен поддерживать обычный non-streaming Chat Completions.
- **Нет автоматического retry для 429/5xx/timeout.** `max_llm_attempts` не является retry-policy; transient provider error завершает job.
- **Lease 120 с не настраивается через UI.** При `request_timeout_seconds` > 120 и нескольких worker-ах возможен claim после истечения lease; держите timeout ниже lease или не масштабируйте worker-ы без пересмотра.
- **Только env для `api_key_ref`.** Нет Vault/AWS Secrets Manager; `vault://` — иллюстрация, не рабочий resolver.
- **Provider settings не snapshot-ятся в job.** `settings_version` — identity/audit, а обработка читает текущие настройки.
- **Метрики не Prometheus.** Только admin JSON; для алертов используйте внешний polling.
- **SQLite и несколько worker-ов.** CAS защищает от дублей, но write contention и общий файл БД ограничивают масштабирование.
- **Browser test может skip.** Без Playwright UI-тест не выполняется.
- **Raw в Task result API.** Процессор не отдаёт raw в своих API, но Task run result API может вернуть исходный текст владельцу/админу; это контракт Task.
- **Ошибки provider не содержат тело ответа.** Это сделано для безопасности, но усложняет диагностику; используйте status/provider code и sandbox.

---

## 16. Связанные документы и исходники

### 16.1. Документы

- [VALERIA_JSON_Processor_ADR_RU.md](VALERIA_JSON_Processor_ADR_RU.md) — архитектурные решения M0–M2 и контракты.
- [VALERIA_VFlow_Operations_RU.md](VALERIA_VFlow_Operations_RU.md) — эксплуатация VFlow, разделы 4.7, 7.7.1, 8.
- [VALERIA_VFlow_Technical_Specification_RU.md](VALERIA_VFlow_Technical_Specification_RU.md) — техническая спецификация VFlow.
- [VALERIA_VFlow_Language_Guide_RU.md](VALERIA_VFlow_Language_Guide_RU.md) — язык DSL.
- [VALERIA_VFlow_Extension_SDK_RU.md](VALERIA_VFlow_Extension_SDK_RU.md) — расширения/обработчики.
- [`.env.example`](../.env.example) — шаблон окружения (добавьте `JSON_PROCESSOR_ALLOW_PRIVATE_NETWORKS` при необходимости).

### 16.2. Ключевые исходники

- `src/opencode_web/json_processor/service.py` — pipeline и finalize.
- `src/opencode_web/json_processor/repository.py` — jobs, lease, idempotency, retention.
- `src/opencode_web/json_processor/api.py` — admin API.
- `src/opencode_web/json_processor/provider.py` — OpenAI-compatible client и SSRF.
- `src/opencode_web/json_processor/integration.py` — VFlow adapter и env key resolver.
- `src/opencode_web/vflow/runtime.py` — `processing_output` и processor transitions.
- `src/opencode_web/vflow/task_output.py` — output contract.
- `src/opencode_web/static/json_processor.js` — admin UI.
- `tests/test_json_processor_qa.py` — матрица T01–T20 (явно поименованные тесты и смежные suite, см. 14.1).
- `tests/test_vflow_json_processor_integration.py` — VFlow integration.
