# Интеграция Hermes Agent

VALERIA умеет работать с [Hermes Agent](https://github.com/NousResearch/hermes-agent)
как с третьим backend-адаптером рядом с OpenCode v1/v2. Очередь, отложенная
отправка, задачи, консультации, артефакты и UI остаются общими.

## Архитектура

`backends/hermes.py` реализует тот же контракт `OpenCodeBackend`, что и
`v1.py`/`v2.py`. Это сохраняет `QueueDispatcher` и браузерный протокол без
переписывания. Адаптер переводит:

- сессии VALERIA в `POST /api/sessions` Hermes с детерминированным ID
  `valeria-<sha256(creation_key)>`;
- `send_prompt_async` в `POST /v1/runs` с `Idempotency-Key = message_id`;
- историю `/api/sessions/{id}/messages` в `{info, parts}`;
- `pending_controls` в approvals (`permissions`) и clarify (`questions`);
- статусы запусков в `busy`/`idle`/`failed`/`aborted`.

Связь `message_id -> run_id` хранится в SQLite (`hermes_run_bindings`), а не
только в памяти. Если ответ `POST /v1/runs` потерян, повтор с тем же
`Idempotency-Key` безопасен: Hermes вернёт тот же `run_id`. Это используется
при `submission_uncertain` и после перезапуска процесса.

Один `hermes gateway run` запускается на workspace с `cwd=workspace`, отдельным
`HERMES_HOME`, loopback-портом и bearer-ключом. Поэтому рабочие каталоги и
состояние разных пользователей не смешиваются. Простаивающий gateway
останавливается тем же reaper'ом, что и OpenCode, если нет активных заданий,
leases, запусков, approvals и clarify.

## Настройки

```dotenv
OPENCODE_API_VERSION=hermes
HERMES_BIN=/usr/bin/hermes
HERMES_HOST=127.0.0.1
HERMES_PORT_RANGE_START=4200
HERMES_PORT_RANGE_END=4299
HERMES_API_KEY=replace-with-a-long-random-key
HERMES_STATE_ROOT=./data/hermes
HERMES_HOME_TEMPLATE=
HERMES_START_TIMEOUT_SECONDS=120
HERMES_IDLE_TIMEOUT_SECONDS=900
HERMES_MODEL_NAME=hermes
HERMES_SESSION_SOURCE=valeria
```

`HERMES_API_KEY` обязателен и передаётся gateway как `API_SERVER_KEY`.
`HERMES_IDLE_TIMEOUT_SECONDS=0` отключает остановку простаивающих gateway.

Hermes хранит провайдеров и авторизацию в `HERMES_HOME`. Адаптер создаёт
отдельный home на workspace, поэтому укажите `HERMES_HOME_TEMPLATE` на заранее
настроенный каталог Hermes: при первом запуске workspace он будет скопирован.
Не используйте шаблон с рабочей историей сессий, если не нужна её копия.

Модели и провайдеры берутся из `GET /api/model/options`; выбранная в VALERIA
пара `provider/model` передаётся в `/v1/runs`. Если модель не выбрана, Hermes
использует свою конфигурацию.

## Переход существующей VALERIA с OpenCode на Hermes

Поддерживается остановленный переход OpenCode v1/v2 → Hermes в той же базе.
Пользователи, пароли, права, привязки workspace, шаблоны, определения задач,
очереди и настройки сохраняются. Истории обычных диалогов и запусков задач
считываются из старого OpenCode и сохраняются в SQLite. Вопросы и итоговые
ответы консультаций уже хранятся в SQLite.

После запуска с Hermes старые диалоги, консультации и запуски задач
архивируются и остаются доступны для чтения. Отправить сообщение в старую
сессию нельзя; новые диалоги и запуски задач получают новые сессии Hermes.
Рабочие файлы остаются в прежних workspace. История не импортируется в контекст
новых Hermes-сессий.

1. Завершите или отмените все задания, включая отложенные, и остановите VALERIA
   вместе с дочерними процессами OpenCode. Для Compose:

   ```sh
   docker compose down
   ```

2. Сделайте резервную копию базы SQLite, конфигурации (включая `SESSION_SECRET`),
   состояния OpenCode и workspace. Сохраните текущие настройки v1/v2 и старый
   бинарник OpenCode: они нужны для чтения истории. Обновите приложение до
   версии с этой командой миграции, пока оставив `OPENCODE_API_VERSION=v1`
   или `v2`. При Docker пересоберите образ с текущим значением backend:

   ```sh
   docker compose build web
   ```

3. **Со старой конфигурацией backend и старым бинарником OpenCode** выполните:

   ```sh
   opencode-web backend-switch --to hermes
   ```

   Для Compose:

   ```sh
   docker compose run --rm --no-deps web opencode-web backend-switch --to hermes
   ```

   Дождитесь сообщения `Saved ... histories`. Команда проверяет отсутствие
   незавершённых заданий, активных выполнений, вопросов и согласований,
   сохраняет истории и подготавливает переход. Работающий сервис, занятые
   runtime-порты, ошибка чтения любой истории или остановки временных процессов
   блокируют подготовку. Активный backend на этом шаге ещё не меняется.
   При ошибке исправьте её и повторите команду: не меняйте backend вручную в БД.

4. Установите Hermes в окружение, в котором VALERIA запускает дочерние процессы.
   Для Docker нужен образ с установленным Hermes: штатный Dockerfile его пока
   не устанавливает. Подготовьте чистый `HERMES_HOME_TEMPLATE` с провайдером,
   моделью и авторизацией. Затем задайте настройки из раздела выше, в частности:

   ```dotenv
   OPENCODE_API_VERSION=hermes
   HERMES_BIN=/usr/bin/hermes
   HERMES_API_KEY=replace-with-a-long-random-key
   HERMES_STATE_ROOT=./data/hermes
   HERMES_HOME_TEMPLATE=/absolute/path/to/preconfigured-hermes-home
   ```

   При Docker все пути должны существовать **в контейнере**; состояние и шаблон
   можно разместить под `/app/data`, уже смонтированным Compose. Не меняйте
   `APP_DATABASE_PATH`, `SESSION_SECRET` и привязки workspace при миграции.
   Проверьте совместимость сохранённых моделей в задачах, консультациях и
   `OPENCODE_DEFAULT_MODEL` с провайдерами Hermes; автоматически они не переводятся.
   Настройки MCP из OpenCode тоже требуют отдельной настройки для Hermes.

5. Запустите VALERIA. Для Compose с подготовленным Hermes-образом:

   ```sh
   docker compose up -d
   ```

   При старте подготовленный переход применяется атомарно, до запуска workers.
   Проверьте вход существующего пользователя, просмотр старого диалога и
   создание нового диалога с ответом Hermes.

Если между подготовкой и переключением снова запустить VALERIA на старом
backend, подготовка сбрасывается — команду сохранения историй нужно повторить.
Обратная миграция Hermes → OpenCode пока не поддерживается. Для возврата к
состоянию до перехода используйте согласованную резервную копию и старую
конфигурацию.

## Ограничения первой версии

- Смена backend на работающем сервисе не поддерживается. Для перехода
  OpenCode v1/v2 → Hermes выполните процедуру выше; продолжение старых сессий
  в Hermes и обратная миграция пока не реализованы.
- У Hermes нет аналога OpenCode agent-режимов. Режим `plan` эмулируется
  отдельным прогоном с планирующей инструкцией, затем идёт build-прогон.
- Clarify Hermes не содержит вариантов ответа. В задачах и консультациях такой
  вопрос требует пользователя: задача завершается с
  `question_requires_user`, консультация переходит в `waiting_question`.
  Approvals в задачах/консультациях завершаются `permission_required`, в
  диалогах пользователь может разрешить или отклонить действие.
- Hermes принимает inline-изображения, но не произвольные файлы. Вложения
  VALERIA по-прежнему выкладываются в workspace, а в промпт добавляются пути.
- MCP/toolsets Hermes не транслируются в OpenCode MCP. Совместимость конкретных
  серверов нужно проверять отдельно.
- Hermes выполняет инструменты на хосте gateway. Это не sandbox; ограничивайте
  workspace и права сервисного пользователя.
- Штатный `Dockerfile` собирает OpenCode. Для Hermes установите CLI в образ
  отдельно (или используйте локальный запуск) и задайте `HERMES_BIN`.

## Проверки

```sh
pytest -q tests/test_hermes_backend.py tests/test_backend_v2.py \
  tests/test_backend_switch.py tests/test_dispatcher.py tests/test_task_dispatcher.py
ruff check src/opencode_web
```

Юнит-тесты используют `httpx.MockTransport`, поэтому не требуют запущенного
Hermes. Для live-проверки поднимите `hermes gateway run` с тестовым провайдером,
задайте `OPENCODE_API_VERSION=hermes` и `HERMES_API_KEY`, затем создайте диалог
и задачу.
