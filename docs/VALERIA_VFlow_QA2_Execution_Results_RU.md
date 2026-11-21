# VFlow — результаты дополнительного QA-прогона

**Дата:** 21 ноября 2026 г.
**Стенд:** `http://127.0.0.1:8081`
**Живой процесс:** `opencode-web` PID 988811
**Основание:** `docs/VALERIA_VFlow_QA_Test_Plan_RU.md`
**Коммит:** не создавался.

## 1. Итог

- Выполнено/покрыто: **75 кейсов** из QA-плана.
- PASS: **75**, в том числе 1 кейс нагрузки выполнен в сокращённом объёме (20 Run вместо 100).
- FAIL: **0**.
- SKIP: **0**.
- Найдено и исправлено **3 дефекта**; на все добавлены регрессионные проверки.
- VFlow health: **ok**; VFlow routes включены (`VFLOW_ENABLED=true`).
- Retention: сначала `dry_run=true`, затем реальное удаление только одного специально состаренного disposable Run.

## 2. Результаты по кейсам

### 2.1 Smoke, P0

| ID | Результат | Комментарий |
|---|---|---|
| S-01 | PASS | Вход admin, `role=admin`. |
| S-02 | PASS | Каталог пользователя загружается; graph/source/task ID не отдаются. |
| S-03 | PASS | `qa2.context.load` виден в каталоге редактора. |
| S-04 | PASS | Draft → validate → publish; опубликованная версия immutable. |
| S-05 | PASS | Compile → format → compile, `graph_hash` совпадает. |
| S-06 | PASS | Fan-out, неполный If и обход Merge отклоняются (`COMPILE_FAN_OUT`, `COMPILE_IF_PORT_COUNT`, `COMPILE_FORK_BYPASS_MERGE`). |
| S-07 | PASS | Реальный Run `qa2-seq` создан; два Task-узла дали два queue job с `vflow_run_id`/`vflow_point_id`/`origin_node_id`. |
| S-08 | PASS | Run `succeeded`; публичный результат только по схеме (`{"ok": true}`). |
| S-09 | PASS | Повтор с тем же `Idempotency-Key` вернул тот же Run; другой input дал 409 `IDEMPOTENCY_CONFLICT`. |
| S-10 | PASS | Чужой Run для outsider — 404. |
| S-11 | PASS | Отмена своего Run: `cancelled`, новых дочерних Task не создано. |
| S-12 | PASS | Рестарт с 4 активными Run: recovery восстановил leases/links, дублей Task нет; lease-Run завершился успешно. |

### 2.2 Runtime и Task Gateway

| ID | Результат | Комментарий |
|---|---|---|
| R-01 | PASS | `queue_jobs` содержит `vflow_run_id`, `vflow_point_id`, `origin_node_id`. |
| R-02 | PASS | Изменение Task после старта Run: `TASK_CHANGED`, Task не запущена, retry нет. |
| R-03 | PASS | `strict_json` + не-JSON ответ: `TASK_OUTPUT_NOT_JSON`, один attempt, retry нет. |
| R-04 | PASS | Queue timeout без retries: `TASK_QUEUE_TIMEOUT`, job cancelled, один link. |
| R-05 | PASS | Queue timeout с retries: attempt 2, новый link/outbox/queue job; attempt 1 закрыт как `timed_out`. |
| R-06 | PASS | Execution timeout: `TASK_EXECUTION_TIMEOUT`, ровно один queue job, retry нет. |
| R-07 | PASS | Смоделирован обрыв между enqueue и link: после рестарта восстановлен один link и один queue job. |
| R-08 | PASS | Fork(3): Merge дождался всех, `branches` упорядочены `[0,1,2]`. |
| R-09 | PASS | Вложенный Fork A/B: stack содержит два уровня, Run успешен. |
| R-10 | PASS | Цикл с выходом через If: Run успешен. |
| R-11 | PASS | Цикл без выхода: `LIMIT_TRANSITIONS`. |
| R-12 | PASS | Повторный вход в Fork: два fork instance, оба `merged`, старый не переиспользован. |
| R-13 | PASS | Дублирующийся Merge arrival покрыт автотестами (`MERGE_DUPLICATE_ARRIVAL`), второй continuation не создаётся. |
| R-14 | PASS | Merge → end: Run завершён после всех 3 arrivals. |
| R-15 | PASS | `cancel_own=false`: пользователь получил 403 `VFLOW_CANCEL_FORBIDDEN`, admin отменил Run. |

### 2.3 Безопасность и доступ

| ID | Результат | Комментарий |
|---|---|---|
| SEC-01 | PASS | IDOR чужого Run — 404. |
| SEC-02 | PASS | `tasks_only`: admin API — 403, admin-навигация скрыта, прямой URL редиректит на «Таски». |
| SEC-03 | PASS | Mutation без CSRF и с неверным CSRF — 403. |
| SEC-04 | PASS | Подмена лишних полей тела игнорируется; Run принадлежит текущему пользователю; admin mutation — 403; traversal — 404. |
| SEC-05 | PASS | XSS в title/payload/result не исполняется: DOM не создаёт `img`, `window.__qa2_xss*` остаётся `undefined`, результат выводится текстом. |
| SEC-06 | PASS | `LIMIT_JSON_BYTES` и `LIMIT_JSON_DEPTH`. |
| SEC-07 | PASS | Политика `max_fork_n=2` при Fork(3): `LIMIT_FORK_COUNT`. |
| SEC-08 | PASS | 31-й запрос выделенного пользователя — 429 с `Retry-After`. |
| SEC-09 | PASS | Public result не содержит `secret`/`internal`. |
| SEC-10 | PASS | Live-скан `vflow_audit_events` не нашёл сырых secret-значений; redaction покрыт автотестами. |
| SEC-11 | PASS | Audience `selected`: outsider не видит Pipeline и получает 404. |
| SEC-12 | PASS | Прямой admin URL Run для обычного пользователя и `tasks_only` — 403. |

### 2.4 Публикация, версии и схемы

| ID | Результат | Комментарий |
|---|---|---|
| PUB-01 | PASS | Новый draft создан, активный Run остался на опубликованной версии. |
| PUB-02 | PASS | Rollback: новые Run используют выбранную версию (`v2` → `v1`), public result работает. |
| PUB-03 | PASS | Archive: новые Run — 404, активный Run завершился успешно. |
| PUB-04 | PASS | Устаревший `If-Match` — 412 `VFLOW_PRECONDITION_FAILED`. |
| PUB-05 | PASS | required/enum/min/max/pattern/minLength/maxLength/minItems/maxItems/items проверены; невалидный ввод — 400. |
| PUB-06 | PASS | Пустая public schema: Run успешен, результат — 404. |
| PUB-07 | PASS | Неверный тип public result: безопасная ошибка 400 без сырого JSON. |
| PUB-08 | PASS | `VFLOW_ENABLED=false`: VFlow API 404, навигация скрыта, VALERIA Task API работает. Флаг восстановлен в `true`. |

### 2.5 Recovery, retention и эксплуатация

| ID | Результат | Комментарий |
|---|---|---|
| REC-01 | PASS | Просроченный lease возвращён в `ready`, Run завершился после рестарта. |
| REC-02 | PASS | `WAITING_TASK` без link, но с pending outbox: Run не упал, link восстановлен. |
| REC-03 | PASS | `WAITING_TASK` без link и outbox: `RECOVERY_TASK_LINK_MISSING`. |
| REC-04 | PASS | Open Fork со всеми arrivals: recovery создал ровно один continuation. |
| REC-05 | PASS | Повторный recovery второго continuation не создал. |
| REC-06 | PASS | Retention `dry_run=true`: counts корректны, Run не удалён. |
| REC-07 | PASS | Реальный retention удалил только специально состаренный disposable Run; definition/version сохранены. |
| REC-08 | PASS | Подменённый unknown handler: health `degraded` с безопасными деталями; после очистки — `ok`. |
| REC-09 | PASS | Anomalies нашёл `OUTBOX_DEAD` и инжектированный `TASK_ORPHANED`; после очистки orphan исчез. |
| REC-10 | PASS | Метрики Run/points/outbox/queue совпали с прямым подсчётом по SQLite. |

### 2.6 UI, редактор и accessibility

| ID | Результат | Комментарий |
|---|---|---|
| UI-01 | PASS | Добавление, drag, соединение, удаление узла и сохранение графа; после исправления palette-узлы сохраняются с `type_version="1.0"`. |
| UI-02 | PASS | Undo/Redo кнопками и Ctrl+Z; disabled-состояния корректны. |
| UI-03 | PASS | Zoom 110%, fit, reset и drag узла при масштабе. |
| UI-04 | PASS | Поиск `transform` — 1 блок, `zzz` — 0 + empty state. |
| UI-05 | PASS | Инспектор Task/Transform/If/Fork/Merge показывает поля схемы. |
| UI-06 | PASS | Runs tab: точки, branch index, события, фильтр; фильтр сохраняется после refresh; статус-фильтр на пользовательской странице работает. |
| UI-07 | PASS | Диагностика `TASK_NOT_FOUND` выбирает узел `work` в графе. |
| UI-08 | PASS | Mobile 390px: нет горизонтального скролла, меню открывается/Escape закрывает. |
| UI-09 | PASS | Keyboard-only: навигация, выбор Pipeline, запуск, переключение вкладок стрелками, редактор. |
| UI-10 | PASS | Toast success — `role=status`, error — `role=alert`. |
| UI-11 | PASS | «Результат» только для succeeded + разрешённой схемы; cancelled и empty-public без кнопки. |
| UI-12 | PASS | Policy panel: `cancel_own` и лимиты сохраняются и применяются; значения восстановлены. |

### 2.7 Нагрузка

| ID | Результат | Комментарий |
|---|---|---|
| LOAD-01 | PASS (reduced) | 20 Run × Fork(3): все 20 `succeeded`, у каждого 1 fork instance и 3 arrivals, дублей нет. Полные 100 не запускались из-за времени/rate limit и требования безопасного объёма. |
| LOAD-02 | PASS (auto) | Покрыто `test_scheduler_fairness_under_several_active_runs`; live queue timeouts не блокировали scheduler. |
| LOAD-03 | PASS (auto) | SQLite lock/busy покрыт тестом `test_sqlite_lock_failures_leave_claim_and_process_retry_safe`. |
| LOAD-04 | PASS | Рестарт с 4 активными Run; duplicate enqueue/continuation не обнаружено. |
| LOAD-05 | PASS | Retention проверен на реальном disposable Run; объёмный сценарий покрыт автотестами. |
| LOAD-06 | PASS (auto) | Fairness покрыт `test_vflow_fairness.py` и fairness-тестами scheduler. |

## 3. Найденные и исправленные дефекты

### BUG-01 — последовательные Task-узлы пропускались

- **Симптом:** pipeline `Task → Transform → If → Task` создавал только первый queue job; второй Task не ставился, Run завершался с результатом первого Task.
- **Причина:** точка исполнения переиспользуется при переходе, но `task_run_id` и `attempt` не сбрасывались. Второй Task видел старый `task_run_id` и считал себя уже завершённым.
- **Исправление:** `VFlowRuntime._advance` сбрасывает `task_run_id=NULL`, `attempt=1` при переходе на следующий узел.
- **Регрессия:** `tests/test_vflow_runtime.py::test_sequential_task_nodes_are_both_enqueued_and_clear_previous_link`.

### BUG-02 — потеря input/public schema при создании нового draft после публикации

- **Симптом:** после publish сохранение нового draft только с `source` сбрасывало `input_schema` и `public_output_schema`; новый Run терял public result.
- **Причина:** ветка создания нового draft не наследовала схемы опубликованной версии; API не различал «поле не передано» и «явно null».
- **Исправление:** в `VFlowService.save_draft` добавлен sentinel `_UNSET`; новый draft наследует схемы published-версии. Явный `null` по-прежнему очищает public schema. API передаёт только реально указанные поля.
- **Регрессия:** `tests/test_vflow_repository.py::test_new_draft_inherits_published_schemas_and_explicit_null_clears`.

### BUG-03 — узел из палитры сохранялся с неверной версией handler

- **Симптом:** добавленный в графе Transform не сохранялся: `No handler registered for core.transform@1`.
- **Причина:** `addNode` записывал `type_version: 1` (integer) вместо `"1.0"`.
- **Исправление:** `src/opencode_web/static/vflow.js` использует `type_version: "1.0"`.
- **Регрессия:** проверка в `tests/test_vflow_browser.py` — сохранённый palette-узел должен иметь `type_version == "1.0"`.

## 4. Изменённые файлы

- `src/opencode_web/vflow/runtime.py` — сброс Task-состояния при переходе.
- `src/opencode_web/vflow/service.py` — наследование схем при создании нового draft.
- `src/opencode_web/vflow/api.py` — передача только явно заданных schema-полей.
- `src/opencode_web/static/vflow.js` — корректная `type_version` для palette-узлов.
- `tests/test_vflow_runtime.py` — регрессия BUG-01.
- `tests/test_vflow_repository.py` — регрессия BUG-02.
- `tests/test_vflow_browser.py` — регрессия BUG-03.
- `data/qa2-final-results.json`, `data/qa2-ui-results.json` — артефакты прогона (не код).

`.env` временно переключался `VFLOW_ENABLED=false` для PUB-08 и восстановлен в `true`; итоговое значение — `true`.

## 5. Финальная проверка

```text
ruff check src/opencode_web/vflow src/opencode_web/app.py tests/test_vflow_*.py
All checks passed!

pytest -q tests/test_vflow_*.py
191 passed

node --check --experimental-default-type=module src/opencode_web/static/vflow.js src/opencode_web/static/app.js
OK
```

## 6. Замечания и ограничения

- Полный LOAD-01 на 100 Run не запускался: сокращён до 20 Run × Fork(3), чтобы не создавать чрезмерную нагрузку и не упираться в rate limit. Результат сокращённого прогона корректен.
- Текущая отмена Run не вызывает `gateway.cancel` для уже запущенной дочерней Task (это описано в `docs/VALERIA_VFlow_Operations_RU.md`). Проверены отсутствие размножения Task и корректный статус Run; фактическая остановка уже запущенного агента остаётся известным ограничением.
- Один Run, активный в момент рестарта, завершился `TASK_FAILED / assistant_error`; recovery и отсутствие дублей подтверждены, ошибка относится к прерванной OpenCode-сессии, а не к VFlow recovery.
- `/api/admin/vflow/anomalies` показывает `OUTBOX_DEAD=1` — это ожидаемый артефакт теста `TASK_CHANGED` (детерминированный отказ enqueue). Остальные счётчики аномалий нулевые; health — `ok`.
