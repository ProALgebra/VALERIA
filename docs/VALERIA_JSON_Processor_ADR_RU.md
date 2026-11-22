# VALERIA JSON Repair Processor — ADR

**Статус:** принято для Phase M0/M1/M2
**Дата:** 22.11.2026
**Область:** самостоятельный модуль `src/opencode_web/json_processor/`
**Связанные документы:** `docs/VALERIA_VFlow_ADR_RU.md`, `docs/VALERIA_VFlow_Technical_Specification_RU.md`

## 1. Контекст и цель

VALERIA получает от OpenCode/Task ответы, которые должны быть JSON. Модель может
добавить markdown-ограждение, пояснение до/после JSON, trailing comma, одинарные
кавычки или Python-литералы. Ошибка синтаксиса не должна попадать в downstream как
«успешный» результат и не должна требовать продолжения той же OpenCode-сессии.

JSON Repair Processor — отдельный ограниченный компонент, который:

1. сначала пытается детерминированно восстановить синтаксис локально;
2. при необходимости делает один или несколько ограниченных LLM-вызовов через
   OpenAI-compatible provider;
3. проверяет схему и сохранность содержимого;
4. либо возвращает валидный JSON, либо завершается fail-closed.

## 2. ADR-JP-001. Отдельный модуль, независимый от VFlow

**Решение.** Код находится в `src/opencode_web/json_processor/` и не импортирует
`opencode_web.vflow`, Task gateway, очередь или OpenCode session code. Core-модули
(`models`, `parser`, `schema`, `content`, `prompt`, `settings`) остаются чистыми и
не работают с БД, сетью или filesystem. Персистентность вынесена в отдельный
submodule `json_processor.repository`, который знает только `Database`.

**Причина.** Repair нужен и Task, и потенциально другим потребителям. Жёсткая
привязка к VFlow сделала бы невозможным повторное использование и увеличила бы
связанность с runtime. Отдельный repository сохраняет чистоту парсера и позволяет
тестировать его без SQLite.

**Следствия.**
- VFlow/Task integration — отдельный последующий слой, который вызывает публичный
  API пакета.
- Любая интеграция с VFlow опциональна и не меняет поведение VFlow при выключенном
  feature flag.
- Core-модули не получают доступ к БД, сети, filesystem, shell или OpenCode
  session. `repository` не вызывает provider и не запускает worker.
- M2 добавляет таблицы `json_processor_settings`, `json_repair_jobs`,
  `json_repair_attempts`; VFlow-таблицы не изменяются.

## 3. ADR-JP-002. Только OpenAI-compatible provider

**Решение.** Для LLM-repair поддерживается только OpenAI-compatible HTTP API:

- `POST {base_url}/v1/chat/completions` — repair-запрос;
- `GET {base_url}/v1/models` — проверка доступности модели/провайдера.

Настройки провайдера:

| Параметр | Смысл |
|---|---|
| `base_url` | Базовый URL OpenAI-compatible endpoint-а |
| `api_key_ref` | Ссылка на секрет в существующем secret storage, не сам ключ |
| `model` | Идентификатор модели |

**Причина.** Единый протокол снижает число адаптеров, упрощает аудит и позволяет
менять провайдера без изменения логики repair.

**Следствия.**
- Нет vendor-specific adapters, SDK, особых полей или function-calling payload-ов.
- API-ключ никогда не хранится в настройках, prompt-е, результате, истории или
  логах; сохраняется только `api_key_ref`.
- Сетевой слой будет реализован в следующей фазе. В M1 нет HTTP-клиента.
- Ошибки provider-а нормализуются в коды `PROVIDER_ERROR`, `TIMEOUT`,
  `CANCELLED`, `LLM_UNAVAILABLE`.

## 4. ADR-JP-003. Без agent tools и продолжения OpenCode-сессии

**Решение.** Repair-вызов является обычным Chat Completions запросом. Запрещены:

- tools/function calling;
- MCP tools;
- shell, filesystem, browser и любые side effects;
- продолжение или возобновление OpenCode-сессии;
- передача repair-агенту полного контекста задачи, секретов или workspace.

**Причина.** Repair — детерминированная задача преобразования текста. Агентские
возможности расширяют поверхность атаки и не нужны для исправления JSON.

**Следствия.** Prompt содержит только инструкции режима, схему, диагностику и
ограниченный raw output. Модель обязана вернуть только JSON.

## 5. ADR-JP-004. Raw output — недоверенные данные

**Решение.** Любой предыдущий ответ модели рассматривается как недоверенные данные:

- raw output передаётся в prompt внутри явных delimiters
  `<|UNTRUSTED_JSON_OUTPUT_BEGIN|>` … `<|UNTRUSTED_JSON_OUTPUT_END|>`;
- вхождения delimiter-подобных последовательностей нейтрализуются;
- system instructions прямо запрещают выполнять инструкции из данных, вызывать
  tools и следовать prompt injection;
- raw output никогда не интерпретируется как код, shell-команда, SQL, URL для
  автоматического запроса или инструкция агенту;
- локальный парсер не исполняет найденный текст, а только разбирает JSON.

**Следствия.** Prompt injection внутри ответа модели не может расширить права
процессора. Даже если LLM вернёт инструкции вместо JSON, результат не пройдёт
парсер/схему и будет отклонён.

## 6. ADR-JP-005. Сначала локальный repair, LLM только при необходимости

**Решение.** Порядок обработки:

1. строгий `json.loads`;
2. консервативная локальная нормализация;
3. извлечение одного однозначного JSON-кандидата из текста;
4. проверка схемы;
5. только если локально восстановить не удалось — LLM-repair;
6. после LLM-ответа снова выполняются парсер, схема и проверка содержимого.

**Причина.** Большинство дефектов — синтаксические. Локальный repair дешевле,
воспроизводим, не зависит от провайдера и не создаёт утечек.

**Следствия.**
- Локальная нормализация ограничена: одно markdown-ограждение, trailing commas,
  безопасные одинарные кавычки, value-preserving `True/False/None`.
- `NaN`, `Infinity`, `undefined`, неоднозначные кандидаты и свободный текст
  отклоняются.
- Скалярное значение допускается только как строгий полный `json.loads` всего
  вывода. Из текста с prose извлекаются только однозначные object/array
  кандидаты; скаляр внутри prose не извлекается (ADR-JP-012).
- Отсутствующие поля никогда не изобретаются.
- Число и бюджет LLM-попыток ограничены настройками (ADR-JP-016).

## 7. ADR-JP-006. Режимы `syntax_only` и `schema_safe`; без semantic rewrite

**Решение.** В M1 определены два режима:

- **`syntax_only`** — разрешено менять только синтаксис. Любое изменение значения,
  типа, набора ключей или порядка массива отклоняется.
- **`schema_safe`** — разрешены только детерминированные преобразования, заранее
  перечисленные allowlist-ом (например, подстановка известного placeholder-а в
  отсутствующее поле схемы). Свободное переписывание смысла запрещено.

**Причина.** Repair не должен незаметно менять факты, числа, имена или структуру
ответа. Схема может требовать поле, но не даёт права выдумывать его содержание.

**Следствия.**
- `content.py` сравнивает декодированные значения до/после.
- В `syntax_only` allowlist игнорируется и любое изменение считается ошибкой
  `CONTENT_CHANGED`.
- В `schema_safe` изменение допускается только при точном совпадении пути и
  операции с `ContentAllowance`.
- Allowlist формируется **на каждый repair request** из Task-схемы и передаётся
  вызывающим слоем. Глобальная настройка только включает режим
  `schema_safe`/`syntax_only`; глобального списка разрешённых изменений нет.
- При `additionalProperties: false` лишнее свойство является ошибкой
  `ADDITIONAL_PROPERTY`. Процессор не удаляет его автоматически и не считает
  `schema_safe` разрешением на удаление; без явного per-request allowlist repair
  завершается fail-closed.
- Реальные deterministic transformations и их UI появятся в следующих фазах.

## 8. ADR-JP-007. Fail closed и контракт ошибок

**Решение.** Если обязательный JSON не восстановлен, не прошёл схему или изменил
содержимое, downstream не получает значение. Ошибка возвращается в отдельном
error envelope, который не может быть принят за task JSON:

```json
{
  "valeria_json_processor": {
    "status": "error",
    "code": "NOT_REPAIRABLE",
    "message": "raw output does not contain one unambiguous JSON value",
    "path": "$",
    "diagnostics": []
  }
}
```

Основные коды:

- `NOT_REPAIRABLE`;
- `AMBIGUOUS_JSON`;
- `EMPTY_OUTPUT`;
- `RAW_OUTPUT_TOO_LARGE`;
- `SCHEMA_DEFINITION_INVALID`;
- `SCHEMA_VALIDATION_FAILED`;
- `CONTENT_CHANGED`;
- зарезервированные `LLM_UNAVAILABLE`, `PROVIDER_ERROR`, `TIMEOUT`, `CANCELLED`,
  `INTERNAL_ERROR`.

**Причина.** Ошибка repair-а не должна случайно стать валидным ответом задачи или
быть распознана как ожидаемая бизнес-схема.

**Следствия.**
- Envelope всегда имеет единственный корневой ключ `valeria_json_processor` и
  `status: "error"`.
- Ошибка не сохраняется как успешный Task result.
- Потребитель обязан проверять envelope до разбора task JSON.

## 9. ADR-JP-008. Сохранение raw output, попыток, ошибок и истории

**Решение.** Следующие данные сохраняются в рамках жизненного цикла repair-записи:

- исходный raw output;
- каждый attempt: номер, фаза (`local`/`llm`), режим, стратегия парсинга,
  применённые нормализации, диагностика, ошибка;
- итоговый результат или error envelope;
- timestamps и correlation/idempotency identifiers (в следующих фазах).

**Причина.** Нужны воспроизводимость, разбор инцидентов, аудит и доказательство,
что downstream получил именно проверенное значение.

**Следствия.**
- Секреты и API-ключи в историю не попадают; attempt metadata редактируется.
- Ограничения размера и retention заданы в M2 (ADR-JP-014, ADR-JP-016);
  destructive cleanup появится позже.
- M1 предоставляет immutable-структуры `RepairAttempt`, `RepairResult`,
  `ParseResult`, готовые к сериализации без изменения контракта.

## 10. ADR-JP-009. Idempotency, queue и recovery

**Решение.** M1 зарезервировал контракт, M2 реализует его персистентную часть:

- idempotency key на уровне записи repair;
- отдельная таблица jobs, не смешанная с OpenCode `queue_jobs`;
- ограниченное число LLM-попыток и timeout хранятся в настройках;
- восстановление после рестарта: `processing`-job с истёкшим lease возвращается
  в `queued` без потери счётчика попыток;
- запрет параллельного выполнения одного и того же repair обеспечивается
  уникальным каноническим ключом и lease/CAS.

**Причина.** Эти механизмы требуют транзакций и эксплуатационных решений,
которые не должны влиять на чистоту core-парсера.

**Следствия.**
- M2 добавляет repository, но не добавляет worker loop, provider, admin API/UI и
  автоматический retry scheduler.
- Job не становится `succeeded` без явного `complete_job` с активным lease.
- Legacy same-chat repair VFlow не изменяется.

## 11. ADR-JP-010. Контракт Task/VFlow и legacy same-chat repair

**Решение.**

- **Успешный repair** отдаёт обычное декодированное JSON-значение. Контракт
  Task/VFlow output не меняется.
- **Неуспешный repair** не отдаёт JSON-значение и не подменяет его `null`, `{}` или
  error envelope внутри task result. Ошибка остаётся в processor envelope/записи.
- **Legacy same-chat repair** (существующий механизм VFlow с
  `max_json_repairs`/`vflow_repair_identity`) не изменяется в M0/M1 и остаётся
  поведением по умолчанию.
- Новая интеграция с Task/VFlow будет включаться отдельным feature flag
  (предлагаемое имя: `VALERIA_JSON_PROCESSOR_VFLOW_ENABLED`, default `false`).
- При выключенном flag используется только legacy path; при включённом — только
  новый processor path. Два repair-цикла одновременно для одного ответа не
  запускаются.
- Удаление legacy path допускается только после отдельного ADR и периода
  совместимости.

**Причина.** Уже существующий same-chat repair стабилен и не должен регрессировать
из-за поэтапного внедрения нового модуля.

**Следствия.** M1 не содержит импортов VFlow и не меняет Task/VFlow код.

## 12. ADR-JP-011. Безопасное подмножество JSON Schema

**Решение.** Валидатор поддерживает только:

`type`, `required`, `properties`, `items`, `enum`, `minimum`, `maximum`,
`minLength`, `maxLength`, `pattern`, `additionalProperties: false`.

Дополнительно:
- размер схемы ограничен;
- глубина ограничена;
- `$ref` (включая внешние ссылки) запрещён;
- неизвестные keywords запрещены;
- ошибки имеют стабильные пути `$.field`, `$.items[0]`;
- `additionalProperties` допускается только как boolean, реальный эффект имеет
  `false`.

**Причина.** Полный JSON Schema — это большая поверхность поведения и внешних
ссылок. Для repair достаточно предсказуемого подмножества.

**Следствия.** Схемы вне подмножества отклоняются до начала repair-а с
`SCHEMA_DEFINITION_INVALID`, а не деградируют молча.

## 13. ADR-JP-012. Скалярный prose extraction запрещён

**Решение.**

- Строгий `json.loads` всего вывода может вернуть любое JSON-значение, включая
  скаляр, `null`, пустую строку и число.
- Если весь вывод не является JSON, из текста с пояснениями допускается
  извлечение только **одного однозначного object/array** кандидата.
- Скаляр (строка/число/boolean/null), найденный внутри prose или markdown, не
  извлекается: невозможно надёжно отличить данные от пояснения.
- Несколько независимых object/array кандидатов дают `AMBIGUOUS_JSON`.

**Причина.** Извлечение `true`, `42` или `"ok"` из обычного текста почти всегда
является угадыванием и может подменить ожидаемую структуру Task-ответа.

**Следствия.** `ParseStrategy.PROSE_EXTRACTED` применяется только к
object/array. Скалярные ответы проходят только через `DIRECT`/`NORMALIZED`/
`FENCED` в зависимости от фактического полного значения.

## 14. ADR-JP-013. `schema_safe` allowances — per request; extra properties не удаляются

**Решение.**

- Глобальная настройка хранит только режим (`syntax_only`/`schema_safe`).
- Список `ContentAllowance` строится вызывающим слоем для каждого repair request
  из конкретной Task-схемы и не сохраняется в глобальных настройках.
- `additionalProperties: false` — жёсткая ошибка при лишнем свойстве
  (`ADDITIONAL_PROPERTY`).
- Процессор никогда не удаляет лишние свойства автоматически. Удаление возможно
  только при явном per-request `ContentAllowance` с операцией `REMOVE`, если
  такой сценарий будет отдельно разрешён; по умолчанию его нет.
- `schema_safe` не разрешает свободное переписывание значений, добавление
  отсутствующих фактов или изменение типов вне allowlist.

**Причина.** Глобальный allowlist смешал бы правила разных Task-схем и позволил
бы одному шаблону ослабить проверку другого. Автоудаление полей может уничтожить
данные, которые модель добавила осмысленно, и скрыть ошибку схемы.

**Следствия.** В M2 validated settings намеренно не содержит поля allowances;
попытка записать неизвестный ключ отклоняется.

## 15. ADR-JP-014. Retention: raw/repaired 30 дней, attempts 90 дней

**Решение.** Значения по умолчанию:

| Данные | Срок | Настройка |
|---|---|---|
| Raw output и repaired result | 30 дней | `raw_output_retention_days` |
| Attempts и metadata | 90 дней | `attempt_retention_days` |

Оба срока настраиваются в пределах 1..3650 дней. Отсчёт raw/result идёт от
`finished_at` (fallback `created_at`) терминального job; attempts — от
`created_at` попытки.

**Причина.** Raw output может содержать персональные данные и должен жить
меньше, чем audit-metadata, необходимая для разбора инцидентов.

**Следствия.**
- M2 хранит raw output за `raw_output_ref`, а не inline; destructive retention
  job в M2 не реализован.
- `retention_preview()` только считает будущие кандидаты и ничего не удаляет.
- Последующая очистка обязана сохранять job/attempt metadata 90 дней и удалять
  raw/repaired payload независимо от неё.

## 16. ADR-JP-015. LLM получает только последнюю диагностику и один кандидат

**Решение.** В LLM-запрос передаются:

- инструкции режима;
- Task-схема (в установленном бюджете);
- **последняя** validation diagnostics, без полной истории попыток;
- не более одного последнего raw/candidate, ограниченного по размеру (целевой
  предел 16 KB; текущий prompt builder дополнительно ограничивает raw 12 KB и
  весь prompt 24 KB).

Полная история attempts, предыдущие ответы LLM и секреты не передаются.

**Причина.** История увеличивает prompt injection surface, стоимость и шанс,
что модель начнёт «исправлять» уже отклонённые варианты вместо текущего.

**Следствия.** Provider layer в следующей фазе обязан использовать
`build_repair_prompt` с этими бюджетами. Attempts остаются в БД для аудита, но не
становятся контекстом по умолчанию.

## 17. ADR-JP-016. Settings, jobs, idempotency и lease (M2)

**Решение.** Persistence состоит из трёх таблиц.

### 17.1. `json_processor_settings` (singleton)

| Поле | Смысл |
|---|---|
| `id` | всегда `1` |
| `settings_json` | только known non-secret settings |
| `version` | optimistic lock, начинается с 1 |
| `updated_by` | FK на `users(id)` или NULL |
| `created_at`, `updated_at` | audit timestamps |

Defaults:

| Настройка | Default | Допустимо |
|---|---|---|
| `enabled` | `false` | boolean |
| `base_url` | `""` | пусто или absolute http(s), без credentials/query/fragment |
| `api_key_ref` | `""` | opaque reference, ≤255 символов |
| `model` | `""` | ≤200 символов |
| `local_repair_enabled` | `true` | boolean |
| `max_llm_attempts` | `2` | 0..3 |
| `request_timeout_seconds` | `45` | 1..300 |
| `max_input_bytes` | `65536` | 1..1048576 |
| `max_output_bytes` | `65536` | 1..1048576 |
| `max_output_tokens` | `4096` | 1..32768 |
| `temperature` | `0.0` | 0..2 |
| `repair_mode` | `syntax_only` | `syntax_only`/`schema_safe` |
| `on_processor_failure` | `fail` | только `fail` |
| `concurrency_limit` | `4` | 1..32 |
| `queue_limit` | `1000` | 1..100000 |
| `raw_output_retention_days` | `30` | 1..3650 |
| `attempt_retention_days` | `90` | 1..3650 |

Правила:
- API-ключ и любые secret-shaped поля не хранятся; сохраняется только
  `api_key_ref`.
- Неизвестные ключи при update отклоняются. При чтении повреждённой строки
  неизвестные ключи игнорируются, поэтому случайно записанный секрет не
  возвращается наружу.
- Update валидирует весь итоговый документ и выполняет
  `UPDATE ... WHERE id = 1 AND version = expected_version`. При несовпадении —
  `SETTINGS_VERSION_CONFLICT`; при успехе `version + 1`.
- `updated_by`, `created_at`, `updated_at` возвращаются как audit metadata.
- `enabled` требует хотя бы один путь repair: local или LLM. При
  `max_llm_attempts > 0` обязательны `base_url` и `model`; local-only режим
  (`max_llm_attempts = 0`, `local_repair_enabled = true`) провайдера не требует.
- `api_key_ref` не обязателен: локальный OpenAI-compatible endpoint может не
  требовать авторизации.
- `concurrency_limit` и `queue_limit` хранятся для будущих worker/API; M2
  repository не запускает worker и не отбрасывает jobs при заполненной очереди.

### 17.2. `json_repair_jobs`

Поля: `id`, `source_type` (`task`/`vflow`/`standalone`), nullable correlation
`task_run_id`/`vflow_run_id`/`point_id`, `raw_output_ref`, `raw_hash`,
`schema_json`, `schema_hash`, `settings_version`, `prompt_version`, `status`,
`method` (`none`/`local`/`llm`), `attempts`, `result_json`,
`error_code`/`error_message`, unique `idempotency_key`, `lease_owner`,
`lease_until`, `created_at`/`updated_at`/`started_at`/`finished_at`.

Lifecycle: `queued → processing → succeeded|failed|cancelled`. Терминальные
статусы не перезаписываются.

**Idempotency.** Канонический ключ:

```text
(source_type, task_run_id, vflow_run_id, point_id,
 raw_hash, schema_hash, settings_version, prompt_version)
```

- `schema_hash` всегда вычисляется repository из канонического JSON схемы.
- Повторный запрос с той же идентичностью возвращает существующий job, даже если
  передан другой explicit `idempotency_key`.
- Тот же explicit key с другой идентичностью — `IDEMPOTENCY_CONFLICT`.
- Уникальный индекс по canonical-полям (NULL нормализуются через `COALESCE`)
  защищает от гонки; `BEGIN IMMEDIATE` делает проверку и insert атомарными.
- Смена `settings_version` создаёт новый job; старый остаётся для аудита.
- `settings_version` по умолчанию фиксируется текущей версией settings на момент
  создания.
- `idempotency_key` хранит explicit key или SHA-256 канонической идентичности.

**Lease/CAS.** Очередь — `json_repair_jobs` со `status='queued'`; отдельная
OpenCode queue не используется.

- `claim_due(limit, lease_seconds, owner)` выбирает queued jobs в порядке
  `created_at, id`, ставит `processing`, `attempts += 1`, `started_at`,
  `lease_owner`, `lease_until` и делает это под `BEGIN IMMEDIATE` с
  CAS-условием `WHERE status='queued'`.
- `attempts` считает claim-ы обработки; детальные шаги хранятся в attempts.
- `complete_job`/`fail_job` требуют `status='processing'`, совпадающего
  `lease_owner` и неистёкший `lease_until`. Иначе — `LEASE_LOST`.
- Успешное завершение очищает lease и заполняет `result_json`, `method`,
  `finished_at`; fail заполняет `error_code`/`error_message`.
- Терминальный job нельзя complete/fail/cancel повторно
  (`JOB_STATE_CONFLICT`).
- `cancel_job` отменяет queued job; processing job — только владелец lease или
  `force=True` для административной отмены.
- `cancel_jobs_for_run(task_run_id/vflow_run_id/point_id)` отменяет все
  non-terminal jobs корреляции; по умолчанию `force=True`.
- `recover_expired_leases` возвращает `processing` c `lease_until <= now` (или
  NULL) в `queued`, очищая lease, но сохраняя `attempts`. Worker, потерявший
  lease, не может завершить job.
- Все SQL-параметры передаются отдельно; транзакции короткие и не содержат
  network/await.

### 17.3. `json_repair_attempts`

Поля: `id`, `job_id` FK → jobs с `ON DELETE CASCADE`, `attempt_no`, `method`,
`provider`, `model`, `request_meta_json`, `validation_error`, `status`
(`succeeded`/`failed`/`cancelled`), `latency_ms`, `token_usage_json`,
timestamps. Уникально `(job_id, attempt_no)`; чтение упорядочено по
`attempt_no`.

`request_meta_json` рекурсивно редактирует secret-shaped ключи (`api_key`,
`authorization`, `password`, `access_token`, ...), но сохраняет не-секретный
`api_key_ref`. `token_usage_json` хранит только счётчики токенов.

## 18. Публичный API M1/M2

```python
from opencode_web.json_processor import (
    RepairMode,
    ProcessorErrorCode,
    ProcessorError,
    ParseResult,
    ParseStrategy,
    ValidationDiagnostic,
    ValidationResult,
    RepairAttempt,
    RepairResult,
    RepairJobStatus,
    RepairMethod,
    RepairSourceType,
    AttemptStatus,
    ProcessorFailurePolicy,
    SchemaDefinitionError,
    JsonProcessorSettings,
    SettingsValidationError,
    parse_json_output,
    validate_schema,
    validate_value,
    is_valid,
    check_content_preserved,
    ensure_content_preserved,
    diff_json_values,
    ContentAllowance,
    ContentOperation,
    placeholder_allowance,
    build_repair_prompt,
    PromptBudget,
    RepairPrompt,
    ERROR_ENVELOPE_KEY,
    is_error_envelope,
)

from opencode_web.json_processor.repository import (
    JsonProcessorRepository,
    JsonProcessorRepositoryError,
    JsonRepairJobNotFound,
    JsonRepairStateConflict,
    JsonRepairLeaseLost,
    JsonRepairIdempotencyConflict,
    SettingsVersionConflict,
    sanitize_metadata,
)
```

## 19. Открытые вопросы для следующих фаз

1. Нужен ли UI-редактор per-request placeholder-ов или достаточно автоматического
   вывода allowlist из Task-схемы? (Глобального allowlist не будет.)
2. Какой provider timeout/retry policy применять поверх `request_timeout_seconds`
   и `max_llm_attempts`?
3. Нужен ли отдельный «repair only» endpoint в admin API или достаточно
   внутренней операции Task/VFlow?
4. Каким образом artifacts storage будет удалять raw output по retention —
   вместе с job или отдельным pass-ом?
5. Нужно ли разрешать `REMOVE` через явный per-request allowlist при
   `additionalProperties: false`, или всегда требовать исправления схемы/ответа?
