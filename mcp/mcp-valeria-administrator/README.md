# Valeria Administrator MCP

Отдельный MCP-сервер для административного HTTP API Valeria. Предоставляет
53 инструмента: создание пользователей и тасок, настройку доступа, очередей,
лимитов, шаблонов и консультаций, просмотр запусков, результатов, статистики и аудита.

Учётная запись администратора задаётся в конфигурации MCP. При входе сервер
проверяет роль `admin`; операции выполняются через существующий API Valeria.
Транспорт MCP — `stdio`, HTTP(S) используется для соединения с Valeria.

## Установка и учётная запись

```bash
cd mcp/mcp-valeria-administrator
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env
chmod 600 config.env
```

Заполните `config.env`:

```dotenv
VALERIA_ADMIN_URL=http://127.0.0.1:8081
VALERIA_ADMIN_LOGIN=admin
VALERIA_ADMIN_PASSWORD_FILE=/absolute/path/to/valeria-admin-password
VALERIA_ADMIN_REQUEST_TIMEOUT_SECONDS=30
```

В файле пароля должен находиться пароль этой учётной записи. Можно использовать
`VALERIA_ADMIN_PASSWORD` вместо файла; при наличии обоих вариантов файл имеет
приоритет. `VALERIA_ADMIN_CA_BUNDLE` задаёт CA bundle для HTTPS с собственным
центром сертификации. URL может включать префикс reverse proxy.

По умолчанию читается `config.env` рядом с `server.py`. Переменные окружения процесса
имеют приоритет. Для другого файла передайте `--env-file /absolute/path/admin.env`.
Префикс `VALERIA_ADMIN_` позволяет независимо настроить этот MCP и пользовательский
[Valeria Tasks MCP](../mcp-valeria-tasks/README.md).

## Подключение

Для клиентов с форматом `mcpServers`:

```json
{
  "mcpServers": {
    "valeria-administrator": {
      "command": "/absolute/path/shodan/mcp/mcp-valeria-administrator/.venv/bin/python",
      "args": ["/absolute/path/shodan/mcp/mcp-valeria-administrator/server.py"]
    }
  }
}
```

Для [OpenCode](https://opencode.ai/docs/mcp-servers/):

```json
{
  "mcp": {
    "valeria-administrator": {
      "type": "local",
      "command": [
        "/absolute/path/shodan/mcp/mcp-valeria-administrator/.venv/bin/python",
        "/absolute/path/shodan/mcp/mcp-valeria-administrator/server.py"
      ],
      "enabled": true
    }
  }
}
```

## Инструменты

Все имена начинаются с `valeria_admin_`. Полные входные JSON Schema публикуются
через MCP `tools/list`: параметры передаются непосредственно аргументами
инструмента. Указанные ниже имена приведены без общего префикса.

| Группа | Инструменты |
| --- | --- |
| Пользователи | `list_users`, `create_user`, `update_user`, `reset_user_password` |
| Доступ пользователя | `list_user_tasks`, `set_user_task_access` |
| История пользователя | `list_user_chats`, `sync_user_chat_titles`, `list_user_task_runs`, `sync_user_task_run_titles` |
| Определения тасок | `list_tasks`, `create_task`, `update_task`, `set_task_audience`, `delete_task` |
| Запуски | `list_task_runs`, `sync_task_run_titles`, `get_job`, `get_task_run_messages`, `get_task_result`, `create_task_run` |
| Очереди и остановка | `get_queue`, `list_queues`, `create_queue`, `update_queue`, `archive_queue`, `set_default_chat_queue`, `stop_job`, `cancel_queued_job` |
| Общий лимит | `set_concurrency`, `reset_concurrency` |
| Шаблоны | `list_templates`, `create_template`, `update_template`, `delete_template` |
| Консультации | `get_consultation_settings`, `set_consultation_settings`, `list_consultations`, `get_consultation` |
| Обычные чаты | `get_chat`, `get_chat_messages`, `get_chat_questions`, `get_chat_permissions`, `get_chat_mcp`, `reply_chat_question`, `respond_chat_permission`, `abort_chat`, `send_chat_message` |
| Наблюдение и настройки | `get_options`, `get_public_config`, `get_mcp_status`, `get_statistics`, `get_audit` |

Список тасок администратора включает черновики, промпты, аудиторию, параметры
модели, очередь и связи цепочек. Для деталей любого запуска используйте `get_job`
с ID запуска: этот API разрешает администратору читать задания всех пользователей.
`get_task_result` возвращает итоговый ответ, `get_task_run_messages` — историю запуска.
Списки запусков и обзор очереди ограничены текущим API, до 500 записей.

Списки возвращаются под именованным ключом: `users`, `tasks`, `runs`, `queues`,
`templates`, `messages`, `chats`, `consultations`, `workspaces` или `events`.
Ответы-объекты сохраняют структуру API. Операции с HTTP `204` возвращают `{"ok": true}`.

## Примеры

Создание пользователя с рабочей папкой и доступом к выбранным таскам:

```text
valeria_admin_create_user(
  login="operator",
  password="<начальный пароль длиной от 12 символов>",
  workspace_path="team/project",
  tasks_only=true
)
valeria_admin_set_user_task_access(user_id="<ID пользователя>", task_ids=["<ID таски>"])
```

`workspace_path` проверяется и создаётся существующим API в разрешённых каталогах.
Для относительного пути на сервере должен быть настроен один корневой каталог.
Для отключения учётной записи используйте
`valeria_admin_update_user(user_id="...", status="disabled")`.
Сброс пароля отзывает пользовательские сессии. При сбросе пароля настроенного
администратора обновите пароль и в конфигурации MCP.

Создание таски:

```text
valeria_admin_get_options()
valeria_admin_create_task(
  title="Проверка сборки",
  content="Проверь сборку проекта и подготовь итоговый отчёт.",
  agent="build",
  model="<provider/model из get_options>",
  is_published=true,
  visibility="selected",
  user_ids=["<ID пользователя>"],
  queue_id="default"
)
```

У таски также доступны `description`, `model_variant`, `plan_first`, `next_task_ids`.
`plan_first=true` используется с агентом `build`. Связи цепочки и циклы проверяет API.
`create_task_run` запускает опубликованную доступную таску для настроенного
администратора, которому нужна собственная рабочая папка. Запуски пользователей
видны через `list_task_runs` и `list_user_task_runs`.

Очереди, лимиты и статистика:

```text
valeria_admin_get_queue()
valeria_admin_create_queue(name="CI", priority=10, max_workers=2)
valeria_admin_set_concurrency(value=3)
valeria_admin_reset_concurrency()
valeria_admin_get_statistics(day="2026-11-01", timezone="Europe/Belgrade")
valeria_admin_stop_job(job_id="<ID задания>")
```

При изменении очереди `update_queue` требует все поля: `queue_id`, `name`, `priority`,
`max_workers`. `archive_queue` архивирует очередь; используемые и системные очереди
защищены проверками API. `get_queue` показывает текущий лимит, лимит из окружения,
жёсткий предел, активные задания, очередь ожидания и маршрутизацию обычных чатов.

## Частичные изменения и повторы

Инструменты `update_*` отправляют только явно переданные поля:

- Пропущенные поля сохраняются.
- `description=null` очищает описание таски или шаблона.
- `max_active_jobs=null` в `update_user` сбрасывает индивидуальный лимит.
- `mattermost_username=null` отключает Mattermost-уведомления.
- `next_task_ids=[]` очищает следующие шаги цепочки; пропуск сохраняет их.
- `set_user_task_access` заменяет выбранные назначения тасок; общедоступные таски
  сохраняют доступ по правилам API.

`send_chat_message` отправляет сообщение в существующий обычный чат от имени его
владельца с аудитом действия администратора. Для него и `create_task_run` обязателен
`idempotency_key`: непустой печатный ASCII-ключ до 200 символов. После сетевой ошибки
повторяйте те же аргументы с тем же ключом; для нового запроса используйте новый ключ.
Повтор может вернуть исходный статус приёма — актуальное состояние читайте через
`get_job`. Для отправки сообщения используются `chat_id`, `message`,
`idempotency_key` и необязательные `agent`, `model`, `model_variant`.

Создание пользователей, тасок, шаблонов и очередей не поддерживает идемпотентность
в существующем API. После таймаута или ответа `5xx` проверьте соответствующий список
перед повтором. MCP не повторяет сетевые ошибки и `5xx` автоматически. После `401`
один раз обновляется авторизация и повторяется исходный запрос.

Ошибки возвращаются как MCP `isError` с объектом `error`, сообщением и HTTP-кодом,
если он доступен. Ошибки аргументов показывают имена полей и типы нарушений.
Пароли, cookie и тела ошибок HTTP в эти сообщения не включаются.

## Проверка

Из корня репозитория:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]' -r mcp/mcp-valeria-administrator/requirements-dev.txt
.venv/bin/pytest -q mcp/mcp-valeria-administrator/tests
.venv/bin/ruff check mcp/mcp-valeria-administrator
```

Тесты проверяют роль администратора, авторизацию и повторы, схемы и маршруты всех
операций, работу с настоящим API/SQLite через ASGI, протокол MCP и отдельный stdio-процесс.
