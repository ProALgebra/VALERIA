# Valeria Tasks MCP

MCP-сервер для запуска доступных пользователю тасок Valeria и отслеживания их
состояния через существующий HTTP API. Адрес Valeria и учётная запись задаются
в конфигурации MCP. Все инструменты одного процесса работают от имени этой записи.

Транспорт MCP — `stdio`: сервер запускается на стороне MCP-клиента и обращается
к Valeria по HTTP(S). Используется [официальный Python SDK MCP 1.x](https://py.sdk.modelcontextprotocol.io/v1/),
как у остальных MCP-серверов проекта; зависимость ограничена `<2`.

## Настройка пользователя

В Valeria выберите или создайте пользователя, назначьте ему рабочую папку и доступ
к нужным опубликованным таскам. Подходит тип «Таски и консультации».

В `VALERIA_LOGIN` задаётся именно этот пользователь. Его логин, пароль и рабочая
папка не передаются аргументами инструментов. API применяет права пользователя,
настроенные очереди и лимиты задач.

## Установка

```bash
cd mcp/mcp-valeria-tasks
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env
chmod 600 config.env
```

Заполните `config.env`:

```dotenv
VALERIA_URL=http://127.0.0.1:8081
VALERIA_LOGIN=mcp-user
VALERIA_PASSWORD_FILE=/absolute/path/to/valeria-password
VALERIA_REQUEST_TIMEOUT_SECONDS=30
```

В файле `VALERIA_PASSWORD_FILE` должен находиться пароль этой учётной записи.
Вместо файла можно задать `VALERIA_PASSWORD`. Если заданы оба варианта, используется
файл. Для HTTPS с собственным центром сертификации задайте `VALERIA_CA_BUNDLE`.
`VALERIA_URL` может включать префикс reverse proxy, например `https://valeria.example/app`.

По умолчанию сервер читает `config.env` рядом с `server.py`; переменные окружения
процесса имеют приоритет. Другой файл можно выбрать через `--env-file`.

## Подключение MCP-клиента

Для клиента с конфигурацией `mcpServers`:

```json
{
  "mcpServers": {
    "valeria-tasks": {
      "command": "/absolute/path/shodan/mcp/mcp-valeria-tasks/.venv/bin/python",
      "args": ["/absolute/path/shodan/mcp/mcp-valeria-tasks/server.py"]
    }
  }
}
```

Конфигурация в формате [OpenCode](https://opencode.ai/docs/mcp-servers/):

```json
{
  "mcp": {
    "valeria-tasks": {
      "type": "local",
      "command": [
        "/absolute/path/shodan/mcp/mcp-valeria-tasks/.venv/bin/python",
        "/absolute/path/shodan/mcp/mcp-valeria-tasks/server.py"
      ],
      "enabled": true
    }
  }
}
```

Адрес и пользователя также можно передать через окружение MCP-процесса. Секреты
хранятся в локальной конфигурации или файле пароля; `config.env` исключён из Git.

## Инструменты

| Инструмент | Назначение | HTTP API |
| --- | --- | --- |
| `valeria_list_tasks()` | Доступные таски: `tasks` с ID, названием, описанием | `GET /api/tasks` |
| `valeria_create_task_run(task_id, idempotency_key, user_input="")` | Создать запуск, вернуть его `id` и исходный статус | `POST /api/tasks/{task_id}/runs` |
| `valeria_list_task_runs()` | Собственные запуски: `runs`, до 500 последних | `GET /api/task-runs` |
| `valeria_get_task_run(run_id)` | Текущий статус конкретного запуска | `GET /api/task-runs/{run_id}` |
| `valeria_get_task_result(run_id)` | Итоговый ответ запуска | `GET /api/task-runs/{run_id}/result` |

Пример последовательности вызовов:

```text
valeria_list_tasks()
valeria_create_task_run(
  task_id="<id из списка>",
  idempotency_key="ci-check-550e8400-e29b-41d4-a716-446655440000",
  user_input="Проверь сборку ветки feature/example"
)
valeria_get_task_run(run_id="<id из ответа запуска>")
valeria_get_task_result(run_id="<тот же id>")
```

Статусы: `queued`, `dispatching`, `running`, `cancelling`, `completed`, `failed`,
`cancelled`. Последние три — конечные. Поле `phase` показывает `plan` или `build`.
Для наблюдения повторяйте `valeria_get_task_run` с интервалом в несколько секунд.
Ориентируйтесь на возвращённый статус: API может сразу принять запуск как
`cancelled`, например при срабатывании защиты от одинаковых запросов.

Результат содержит `run_id`, `status`, `text`:

- `available` — итоговое сообщение в `text`;
- `not_completed` — запуск ещё не завершён успешно;
- `unavailable` — итогового сообщения нет.

## Повторы и ошибки

`idempotency_key` обязателен: непустой печатный ASCII-ключ длиной до 200 символов.
Для нового запуска используйте новый ключ. После сетевой ошибки повторяйте запрос
с тем же ключом и теми же аргументами: Valeria могла принять его до обрыва связи.
Клиент не повторяет сетевые ошибки автоматически. После истечения сессии он один
раз повторяет вход и запрос, сохраняя ключ.

Повтор с тем же ключом возвращает исходный ответ о приёме запуска. Для актуального
статуса используйте `valeria_get_task_run`. Повтор ключа с другими аргументами даёт
`409`. Для повторного запуска уже завершённой или неудачной таски нужен новый ключ.

Ошибки HTTP передаются как ошибки инструментов с кодом ответа. В частности, `404`
означает отсутствие доступной таски или собственного запуска, `422` требует проверки
параметров и рабочей папки пользователя, `429` означает заполненную очередь.
При `503` чтение результата можно повторить. Пароли, cookie и тела ошибок API
в сообщения ошибок MCP не включаются.

## Проверка

Из корня репозитория в тестовом окружении:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]' -r mcp/mcp-valeria-tasks/requirements-dev.txt
.venv/bin/pytest -q mcp/mcp-valeria-tasks/tests
.venv/bin/ruff check mcp/mcp-valeria-tasks
```

Тесты проверяют конфигурацию, cookie/CSRF, повторы, права пользователя на настоящем
API через ASGI, инструменты MCP и запуск отдельного процесса по `stdio`.
