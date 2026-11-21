# VFlow — план тестирования

**Статус:** рабочий QA-план
**Дата:** 21 ноября 2026 г.
**Стенд:** `http://127.0.0.1:8081`
**Основание:** `docs/VALERIA_VFlow_Technical_Specification_RU.md`, `docs/VALERIA_VFlow_Operations_RU.md`, `docs/VALERIA_VFlow_Implementation_Backlog_RU.md`, `docs/VALERIA_VFlow_Execution_Plan_RU.md`.

> Пароль администратора находится в `.secrets/admin-password`. Не выводить его в чат, логи, скриншоты и баг-репорты.

---

## 1. Окружение и подготовка

### 1.1. Доступы

| Роль | Логин | Назначение |
|---|---|---|
| Администратор | `admin` | Создание задач, пайплайнов, публикация, диагностика |
| Обычный пользователь | `qa.user` | Запуск опубликованных пайплайнов |
| `tasks_only` | `qa.tasks_only` | Проверка ограниченного доступа |
| Посторонний | `qa.outsider` | Проверка IDOR и audience |

Пароли тестовых пользователей задаются при создании и не должны совпадать с production-паролями.

### 1.2. Тестовые задачи VALERIA

Создать и опубликовать:

| Task ID | Назначение |
|---|---|
| `qa.context.load` | Возвращает JSON с полями `repository`, `ok` |
| `qa.review.ok` | Возвращает `{"ok": true, "secret": "internal"}` |
| `qa.review.fail` | Завершается ошибкой |
| `qa.slow` | Выполняется дольше execution timeout |
| `qa.bad.json` | Возвращает невалидный JSON для `strict_json` |

### 1.3. Тестовые пайплайны

| ID | Сценарий |
|---|---|
| `qa-seq` | `start → Task → Transform → If → Task → end` |
| `qa-fork3` | Fork(3) → три Transform → Merge → end |
| `qa-nested` | Fork A, внутри ветки A0 — Fork B и Merge B, затем Merge A |
| `qa-loop` | Цикл с выходом через If |
| `qa-public` | Публичный результат разрешает только `ok` |
| `qa-timeout` | Task с queue/execution timeout и retries |
| `qa-policy` | `cancel_own=false`, строгие лимиты |

### 1.4. Правила безопасности тестирования

- Не удалять production-данные и чужие Run.
- Retention проверять сначала с `dry_run=true`.
- Не печатать пароли, CSRF-токены, session cookies и внутренние prompt-ы.
- Для разрушающих сценариев использовать отдельную БД или подтверждённую тестовую копию.

---

## 2. Smoke, P0

Выполнять первыми. Если smoke не проходит, глубокое тестирование останавливается.

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| S-01 | Войти как `admin` с паролем из `.secrets/admin-password` | 200, доступны админские разделы |
| S-02 | Открыть пользовательскую вкладку «Пайплайны» | Каталог загружается, граф и task ID не видны |
| S-03 | Создать и опубликовать `qa.context.load` | Задача доступна в каталоге редактора |
| S-04 | Создать `qa-seq`: draft → validate → publish | Публикация успешна, версия immutable |
| S-05 | Переключить Graph ↔ Code | Граф и DSL синхронизированы, данные не потеряны |
| S-06 | Отправить невалидный граф: fan-out без Fork, оба выхода If, обход Merge | Ошибки валидации, публикация запрещена |
| S-07 | Запустить `qa-seq` пользователем `qa.user` | Run создан, Task ушёл в общую очередь |
| S-08 | Дождаться завершения Run | Статус `succeeded`, публичный результат только по схеме |
| S-09 | Повторить запуск с тем же `Idempotency-Key` | Возвращается тот же Run, дубля нет |
| S-10 | Открыть чужой Run пользователем `qa.outsider` | 404, без утечки данных |
| S-11 | Отменить свой Run | `cancel_requested → cancelled`, дочерние задачи не плодятся |
| S-12 | Перезапустить сервер при активном Run | Recovery восстанавливает состояние без дублей |

---

## 3. Runtime и Task Gateway, P0/P1

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| R-01 | Task-узел: проверить `queue_jobs` | Есть `vflow_run_id`, `vflow_point_id`, `origin_node_id` |
| R-02 | Изменить Task после старта Run | `TASK_CHANGED`, queue job не создаётся |
| R-03 | `strict_json`, ответ не JSON | Run failed с безопасной ошибкой, retry по умолчанию нет |
| R-04 | Queue timeout без retries | `TASK_QUEUE_TIMEOUT` |
| R-05 | Queue timeout с retries | Новый attempt, новый task run/link/outbox, старый закрыт |
| R-06 | Execution timeout | `TASK_EXECUTION_TIMEOUT`, повторного запуска нет |
| R-07 | Оборвать сервер между enqueue и link | После рестарта одна логическая задача, без дубля |
| R-08 | Fork(3), ветки завершаются в разном порядке | Merge ждёт все, `branches` упорядочены по индексу |
| R-09 | Вложенный Fork A/B | B закрывается раньше A, `fork_stack` корректен |
| R-10 | Цикл с выходом через If | Run завершается успешно |
| R-11 | Цикл без выхода | `LIMIT_TRANSITIONS` |
| R-12 | Повторный вход в тот же Fork | Новый fork instance, старый merged не переиспользуется |
| R-13 | Дублирующийся Merge arrival | Второй continuation не создаётся |
| R-14 | Merge → end | Run завершается только после закрытия всех Fork |
| R-15 | `cancel_own=false` | Пользователь получает `403 VFLOW_CANCEL_FORBIDDEN`, админ отменяет |

---

## 4. Безопасность и доступ, P0/P1

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| SEC-01 | `qa.outsider` открывает чужой Run | 404 |
| SEC-02 | `tasks_only` открывает админский раздел | 403/404, навигация скрыта |
| SEC-03 | Mutation без `x-csrf-token` | Отклонено |
| SEC-04 | Подмена `pipeline_id`, `run_id`, `task_id` | 403/404, выполнения нет |
| SEC-05 | XSS в названии Task, payload, public result | Скрипт не исполняется |
| SEC-06 | JSON больше лимита / глубже лимита | `LIMIT_JSON_BYTES` / `LIMIT_JSON_DEPTH` |
| SEC-07 | Fork больше лимита | `LIMIT_FORK_COUNT` |
| SEC-08 | Более 30 запусков в минуту | 429 + `Retry-After` |
| SEC-09 | Public result содержит `secret` | Поле отсутствует в ответе |
| SEC-10 | В audit metadata переданы password/token | Значения замаскированы `[REDACTED]` |
| SEC-11 | Audience `selected`: outsider открывает каталог | Pipeline отсутствует |
| SEC-12 | Прямой URL админского Run | 403/404, без данных |

---

## 5. Публикация, версии и схемы, P1

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| PUB-01 | Изменить published pipeline | Создаётся новый draft, активные Run на старой версии |
| PUB-02 | Rollback на предыдущую версию | Новые Run используют выбранную версию |
| PUB-03 | Archive | Новые запуски запрещены, активные Run не ломаются |
| PUB-04 | Два админа сохраняют draft | Второй получает 412 по ETag |
| PUB-05 | Input schema: required/enum/min/max/pattern/array bounds | Невалидный ввод отклоняется до Run |
| PUB-06 | Public result schema пустая | Результат недоступен |
| PUB-07 | Public result schema с неверным типом | Безопасная ошибка, без сырого JSON |
| PUB-08 | `VFLOW_ENABLED=false` | VFlow-маршруты 404, навигация скрыта, VALERIA работает |

---

## 6. Recovery, retention и эксплуатация, P1

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| REC-01 | Просроченный lease | Точка возвращается в `ready` |
| REC-02 | `WAITING_TASK` без link, но с pending outbox | Run не падает, проверка переносится |
| REC-03 | `WAITING_TASK` без link и без outbox | `RECOVERY_TASK_LINK_MISSING` |
| REC-04 | Open Fork со всеми arrivals | Ровно один continuation после recovery |
| REC-05 | Повторный recovery | Второго continuation нет |
| REC-06 | Retention `dry_run=true` | Ничего не удалено, counts корректны |
| REC-07 | Retention реальный | Удаляются только terminal Run старше срока, definitions/versions остаются |
| REC-08 | Healthcheck при неизвестном handler | `degraded`, безопасные детали |
| REC-09 | Anomalies endpoint | Находит stuck/orphan/missing/merge-ready/dead outbox |
| REC-10 | Metrics endpoint | Счётчики Run/точек/outbox/queue корректны |

---

## 7. UI, редактор и accessibility, P1

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| UI-01 | Добавить, переместить, соединить и удалить узел | Граф обновляется, сохранение работает |
| UI-02 | Undo/Redo кнопками и Ctrl/Cmd+Z | Восстанавливается граф/код, кнопки disabled корректны |
| UI-03 | Zoom/pan/fit и drag при масштабе | Позиции не уезжают, порты работают |
| UI-04 | Поиск в палитре `transform` / `zzz` | Один блок / ноль результатов |
| UI-05 | Инспектор Task/Transform/If/Fork/Merge | Поля соответствуют схеме, ошибки видны |
| UI-06 | Runs tab: точки, branch index, события, фильтр | Данные корректны, фильтр сохраняется |
| UI-07 | Клик по диагностике | Выбирается узел/связь, фокус и подсветка |
| UI-08 | Mobile 390px | Нет горизонтального скролла, действия доступны |
| UI-09 | Keyboard-only | Навигация, вкладки, запуск, отмена, редактор |
| UI-10 | Toast success/error | `role=status` / `role=alert`, корректные live-region |
| UI-11 | Публичный результат | Кнопка «Результат» только для succeeded + разрешённой схемы |
| UI-12 | Политика пайплайна | Лимиты и `cancel_own` сохраняются и применяются |

---

## 8. Нагрузка и отказоустойчивость, P2

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| LOAD-01 | 100 Run × Fork(3) | Все Run завершаются, дублей нет |
| LOAD-02 | Много `WAITING_TASK` | Очередь не блокирует scheduler |
| LOAD-03 | SQLite lock/busy | Короткие транзакции, retry без потери состояния |
| LOAD-04 | Рестарт под нагрузкой | Recovery без duplicate continuation/enqueue |
| LOAD-05 | Retention на большом объёме | Удаление в пределах таймаута, БД консистентна |
| LOAD-06 | Fairness | Один Run не монополизирует тик |

---

## 9. Реальный E2E — главный пробел

Автотесты VFlow используют fake/mock gateway, а UI-тесты — mocked API. Поэтому отдельно и обязательно проверить на реальном сервере:

1. Реальный Task через `queue_jobs` и OpenCode.
2. Реальный ответ LLM + `strict_json`.
3. Рестарт сервера с активными Run.
4. Два процесса/воркера: SQLite locking и отсутствие дублей.
5. Retention на реальных данных.
6. Rate limit между несколькими процессами (сейчас limiter in-memory).
7. Реальную отмену running Task, а не только queued.

---

## 10. Автоматизация

```bash
/tmp/vflow-venv/bin/ruff check src/opencode_web/vflow src/opencode_web/app.py tests/test_vflow_*.py
/tmp/vflow-venv/bin/pytest -q tests/test_vflow_*.py
node --check --experimental-default-type=module src/opencode_web/static/vflow.js src/opencode_web/static/app.js
```

Текущий baseline: **189 VFlow-тестов** и **1 Playwright smoke-test**.
Полный Playwright suite требует установленного Chromium и не должен считаться пройденным, если браузер недоступен.

---

## 11. Критерии завершения

- Все P0-кейсы пройдены.
- Нет открытых P0/P1-дефектов.
- P1-кейсы пройдены или явно приняты с риском.
- Recovery проверен после реального рестарта.
- Security-кейсы IDOR/XSS/CSRF/limits пройдены.
- Retention проверен в dry-run и реальном режиме.
- Мобильный и keyboard-only сценарии пройдены.
- Реальный E2E Task → очередь → OpenCode → результат подтверждён.

---

## 12. Шаблон баг-репорта

```text
ID:
Кейс:
Окружение/версия:
Пользователь/роль:
Шаги:
1.
2.
3.
Фактический результат:
Ожидаемый результат:
Run ID / Pipeline ID:
Скриншот/лог:
Воспроизводимость:
```
