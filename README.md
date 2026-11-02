# V.A.L.E.R.I.A.

**Virtual Agentic Language-model Execution, Routing & Interface Administrator**

[English](#english) | [Русский](#русский)

## English

Administrative interface for running and routing LLM agents.
V.A.L.E.R.I.A. provides a multi-user web interface for OpenCode with assigned
workspaces, a shared job queue, Build/Plan modes, templates, tasks, Markdown
responses, and an administration panel.

### Requirements

- Python 3.12+;
- `/usr/bin/opencode` installed;
- access to an LLM provider configured in OpenCode.

### Local setup

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

Before starting, configure at least:

- `SESSION_SECRET` — a random string of at least 32 characters;
- `OPENCODE_SERVER_PASSWORD`;
- `ALLOWED_WORKSPACE_ROOTS` — defaults to `./data/workspaces` for local setup;
- `BOOTSTRAP_ADMIN_PASSWORD_FILE`.

The bootstrap administrator is created only on the first startup with an empty
database. Its password is read from a protected file and stored only as an
Argon2id hash.

Open `http://127.0.0.1:8081`. Additional accounts, workspaces, templates, and the
active session limit are configured in the administration panel.

The initial administrator has no workspace assigned. In “Profile” (“Профиль”),
enter a path in “Folder path” (“Путь к папке”) and click “Save folder”
(“Сохранить папку”). For example, `admin/project` creates a directory inside
`ALLOWED_WORKSPACE_ROOTS` when a single root directory is configured. You can also
specify an absolute path within the allowed directories. The workspace takes
effect immediately without signing in again; it can also be changed in your
account entry under “Users” (“Пользователи”). The workspace cannot be changed
while jobs are queued or running.

A [systemd setup with OpenCode/MCP/LSP process cleanup](docs/server-process-lifecycle.md)
is available for the `/opt/valeria` deployment. Install it before using the updated
`run-local.sh`; migration and rollback instructions are provided in that document.

### Workspace isolation

The administrator can switch the whole installation from the default `direct`
mode to rootless Podman workers, each with its assigned workspace and private
OpenCode state. Provision the non-root service account, worker image and provider
configuration using [the Linux isolation guide](docs/opencode-isolation.md).
Switching requires an idle queue and saves history before archiving conversations
from the previous mode. Returning to a mode restores its conversations; task
history and saved results remain available throughout.

### Tasks

In “Task settings” (“Настройка тасок”), the administrator creates a task with a
user-facing title and description, a hidden prompt, an OpenCode mode, a specific
model from the same catalog available in chat, and an optional model variant.
Saving is disabled while the model catalog is loading or unavailable; the catalog
can be reloaded without losing entered data. If a previously selected model is no
longer available, select an available model before saving. Published tasks appear
under “Tasks” (“Таски”); drafts are visible only to administrators. Editing or
deleting a task definition does not change existing runs or remove their history.

Users select a task, optionally add their own text, and run it in their assigned
workspace. Their text is appended after the administrator's prompt. Users see only
the run title, timestamps, and status: queued, preparing the environment, running,
completed, or failed. The prompt, messages, reasoning, tools, and technical run
errors are not exposed through the user interface or API.

A run initially uses the selected task's title. When OpenCode assigns a session
title, it is appended after a colon: “Task title: OpenCode session title”. The task
title is saved when the run starts, so later edits to the definition do not rename
historical runs. Titles update in the background without reloading the form or
losing the user's additional text.

Creating a dialog, consultation, or manual task run saves the request to SQLite
without waiting for OpenCode. The dispatcher starts the environment and creates a
session when the job receives a slot in the shared queue. Until then, the history
shows the saved request and preparation status; an empty dialog does not start an
environment. Startup failures are saved with the job, after which users can submit
the request again. Accepted jobs survive application restarts. The browser uses
`Idempotency-Key`: retrying an HTTP request after a network failure with the same
key returns the original result without creating a duplicate, while a different
request with that key receives `409`.

Each run uses a single regular OpenCode session with one user message. Tasks share
the FIFO queue and active session limit with chats. OpenCode questions are answered
automatically using the recommended option, or the first available option if none
is marked as recommended. Users do not need to continue the interaction manually.

If a question has no answer options, or the current OpenCode policy requires manual
approval of an action, the task fails. Automatic answers do not change the
configured permission policy.

The “Users” section offers a “Tasks and consultations” account type (`tasks_only`).
These users can run published tasks, view their run statuses, and use consultations;
regular chats, templates, and their APIs are unavailable to them. Consultations are
available independently of whether regular dialogs are enabled. This mode can also
be enabled or disabled for existing users. Administrators see all task runs and
their positions in the shared queue.

To run tasks from MCP clients, use [Valeria Tasks MCP](mcp/mcp-valeria-tasks/README.md).
Its configuration specifies the Valeria URL and the account used to create runs.
The tools show available tasks, run statuses, and final answers.

[Valeria Administrator MCP](mcp/mcp-valeria-administrator/README.md) provides
administration from an MCP client: users, tasks and their audiences, queues, limits,
templates, consultations, runs, statistics, and audit records. Configure a Valeria
account with the `admin` role.

[Valeria Telegram Bot](integrations/telegram-valeria-bot/README.md) provides access
from Telegram. After signing in with their own account, users can select tasks,
consultations, or dialogs, receive results, and answer clarification questions.
The bot runs as a separate process; setup instructions and an example configuration
are available in the integration directory.

### Statistics

The “Statistics” tab is available to administrators and shows successful task runs
across all users: counts for the selected day and all time, plus two daily averages —
from execution start to completion and from queue entry to completion. Today is
selected by default; each day is determined by the browser's time zone using the
task's completion timestamp. Data refreshes on task events and through the
“Refresh” button.

Failures, cancellations, dialogs, and consultations are excluded. Repeated runs and
pipeline stages are counted separately. Tasks without valid timestamps are excluded
from the corresponding average; an empty sample displays “No data”. Counts include
the full saved history, including more than 500 runs.

API: `GET /api/admin/statistics?day=2026-10-04&timezone=UTC`.
Both parameters are optional and default to the current day and `UTC`.

### Running with Docker

The stack runs the OpenCode Web container together with OpenCode CLI, Conan, and local
MCP servers. The service connects to the `opencode` bridge network with outbound
internet access enabled. OpenCode and MCP configuration is included in the image,
while all mutable state is stored inside the project directory.

Install Docker Engine and Compose on Ubuntu:

```bash
sudo apt update
sudo apt install docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

After copying the entire directory, including hidden files, run:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f web
```

Copy the entire directory, including the hidden `.env` and `.secrets` files and directories and the shared `data` directory. `data` stores SQLite, user workspaces, OpenCode state and cache, the Memory MCP database, and Conan. `data/workspaces` is mounted inside the container at `/home/appuser/workspaces`, keeping saved user paths inside the container stable.

Example of copying to another server with all hidden data:

```bash
rsync -a --exclude='.git/' --exclude='.venv/' './' user@server:/opt/opencode-web/
ssh user@server 'cd /opt/opencode-web && docker compose up --build -d'
```

For Git operations over SSH, place the key and `known_hosts` in `.secrets/ssh`. The private SSH key is not copied automatically.

#### Automatic GPG commit signing in Docker

The container supports OpenPGP/GPG signing of all Git commits and tags. On a new server, first create a dedicated key with an email address verified in GitLab:

```bash
sudo apt install gnupg
gpg --full-generate-key
gpg --list-secret-keys --keyid-format LONG
```

Add the output of `gpg --armor --export <KEY_ID>` to GitLab under “Profile → Access → GPG keys”. Then prepare the private key and passphrase for the container:

```bash
mkdir -p .secrets/gpg
chmod 700 .secrets/gpg
gpg --armor --export-secret-keys <KEY_ID> > .secrets/gpg/private.asc
read -r -s -p 'GPG passphrase: ' GPG_KEY_PASSPHRASE
printf '%s' "$GPG_KEY_PASSPHRASE" > .secrets/gpg/passphrase
unset GPG_KEY_PASSPHRASE
chmod 600 .secrets/gpg/private.asc .secrets/gpg/passphrase
```

Set the full key fingerprint in `.env` and enable signing:

```dotenv
GIT_USER_NAME=Aleksey Khamitsevich
GIT_USER_EMAIL=verified-email@example.com
GIT_GPG_SIGNING_ENABLED=true
GIT_GPG_KEY_ID=FULL_40_CHARACTER_FINGERPRINT
GIT_GPG_PRIVATE_KEY_FILE=/app/.secrets/gpg/private.asc
GIT_GPG_PASSPHRASE_FILE=/app/.secrets/gpg/passphrase
```

After `docker compose up --build -d`, the entrypoint imports the key into the persistent `data/gnupg` directory and enables `commit.gpgSign` and `tag.gpgSign`. If the key, fingerprint, or passphrase file is unavailable, the container exits with an explicit error instead of starting the application with unsigned commits.

The passphrase file is stored as plain text on the server. Restrict access to `.secrets`, use a dedicated signing-only key, and keep `.secrets` out of Git and the Docker image. OpenCode processes run in the same container and technically have access to mounted secrets; strict isolation requires moving signing to a separate service.

When deploying from `git clone`, create `.env` from `.env.example`, generate a new `SESSION_SECRET`, create `.secrets/admin-password`, and fill in the tokens. Databases, dialogs, and workspaces are not tracked in Git.

By default, the site remains accessible only locally at `http://127.0.0.1:8081`. Inside the container, the application listens on `0.0.0.0`, while the published address is still determined by `APP_HOST`.

To stop:

```bash
docker compose down
```

The `data` and `.secrets` directories are excluded from Docker images and Git. Removing containers does not delete this data. YouTrack, GitLab, and LLM tokens are stored only in `.env`; `opencode/config/opencode.jsonc` contains only references to environment variables.

Mutable data layout:

```text
data/
├── opencode-web.db
├── workspaces/
├── opencode/{share,state,cache}/
├── mcp/memory/
├── conan/
└── gnupg/
```

### Development

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest
```

Project documentation:

- [technical specification](docs/technical-specification.md);
- [consultation interface and its configuration](docs/consultations.md);
- [design prompts](docs/design-prompts.md).

### Licensing

V.A.L.E.R.I.A. — Virtual Agentic Language-model Execution, Routing & Interface
Administrator — is source-available under a three-part licensing model.

- The [V.A.L.E.R.I.A. Community License 1.0](LICENSE.md) permits personal,
  educational, research, and unlimited internal organizational use, including
  self-hosting, private modifications, and commercial work performed with the
  platform.
- A separate [Commercial License](legal/LICENSE_COMMERCIAL.md) is required for
  hosted SaaS, provider-controlled managed hosting, external customer access,
  commercial redistribution, OEM embedding, and white-label offerings.
- The [Enterprise License](legal/LICENSE_ENTERPRISE.md) governs only separately
  identified proprietary Enterprise Components and negotiated services. It does
  not reduce Community rights.

Revenue from code, reports, consulting, or other independent work product made
with V.A.L.E.R.I.A. does not by itself require a paid license. Organizations of
any size may use Community Components internally without limits based on company
size, revenue, employees, agents, tasks, tokens, internal users, or servers.

See the [license FAQ](legal/LICENSE_FAQ.md), [commercial overview](legal/COMMERCIAL.md),
[Russian summary](legal/LICENSING_SUMMARY_RU.md), and [third-party notices](NOTICE.md).
Commercial licensing contact: **[COMMERCIAL LICENSING CONTACT TO BE ADDED]**.

The Community License is a custom source-available license, not an OSI-approved
open-source license. Third-party materials retain their own licenses. The legal
documents are drafts requiring professional review before publication or use in
a transaction.

### Current MVP limitations

- The backend starts a separate `opencode serve` process for each user workspace that is needed.
- OpenCode processes listen only on loopback and are not directly accessible from the browser.
- SQLite stores users, dialog associations, templates, task definitions, the queue with run history, and the audit log.
- OpenCode stores dialog content and performs the agent's work.
- Consultations use the shared queue and Build mode; SQLite stores only questions and final answers for display, and the administrator configures the model.
- The owner of a regular dialog and the administrator can see the full OpenCode runtime payload and reasoning; user access to a task is limited to its run status.
- `cwd` and the permission policy do not provide a complete filesystem sandbox. Strict user isolation requires containers or separate system accounts.

---

## Русский

Административный интерфейс для выполнения и маршрутизации LLM-агентов.
V.A.L.E.R.I.A. предоставляет многопользовательский веб-интерфейс к OpenCode с
назначаемыми рабочими папками, общей очередью заданий, режимами Build/Plan,
шаблонами, задачами, Markdown-ответами и административной панелью.

### Требования

- Python 3.12+;
- установленный `/usr/bin/opencode`;
- доступ к настроенному в OpenCode LLM-провайдеру.

### Локальный запуск

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

Перед запуском измените как минимум:

- `SESSION_SECRET` — случайная строка не короче 32 символов;
- `OPENCODE_SERVER_PASSWORD`;
- `ALLOWED_WORKSPACE_ROOTS` — для локального запуска по умолчанию `./data/workspaces`;
- `BOOTSTRAP_ADMIN_PASSWORD_FILE`.

Bootstrap-администратор создаётся только при первом запуске с пустой базой. Его пароль читается из защищённого файла и сохраняется только как Argon2id-хеш.

Откройте `http://127.0.0.1:8081`. Дальнейшие учётные записи, рабочие папки, шаблоны и лимит активных сессий настраиваются в админке.

Первому администратору рабочая папка изначально не назначена. В разделе «Профиль» укажите её в поле «Путь к папке» и нажмите «Сохранить папку». Например, `admin/project` создаст папку внутри `ALLOWED_WORKSPACE_ROOTS`, если задан один корневой каталог. Можно указать и абсолютный путь внутри разрешённых каталогов. Папка применяется сразу, без повторного входа; её также можно изменить в своей карточке в разделе «Пользователи». Смена папки недоступна, пока есть задания в очереди или в работе.

Для сервера `/opt/valeria` подготовлен [запуск под systemd с очисткой процессов
OpenCode/MCP/LSP](docs/server-process-lifecycle.md). Его нужно установить перед
использованием обновлённого `run-local.sh`; порядок переноса и отката описан
в документе.

### Изоляция рабочих папок

Администратор может переключить всю установку из режима `direct` по умолчанию
на rootless Podman: отдельное окружение с назначенной рабочей папкой и собственным
состоянием OpenCode. Подготовка непривилегированной учётной записи сервиса, образа
и провайдера описана в [инструкции для Linux](docs/opencode-isolation.md).
Переключение требует отсутствия активных заданий и сохраняет историю перед
архивацией диалогов предыдущего режима. Возврат к режиму восстанавливает его
диалоги; история тасок и сохранённые результаты доступны в обоих режимах.

### Таски

В разделе «Настройка тасок» администратор создаёт таску: задаёт название и описание для пользователей, скрытый промпт, режим OpenCode, выбирает конкретную модель из того же списка доступных моделей, что и в чате, и, при необходимости, указывает вариант модели. Пока список моделей загружается или недоступен, сохранение заблокировано; список можно загрузить повторно без потери введённых данных. Если ранее выбранная модель больше недоступна, для сохранения нужно выбрать доступную модель. Опубликованные таски появляются в разделе «Таски»; черновики видны только администратору. Изменение или удаление определения не меняет параметры уже созданных запусков и сохраняет их историю.

Пользователь выбирает таску, при необходимости добавляет свой текст и запускает её в назначенной рабочей папке. Дополнение добавляется после промпта администратора. Пользователь видит только название, время и статус запуска: «В очереди», «Подготовка окружения», «В работе», «Завершена» или «Завершена с ошибкой». Промпт, сообщения, reasoning, инструменты и технические ошибки запуска через пользовательский интерфейс и API не выдаются.

Сначала запуск называется так же, как выбранная таска. Когда OpenCode присвоит сессии название, оно добавится после двоеточия: «Название таски: Название сессии OpenCode». Название таски сохраняется на момент запуска, поэтому последующее редактирование определения не переименовывает историю. Названия обновляются в фоне без перезагрузки формы и потери введённого дополнения.

Создание диалога, консультации и ручной запуск таски сохраняются в SQLite без ожидания OpenCode. Диспетчер запускает окружение и создаёт сессию, когда задание получает слот в общей очереди. До этого история показывает сохранённый запрос и статус подготовки; пустой диалог не запускает окружение. Ошибка запуска сохраняется у задания, после чего пользователь может отправить запрос повторно. Принятые задания переживают перезапуск приложения. Браузер использует `Idempotency-Key`: повтор HTTP-запроса после сетевого сбоя с тем же ключом возвращает первоначальный результат без дубля, а другой запрос с этим ключом получает `409`.

Каждый запуск выполняется как одна обычная сессия OpenCode с одним пользовательским сообщением. Таски используют общую FIFO-очередь и общий лимит активных сессий вместе с чатами. На вопросы OpenCode автоматически выбирается рекомендуемый вариант; если он не обозначен, выбирается первый предложенный вариант. Интерактивное продолжение со стороны пользователя не требуется.

Если вопрос не содержит вариантов ответа или действующая политика OpenCode требует ручного разрешения действия, таска завершается с ошибкой. Автоответы на вопросы не меняют настроенную политику разрешений.

В разделе «Пользователи» доступен тип учётной записи «Таски и консультации» (`tasks_only`). Такой пользователь может запускать опубликованные таски, видеть статусы своих запусков и пользоваться консультациями; обычные чаты, шаблоны и их API для него закрыты. Консультации доступны независимо от разрешения обычных диалогов. Режим можно включить или отключить и у существующего пользователя. Администратор видит все запуски тасок и их место в общей очереди.

Для запуска тасок из MCP-клиентов используйте [Valeria Tasks MCP](mcp/mcp-valeria-tasks/README.md).
В его конфигурации задаются адрес Valeria и пользователь, от имени которого создаются
запуски. Инструменты показывают доступные таски, состояние запусков и итоговые ответы.

Для администрирования из MCP-клиента доступен
[Valeria Administrator MCP](mcp/mcp-valeria-administrator/README.md): пользователи,
таски и их аудитория, очереди, лимиты, шаблоны, консультации, запуски, статистика и аудит.
В конфигурации задаётся учётная запись Valeria с ролью `admin`.

Для работы из Telegram доступен [Valeria Telegram Bot](integrations/telegram-valeria-bot/README.md).
После входа под своей учётной записью пользователь выбирает таски, консультации или
диалоги, получает результаты и отвечает на уточняющие вопросы. Бот запускается
отдельным процессом; инструкция и пример конфигурации находятся в каталоге интеграции.

### Статистика

Вкладка «Статистика» доступна администратору и показывает успешные запуски тасок всех
пользователей: количество за выбранный день и за всё время, а также два средних времени
за день — от запуска до завершения и от постановки в очередь до завершения.
По умолчанию выбран сегодняшний день; сутки определяются в часовом поясе браузера
по времени завершения задачи. Данные обновляются при событиях задач и кнопкой «Обновить».

Ошибки, отмены, диалоги и консультации исключены. Повторные запуски и этапы цепочек
учитываются отдельно. При отсутствии корректных временных отметок задача не входит
в соответствующее среднее; вместо среднего по пустой выборке отображается «Нет данных».
Подсчёт охватывает всю сохранённую историю, включая более 500 запусков.

API: `GET /api/admin/statistics?day=2026-10-04&timezone=UTC`.
Параметры необязательны: по умолчанию используются текущий день и `UTC`.

### Запуск в Docker

Комплект запускает контейнер OpenCode Web вместе с OpenCode CLI, Conan и локальными
MCP-серверами. Сервис подключён к bridge-сети `opencode` с разрешённым исходящим
доступом в интернет. Конфигурация OpenCode и MCP входит в образ, а всё изменяемое
состояние лежит внутри папки проекта.

Установите Docker Engine и Compose в Ubuntu:

```bash
sudo apt update
sudo apt install docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
```

При полном копировании папки вместе со скрытыми файлами достаточно запустить:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f web
```

Важно копировать всю папку, включая скрытые `.env`, `.secrets` и единый каталог `data`. В `data` хранятся SQLite, пользовательские рабочие папки, состояние и кэш OpenCode, база Memory MCP и Conan. `data/workspaces` монтируется внутрь контейнера как `/home/appuser/workspaces`, поэтому сохранённые контейнерные пути пользователей остаются стабильными.

Пример копирования на другой сервер со всеми скрытыми данными:

```bash
rsync -a --exclude='.git/' --exclude='.venv/' './' user@server:/opt/opencode-web/
ssh user@server 'cd /opt/opencode-web && docker compose up --build -d'
```

Для Git-операций по SSH поместите ключ и `known_hosts` в `.secrets/ssh`. Приватный SSH-ключ не копируется автоматически.

#### Автоматическая GPG-подпись коммитов в Docker

Контейнер поддерживает OpenPGP/GPG-подпись всех Git-коммитов и тегов. На новом сервере сначала создайте отдельный ключ с email, подтверждённым в GitLab:

```bash
sudo apt install gnupg
gpg --full-generate-key
gpg --list-secret-keys --keyid-format LONG
```

Добавьте результат `gpg --armor --export <KEY_ID>` в GitLab через «Профиль → Access → GPG keys». Затем подготовьте закрытый ключ и парольную фразу для контейнера:

```bash
mkdir -p .secrets/gpg
chmod 700 .secrets/gpg
gpg --armor --export-secret-keys <KEY_ID> > .secrets/gpg/private.asc
read -r -s -p 'GPG passphrase: ' GPG_KEY_PASSPHRASE
printf '%s' "$GPG_KEY_PASSPHRASE" > .secrets/gpg/passphrase
unset GPG_KEY_PASSPHRASE
chmod 600 .secrets/gpg/private.asc .secrets/gpg/passphrase
```

В `.env` укажите полный fingerprint ключа и включите подпись:

```dotenv
GIT_USER_NAME=Aleksey Khamitsevich
GIT_USER_EMAIL=verified-email@example.com
GIT_GPG_SIGNING_ENABLED=true
GIT_GPG_KEY_ID=FULL_40_CHARACTER_FINGERPRINT
GIT_GPG_PRIVATE_KEY_FILE=/app/.secrets/gpg/private.asc
GIT_GPG_PASSPHRASE_FILE=/app/.secrets/gpg/passphrase
```

После `docker compose up --build -d` entrypoint импортирует ключ в постоянный `data/gnupg` и включает `commit.gpgSign` и `tag.gpgSign`. Если ключ, fingerprint или файл парольной фразы недоступен, контейнер завершится с явной ошибкой и не запустит приложение с неподписанными коммитами.

Файл парольной фразы хранится открытым текстом на сервере. Ограничьте доступ к `.secrets`, используйте отдельный signing-only ключ и не добавляйте `.secrets` в Git или Docker-образ. Процессы OpenCode работают в том же контейнере и технически имеют доступ к смонтированным секретам; для строгой изоляции подпись необходимо вынести в отдельный сервис.

При развёртывании из `git clone` создайте `.env` из `.env.example`, новый `SESSION_SECRET`, файл `.secrets/admin-password` и заполните токены. Базы, диалоги и рабочие папки в Git не попадают.

Сайт по умолчанию остаётся доступен только локально по адресу `http://127.0.0.1:8081`. Внутри контейнера приложение слушает `0.0.0.0`, но адрес публикации по-прежнему определяется `APP_HOST`.

Остановка:

```bash
docker compose down
```

Каталог `data` и каталог секретов `.secrets` не попадают в Docker-образы и Git. Удаление контейнеров не удаляет эти данные. Токены YouTrack, GitLab и LLM хранятся только в `.env`; в `opencode/config/opencode.jsonc` находятся только ссылки на переменные окружения.

Структура изменяемых данных:

```text
data/
├── opencode-web.db
├── workspaces/
├── opencode/{share,state,cache}/
├── mcp/memory/
├── conan/
└── gnupg/
```

### Разработка

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest
```

Документы проекта:

- [техническое задание](docs/technical-specification.md);
- [консультационный интерфейс и его настройка](docs/consultations.md);
- [дизайн-промты](docs/design-prompts.md).

### Лицензирование

V.A.L.E.R.I.A. распространяется по трёхуровневой модели source-available:

- [V.A.L.E.R.I.A. Community License 1.0](LICENSE.md) разрешает личное, учебное,
  исследовательское и неограниченное внутреннее использование организациями,
  self-hosting, приватные изменения и коммерческую работу с результатами системы;
- отдельная [Commercial License](legal/LICENSE_COMMERCIAL.md) нужна для SaaS,
  управляемого хостинга под контролем поставщика, внешнего клиентского доступа,
  коммерческого распространения, OEM и white-label;
- [Enterprise License](legal/LICENSE_ENTERPRISE.md) применяется только к явно
  обозначенным проприетарным Enterprise Components и договорным услугам и не
  сокращает Community-права.

Доход от кода, отчётов, консалтинга и других самостоятельно созданных результатов
не требует платной лицензии сам по себе. Для внутреннего Community-использования
нет ограничений по размеру или выручке компании, числу сотрудников, агентов,
задач, токенов, внутренних пользователей или серверов.

Подробности: [FAQ](legal/LICENSE_FAQ.md),
[коммерческая модель](legal/COMMERCIAL.md),
[русское резюме](legal/LICENSING_SUMMARY_RU.md) и
[уведомления о сторонних компонентах](NOTICE.md).
Контакт для коммерческого лицензирования:
**[COMMERCIAL LICENSING CONTACT TO BE ADDED]**.

Community License является собственной source-available лицензией, а не
OSI-approved open-source лицензией. Сторонние компоненты сохраняют свои условия.
Юридические документы должны пройти профессиональную проверку до публикации или
использования в сделках.

### Границы текущего MVP

- backend запускает отдельный `opencode serve` для каждой востребованной пользовательской папки;
- процессы OpenCode слушают только loopback и недоступны браузеру напрямую;
- SQLite хранит пользователей, привязку диалогов, шаблоны, определения тасок, очередь с историей запусков и аудит;
- OpenCode хранит содержимое диалогов и выполняет работу агента;
- консультации используют общую очередь и Build mode; SQLite хранит только вопросы и итоговые ответы для их отображения, модель настраивает администратор;
- владелец обычного диалога и администратор видят полный runtime payload и reasoning OpenCode; пользовательский доступ к таске ограничен статусом её запуска;
- `cwd` и permission policy не являются полной файловой песочницей. Для строгой изоляции пользователей необходимы контейнеры или отдельные системные аккаунты.
