# VALERIA VFlow — эксплуатационная и разработческая документация

**Статус:** описывает фактическую реализацию в текущем рабочем дереве (22.11.2026, включая JSON Processor M8–M11).
**Аудитория:** администраторы VALERIA, DevOps/сопровождение, разработчики модуля.
**Границы:** это практическое руководство, а не замена технической спецификации. Там, где спецификация описывает целевое поведение, а код реализует подмножество, это явно отмечено.

VFlow — модуль пайплайнов внутри VALERIA. Он позволяет администратору собрать граф из системных операторов и существующих задач VALERIA, опубликовать неизменяемую версию и запускать её от имени пользователя. VFlow не запускает OpenCode напрямую: задачи ставятся в общую очередь VALERIA через Task Gateway.

## Содержание

1. [Обзор и архитектура](#1-обзор-и-архитектура)
2. [Быстрый старт и конфигурация](#2-быстрый-старт-и-конфигурация)
3. [Роли и интерфейс](#3-роли-и-интерфейс)
4. [Язык VFlow DSL](#4-язык-vflow-dsl)
5. [HTTP API](#5-http-api)
6. [Хранение данных](#6-хранение-данных)
7. [Task Gateway](#7-task-gateway)
8. [Эксплуатация](#8-эксплуатация)
9. [Тестирование](#9-тестирование)
10. [Связанные документы](#10-связанные-документы)

---

## 1. Обзор и архитектура

### 1.1. Назначение

VFlow решает три задачи:

1. **Описание процесса.** Администратор собирает граф в визуальном редакторе или пишет VFlow-код. Оба представления компилируются в один канонический Graph IR.
2. **Надёжное исполнение.** Пайплайн исполняется как набор персистентных точек выполнения (execution points). Ожидание задачи не занимает HTTP-запрос, поток планировщика или слот OpenCode.
3. **Безопасная выдача пользователю.** Пользователь видит только доступные ему опубликованные пайплайны, свои запуски, агрегированный прогресс и — только если администратор явно настроил схему — очищенный публичный результат. Граф, внутренние ID задач и промежуточный JSON в пользовательский API не попадают.

### 1.2. Компоненты

```text
Браузер / API-клиент
        │  HTTP, cookie-сессия, CSRF
        ▼
FastAPI (src/opencode_web/app.py)
        │  register_vflow(...)
        ▼
VFlow HTTP API (vflow/api.py)
        │
        ├── VFlowService / VFlowRepository ──► vflow_definitions, vflow_versions
        │       компиляция, черновики, публикация, аудитория
        │
        └── VFlowRuntime ────────────────────► vflow_runs, vflow_points,
                │                               vflow_fork_instances,
                │                               vflow_merge_arrivals,
                │                               vflow_task_links,
                │                               vflow_events, vflow_outbox
                │
                ├── VFlowScheduler (фоновый цикл)
                │       claim_due() → process_point()
                │
                ├── Node handlers (чистые plan() → Action)
                │       Task, Transform, If, Fork, Merge
                │
                └── ValeriaTaskGateway
                        │  queue_jobs + chats (та же транзакция SQLite)
                        ▼
                Существующий QueueDispatcher VALERIA
                        │
                        ▼
                OpenCode / агент
```

Ключевые свойства:

- **Единый процесс и единая SQLite-БД.** VFlow не является отдельным сервисом и не имеет отдельной авторизации.
- **Runtime — единственное место записи состояния.** Обработчики узлов не получают соединение с БД; они возвращают типизированное действие (`Advance`, `EnqueueTask`, `Suspend`, `SpawnFork`, `ArriveMerge`, `Finish`, `Fail`), которое runtime применяет в короткой транзакции.
- **Источник истины — БД, а не SSE/память.** Событие `job.updated` только будит планировщик; после рестарта состояние восстанавливается из таблиц.
- **Публикация фиксирует версию.** Активный Run всегда исполняет ту версию графа и те версии обработчиков, которые были зафиксированы при старте Run.

### 1.3. Основные сущности

| Сущность | Что означает |
|---|---|
| **Pipeline Definition** | Логический пайплайн: slug, название, описание, аудитория, статус `active`/`archived`, указатели на черновик и опубликованную версию. |
| **Pipeline Version** | Версия графа. Черновик можно менять; опубликованная версия неизменяема через API. Хранит Graph IR, DSL-исходник, входную схему, публичную выходную схему, `required_handlers_json`, `graph_hash`. |
| **Pipeline Run** | Запуск конкретной опубликованной версии конкретным пользователем с входным JSON. Имеет статус, безопасную ошибку, итоговый JSON и счётчики. |
| **Execution Point** | Персистентная единица исполнения: `run_id`, `node_id`, JSON-данные, статус, `visit_seq`, `attempt`, контекст Fork/Merge, аренда. Одна точка проходит граф; Fork создаёт несколько точек. |
| **Fork Instance** | Одна конкретная активация узла Fork в Run: ожидаемое число ветвей, исходные данные, родительский контекст, связь с Merge. |
| **Merge Arrival** | Факт прибытия конкретной ветки конкретного Fork Instance на Merge. Уникален по `(fork_instance_id, branch_index)`. |
| **Task Link** | Связь точки-попытки с конкретным `queue_jobs` (Task Run), включая `enqueue_key` и временные метки. |
| **Event** | Диагностическая запись о переходе: `RUN_STARTED`, `POINT_ADVANCED`, `TASK_ENQUEUED`, `FORK_SPAWNED`, `MERGE_COMPLETED`, `RUN_FAILED` и т. п. |
| **Outbox** | Запись намерения постановки задачи (`task_enqueue`), используемая как диагностический/durable intent и учитываемая health/metrics. Отдельного фонового отправителя outbox в текущей реализации нет. |

Статусы Run: `created`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled`.
Статусы точки: `ready`, `claimed`, `waiting_task`, `processing_output`, `waiting_merge`, `forked`, `consumed`, `failed`, `cancelled`.

### 1.4. IR и версионирование

Graph IR — обычный JSON, пригодный для хранения в SQLite и чтения старыми runtime:

```json
{
  "ir_version": 1,
  "workflow": "review_changes",
  "entry": "context",
  "nodes": [
    {
      "id": "context",
      "type": "valeria.task",
      "type_version": "1.0",
      "config": {"task_id": "context.load", "input_path": "$", "result_mode": "strict_json"},
      "ui": {"x": 80, "y": 120}
    }
  ],
  "edges": [
    {"source": "start", "source_port": "out", "target": "context"},
    {"source": "context", "source_port": "out", "target": "end"}
  ]
}
```

- `ir_version` сейчас равен `1`.
- `entry` — цель единственного ребра `start -> ...`.
- `nodes` и `edges` не задают порядок исполнения. Порядок определяется переходами и состоянием точек.
- `ui` хранит позиции редактора. Позиции не влияют на исполнение, но входят в канонический JSON, `graph_hash` и ETag версии: перемещение узла меняет хеш черновика.
- Компилятор нормализует конфигурацию и считает `graph_hash = SHA-256(canonical_json)`.
- `required_handlers_json` — отсортированный список вида `["core.fork@1.0", "valeria.task@1.0"]`. Он используется healthcheck-ом и при публикации.
- Публикация не пересобирает сохранённый IR из текущего Registry. Активный Run не должен незаметно перейти на новую версию обработчика.

### 1.5. Встроенные блоки

Registry — code-only. Администратор не добавляет типы блоков через UI; новый тип появляется только с релизом кода.

| DSL-вызов | IR type | Порты | Основные параметры |
|---|---|---|---|
| `task.run(...)` | `valeria.task@1.0` | `out` | `task_id`, `input`/`input_path`, `result_mode`, `output_mode`, `output_schema`, `on_json_error`, `repair_trigger`, `json_schema`, `max_json_repairs`, `queue_timeout_seconds`, `execution_timeout_seconds`, `retries` |
| `transform(...)` | `core.transform@1.0` | `out` | `operations`, `create_missing` |
| `if(...)` | `core.if@1.0` | `true`, `false` | `path`, `operator`, `value`, `missing` |
| `fork(...)` | `core.fork@1.0` | `branch[0..N-1]` или `all` | `count`, `join` |
| `merge(...)` | `core.merge@1.0` | `out` | `mode` (только `all`) |

`start` и `end` — служебные терминалы, а не узлы Registry.

### 1.6. Task Gateway и очередь

`ValeriaTaskGateway` — тонкий адаптер между точкой VFlow и существующей очередью VALERIA. Он создаёт обычные `queue_jobs` и `chats`; исполняет их существующий `QueueDispatcher`. VFlow не вызывает OpenCode, не управляет сессиями и не создаёт второй механизм очереди.

Для `strict_json` runtime проверяет весь финальный текст: сначала синтаксис JSON, затем необязательную `json_schema`. При ошибке и остатке `max_json_repairs` Gateway создаёт follow-up `queue_jobs` в **том же** `chat_id`/`session_id` с `task_id = NULL`, `job_kind = 'vflow_repair'`, `pipeline_json = ''` и детерминированным `message_id` (`msg_vflow_repair_<sha256(run:point:attempt)>`). Это обычное продолжение чата, а не новый Task Run и не legacy-потомок pipeline. Повторный вызов идемпотентен. Ответ follow-up сохраняется в `task_run_results` и читается Gateway как финальный текст. Очередь исправлений восстанавливается из `vflow_outbox.kind = 'task_json_repair'`; отмена Run отменяет ещё не запущенные repair-jobs.

Для `output_mode: "json"` используется отдельный JSON Processor. Валидный JSON не создаёт job. Невалидный JSON (или несоответствие схеме при `repair_trigger: "invalid_json_or_schema"` и `on_json_error: "repair"`) создаёт запись `json_repair_jobs` с корреляцией `vflow_run_id`/`point_id`/`task_run_id`; точка переходит в `processing_output` и ждёт отдельный worker. LLM-вызов никогда не выполняется внутри транзакции VFlow. При успехе значение попадает в `output.json` и `task.output.json`; при ошибке Run падает `OUTPUT_INVALID`. Выключенный процессор даёт `JSON_PROCESSOR_NOT_CONFIGURED`. Legacy same-chat repair и новый processor path взаимоисключающие: компилятор отклоняет `max_json_repairs > 0` вместе с `on_json_error: "repair"` (`COMPILE_REPAIR_ENGINE_CONFLICT`).

Подробности — в разделе 7.

---

## 2. Быстрый старт и конфигурация

### 2.1. Минимальный запуск

Локально, из корня репозитория:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
mkdir -p .secrets data/workspaces
openssl rand -base64 32 > .secrets/admin-password
chmod 600 .secrets/admin-password
set -a
source .env
set +a
.venv/bin/opencode-web
```

Перед запуском задайте как минимум `SESSION_SECRET`, `OPENCODE_SERVER_PASSWORD`, `ALLOWED_WORKSPACE_ROOTS` и `BOOTSTRAP_ADMIN_PASSWORD_FILE`. Bootstrap-администратор создаётся только при первом старте с пустой БД.

Docker Compose:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f web
```

Systemd-развёртывание:

```bash
./run-local.sh start
./run-local.sh status
./run-local.sh stop
```

VFlow не требует отдельного процесса. Он запускается и останавливается вместе с приложением VALERIA.

### 2.2. Переменные окружения VFlow

Все значения читаются при создании приложения. После изменения `.env` нужен перезапуск. Пустое значение означает default. Некорректное значение приводит к ошибке запуска.

| Переменная | Default | Валидация | Назначение |
|---|---:|---|---|
| `VFLOW_ENABLED` | `true` | `1/true/yes/on` или `0/false/no/off` | Главный feature flag. |
| `VFLOW_MAX_FORK_N` | `32` | положительное целое | Максимальное `count` для Fork, проверяется runtime. |
| `VFLOW_MAX_ACTIVE_POINTS` | `128` | положительное целое | Максимум одновременно активных точек в Run при создании Fork. |
| `VFLOW_MAX_TOTAL_POINTS` | `1000` | положительное целое | Максимум созданных точек в Run. |
| `VFLOW_MAX_TRANSITIONS` | `10000` | положительное целое | Максимум переходов Run; превышение даёт `LIMIT_TRANSITIONS`. |
| `VFLOW_MAX_JSON_BYTES` | `1048576` (1 МиБ) | положительное целое | Максимальный размер JSON-значения (вход Run, выход узла). |
| `VFLOW_MAX_JSON_DEPTH` | `64` | положительное целое | Максимальная глубина JSON. |
| `VFLOW_MAX_RUN_SECONDS` | `86400` | положительное целое | Загружается и валидируется, но отдельного wall-clock enforcement в текущем runtime нет; см. 2.7. |
| `VFLOW_RUNS_PER_MINUTE` | `30` | положительное целое | Лимит запусков на пользователя в окне. |
| `VFLOW_RUN_WINDOW_SECONDS` | `60.0` | положительное конечное число | Окно rate limit. |
| `VFLOW_RETENTION_DAYS` | `30` | положительное целое | Хранение завершённых Run. |

Пример `.env`:

```dotenv
VFLOW_ENABLED=true
VFLOW_MAX_FORK_N=32
VFLOW_MAX_ACTIVE_POINTS=128
VFLOW_MAX_TOTAL_POINTS=1000
VFLOW_MAX_TRANSITIONS=10000
VFLOW_MAX_JSON_BYTES=1048576
VFLOW_MAX_JSON_DEPTH=64
VFLOW_MAX_RUN_SECONDS=86400
VFLOW_RUNS_PER_MINUTE=30
VFLOW_RUN_WINDOW_SECONDS=60
VFLOW_RETENTION_DAYS=30
```

### 2.3. `VFLOW_ENABLED`

При `VFLOW_ENABLED=false`:

- все VFlow HTTP-маршруты не регистрируются и отвечают `404`;
- в `/api/config/public` приходит `vflow_enabled: false`, UI скрывает «Пайплайны» и административный раздел;
- scheduler, recovery и retention scheduler не запускаются;
- данные в `vflow_*` не удаляются;
- активные Run не продвигаются, пока флаг не включат снова и приложение не перезапустят.

Флаг не является способом «мягко отключить только UI». Это именно отключение модуля с сохранением данных.

### 2.4. Лимиты: что и когда проверяется

| Лимит | Где проверяется | Поведение |
|---|---|---|
| `VFLOW_MAX_FORK_N` | runtime при `SpawnFork` | `LIMIT_FORK_COUNT`, Run завершается `failed`. |
| `VFLOW_MAX_ACTIVE_POINTS` | runtime при Fork | `LIMIT_ACTIVE_POINTS`. |
| `VFLOW_MAX_TOTAL_POINTS` | runtime при Fork | `LIMIT_TOTAL_POINTS`. |
| `VFLOW_MAX_TRANSITIONS` | каждый переход | `LIMIT_TRANSITIONS`. |
| `VFLOW_MAX_JSON_BYTES` | вход Run и выход узла | `LIMIT_JSON_BYTES`. |
| `VFLOW_MAX_JSON_DEPTH` | вход Run и выход узла | `LIMIT_JSON_DEPTH`. |
| `queue_timeout_seconds` | Task | `TASK_QUEUE_TIMEOUT` (с retry, если разрешён; для repair-job retry не создаётся). |
| `execution_timeout_seconds` | Task | `TASK_EXECUTION_TIMEOUT` без автоматического retry. |
| `max_json_repairs` | Task `strict_json` | `TASK_OUTPUT_NOT_JSON` / `TASK_SCHEMA_INVALID` после исчерпания исправлений. |

Важное расхождение: компилятор принимает `fork count` от 2 до 32 (константа компилятора), а runtime применяет `VFLOW_MAX_FORK_N`. Если `VFLOW_MAX_FORK_N` меньше 32, граф с большим `count` может сохраниться и опубликоваться, но Run завершится `LIMIT_FORK_COUNT`. Держите значение env согласованным с политикой публикации.

Лимиты JSON проверяются на входе Run и на выходе узлов. Проверка входа выполняется до создания Run; ошибка возвращается как runtime-ошибка в общем конверте.

### 2.5. Rate limit

- Ограничение — **на пользователя**, скользящее окно `VFLOW_RUN_WINDOW_SECONDS`.
- Действует только на `POST /api/pipelines/{identifier}/runs`.
- При превышении: HTTP `429`, код `VFLOW_RATE_LIMITED`, заголовок `Retry-After` в секундах.
- Реализация process-local, в памяти. После рестарта счётчики сбрасываются; при нескольких процессах/репликах лимит не является общим. Для горизонтального масштабирования нужен внешний store.

### 2.6. Retention

`VFLOW_RETENTION_DAYS` задаёт окно только для **завершённых** Run (`succeeded`, `failed`, `cancelled`). Определения, версии, активные Run и аудит не удаляются. Фоновый retention-цикл запускается сразу при старте и затем раз в 24 часа (интервал в текущей реализации фиксирован, отдельной env-переменной нет). Подробнее — в 6.4 и 8.4.

### 2.7. Текущие ограничения конфигурации

- `VFLOW_MAX_RUN_SECONDS` пока не применяется как отдельный таймер. Ограничение времени Run фактически задаётся таймаутами Task и лимитом переходов.
- `settings_json` определения сохраняется, но per-pipeline override лимитов в текущем runtime не реализован. Действуют глобальные `VFLOW_*`.
- Rate limit не распределён между процессами.
- Компилятор не знает runtime-лимиты Fork (см. 2.4).

---

## 3. Роли и интерфейс

### 3.1. Роли

| Роль | Доступ |
|---|---|
| **Пользователь** (`role=user`) | Видит пункт «Пайплайны», доступные по аудитории опубликованные пайплайны, свои Run, прогресс, отмену и публичный результат (если включён). Не видит граф, DSL, Task ID, точки, fork stack и промежуточный JSON. |
| **Пользователь `tasks_only`** | То же, что обычный пользователь, если модуль включён. Доступ к `#/pipelines` разрешён; административные разделы скрыты и закрыты сервером. |
| **Администратор** (`role=admin`) | Создаёт определения, редактирует черновик, проверяет, публикует, откатывает, архивирует, настраивает аудиторию и схемы, запускает тестовые Run, смотрит админ-диагностику всех Run. |

Администратор не становится разработчиком расширений: новые типы блоков добавляются только кодом и деплоем.

### 3.2. Пользовательская вкладка «Пайплайны»

SPA-маршрут: `#/pipelines`.

Содержимое:

- карточки опубликованных пайплайнов, доступных пользователю по аудитории;
- название, описание, версия, дата публикации;
- форма ввода, генерируемая по `input_schema` (enum, boolean, number/integer, object/array, string-ограничения);
- fallback — JSON-редактор, если у схемы нет `properties`;
- история **только своих** Run с фильтром по статусу и пагинацией «Показать ещё»;
- статус, прогресс, безопасная ошибка;
- отмена своего Run;
- кнопка публичного результата, только если он реально доступен.

Пользователь не получает:

- `graph_ir_json`, `vflow_source`, `node_id`, `task_id`, `task_run_id`, `fork_stack`;
- `data_json` промежуточных точек;
- prompt-ы и содержимое задач;
- Run других пользователей (чужие Run дают `404`, а не `403`).

### 3.3. Административный редактор

SPA-маршруты: `#/admin-pipelines`, `#/admin-pipeline-editor`.

Возможности текущего UI:

- список всех определений, создание нового по slug/title/description;
- вкладки «Граф», «VFlow-код», «Запуски»;
- палитра системных блоков и задач VALERIA (включая неопубликованные задачи — это осознанное делегирование администратора);
- drag-and-drop, zoom/pan, поиск по палитре, undo/redo;
- инспектор узла: поля по `config_schema` и JSON-редактор конфигурации;
- сохранение черновика, проверка, публикация, тестовый запуск;
- настройка аудитории (`all` или список user ID);
- редактирование `input_schema` и `public_output_schema`;
- журнал Run: точки, события, фильтр по event type/node, отмена.

Откат на прежнюю опубликованную версию и архивация доступны через административный API (`rollback`, `archive`); в текущем UI отдельных кнопок для них может не быть.

### 3.4. Аудитория

Аудитория хранится в `vflow_definitions.audience_json`:

```json
{"mode": "all"}
```

или

```json
{"mode": "selected", "user_ids": ["user-id-1", "user-id-2"]}
```

Правила:

- `all` — видят все пользователи, у которых есть доступ к модулю;
- `selected` — только перечисленные ID;
- при `PUT .../audience` в режиме `selected` сервер проверяет существование каждого пользователя;
- список дедуплицируется;
- скрытый пайплайн для пользователя выглядит как `404`;
- аудитория применяется к каталогу и к созданию Run, а не только к UI.

Администратор видит все определения независимо от аудитории.

### 3.5. Входная схема (`input_schema`)

Схема хранится в версии и используется дважды: для генерации формы в UI и для **серверной** проверки входа Run. Это не полная JSON Schema, а безопасное подмножество.

Поддерживаются:

- `type`: `object`, `array`, `string`, `boolean`, `integer`, `number`, `null`;
- `enum`;
- для object: `required`, `properties`, рекурсивная проверка;
- для array: `minItems`, `maxItems`, `items`;
- для string: `minLength`, `maxLength`, `pattern` (Python `re.search`);
- для integer/number: `minimum`, `maximum`.

Не поддерживаются (игнорируются): `additionalProperties`, `format`, `oneOf`, `anyOf`, `$ref`, условные схемы и т. п. Если схема пустая или не является объектом, проверка не выполняется.

Пример:

```json
{
  "type": "object",
  "required": ["repository", "pr"],
  "properties": {
    "repository": {"type": "string", "minLength": 1},
    "pr": {"type": "integer", "minimum": 1}
  }
}
```

### 3.6. Публичная выходная схема (`public_output_schema`)

По умолчанию пользователь получает только статус. Чтобы разрешить результат, администратор задаёт непустую схему-allowlist. `GET /api/pipeline-runs/{id}/result`:

1. проверяет, что Run принадлежит пользователю;
2. проверяет, что Run имеет статус `succeeded`;
3. берёт `final_result_json`;
4. проецирует его через `public_output_schema`;
5. возвращает только разрешённые поля.

Пример:

```json
{
  "type": "object",
  "properties": {
    "summary": {"type": "string"},
    "approved": {"type": "boolean"}
  }
}
```

Если итоговый JSON содержит `summary`, `approved` и `internal_token`, пользователь получит только `summary` и `approved`. Поля, которых нет в итоговом JSON, просто не попадают в результат.

Это **allowlist-проекция**, а не полная валидация JSON Schema:

- поддерживаются object/properties, array/items, scalar-типы и `enum`;
- `required`, `minLength`, `minimum` и другие ограничители при проекции не проверяются;
- массивы проецируются поэлементно;
- при несовпадении типа возвращается безопасная ошибка без раскрытия значения.

Текущее ограничение: `PUT .../draft` с `public_output_schema: null` означает «не менять» и не очищает ранее заданную схему. Пустая схема `{}` трактуется как отсутствие публичного результата при выдаче.

### 3.7. Что видит пользователь, а что администратор

| Данные | Пользователь | Администратор |
|---|---|---|
| Название, описание, версия, `input_schema` | Да, для доступных пайплайнов | Да |
| `public_result_enabled` | Да | Да |
| Свои Run, статус, прогресс, безопасная ошибка | Да | Да, все Run |
| Публичный результат | Только свой, только при успехе и непустой схеме | Через диагностику/БД; отдельного «все результаты» API нет |
| Graph IR, DSL-исходник | Нет | Да |
| `node_id`, `task_id`, `task_run_id`, `fork_stack` | Нет | Да, в админ-диагностике |
| Промежуточный JSON точек, события | Нет | Да, в `GET /api/admin/vflow/runs/{id}` |
| Task snapshots | Нет | Только безопасные метаданные: task_id, title, updated_at, agent/provider/model, queue_id. Без prompt/content. |
| Чужие Run | `404` | Да |

---

## 4. Язык VFlow DSL

### 4.1. Лексика

- UTF-8.
- Идентификаторы: `[A-Za-z_][A-Za-z0-9_]*`.
- Строки — JSON-строки с JSON-экранированием.
- Числа — JSON-формат: целые и с плавающей точкой, включая экспоненту.
- Комментарии: `// ...` или `# ...` до конца строки.
- `;` после заголовков, шагов и рёбер необязательна.
- Отступы и переводы строк не значимы.
- `true`, `false`, `null` — литералы.
- Необязательные `identifier`-значения допустимы там, где ожидается строковая ссылка, например `join: combined`.

### 4.2. Структура

```vflow
workflow "name" {
    version: 1
    input: json

    step node_id = task.run(task_id: "task.id")
    step transform_id = transform(operations: [{op: "set", path: "$.ok", value: true}])
    step decision = if(path: "$.ok", operator: "eq", value: true)
    step split = fork(count: 2, join: combined)
    step combined = merge()

    start -> node_id -> transform_id -> decision
    decision.true -> split
    decision.false -> combined
    split[0] -> ... -> combined
    split[1] -> ... -> combined
    combined -> end
}
```

Обязательны `version` (положительное целое) и `input` (в текущей реализации нормативно `json`; парсер принимает идентификатор или строку).

Порядок объявления `step` не задаёт порядок исполнения. Он определяется только рёбрами и состоянием точек.

### 4.3. Узлы и аргументы

#### `task.run(...)`

Ставит существующую задачу VALERIA в общую очередь и ждёт результат.

| Аргумент | Обязателен | Значение |
|---|---|---|
| `task_id` | Да | ID существующей, не удалённой задачи VALERIA. |
| `input` или `input_path` | Нет | JSONPath к фрагменту входного JSON. Default `$`. `input` — alias. |
| `result_mode` | Нет | **Legacy**: `auto` (default), `strict_json`, `none`. |
| `output_mode` | Нет | `text` (default) или `json`; включает контракт `output.json`. |
| `output_schema` | Нет | JSON Schema для `output_mode=json`; приоритетнее legacy `json_schema`. |
| `on_json_error` | Нет | `fail` (default) или `repair`; `repair` требует `output_mode=json`. |
| `repair_trigger` | Нет | `invalid_json` (default) или `invalid_json_or_schema`; только с `on_json_error=repair`. |
| `queue_timeout_seconds` | Нет | Неотрицательное целое; время ожидания до старта задачи. |
| `execution_timeout_seconds` | Нет | Неотрицательное целое; время выполнения после старта. |
| `retries` | Нет | Неотрицательное целое; число дополнительных попыток. UI-схема рекомендует максимум 10. |

`output_mode: "json"` включает JSON Repair Processor: процессор проверяет JSON/схему, при `on_json_error: "repair"` создаёт отдельный `json_repair_jobs` и возвращает `output.text`/`output.json`. Полный эксплуатационный контракт — в [VALERIA_JSON_Processor_Operations_RU.md](VALERIA_JSON_Processor_Operations_RU.md). Legacy `result_mode`/`json_schema`/`max_json_repairs` — отдельный same-chat путь; не смешивайте его с `on_json_error: "repair"`.

Неизвестные аргументы отклоняются компилятором.

#### `transform(...)`

| Аргумент | Обязателен | Значение |
|---|---|---|
| `operations` | Да | Непустой список операций. |
| `create_missing` | Нет | Boolean, default `false`. Разрешает создавать отсутствующие объектные ключи и дописывать элементы в конец массива. |

Компилятор проверяет, что каждая операция — объект с непустым `op`. Семантика конкретной операции проверяется при исполнении; для отладки используйте `POST /api/admin/vflow/preview-transform`.

#### `if(...)`

| Аргумент | Обязателен | Значение |
|---|---|---|
| `path` | Да | Непустой JSONPath. |
| `operator` | Да | `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, `in`, `exists`, `is_null`. |
| `value` | Зависит | Нужен для всех операторов, кроме `exists` и `is_null`. |
| `missing` | Нет | `error` (default), `false`, `true`. |

`if` не изменяет JSON и выбирает ровно один порт: `true` или `false`.

#### `fork(...)`

| Аргумент | Обязателен | Значение |
|---|---|---|
| `count` | Да | Целое от 2 до 32 (компилятор); runtime дополнительно применяет `VFLOW_MAX_FORK_N`. |
| `join` | Да | ID парного узла `merge`. |

Порты: `branch[0]` … `branch[count-1]` или один `all`.

#### `merge(...)`

| Аргумент | Обязателен | Значение |
|---|---|---|
| `mode` | Нет | Только `all` (default). |

Merge обязан быть парным ровно одному Fork.

### 4.4. Рёбра, цепочки и порты

- `a -> b -> c` эквивалентно двум рёбрам `a -> b` и `b -> c`.
- `start` — единственная точка входа; ровно одно исходящее ребро.
- `end` — терминал; входящие рёбра допустимы, исходящие запрещены.
- Порт источника:
  - обычный узел — `out`;
  - `if` — `true` или `false`;
  - `fork` — `branch[i]` (в DSL `split[0]`) или `all`.
- Цель ребра не может иметь порт: `a -> b.true` запрещено; порт указывается только у источника.
- `split -> worker` — сокращение `all`: все N веток идут в один узел, создавая N отдельных посещений и, если следующий узел Task, N отдельных Task Run.
- `split[0] -> review`, `split[1] -> tests` — явные ветки.

### 4.5. Правила валидации

Компилятор и публикация проверяют:

1. Синтаксис и позиции ошибок.
2. Уникальность ID шагов.
3. Известность типа блока и версии обработчика.
4. Обязательные и неизвестные аргументы, типы значений.
5. Единственный `start` и достижимый `end`.
6. Все узлы достижимы из `start`; нет «висячих» узлов.
7. Нет дублирующихся рёбер; нет рёбер из `end` и в `start`.
8. У Task/Transform/Merge ровно одно исходящее ребро `out`; fan-out запрещён — для параллельности используйте Fork.
9. У If ровно одно ребро `true` и ровно одно `false`.
10. У Fork допустимый `count`, корректные порты и существующий `join`-Merge.
11. Merge не переиспользуется и не остаётся без Fork.
12. Вложенные Fork/Merge не пересекаются, внутренний Merge закрывается раньше внешнего.
13. Ни одна ветка не достигает `end` в обход своего Merge.
14. Каждая ветка (включая оба исхода If) имеет маршрут к своему Merge.
15. Повторный вход в тот же Fork при открытом экземпляре запрещён.
16. Задачи, на которые ссылается версия, существуют (проверка при validate/publish).
17. Требуемые handler-версии зарегистрированы.

Циклы вне открытых Fork/Merge-областей допускаются; runtime ограничивает их `VFLOW_MAX_TRANSITIONS`. Статически доказать завершение произвольного цикла нельзя, поэтому публикация не гарантирует бизнес-завершение.

### 4.6. Канонический пример

Пример использует вымышленные ID задач. Перед публикацией администратор должен создать соответствующие задачи в VALERIA.

```vflow
workflow "review_changes" {
    version: 1
    input: json

    step context = task.run(
        task_id: "context.load",
        input: "$",
        result_mode: "strict_json"
    )

    step prepare = transform(
        operations: [
            {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"},
            {op: "copy", from: "$.task.output.json.pull_request", to: "$.context.pull_request"}
        ]
    )

    step split = fork(count: 3, join: combined)

    step review = task.run(task_id: "code.review", result_mode: "strict_json")
    step tests = task.run(task_id: "tests.run", result_mode: "strict_json")
    step security = task.run(task_id: "security.audit", result_mode: "strict_json")

    step combined = merge()

    step decision = if(
        path: "$.branches[0].data.task.output.json.ok",
        operator: "eq",
        value: true,
        missing: "error"
    )

    step accepted = task.run(task_id: "report.approved")
    step rejected = task.run(task_id: "report.rejected")

    start -> context -> prepare -> split
    split[0] -> review -> combined
    split[1] -> tests -> combined
    split[2] -> security -> combined
    combined -> decision
    decision.true -> accepted -> end
    decision.false -> rejected -> end
}
```

Что происходит:

1. `context` ставит задачу и возвращает конверт Task.
2. `prepare` копирует поля из `task.output.json` в `context`.
3. `split` создаёт три независимые ветки с копией входного JSON.
4. Каждая ветка выполняет свою задачу.
5. `combined` ждёт все три индекса и формирует `{source, branches}`.
6. `decision` читает `branches[0].data.task.output.json.ok`.
7. Выбирается `accepted` или `rejected`, затем `end`.

### 4.7. Контракты данных Task и Merge

Успешный Task (`result_mode=auto` или `strict_json`) возвращает:

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-id",
    "status": "completed",
    "output": {
      "text": "{\"ok\":true,\"issues\":[]}",
      "json": {"ok": true, "issues": []}
    }
  }
}
```

- `input` — полный JSON, который был у точки до Task (не только выбранный `input_path`).
- `task.output.text` — финальный текст ответа.
- `task.output.json` — распарсенный JSON при `auto`/`strict_json`; `null`, если `auto` не смог распарсить или `result_mode=none`.
- `strict_json` требует валидный JSON; при заданной `json_schema` значение также проверяется по схеме. С учётом `max_json_repairs` runtime может сначала попросить исправление в том же чате; после исчерпания — `TASK_OUTPUT_NOT_JSON` или `TASK_SCHEMA_INVALID`.
- `none` не требует JSON и не парсит его; текст всё равно остаётся в конверте.
- Prompt-ы, reasoning и tool-сообщения в результат не входят.

Успешный Task в режиме `output_mode: "json"` дополнительно содержит верхнеуровневый `output`:

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "output": {"text": "{\"ok\":true}", "json": {"ok": true}},
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-id",
    "status": "completed",
    "output": {"text": "{\"ok\":true}", "json": {"ok": true}}
  }
}
```

- `$.output.json...` — новый предпочтительный путь для If/Transform.
- `$.task.output.json...` сохранён для старых пайплайнов.
- `output.json` всегда валиден по JSON и схеме; ошибка repair не подменяет его `null`/`{}`.

После Merge:

```json
{
  "source": {"repository": "example/repo", "pr": 24},
  "branches": [
    {"index": 0, "data": {"task": {"output": {"json": {"ok": true}}}}},
    {"index": 1, "data": {"task": {"output": {"json": {"tests": 112}}}}},
    {"index": 2, "data": {"task": {"output": {"json": {"warnings": 0}}}}}
  ]
}
```

`source` — данные непосредственно перед Fork. Ветви упорядочены по `branch_index`, а не по времени завершения. Автоматического «умного» слияния полей нет; используйте Transform после Merge.

### 4.8. Операции Transform

Поддерживается ограниченный JSONPath: `$`, `.field`, `['field']`, `[index]`. Wildcards, фильтры, функции и вычисляемые выражения запрещены.

| Операция | Аргументы | Действие |
|---|---|---|
| `set` | `path`, `value` | Установить значение. `value` может быть литералом или `{"from_path": "$.field"}`. |
| `copy` | `from`, `to` | Скопировать значение. |
| `move` | `from`, `to` | Скопировать и удалить источник. |
| `remove` | `path` | Удалить значение. |
| `select` | `path` | Заменить корень выбранным фрагментом. |
| `merge_object` | `path`, `object` | Поверхностно слить объект в целевой объект. |
| `append` | `path`, `value` | Добавить значение в массив. |
| `rename` | `from`, `to` | Синоним `move` для переименования поля. |

Операции применяются последовательно к промежуточному JSON. Ошибка пути, несовместимый тип или превышение лимита JSON дают детерминированную ошибку узла. `create_missing` влияет на создание отсутствующих контейнеров/ключей, но не отменяет проверки типов.

### 4.9. Семантика If

- Сравнение типизированное: `"12"` не равно `12`.
- `gt/ge/lt/le` работают только для двух number или двух string; при разных типах результат `false`, для неподдерживаемых типов — ошибка.
- `contains` — подстрока для string или типизированное вхождение для array.
- `in` — типизированное вхождение в массив `value`.
- `exists` возвращает `false` для отсутствующего пути и не срабатывает на `missing`.
- `is_null` возвращает `true` только если путь существует и значение `null`; отсутствующий путь даёт `false`.
- Для остальных операторов отсутствующий путь обрабатывается по `missing`: `error` (ошибка), `false`, `true`.
- If не мутирует данные.

### 4.10. Граф и код

- Визуальный редактор редактирует Graph IR.
- Текстовый редактор редактирует DSL.
- `POST /api/admin/vflow/compile` превращает DSL в IR и возвращает диагностику.
- `POST /api/admin/vflow/format` превращает IR в канонический DSL.
- При сохранении можно передать `source`, `graph` или оба. Если переданы оба, их канонические графы должны совпадать, иначе `400`.
- Форматирование, пробелы и комментарии могут нормализоваться; семантика должна сохраняться.

---

## 5. HTTP API

### 5.1. Аутентификация, CSRF и роли

- Используется существующая cookie-сессия VALERIA.
- Мутации требуют заголовок `x-csrf-token` со значением CSRF-токена сессии.
- Пользовательские маршруты используют `current_principal`; создание Run и отмена — `csrf_principal`.
- Административные чтения — `admin_read_principal` (роль `admin`, без CSRF).
- Административные мутации — `admin_principal` (роль `admin` + CSRF).
- Ошибки аутентификации/CSRF/роли могут использовать общий формат VALERIA/FastAPI (`detail`), а не VFlow-конверт. Доменные ошибки VFlow используют конверт из 5.6.

### 5.2. Пользовательские маршруты

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/api/pipelines` | Каталог доступных опубликованных пайплайнов. Без графа. |
| `GET` | `/api/pipelines/{identifier}` | Публичные метаданные + `input_schema`. `identifier` — ID или slug. |
| `POST` | `/api/pipelines/{identifier}/runs` | Создать Run. Ответ `202`. |
| `GET` | `/api/pipeline-runs` | Своя история. Параметры: `status`, `pipeline_id`, `limit` (1..200, default 50), `offset`. |
| `GET` | `/api/pipeline-runs/{run_id}` | Свой Run: статус, прогресс, безопасная ошибка. |
| `GET` | `/api/pipeline-runs/{run_id}/result` | Публичный результат, если Run успешен и схема задана. |
| `POST` | `/api/pipeline-runs/{run_id}/cancel` | Отменить свой Run. |

Правила:

- Чужой или несуществующий Run — `404`.
- `status` в списке должен быть одним из `created`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, иначе `400`.
- `limit` ограничивается 200; отрицательный `offset` даёт `400`.
- `pipeline_id` фильтрует по ID определения (не по slug).
- Создание Run проверяет аудиторию, `input_schema`, workspace пользователя и rate limit.

Пример:

```http
POST /api/pipelines/review-changes/runs
Content-Type: application/json
x-csrf-token: <csrf-token>
Idempotency-Key: <uuid>

{"input": {"repository": "example/repo", "pr": 24}}
```

Сокращённый ответ `202`:

```json
{
  "id": "run-id",
  "public_result_enabled": false,
  "pipeline": {
    "id": "definition-id",
    "slug": "review-changes",
    "title": "Review changes",
    "version": 3
  },
  "status": "running",
  "created_at": "2026-11-21T10:00:00+00:00",
  "started_at": "2026-11-21T10:00:00+00:00",
  "finished_at": null,
  "progress": {
    "points_total": 1,
    "points_done": 0,
    "waiting_tasks": 0,
    "waiting_merges": 0
  },
  "error": null
}
```

### 5.3. Административные маршруты

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/api/admin/pipelines` | Все определения. |
| `POST` | `/api/admin/pipelines` | Создать определение. `201`. |
| `GET` | `/api/admin/pipelines/{definition_id}` | Определение, версии, черновик, публикация. |
| `PUT` | `/api/admin/pipelines/{definition_id}/draft` | Сохранить/обновить черновик. |
| `POST` | `/api/admin/pipelines/{definition_id}/validate` | Проверить граф и задачи. |
| `POST` | `/api/admin/pipelines/{definition_id}/publish` | Опубликовать черновик. |
| `POST` | `/api/admin/pipelines/{definition_id}/rollback` | Сделать прежнюю версию опубликованной. Тело: `{"version_id": "..."}`. |
| `POST` | `/api/admin/pipelines/{definition_id}/archive` | Архивировать определение. |
| `PUT` | `/api/admin/pipelines/{definition_id}/audience` | Задать аудиторию. |
| `GET` | `/api/admin/vflow/runs` | Все Run. Параметры: `definition_id`, `limit` (1..200). Без offset/status. |
| `GET` | `/api/admin/vflow/runs/{run_id}` | Детальная диагностика: точки, события, task snapshots. |
| `POST` | `/api/admin/vflow/runs/{run_id}/cancel` | Отменить Run по админ-праву. |
| `GET` | `/api/admin/vflow/health` | Health snapshot. |
| `GET` | `/api/admin/vflow/metrics` | Агрегированные метрики. |
| `GET` | `/api/admin/vflow/retention` | Статистика retention. |
| `POST` | `/api/admin/vflow/retention/run` | Запустить retention; тело `{"dry_run": true}` опционально. |
| `GET` | `/api/admin/vflow/block-registry` | Типы блоков, версии, схемы, порты, capabilities. |
| `GET` | `/api/admin/vflow/tasks-catalog` | Задачи VALERIA для палитры, включая неопубликованные. |
| `POST` | `/api/admin/vflow/compile` | DSL → IR + диагностика. Тело: `{"source": "..."}`. |
| `POST` | `/api/admin/vflow/format` | IR → канонический DSL. Тело: `{"graph": {...}}`. |
| `POST` | `/api/admin/vflow/preview-transform` | Безопасный предпросмотр Transform. |
| `POST` | `/api/admin/vflow/preview-if` | Безопасный предпросмотр If; ответ `{result, port}`. |

Примечание: исторические имена в технической спецификации (`/api/admin/pipeline-runs...`) отличаются от фактических (`/api/admin/vflow/runs...`). В коде действуют маршруты из этой таблицы.

`POST .../validate` не возвращает `422`: он отдаёт `200` с `{"valid": false, "diagnostics": [...]}`. `422 VFLOW_VALIDATION_ERROR` возникает при `compile` и при `publish`, если черновик не проходит проверку.

### 5.4. Создание Run и идемпотентность

- Заголовок `Idempotency-Key` опционален; максимум 200 символов.
- Ключ уникален в рамках пользователя: `UNIQUE (started_by_user_id, idempotency_key)`.
- Хеш запроса считается по каноническому `{identifier, input}` (sort keys, compact JSON).
- Тот же ключ + тот же запрос → возвращается существующий Run (снова `202`).
- Тот же ключ + другой запрос → `409 IDEMPOTENCY_CONFLICT`.
- Без ключа каждый запрос создаёт новый Run.
- Версия пайплайна не входит в хеш: повтор после публикации новой версии вернёт старый Run, если ключ и вход совпадают. Это ожидаемое поведение идемпотентности.

### 5.5. Черновик, ETag и публикация

- `PUT /api/admin/pipelines/{id}/draft` принимает `source`, `graph`, `input_schema`, `public_output_schema`.
- Если черновика ещё нет, `If-Match` не требуется; создаётся версия.
- Если черновик уже есть, `If-Match` **обязателен** и должен совпасть с текущим ETag; иначе `412 VFLOW_PRECONDITION_FAILED`.
- ETag — строка в кавычках: `"<sha256>"`. Она возвращается в `etag` версии и в ответе сохранения.
- `publish` принимает `If-Match` опционально; если заголовок передан и не совпал — `412`. UI передаёт ETag.
- Публикация возможна только из `draft`; предыдущая опубликованная версия становится `superseded`.
- `rollback` принимает `version_id`; черновик как цель запрещён. Выбранная версия снова становится `published`, текущая — `superseded`.
- `archive` прекращает новые запуски, но не отменяет активные Run.
- `validate` проверяет черновик (если он есть), иначе опубликованную версию. Он проверяет граф и существование задач, но не является полной проверкой JSON Schema или runtime-лимитов.

### 5.6. Конверт ошибок и HTTP-статусы

Доменные ошибки VFlow:

```json
{
  "error": {
    "code": "VFLOW_VALIDATION_ERROR",
    "message": "Не все ветки Fork достигают парного Merge",
    "details": {
      "diagnostics": [
        {
          "code": "COMPILE_FORK_BRANCH_UNJOINED",
          "message": "Branch 0 of Fork 'split' has no route to Merge 'combined'",
          "line": null,
          "column": null,
          "node_id": "split",
          "edge_id": null
        }
      ]
    },
    "request_id": "req-or-uuid"
  }
}
```

`request_id` берётся из заголовка `x-request-id` или генерируется. Для runtime-ошибок `details` обычно `null`, а `code` — конкретный код (`TASK_CHANGED`, `LIMIT_TRANSITIONS`, …).

| Статус | Когда |
|---|---|
| `400 VFLOW_BAD_REQUEST` | Некорректный запрос/состояние: нет source/graph, неверная аудитория, неизвестный статус, нет workspace и т. п. |
| `401` | Нет/истекла сессия (общий механизм VALERIA). |
| `403` | Нет роли, CSRF неверен, `tasks_only` не имеет доступа. |
| `404 VFLOW_NOT_FOUND` | Объект не найден, скрыт аудиторией или принадлежит другому пользователю. |
| `409 VFLOW_CONFLICT` | Конфликт доменного состояния: slug занят, архивный пайплайн редактируют, публикуют не черновик. |
| `409 IDEMPOTENCY_CONFLICT` | Тот же Idempotency-Key с другим телом. |
| `412 VFLOW_PRECONDITION_FAILED` | Несовпадение/отсутствие If-Match. |
| `422 VFLOW_VALIDATION_ERROR` | Ошибки компиляции/валидации с diagnostics. |
| `422 <runtime code>` | Runtime-ошибка создания Run/исполнения (`TASK_UNAVAILABLE`, `LIMIT_JSON_BYTES`, …). |
| `429 VFLOW_RATE_LIMITED` | Rate limit; есть `Retry-After`. |

Ошибки Pydantic/FastAPI при разборе тела запроса могут иметь стандартный формат `{"detail": ...}`. Это не доменная ошибка VFlow.

### 5.7. Health, retention и metrics

`GET /api/admin/vflow/health`:

```json
{
  "status": "ok",
  "generated_at": "2026-11-21T10:00:00+00:00",
  "checks": {
    "database": {
      "status": "ok",
      "missing_tables": [],
      "unreadable_tables": []
    },
    "handlers": {
      "status": "ok",
      "active_runs_checked": 2,
      "incompatible_versions": [],
      "invalid_versions": []
    }
  },
  "counts": {
    "active_runs": 2,
    "active_points": 5,
    "open_forks": 1,
    "expired_leases": 0,
    "outbox_pending": 0,
    "outbox_processing": 0
  }
}
```

- `database.status=error` даёт общий `status=error`; проверка handlers тогда `skipped`.
- Неизвестные handler-версии у активных Run дают `degraded`.
- Health не читает граф, input, prompt-ы или промежуточный JSON.
- Startup-проверка только логирует warning и **не блокирует** запуск приложения.

`GET /api/admin/vflow/metrics`:

```json
{
  "generated_at": "2026-11-21T10:00:00+00:00",
  "runs": {"running": 2, "succeeded": 10, "failed": 1},
  "points": {"ready": 2, "waiting_task": 3, "consumed": 40},
  "outbox": {"sent": 30, "pending": 1},
  "queue": {"queued": 4, "running": 1, "completed": 100},
  "active_points": 5,
  "open_forks": 1,
  "failed_by_code": [{"code": "TASK_CHANGED", "count": 1}]
}
```

Важно: `runs`, `points`, `outbox` — только VFlow. `queue` — **вся** таблица `queue_jobs` VALERIA, не только задачи VFlow. `failed_by_code` — top-20 по количеству.

`GET /api/admin/vflow/retention` возвращает `retention_days`, `cutoff`, `eligible` (сколько строк каждого типа будет удалено), `active_runs`, `total_runs`. `POST .../retention/run` возвращает те же counts; при `dry_run` ничего не удаляет.

### 5.8. Безопасность API

- Все проверки прав выполняются сервером; скрытие кнопок в UI не является защитой.
- Публикация, аудитория, владение Run, workspace и допустимость Task проверяются на сервере.
- `workspace_id` берётся из профиля пользователя, а не из входного JSON.
- Нельзя запустить скрытую/чужую задачу, передав её ID в Run: Run принимает только `input`, а Task ID зафиксированы в опубликованной версии.
- DSL не исполняет Python/JS/SQL/shell; нет `eval`, `exec`, импорта и произвольных функций.
- JSONPath — ограниченное подмножество над данными.
- Вход Run и выходы узлов ограничены по размеру и глубине.
- Пользовательский API не отдаёт raw graph, source, node/task ID, fork stack и промежуточный JSON.
- Публичный результат — только allowlist-проекция.
- Секреты и токены не должны попадать в input, public result, task snapshots, события и audit. Task snapshots намеренно хранят только метаданные.

---

## 6. Хранение данных

### 6.1. Таблицы

Все таблицы VFlow имеют префикс `vflow_`, чтобы не смешиваться с legacy-цепочкой `task_successors` и `queue_jobs.pipeline_json`.

| Таблица | Назначение | Ключи/связи |
|---|---|---|
| `vflow_definitions` | Логические определения | `id` PK, `slug` UNIQUE, `created_by -> users`, `draft_version_id`, `published_version_id`, `audience_json`, `settings_json`. |
| `vflow_versions` | Версии графа | `id` PK, `definition_id -> vflow_definitions ON DELETE CASCADE`, `UNIQUE(definition_id, version_number)`, `status` (`draft`/`published`/`superseded`), `graph_hash`. |
| `vflow_runs` | Запуски | `id` PK, `definition_id`, `version_id`, `started_by_user_id -> users`, `workspace_id`, `status`, `input_json`, `final_result_json`, `task_snapshots_json`, `UNIQUE(started_by_user_id, idempotency_key)`. |
| `vflow_points` | Точки выполнения | `id` PK, `run_id -> vflow_runs ON DELETE CASCADE`, `parent_point_id -> vflow_points`, `json_repair_attempt` (0..бюджет), `UNIQUE(run_id, node_id, visit_seq)`, lease-поля. |
| `vflow_fork_instances` | Активации Fork | `id` PK, `run_id -> vflow_runs ON DELETE CASCADE`, `parent_point_id -> vflow_points`, `UNIQUE(run_id, parent_point_id, parent_visit_seq, fork_node_id)`. |
| `vflow_merge_arrivals` | Прибытия ветвей | `id` PK, `fork_instance_id -> vflow_fork_instances ON DELETE CASCADE`, `point_id -> vflow_points UNIQUE`, `UNIQUE(fork_instance_id, branch_index)`. |
| `vflow_task_links` | Связь точки с Task Run и историей repair | `id` PK, `run_id -> vflow_runs ON DELETE CASCADE`, `point_id -> vflow_points`, `task_run_id` UNIQUE, `enqueue_key` UNIQUE, `repair_attempt`, `parent_task_run_id`, `repair_error_code`, `repair_error_message`. |
| `vflow_events` | Журнал событий | `id` AUTOINCREMENT, `run_id -> vflow_runs ON DELETE CASCADE`, `event_type`, `payload_json`. |
| `vflow_audit_events` | Аудит административных мутаций | `id` AUTOINCREMENT, `actor_user_id -> users`, `action`, `object_type`, `object_id`, `metadata_json`. |
| `vflow_outbox` | Намерения постановки задач | `id` PK, `dedupe_key` UNIQUE, `run_id -> vflow_runs ON DELETE CASCADE`, `point_id -> vflow_points`, `status` (`pending`/`processing`/`sent`/`dead`). |

Схема создаётся/дополняется при инициализации `Database`; `PRAGMA foreign_keys=ON`, `journal_mode=WAL`. Additive-миграции добавляют `vflow_task_links.started_at`, `vflow_points.json_repair_attempt`, поля repair-истории `vflow_task_links` и корреляционные поля repair в `queue_jobs`; повторная инициализация безопасна.

### 6.2. Внешние ключи и каскады

- Удаление Run каскадно удаляет его points, forks, arrivals (через fork), task links, events, outbox.
- `vflow_merge_arrivals` ссылается и на fork, и на point; retention удаляет arrivals первыми, затем связанные дочерние строки.
- `vflow_definitions` и `vflow_versions` retention не удаляет.
- `vflow_audit_events` не привязаны к Run и retention их не удаляет.
- `vflow_task_links.task_id` — логический ID задачи, не FK на `tasks`; Task проверяется gateway-ем.

### 6.3. Жизненный цикл версии

1. `draft` создаётся при первом сохранении; последующие сохранения обновляют тот же черновик.
2. `publish` переводит draft в `published`, старую published — в `superseded`, очищает `draft_version_id`.
3. `rollback` делает выбранную не-черновую версию `published`, текущую — `superseded`.
4. `archive` меняет статус определения на `archived`; опубликованная версия остаётся, но новые Run запрещены.
5. Активный Run всегда ссылается на конкретный `version_id`; новая публикация не меняет его граф.

### 6.4. Retention

Удаляются только terminal Run (`succeeded`, `failed`, `cancelled`), у которых `COALESCE(finished_at, created_at) <= cutoff`.

| Данные | Удаляется |
|---|---|
| Определения и версии | Никогда |
| Активные Run (`created`, `running`, `cancel_requested`) | Никогда |
| `vflow_points`, `vflow_fork_instances`, `vflow_merge_arrivals` | Вместе с terminal Run |
| `vflow_task_links`, `vflow_events`, `vflow_outbox` | Вместе с terminal Run |
| `vflow_audit_events` | Нет |
| `queue_jobs` и результаты дочерних задач | Нет; это история существующей очереди VALERIA |

Retention выполняется одной транзакцией `BEGIN IMMEDIATE`. Счётчики возвращаются ровно по удалённым (или попадающим под удаление при dry-run) строкам. Фоновый цикл делает первый проход сразу при старте и затем раз в 24 часа.

### 6.5. Recovery

Recovery выполняется **один раз при старте приложения** (после старта dispatcher, до запуска scheduler). Он не является периодическим.

Что делает `VFlowRecovery.run_once()`:

1. Просроченные `claimed` leases переводит обратно в `ready`.
2. `waiting_task` без `vflow_task_links` и без pending/processing outbox → Run падает с `RECOVERY_TASK_LINK_MISSING` (консервативно, чтобы не создать дубль задачи).
3. `waiting_task` с link/outbox получает `next_check_at = now`, при необходимости восстанавливает `task_run_id`.
4. Открытые Fork Instance, у которых все arrivals уже есть, но continuation не создан, добиваются CAS-обновлением `open -> merged` и созданием ровно одной продолжающей точки.
5. Открытые forks у terminal Run переводятся в `cancelled`.
6. `running` Run без активных точек и без открытых forks падает с `RUN_STUCK`.

Recovery не перезапускает уже принятые Task и не делает повторный enqueue. Если после падения процесса остался `claimed` с истёкшей арендой, до следующего recovery (рестарта) планировщик его не подхватит: `claim_due` выбирает только `ready` и `waiting_task`.

### 6.6. Outbox

`vflow_outbox` фиксирует намерение `task_enqueue` до вызова gateway. В текущей реализации:

- enqueue и создание `vflow_task_links` происходят в той же транзакции SQLite, что и переход точки;
- при успехе outbox получает `sent`;
- при детерминированном отказе gateway (`VFlowTaskError`) outbox получает `dead` и `last_error`;
- отдельного фонового sender-а, который бы повторял pending/processing, нет;
- `processing` в текущем коде VFlow не выставляется, но health его считает;
- recovery учитывает pending/processing, чтобы не объявить waiting-точку потерянной, но не выполняет повторную постановку;
- retention удаляет outbox вместе с Run.

Поэтому pending outbox — это диагностический сигнал, а не гарантия автоматической доставки. Если он появился, проверьте gateway/очередь и Run вручную.

### 6.7. Аренда точки

- Планировщик выбирает `ready` или `waiting_task` с наступившим `next_check_at`, у которых истекла аренда.
- Аренда: `lease_owner='runtime'`, случайный `lease_token`, `lease_until = now + 30s` (default `claim_due(lease_seconds=30)`).
- Claim выполняется атомарным `UPDATE ... WHERE status IN (...) AND (lease_until IS NULL OR lease_until <= now)`.
- `process_point` проверяет `lease_token` и статус Run; при несовпадении выходит без изменений.
- Успешный переход, ожидание, завершение или ошибка очищают lease-поля.
- Просроченная аренда восстанавливается recovery при рестарте. Health показывает `expired_leases`.

### 6.8. События и аудит

События Run: `RUN_STARTED`, `POINT_ADVANCED`, `TASK_ENQUEUED`, `TASK_RETRY`, `TASK_QUEUE_TIMEOUT`, `TASK_EXECUTION_TIMEOUT`, `FORK_SPAWNED`, `MERGE_ARRIVED`, `MERGE_COMPLETED`, `MERGE_STALE_ARRIVAL`, `MERGE_DUPLICATE_ARRIVAL`, `MERGE_RECOVERED`, `RUN_SUCCEEDED`, `RUN_FAILED`, `RUN_CANCELLED`.

Payload событий содержит структурные данные (ID, порт, branch index, count), но не input Run и не prompt-ы. Админ-детализация точки (`data`) и события доступны только через admin API.

Аудит `vflow_audit_events` пишется при мутациях definition/draft/publish/rollback/audience/archive. Чтение админ-диагностики отдельной audit-записью в текущей реализации не сопровождается.

---

## 7. Task Gateway

### 7.1. Интеграция с очередью

`ValeriaTaskGateway` — единственный путь из VFlow в задачи:

- создаёт обычные `queue_jobs` и `chats`;
- использует существующие лимиты и dispatcher VALERIA;
- не запускает OpenCode и не создаёт отдельную очередь;
- передаёт `actor_user_id` инициатора и его workspace;
- Task Run исполняется в workspace пользователя.

Задача может быть неопубликованной: администратор делегирует её через опубликованную версию пайплайна. Пользователь не получает прямого доступа к задаче или её prompt-у.

### 7.2. Детерминированная постановка

Ключ попытки:

```text
vflow:{run_id}:{point_id}:{visit_seq}:{attempt}
```

Из него считается стабильный `message_id`:

```text
vflow_<sha256(enqueue_seed)[:40]>
```

Свойства:

- повторная обработка той же попытки даёт тот же `message_id`;
- retry или повторный вход в Task (новый `visit_seq`/`attempt`) даёт новый Task Run;
- `create_task_job` при существующем `message_id` возвращает существующий job, а не создаёт дубль;
- VFlow передаёт `allow_duplicate=True`, потому что параллельные ветки Fork могут легально запускать одну и ту же задачу несколько раз;
- `pipeline_json=""` — критически важно, см. 7.7.

### 7.3. Снимок Task

При `create_run` runtime собирает `task_snapshots_json` по всем Task-узлам опубликованного графа:

- `task_id`, `title`, `updated_at`;
- `agent`, `provider_id`, `model_id`, `model_variant`, `queue_id`.

Prompt/content в снимок **не** сохраняются. Если задача не существует/удалена, создание Run завершается `TASK_UNAVAILABLE`.

При enqueue gateway сверяет:

- `task_snapshot.task_id` с текущей задачей;
- `task_snapshot.updated_at` с текущим `updated_at`.

Если задача изменилась после старта Run — детерминированная ошибка `TASK_CHANGED`; VFlow не выполняет более новую версию задачи и не подставляет её молча. Новый Run увидит новую версию.

Дополнительно проверяются:

- пользователь активен (`USER_UNAVAILABLE`);
- у пользователя есть workspace (`WORKSPACE_UNAVAILABLE`);
- `workspace_id` Run совпадает с workspace пользователя (`WORKSPACE_MISMATCH`).

### 7.4. Статусы и результат

Gateway читает существующий `queue_jobs`:

- `completed` → текст из `task_run_results` (для Task Run и repair-job);
- `failed`/`cancelled` → безопасная ошибка/код;
- остальные статусы → ожидание.

Ожидающие состояния: `queued`, `dispatching`, `running`, `waiting_permission`, `waiting_question`.
`cancelling`/`cancelled` → `TASK_CANCELLED`.

Опрос Task — каждые 2 секунды (`TASK_POLL_SECONDS`). Планировщик дополнительно будится внутренним событием `job.updated`, но БД остаётся источником истины.

### 7.5. Таймауты и retry

| Событие | Когда | Поведение |
|---|---|---|
| `queue_timeout_seconds` | Job ещё `queued`, не начал выполняться | Отменяет job. Если `attempt <= retries` и это не repair-job, создаёт новую попытку; иначе Run падает `TASK_QUEUE_TIMEOUT`. |
| `execution_timeout_seconds` | Job уже в `dispatching/running/waiting_permission/waiting_question` | Отменяет job, закрывает link, Run падает `TASK_EXECUTION_TIMEOUT`. Автоматического retry нет. |
| `retries` при ошибке Task | Task завершился неуспешно, код не входит в non-retryable | Новая попытка, новый Task Run. Исчерпание → `TASK_FAILED`. |
| `max_json_repairs` | Завершённый `strict_json` вернул невалидный JSON/схему | Legacy follow-up в тот же чат, пока есть бюджет. Ошибки исполнения/таймауты/отмена не repair-ятся. Исчерпание → `TASK_OUTPUT_NOT_JSON`/`TASK_SCHEMA_INVALID`. |
| `on_json_error: "repair"` | Завершённый `output_mode=json` вернул невалидный JSON (или схему при `invalid_json_or_schema`) | Создаёт один `json_repair_jobs` job, точка `processing_output`; worker JSON Processor выполняет repair. Успех → `output.json`; ошибка → `OUTPUT_INVALID`. |
| `TASK_CHANGED`, `TASK_UNAVAILABLE`, `TASK_CANCELLED` | Явные terminal-коды | Без retry. |

Гонка queue timeout: если dispatcher успел забрать job между опросом и отменой, gateway возвращает `False`; runtime фиксирует `started_at` и не создаёт дубль, после чего действует execution deadline.

Каждая попытка — отдельный `attempt`, отдельный Task Run и отдельный `vflow_task_links`; старый link закрывается как `retried`/`timed_out`.

### 7.6. Отмена

- При queue/execution timeout runtime вызывает `gateway.cancel(task_run_id)`.
- `POST /api/pipeline-runs/{id}/cancel` и админ-отмена переводят VFlow Run в `cancelled`, активные точки и открытые forks — в `cancelled`, закрывают незавершённые `vflow_task_links` как `cancelled`, отменяют ещё `queued` Task/repair-jobs этого Run, включая незавершённые `json_repair_jobs` (`status → cancelled`), и пишут `RUN_CANCELLED`. Уже выполняющаяся внешняя задача может продолжить side effects — для остановки агента используйте существующие средства очереди/админки VALERIA.
- При fail-fast (`_fail`) runtime переводит активные точки/forks в `cancelled`, task links — в `failed`; уже выполняющаяся внешняя задача также может продолжить side effects.

### 7.7. Repair невалидного JSON

Для `result_mode: "strict_json"` runtime проверяет весь финальный текст: `json.loads`, затем (если задана) `json_schema`. Ошибка синтаксиса даёт `TASK_OUTPUT_NOT_JSON`, несоответствие схеме — `TASK_SCHEMA_INVALID`; сообщения содержат безопасный путь (`$.field`, `$.items[0]`), но не значения.

Если repair-бюджет (`max_json_repairs`, по умолчанию 1) ещё не исчерпан, Gateway создаёт follow-up job в **том же** `chat_id`/`session_id`:

- `task_id = NULL` — dispatcher исполняет его как обычное продолжение чата;
- `job_kind = 'vflow_repair'`, `repair_attempt = N`, `parent_task_run_id = <исходный job>`;
- `pipeline_json = ''` — legacy `task_successors` не срабатывает;
- `message_id = msg_vflow_repair_<sha256(run_id:point_id:repair_attempt)[:40]>`;
- сохраняются `vflow_run_id`, `vflow_point_id`, `origin_node_id`, agent/model/queue/workspace исходного job.

Повторный вызов с теми же параметрами возвращает тот же job. Намерение постановки сначала пишется в `vflow_outbox` (`kind='task_json_repair'`); `VFlowRuntime.reconcile_repair_outbox()` завершает прерванную постановку идемпотентно. Ответ follow-up сохраняется в `task_run_results`, поэтому `gateway.status()` возвращает его как `text`, и runtime продолжает обычную проверку. История видна администратору в `point.json_repair_attempt`, `task_correlations`/`task_repairs` и событиях `TASK_JSON_REPAIR_REQUESTED`/`TASK_JSON_REPAIR_COMPLETED`.

#### 7.7.1. JSON Processor (M8–M11)

Полное руководство по настройке, provider-у, безопасности, метрикам, retention и rollout: [VALERIA_JSON_Processor_Operations_RU.md](VALERIA_JSON_Processor_Operations_RU.md). Ниже — только VFlow-часть контракта.

Для `output_mode: "json"` действует отдельный контракт:

- runtime сначала строго парсит весь финальный текст; валидный JSON сразу продвигает точку и job не создаётся;
- при `on_json_error: "repair"` невалидный JSON — или несоответствие `output_schema` при `repair_trigger: "invalid_json_or_schema"` — создаёт `json_repair_jobs` (`source_type='vflow'`, `raw_output_ref = task_run_id`) через `JsonProcessorRepository`; канонический idempotency key включает run/point/task-run, raw hash, schema hash, версию настроек и `prompt_version`;
- точка получает `status = processing_output`, `json_processor_job_id` и `next_check_at`; повторное событие завершения возвращает тот же job, дублей нет;
- job выполняет отдельный `JsonRepairWorker` в lifespan приложения. Provider/LLM не вызывается в транзакции или потоке VFlow scheduler;
- при успехе runtime читает `result_json`, повторно проверяет схему и продвигает точку по `out` с конвертом `input` + `output` + `task.output`;
- при ошибке/отмене job Run падает с `OUTPUT_INVALID`, downstream-узлы не запускаются. Выключенный/ненастроенный процессор даёт `JSON_PROCESSOR_NOT_CONFIGURED`;
- отмена Run отменяет незавершённые `json_repair_jobs` этого Run; worker при старте возвращает просроченные lease в очередь.

`api_key_ref` — только имя переменной окружения (`NAME` или `env:NAME`), не секрет. Ключ читается в память на один provider-вызов и не попадает в ответы, события или логи. Worker запускается в lifespan только при `enabled=true`; после включения/изменения настроек процессора перезапустите приложение, чтобы применить worker.

Task Run при этом остаётся `completed`: процессор не перезапускает задачу и не меняет `task_run_results.text`; VFlow-точка ждёт отдельный job в `processing_output`. Успешный конверт содержит верхнеуровневый `output` и совместимый `task.output` (`text` — исходный финальный текст, `json` — проверенное значение). При ошибке repair Run падает `OUTPUT_INVALID`; `output.json` не подменяется `null`/`{}`.

### 7.8. Legacy `task_successors` — предупреждение

VFlow **не использует** legacy-цепочку `task_successors` и не должен использовать `queue_jobs.pipeline_json`.

- Gateway всегда передаёт `pipeline_json=""`.
- `_enqueue_pipeline_children()` в существующей VALERIA срабатывает только при непустом `pipeline_json`; пустая строка предотвращает автоматическое создание legacy-детей.
- Если записать VFlow-граф в `pipeline_json`, можно получить неконтролируемые дочерние задачи, дубли и путаницу между двумя моделями «pipeline».
- Если у задачи настроены legacy `task_successors`, обычные (не VFlow) запуски этой задачи могут порождать цепочку. Это поведение вне VFlow.
- Не переносите граф VFlow в `task_successors` и не пытайтесь моделировать Fork/Merge через legacy-цепочку.

---

## 8. Эксплуатация

### 8.1. Запуск и остановка

Docker Compose:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f web
docker compose down
```

Systemd:

```bash
./run-local.sh start
./run-local.sh status
./run-local.sh restart
./run-local.sh stop
```

Локально:

```bash
.venv/bin/opencode-web
```

VFlow не имеет отдельного unit/service. Остановка приложения останавливает scheduler, retention и Task Gateway-цикл.

### 8.2. Порядок старта и остановки

При старте приложения:

1. Инициализируется БД и схема.
2. Создаются runtime, scheduler, retention.
3. При включённом флаге снимается health snapshot; при проблеме пишется warning (старт не блокируется).
4. Запускается dispatcher.
5. Выполняется `VFlowRecovery.run_once()`.
6. Запускаются VFlow scheduler и retention scheduler.
7. Retention делает первый проход сразу.

При остановке:

1. Отписывается listener событий.
2. Останавливается retention scheduler.
3. Останавливается VFlow scheduler.
4. Останавливаются notification dispatcher, dispatcher, manager.
5. Закрывается БД.

### 8.3. Healthcheck

Общий health приложения:

```bash
curl -fsS http://127.0.0.1:8081/api/health
```

Он проверяет процесс и dispatcher, но **не** проверяет таблицы, handler-версии и VFlow-состояние. Docker healthcheck использует именно его.

VFlow health — только для администратора:

```http
GET /api/admin/vflow/health
```

Смотрите:

- `status`: `ok` / `degraded` / `error`;
- `checks.database.missing_tables`, `unreadable_tables`;
- `checks.handlers.incompatible_versions`, `invalid_versions`;
- `counts.expired_leases`, `outbox_pending`, `outbox_processing`.

Если `degraded`, приложение продолжает работать. Это сигнал, что активный Run ссылается на handler-версию, которой нет в текущем деплое, или что schema неполная.

### 8.4. Retention job

Фон:

- запускается сразу при старте (если VFlow включён и dispatcher запущен);
- далее раз в 24 часа;
- логирует counts, если что-то удалено;
- не удаляет активные Run, определения и версии.

Ручной dry-run:

```http
POST /api/admin/vflow/retention/run
Content-Type: application/json
x-csrf-token: <csrf-token>

{"dry_run": true}
```

Статистика:

```http
GET /api/admin/vflow/retention
```

Перед первым включением retention на большой БД сделайте backup и сначала выполните dry-run.

### 8.5. Backup и restore

Все VFlow-данные находятся в основной SQLite-БД (`APP_DATABASE_PATH`, по умолчанию `./data/opencode-web.db`). Отдельного хранилища графов нет.

Правильный backup:

1. Остановите приложение/контейнер, чтобы не копировать БД в середине Write-Ahead Log.
2. Сделайте backup через SQLite, а не простым `cp` при работающем сервисе:

```bash
mkdir -p backups
sqlite3 data/opencode-web.db ".backup 'backups/opencode-web-$(date +%F-%H%M).db'"
sqlite3 backups/opencode-web-YYYY-MM-DD-HHMM.db "PRAGMA integrity_check;"
```

3. Для Docker остановите `web`, затем выполните backup на host-пути `data/opencode-web.db`.
4. Запустите приложение и проверьте `/api/health` и `/api/admin/vflow/health`.

Restore:

```bash
docker compose stop web
cp data/opencode-web.db data/opencode-web.db.before-restore
cp backups/opencode-web-YYYY-MM-DD-HHMM.db data/opencode-web.db
rm -f data/opencode-web.db-wal data/opencode-web.db-shm
docker compose start web
```

После restore:

- проверьте `PRAGMA integrity_check` на копии;
- проверьте VFlow health;
- помните, что очереди/дочерние Task Run и внешние side effects не откатываются вместе с БД;
- активные Run могут продолжить с точки, зафиксированной в восстановленной БД, а уже выполненные внешние задачи могут быть повторены. Восстанавливайте БД в режиме обслуживания, когда очередь остановлена.

### 8.6. Включение и выключение feature flag

Включение:

1. Сделайте backup.
2. Установите `VFLOW_ENABLED=true`.
3. Перезапустите приложение.
4. Проверьте `/api/admin/vflow/health`.
5. Создайте тестовый пайплайн, опубликуйте, назначьте аудиторию.
6. Запустите тестовый Run от тестового пользователя.

Выключение:

1. Установите `VFLOW_ENABLED=false`.
2. Перезапустите приложение.
3. Убедитесь, что `/api/pipelines` отвечает `404`, а UI скрывает разделы.
4. Данные сохраняются; активные Run не продвигаются до повторного включения.

### 8.7. Логи и метрики

- Логи приложения — обычный stdout/journald/Docker logs; `LOG_LEVEL` управляет уровнем.
- Scheduler логирует исключения итерации: logger `opencode_web.vflow.scheduler`.
- Retention логирует успешные удаления: logger `opencode_web.vflow.retention`.
- Runtime не пишет prompt-ы и input в лог.
- Для диагностики используйте `vflow_events` через admin API, `/api/admin/vflow/metrics`, `/api/admin/vflow/health` и `/api/admin/vflow/retention`.
- `queue` в metrics — вся очередь VALERIA; не интерпретируйте её как «только VFlow».

### 8.8. Типовые сбои и действия

| Симптом | Вероятная причина | Действие |
|---|---|---|
| `404` на всех VFlow-маршрутах | `VFLOW_ENABLED=false` | Включить флаг и перезапустить. |
| `403 CSRF check failed` | Нет/неверен `x-csrf-token` | Обновить CSRF-токен сессии; UI делает это автоматически. |
| `429 VFLOW_RATE_LIMITED` | Превышен лимит запусков | Подождать `Retry-After` или увеличить `VFLOW_RUNS_PER_MINUTE`. |
| `412 VFLOW_PRECONDITION_FAILED` | Устаревший/отсутствующий `If-Match` | Перечитать определение, взять актуальный `etag`, повторить. |
| `409 IDEMPOTENCY_CONFLICT` | Тот же ключ с другим input | Использовать новый `Idempotency-Key` или тот же input. |
| `422 VFLOW_VALIDATION_ERROR` | Ошибка графа/задачи/схемы | Открыть diagnostics, исправить DSL/граф, проверить задачу. |
| Run `failed`, `TASK_UNAVAILABLE` | Задача удалена/не существует | Восстановить задачу или опубликовать новую версию. |
| Run `failed`, `TASK_CHANGED` | Задача изменена после старта Run | Новый Run возьмёт новую версию; старый не продолжать. |
| Run `failed`, `TASK_INPUT_MISSING` | `input_path` не найден | Исправить путь/вход и перезапустить. |
| Run `failed`, `TASK_QUEUE_TIMEOUT` | Job не стартовал до queue timeout | Проверить очередь/лимиты; увеличить timeout или retries. |
| Run `failed`, `TASK_EXECUTION_TIMEOUT` | Задача выполнялась дольше execution timeout | Проверить задачу/агента; timeout не ретраится автоматически. |
| Run `failed`, `LIMIT_TRANSITIONS` | Цикл/слишком длинный граф | Исправить граф или увеличить лимит осознанно. |
| Run `failed`, `RECOVERY_TASK_LINK_MISSING` | Потеряна связь с Task после сбоя | Не создавать дубль вручную; проверить `vflow_task_links`/очередь. |
| Run `failed`, `RUN_STUCK` | Нет активных точек и открытых forks | Проверить recovery/точки; это аномалия состояния. |
| Health `degraded` | Нет handler-версии активного Run | Вернуть старый handler или завершить Run; не деплоить без совместимости. |
| Health `error`, `SCHEMA_INCOMPATIBLE` | Неполная/старая схема БД | Проверить миграции/таблицы, восстановить backup при необходимости. |
| `expired_leases > 0` | Процесс упал/завис, аренда истекла | Перезапустить приложение, чтобы recovery сбросил leases. |
| `outbox_pending > 0` | Нет фонового sender-а/остался intent | Проверить gateway, Run и очередь; автоматического retry outbox нет. |
| Очередь переполнена | `queue_jobs` queued >= лимит gateway | Разобрать очередь или увеличить лимит осознанно; Run получит `TASK_ENQUEUE_FAILED`. |

### 8.9. Масштабирование и ограничения

- Рассчитано на один процесс приложения с одной SQLite-БД.
- CAS аренды и уникальные индексы защищают от дублей точек/Fork/Merge, но scheduler/retention не координируются между репликами.
- Rate limiter process-local; при нескольких процессах лимит будет неточным.
- SQLite имеет одного писателя; транзакции короткие, ожидание Task не держит транзакцию.
- Fork увеличивает число параллельных агентов; фактический параллелизм ограничен общей очередью VALERIA и лимитами сессий, а не только VFlow.
- Workspace не является полноценной песочницей; несколько агентов в одном workspace могут конфликтовать по файлам.

### 8.10. Безопасность эксплуатации

- Ограничьте доступ к админ-разделу и admin API.
- Защищайте backup БД: в ней есть input Run, промежуточные данные точек, события и task links.
- Не помещайте токены/пароли в input, публичный результат, prompt-ы, события или audit.
- Task snapshots не содержат prompt/content, но админ-диагностика точек может содержать чувствительные промежуточные JSON. Не выдавайте её обычным пользователям.
- Публичный результат включайте только через явную allowlist-схему.
- При смене handler-версий сохраняйте старые обработчики, пока есть активные Run.

---

## 9. Тестирование

### 9.1. Основные команды

Из корня репозитория, при активированном venv:

```bash
# Только VFlow suite
.venv/bin/pytest -q tests/test_vflow_*.py

# Полный suite проекта
.venv/bin/pytest -q

# Линтер
.venv/bin/ruff check src tests

# Синтаксис frontend
node --check src/opencode_web/static/vflow.js
node --check src/opencode_web/static/app.js
```

Если venv не активирован, используйте `.venv/bin/python -m pytest ...`.

Ориентир: полный VFlow suite должен быть полностью зелёным; число тестов может расти. Перед релизом прогоняйте не только VFlow-файлы, но и общий suite, потому что изменения в `app.py`, `database.py`, `dispatcher.py` и событиях затрагивают существующую VALERIA.

### 9.2. Покрытие VFlow

Основные файлы:

- `tests/test_vflow_compiler.py`, `test_vflow_nested.py` — парсер, компилятор, Fork/Merge, вложенность, диагностика.
- `tests/test_vflow_logic.py`, `test_vflow_handlers.py`, `test_vflow_limits.py` — Transform/If, действия, лимиты.
- `tests/test_vflow_repository.py`, `test_vflow_database.py` — версии, публикация, откат, схема.
- `tests/test_vflow_runtime.py`, `test_vflow_task_snapshots.py`, `test_vflow_task_timeouts.py` — Run, точки, snapshots, timeouts/retry.
- `tests/test_vflow_recovery.py`, `test_vflow_scheduler.py` — recovery, leases, wake.
- `tests/test_vflow_api.py`, `test_vflow_runs_api.py`, `test_vflow_input_schema.py`, `test_vflow_public_result.py` — HTTP, права, идемпотентность, схемы.
- `tests/test_vflow_gateway.py` — Task Gateway.
- `tests/test_vflow_retention.py`, `test_vflow_health.py`, `test_vflow_metrics.py` — эксплуатационные проверки.
- `tests/test_vflow_preview_if.py` — предпросмотр If.
- `tests/test_vflow_rate_limit.py` — rate limit.
- `tests/test_vflow_browser.py` — UI.

### 9.3. Playwright

```bash
.venv/bin/pytest -q tests/test_vflow_browser.py
```

Нюансы:

- тест использует `pytest.importorskip("playwright.sync_api")`: без установленного пакета он будет **skipped**, а не failed;
- нужен Chromium для Playwright; при необходимости:

```bash
.venv/bin/python -m playwright install chromium
```

- тест поднимает страницу с **замоканным API** и реальными static-файлами, а не с живым backend. Он проверяет рендеринг пользовательской вкладки, редактора, undo/redo, аудиторию, диагностику и фильтры событий;
- для проверки реального backend используйте API-тесты и отдельный staging-запуск.

### 9.4. Тестовые данные и секреты

- В тестах и документации используйте только вымышленные ID, `example/repo` и placeholder-токены.
- Не коммитьте реальные `.env`, БД, workspace, prompt-ы, пароли и токены.
- Перед запуском тестов, затрагивающих БД, используйте временный путь из фикстур, а не production-базу.

---

## 10. Связанные документы

- [JSON Repair Processor — эксплуатация](VALERIA_JSON_Processor_Operations_RU.md) — настройки, provider, admin API/UI, Task/VFlow-контракт, безопасность, retention, rollout и troubleshooting.
- [Техническая спецификация VFlow](VALERIA_VFlow_Technical_Specification_RU.md) — полная модель, сценарии, требования и архитектурные обоснования.
- [ADR-001…004](VALERIA_VFlow_ADR_RU.md) — принятые решения по границам модуля, Task Gateway, ролям и версиям IR.
- [Implementation Backlog](VALERIA_VFlow_Implementation_Backlog_RU.md) — задачи, приоритеты и критерии готовности.
- [Execution Plan](VALERIA_VFlow_Execution_Plan_RU.md) — порядок выполнения остатка работ и вехи.
- [`.env.example`](../.env.example) — шаблон конфигурации, включая все `VFLOW_*`.

Ключевые исходники:

- `src/opencode_web/vflow/api.py` — HTTP-маршруты и конверт ошибок.
- `src/opencode_web/vflow/runtime.py` — Run, points, Fork/Merge, leases, Task transitions.
- `src/opencode_web/vflow/parser.py`, `compiler.py`, `formatter.py` — DSL и IR.
- `src/opencode_web/vflow/handlers.py`, `logic.py` — семантика блоков.
- `src/opencode_web/vflow/gateway.py` — Task Gateway.
- `src/opencode_web/vflow/recovery.py`, `scheduler.py` — фоновые циклы.
- `src/opencode_web/vflow/retention.py`, `health.py`, `metrics.py` — эксплуатация.
- `src/opencode_web/database.py` — схема `vflow_*` и транзакции.
- `src/opencode_web/static/vflow.js`, `vflow.css` — UI.
