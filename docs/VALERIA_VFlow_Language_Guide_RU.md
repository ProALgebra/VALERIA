# VALERIA VFlow — руководство по языку

**Статус:** нормативное практическое руководство по реализованному DSL.
**Дата:** 21 ноября 2026 г.
**Аудитория:** администраторы-авторы пайплайнов, разработчики VFlow, QA и сопровождение.
**Область:** текстовый язык VFlow, его компиляция в Graph IR, контракты данных и правила публикации.

> Это руководство описывает **фактическую реализацию** в `src/opencode_web/vflow/`, а не концептуальный синтаксис из ранних макетов. Если руководство и код расходятся, источником истины является код: `parser.py`, `compiler.py`, `logic.py`, `handlers.py`, `runtime.py`, `policy.py` и `config.py`. Макет `VALERIA_VFlow_mockup_v2.html` относится к UI и не задаёт грамматику.

**Связанные документы**

- [Техническая спецификация VFlow](VALERIA_VFlow_Technical_Specification_RU.md) — модель графа, точки выполнения, Fork/Merge, безопасность.
- [ADR-001…004](VALERIA_VFlow_ADR_RU.md) — принятые архитектурные решения.
- [Эксплуатационная документация](VALERIA_VFlow_Operations_RU.md) — API, БД, Task Gateway, эксплуатация.
- [VFlow Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md) — code-only добавление новых системных блоков разработчиком.
- [План тестирования VFlow](VALERIA_VFlow_QA_Test_Plan_RU.md) — QA-сценарии и приёмочные проверки.
- [Совместимость блоков](VALERIA_VFlow_Block_Compatibility_RU.md) — версии handler-ов и миграции.
- [English version](VALERIA_VFlow_Language_Guide_EN.md).

---

## Содержание

1. [Назначение и концепции](#1-назначение-и-концепции)
2. [Лексический синтаксис](#2-лексический-синтаксис)
3. [Заголовок workflow](#3-заголовок-workflow)
4. [Шаги и вызовы](#4-шаги-и-вызовы)
5. [Цепочки рёбер и порты](#5-цепочки-рёбер-и-порты)
6. [Справочник встроенных блоков](#6-справочник-встроенных-блоков)
7. [Контракты данных и семантика `input_path`](#7-контракты-данных-и-семантика-input_path)
8. [Полные примеры](#8-полные-примеры)
9. [Правила валидации](#9-правила-валидации)
10. [Лимиты и конфигурация](#10-лимиты-и-конфигурация)
11. [Ошибки и наблюдаемость](#11-ошибки-и-наблюдаемость)
12. [Авторский процесс в UI и API](#12-авторский-процесс-в-ui-и-api)
13. [Безопасность и лучшие практики](#13-безопасность-и-лучшие-практики)
14. [Краткая справка](#14-краткая-справка)

---

## 1. Назначение и концепции

### 1.1. Что такое VFlow

VFlow — встроенный в VALERIA модуль персистентных графовых пайплайнов. Пайплайн описывает, как JSON-данные проходят через системные операторы и существующие задачи VALERIA. VFlow не запускает OpenCode напрямую: узел `Task` ставит обычную задачу VALERIA в общую очередь и ждёт её завершения.

Текстовый VFlow — **декларативное представление графа**, а не процедурная программа. В нём нет переменных, циклов `while`/`for`, функций, классов, импортов, вычисляемых выражений и произвольного кода. Повторный проход по графу возможен только как цикл рёбер, ограниченный runtime-бюджетом.

### 1.2. workflow, step, edge, port

| Понятие | Значение |
|---|---|
| **workflow** | Именованное определение графа. Один исходник VFlow содержит одно определение. |
| **step** | Объявление узла графа с уникальным ID. Узел — это вызов встроенного блока (`task.run`, `transform`, `if`, `fork`, `merge`). |
| **edge** | Направленный переход от выходного порта одного узла к входу другого узла или к терминалу `end`. |
| **port** | Именованный выход узла: `out` у обычных узлов, `true`/`false` у `if`, `branch[i]` или `all` у `fork`. |
| **start / end** | Служебные терминалы, а не узлы Registry. `start` имеет ровно одно исходящее ребро; `end` — терминал без исходящих рёбер. |

Порядок объявления `step` не задаёт порядок исполнения. Исполнение определяется только рёбрами, портами и состоянием точек выполнения.

### 1.3. Граф и запуск

- **Pipeline Definition** — логическая сущность: slug, название, описание, аудитория, настройки.
- **Pipeline Version** — неизменяемая после публикации версия Graph IR, исходника DSL, входной схемы, публичной выходной схемы и требуемых handler-версий.
- **Pipeline Run** — запуск конкретной опубликованной версии конкретным пользователем с входным JSON.
- **Execution Point** — персистентная единица выполнения: текущий `node_id`, JSON-данные, статус, `visit_seq`, `attempt`, контекст Fork/Merge, аренда.

Опубликованная версия неизменяема. Новый Run получает зафиксированную версию графа и снимок конфигураций Task. Редактирование или повторная публикация не меняет уже начатые Run.

### 1.4. Несколько точек выполнения

У Run **нет единственного глобального `current_node`**. В один момент времени один Run может иметь несколько независимых точек: одна ждёт Task, другая ждёт Merge, третья готова к переходу. Это ключевое отличие VFlow от последовательного исполнителя.

```text
Pipeline Run #9001 — RUNNING — Version #12
    Point #51: node=review_task   state=WAITING_TASK   task_run=job-710
    Point #52: node=security      state=READY          data={...}
    Point #53: node=combined      state=WAITING_MERGE  fork=fork-411
```

`Fork` создаёт N дочерних точек; `Merge` поглощает их и создаёт одну продолжающую точку. `If` не создаёт новую точку, а перемещает существующую по одному выбранному порту. `Task` не блокирует другие точки и не занимает слот OpenCode во время ожидания.

### 1.5. Чего язык не делает

- Не выполняет `eval`, `exec`, Python, JavaScript, SQL, shell или импорт по имени.
- Не позволяет пользователю или администратору загрузить собственный handler, плагин или скрипт.
- Не даёт доступа к prompt-ам, reasoning, tool-сообщениям или чужим Run.
- Не гарантирует статическое завершение произвольного цикла: runtime ограничивает число переходов и время Run.
- Не является заменойTask-очереди VALERIA: `Task` всегда идёт через существующий Task Gateway.

---

## 2. Лексический синтаксис

### 2.1. Кодировка, пробелы и комментарии

- Исходник — **UTF-8**.
- Пробелы, табуляция и переводы строк — разделители. Отступы не значимы.
- Комментарии начинаются с `//` или `#` и продолжаются до конца строки.
- Комментарии и пробелы внутри JSON-строк не являются комментариями.
- Пустые строки допустимы.

```vflow
// Строковый комментарий
# Альтернативный строковый комментарий
workflow "comments" {
    version: 1;   // точка с запятой необязательна
    input: json
    step mark = transform(operations: [
        {op: "set", path: "$.ok", value: true}  # комментарий после значения
    ])
    start -> mark -> end
}
```

### 2.2. Идентификаторы

Идентификаторы шагов, аргументов и имён вызовов в текущем компиляторе используют ASCII-шаблон:

```text
[A-Za-z_][A-Za-z0-9_]*
```

UTF-8 свободно используется в строковых литералах и комментариях. Для JSON-ключей с не-ASCII символами используйте bracket-нотацию JSONPath: `$['ключ']`. Идентификаторы с Unicode-буквами текущий лексер не принимает; это осознанное ограничение грамматики, а не ошибка кодировки.

Зарезервированные и служебные слова: `workflow`, `step`, `version`, `input`, `start`, `end`, `true`, `false`, `null`. ID шага не должен совпадать с `start` или `end`.

### 2.3. Литералы

| Тип | Примеры | Примечание |
|---|---|---|
| Строка | `"text"`, `"путь"`, `"a\nb"` | JSON-строка с JSON-экранированием. |
| Целое | `0`, `1`, `42`, `-3` | JSON-формат; branch index — только неотрицательный. |
| Число | `1.5`, `2e3`, `-0.25` | JSON-формат, включая экспоненту. |
| Boolean | `true`, `false` | Литералы. |
| Null | `null` | Литерал. |
| Массив | `[1, "a", true]` | Элементы через запятую. |
| Объект | `{op: "set", path: "$.x"}` | Ключ — идентификатор или строка. |

`NaN`, `Infinity`, даты, бинарные данные и Python-объекты литералами не являются. Значения должны быть корректным JSON.

### 2.4. Объекты и массивы

Объект: `{ key: value, "quoted key": value }`. Ключом может быть идентификатор или строка. Массив: `[ value, value ]`. Вложенность произвольная в пределах лимита глубины JSON. Trailing comma не поддерживается — после последнего элемента запятая не нужна и не допускается.

### 2.5. Необязательные точки с запятой

`;` можно ставить после `version`, `input`, объявления `step` и цепочки рёбер. Она также может стоять отдельно между инструкциями. Точка с запятой не обязательна; стиль примеров в этом руководстве использует её для читаемости. Переносы строк не заменяют запятые внутри аргументов и массивов.

### 2.6. Bare-идентификатор как строковая ссылка

В месте, где ожидается строковая ссылка, допускается незакавыченный идентификатор:

```vflow
step split = fork(count: 2, join: combined)
```

`combined` здесь компилируется как строка `"combined"`. Для произвольных значений (task ID, JSONPath, режимы) используйте кавычки. Не используйте bare-идентификатор как «переменную»: переменных в языке нет.

---

## 3. Заголовок workflow

Минимальная структура:

```vflow
workflow "name" {
    version: 1
    input: json

    step mark = transform(operations: [{op: "set", path: "$.ok", value: true}])
    start -> mark -> end
}
```

| Поле | Обязательно | Значение |
|---|---:|---|
| `workflow "name"` | Да | Имя определения — JSON-строка. Используйте стабильное читаемое имя. |
| `version` | Да | Положительное целое (`>= 1`). Это версия **языка/определения**, а не номер публикации в БД. |
| `input` | Да | В текущем контракте — `json`. Парсер принимает идентификатор или строку, но нормативное значение — `json`. |

`version` и `input` могут идти в любом порядке до/после шагов; повторное объявление не является нормальным стилем. После заголовка идут объявления `step` и цепочки рёбер. Пустой workflow без исполняемых шагов отклоняется (`COMPILE_EMPTY_WORKFLOW`).

---

## 4. Шаги и вызовы

### 4.1. Форма объявления

```text
step <id> = <call_name>(<arg>: <value>, ...)
```

- `<id>` — уникальный идентификатор шага в пределах workflow.
- `<call_name>` — имя вызова из Registry. Встроенные: `task.run`, `transform`, `if`, `fork`, `merge`.
- Аргументы разделяются запятыми; каждый аргумент имеет вид `имя: значение`.
- Порядок аргументов не важен.
- Повтор одного аргумента — ошибка парсера (`PARSE_DUPLICATE_ARGUMENT`).
- Неизвестный аргумент — ошибка компилятора (`COMPILE_UNKNOWN_ARGUMENT`).
- Значения — JSON-подобные литералы, объекты, массивы или bare-идентификаторы для строковых ссылок.

```vflow
workflow "shape" {
    version: 1;
    input: json;

    step init = transform(operations: [
        {op: "set", path: "$.ready", value: false}
    ]);
    step decide = if(path: "$.ready", operator: "eq", value: true);

    start -> init -> decide;
    decide.true -> end;
    decide.false -> end;
}
```

### 4.2. Доступные вызовы и версии handler-ов

| DSL-вызов | IR type | Версия | Назначение |
|---|---|---|---|
| `task.run(...)` | `valeria.task` | `1.0` | Поставить существующую задачу VALERIA в очередь и дождаться результата. |
| `transform(...)` | `core.transform` | `1.0` | Детерминированно преобразовать JSON. |
| `if(...)` | `core.if` | `1.0` | Выбрать один из двух портов, не меняя JSON. |
| `fork(...)` | `core.fork` | `1.0` | Атомарно создать N независимых веток. |
| `merge(...)` | `core.merge` | `1.0` | Дождаться всех веток и создать одну продолжающую точку. |

Registry — code-only. Новый системный тип может появиться только с релизом кода VALERIA и регистрацией `NodeSpec`/handler-а. Администратор не добавляет типы блоков через UI. Подробности — в [Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md).

### 4.3. Порядок объявления и исполнения

Компилятор сохраняет узлы в порядке объявления для канонического IR, но runtime выбирает точку по состоянию, а не по порядку массива. Поэтому:

```vflow
step a = transform(...)
step b = transform(...)
start -> b -> a -> end
```

исполнит `b`, затем `a`, несмотря на порядок объявлений.

---

## 5. Цепочки рёбер и порты

### 5.1. Оператор `->`

`a -> b -> c` эквивалентно двум рёбрам: `a -> b` и `b -> c`. Цепочка должна содержать минимум два endpoint-а. Одно и то же ребро нельзя объявить дважды (`COMPILE_DUPLICATE_EDGE`).

```vflow
start -> load -> normalize -> decide;
decide.true -> publish -> end;
decide.false -> reject -> end;
```

### 5.2. start и end

- `start` — единственная точка входа; ровно одно исходящее ребро. Второе ребро из `start` отклоняется (`COMPILE_MULTIPLE_START_EDGES`).
- `end` — терминал. Входящих рёбер может быть несколько; исходящих быть не должно (`COMPILE_EDGE_FROM_END`).
- Ребро в `start` запрещено (`COMPILE_EDGE_TO_START`).
- `start` и `end` не имеют портов: `start.out` и `end.true` недопустимы.
- Каждый достижимый терминальный маршрут должен достигать `end`; висячий `end` без входящих рёбер также является ошибкой.

### 5.3. Порты источника

Порт указывается **только у источника** ребра:

```text
<source>[.<port> | [<index>]] -> <target>
```

| Узел | Выходные порты | Синтаксис |
|---|---|---|
| `task.run` | `out` | `task_step -> next` |
| `transform` | `out` | `transform_step -> next` |
| `merge` | `out` | `merge_step -> next` |
| `if` | `true`, `false` | `decision.true -> ...`, `decision.false -> ...` |
| `fork` | `branch[0]` … `branch[N-1]` или `all` | `split[0] -> ...`, `split -> next` |

У цели ребра не может быть порта: `a -> b.true` запрещено (`COMPILE_TARGET_HAS_PORT`). Если нужен порт, он относится к источнику.

### 5.4. Сокращение `all`

```vflow
step split = fork(count: 3, join: combined)
start -> split
split -> worker -> combined
```

`split -> worker` — сокращение `all`: все N веток начинают с одного узла `worker`. Это **N отдельных посещений** и, если `worker` — `Task`, **N отдельных Task Run**. Нельзя смешивать `all` и явные `branch[i]` у одного Fork.

### 5.5. Fan-out запрещён

У `Task`, `Transform` и `Merge` ровно одно исходящее ребро `out`. Безусловное раздвоение выхода запрещено (`COMPILE_FAN_OUT`). Для параллельности используйте `Fork`. У `If` каждый из портов `true` и `false` должен иметь ровно одно ребро.

---

## 6. Справочник встроенных блоков

### 6.1. Task — запуск существующей задачи VALERIA

```vflow
step review = task.run(
    task_id: "code.review",
    input_path: "$.request",
    result_mode: "strict_json",
    queue_timeout_seconds: 300,
    execution_timeout_seconds: 1800,
    retries: 1
)
```

| Аргумент | Обязателен | Значение |
|---|---:|---|
| `task_id` | Да | ID существующей задачи VALERIA. Проверяется при validate/publish и при создании Run. |
| `input_path` | Нет | Ограниченный JSONPath к фрагменту входного JSON. По умолчанию `$`. |
| `input` | Нет | Исторический alias для `input_path`. Предпочитайте `input_path`; если заданы оба, побеждает `input_path`. |
| `result_mode` | Нет | `auto` (по умолчанию), `strict_json`, `none`. |
| `queue_timeout_seconds` | Нет | Неотрицательное целое: сколько ждать старта задачи. Отсутствие — без отдельного лимита. |
| `execution_timeout_seconds` | Нет | Неотрицательное целое: сколько задача может выполняться после старта. |
| `retries` | Нет | Неотрицательное целое: число **дополнительных** попыток. По умолчанию `0`. UI-схема рекомендует не более 10. |

Неизвестные аргументы отклоняются компилятором. `task_id` должен быть непустой строкой.

**Что происходит:**

1. Runtime выбирает значение по `input_path` из данных точки.
2. Если путь отсутствует — детерминированная ошибка `TASK_INPUT_MISSING`; задача не ставится.
3. Gateway ставит **один** обычный Task Run в существующую очередь с детерминированным `enqueue_key` и снимком Task.
4. Точка переходит в `waiting_task`; ожидание не держит HTTP-запрос или слот OpenCode.
5. После подтверждённого завершения runtime читает только финальный текст ответа и формирует конверт.
6. `strict_json` требует, чтобы весь финальный текст был валидным JSON; иначе Run падает с `TASK_OUTPUT_NOT_JSON`.

**Конверт успешного Task (`result_mode=auto` или `strict_json`):**

```json
{
  "input": {"repository": "example/repo", "pr": 24},
  "task": {
    "task_id": "code.review",
    "run_id": "task-run-701",
    "status": "completed",
    "output": {
      "text": "{\"ok\":true,\"issues\":[]}",
      "json": {"ok": true, "issues": []}
    }
  }
}
```

- `input` — **полный** JSON точки до Task, а не только выбранный `input_path`.
- `task.output.text` — финальный текст ответа (пустая строка, если текста нет).
- `task.output.json` — JSON, если весь текст разобрался; при `auto` и ошибке разбора — `null`; при `none` — `null`.
- `task.run_id` — ID существующего Task Run в очереди VALERIA.
- Prompt-ы, reasoning и tool-сообщения в конверт не входят.

Снимок Task фиксируется при создании Run. Если задача изменилась после старта, enqueue завершается `TASK_CHANGED`; VFlow не подставляет новую версию молча. Новый Run увидит новую версию задачи.

### 6.2. Transform — безопасное преобразование JSON

```vflow
step normalize = transform(
    operations: [
        {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"},
        {op: "set", path: "$.context.ready", value: true},
        {op: "remove", path: "$.task.output.text"}
    ],
    create_missing: false
)
```

| Аргумент | Обязателен | Значение |
|---|---:|---|
| `operations` | Да | Непустой массив объектов операций. |
| `create_missing` | Нет | Boolean, по умолчанию `false`. Разрешает создавать отсутствующие контейнеры. |

**Операции v1:**

| Операция | Аргументы | Действие |
|---|---|---|
| `set` | `path`, `value` | Установить значение по пути. Замена корня `$` допустима. |
| `copy` | `from`, `to` | Скопировать существующее значение. |
| `move` | `from`, `to` | Скопировать и удалить источник. |
| `remove` | `path` | Удалить значение; удаление корня запрещено. |
| `select` | `path` | Заменить текущий корень выбранным фрагментом. |
| `merge_object` | `path`, `object` | Поверхностно слить объект в целевой объект. |
| `append` | `path`, `value` | Добавить значение в конец массива. |
| `rename` | `from`, `to` | Синоним `move` для переименования ключа. |

В `value` операций `set` и `append` можно передать ссылочный операнд:

```json
{"op": "set", "path": "$.copy", "value": {"from_path": "$.source"}}
```

`from_path` читает значение из **текущего промежуточного** JSON и копирует его по значению. Это не eval и не выражение: путь обязан существовать. Для `copy`/`move`/`rename` источник задаётся отдельным аргументом `from`.

**Правила:**

- Операции применяются строго по порядку к промежуточному JSON; каждая операция получает результат предыдущей.
- Входные данные не мутируются: Transform создаёт новую копию.
- Поддерживается только ограниченный JSONPath: `$`, `.field`, `['field']`, `[index]`. Wildcards, фильтры, функции и вычисляемые выражения запрещены.
- `create_missing=false`: отсутствующий ключ, индекс или контейнер — ошибка. `true` разрешает создать отсутствующий объектный ключ, дописать элемент в конец массива и создать промежуточные контейнеры; проверки типов при этом сохраняются.
- Ошибка пути, несовместимый тип, попытка удалить корень или превышение лимита JSON дают детерминированную ошибку узла. Run переходит в `failed` по fail-fast.

### 6.3. If — выбор одного порта без изменения данных

```vflow
step decision = if(
    path: "$.quality.score",
    operator: "ge",
    value: 80,
    missing: "error"
)
```

| Аргумент | Обязателен | Значение |
|---|---:|---|
| `path` | Да | Непустой ограниченный JSONPath. |
| `operator` | Да | Один из `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, `in`, `exists`, `is_null`. |
| `value` | Зависит | Нужен для всех операторов, кроме `exists` и `is_null`. |
| `missing` | Нет | `error` (по умолчанию), `false`, `true` — поведение при отсутствии пути. |

**Семантика операторов:**

| Оператор | Условие |
|---|---|
| `eq` / `ne` | Типизированное равенство/неравенство. `"12"` не равно `12`. |
| `gt` / `ge` / `lt` / `le` | Только два числа или две строки. Разные типы дают `false`; неподдерживаемые типы — ошибку. |
| `contains` | Для строки — подстрока; для массива — типизированное вхождение элемента. |
| `in` | Типизированное вхождение значения в массив `value`. |
| `exists` | `true`, если путь существует (даже если значение `null`). Отсутствие всегда `false`, `missing` не применяется. |
| `is_null` | `true` только если путь существует и значение `null`; отсутствие даёт `false`. |

Для остальных операторов отсутствие пути обрабатывается по `missing`: `error` — ошибка Run, `false`/`true` — соответствующее ветвление.

**Поведение:**

1. If читает поле из **того JSON, который получил**.
2. Вычисляет boolean.
3. Выбирает ровно один порт `true` или `false`.
4. Перемещает **ту же точку** по выбранному ребру.
5. JSON не меняется и не оборачивается в новую структуру; вторая точка не создаётся.

У If при публикации обязаны быть ровно по одному ребру на `true` и `false`. Если обе ветки позже сходятся в один обычный узел, это допустимый join без Merge, потому что активна только одна ветка. Если If находится внутри Fork, **оба** исхода должны иметь маршрут к парному Merge.

### 6.4. Fork — N независимых веток

```vflow
step split = fork(count: 3, join: combined)
```

| Аргумент | Обязателен | Значение |
|---|---:|---|
| `count` | Да | Целое от 2 до 32 (жёсткая граница компилятора). Runtime дополнительно применяет `VFLOW_MAX_FORK_N` и per-pipeline policy. |
| `join` | Да | ID парного узла `merge` в этом же workflow. |
| `mode` | Нет | Не поддерживается у Fork; не путайте с `merge(mode: "all")`. |

**Порты:** ровно `branch[0]` … `branch[count-1]` **или** ровно один `all`. Смешивать режимы нельзя; порт `out` у Fork запрещён.

**Атомарная семантика:**

1. Runtime в одной транзакции создаёт `fork_instance` с `expected_count=N`, снимком входного JSON и родительским `fork_stack`.
2. Последовательно, для `i=0..N-1`, создаёт N дочерних точек с `branch_index=i` и независимой копией входного JSON.
3. Исходная точка помечается `forked` и больше не планируется.
4. Только после коммита дочерние точки становятся доступны планировщику и могут выполняться конкурентно.

Последовательное создание точек не означает последовательное выполнение. Fork не гарантирует физическую одновременность: общая очередь VALERIA и лимиты OpenCode могут запускать ветки по-разному.

**Сокращение `all`:**

```vflow
split -> worker -> combined
```

Все N точек начинают с `worker`; если это Task, создаются N отдельных Task Run. Значения входных данных у веток равны по JSON, но копии независимы.

**Метаданные:** `branch_index`, `fork_instance_id`, `merge_node_id` и `expected_count` хранятся в `fork_stack` точки. Они не добавляются в пользовательский JSON автоматически. Чтобы передать номер ветки в задачу, используйте разные порты `branch[i]` и разные Task-узлы; либо будущий доверенный Transform-оператор.

### 6.5. Merge — строгий барьер wait-all

```vflow
step combined = merge()
```

| Аргумент | Обязателен | Значение |
|---|---:|---|
| `mode` | Нет | Только `all` (значение по умолчанию). Любое другое значение отклоняется. |

**Семантика:**

- Merge обязан быть парным ровно одному Fork; один Merge нельзя переиспользовать для двух Fork.
- Каждая ветка конкретного Fork Instance приносит arrival с уникальным `(fork_instance_id, branch_index)`.
- Merge ждёт **все** индексы `0..N-1`, а не N произвольных событий.
- После последнего arrival один CAS-переход `open -> merged` создаёт **ровно одну** продолжающую точку. N дочерних точек помечаются `consumed`.
- Продолжающая точка наследует `fork_stack` без закрытого кадра.
- Порядок результата — по `branch_index`, а не по времени завершения.
- Дубликат arrival той же ветки поглощается; устаревший arrival закрытого Fork Instance не создаёт вторую точку.

**Результат Merge:**

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

- `source` — данные непосредственно перед соответствующим Fork (снимок в Fork Instance).
- `branches[*].data` — полный JSON, фактически пришедший из ветки.
- Автоматического «умного» слияния полей нет; используйте `Transform` после Merge.
- Если обязательная ветка упала, Run переходит в `failed` по fail-fast. Merge **не** выпускает частичный результат и не поддерживает `first success`.

**Вложенность:** внутренний Merge закрывает только свой Fork и должен сработать раньше внешнего. Прямой вход во внешний Merge с незакрытым внутренним Fork невозможен: валидатор и runtime проверяют верхний кадр `fork_stack`.

---

## 7. Контракты данных и семантика `input_path`

### 7.1. JSON-значение точки

Данные точки — корректное JSON-значение: объект, массив, строка, число, boolean или `null`. Ограничения:

- размер — `VFLOW_MAX_JSON_BYTES` (по умолчанию 1 МиБ);
- глубина — `VFLOW_MAX_JSON_DEPTH` (по умолчанию 64);
- несериализуемые типы, даты без преобразования, бинарные объекты и `NaN`/`Infinity` запрещены.

Проверка выполняется на входе Run, после Transform, после Task, после Merge и при завершении. Нарушение даёт `LIMIT_JSON_BYTES`, `LIMIT_JSON_DEPTH` или `LIMIT_JSON_INVALID`.

### 7.2. Вход Run

Первый узел (`entry`) получает `input` Run без обёртки. Если пользователь отправил:

```json
{"repository": "example/repo", "pr": 24, "request": {"severity": "high"}}
```

то `start -> prepare` передаёт `prepare` именно этот объект.

### 7.3. Task и `input_path`

`input_path` выбирает **фрагмент**, который сериализуется в текст задания для агента. При этом конверт Task сохраняет **полный** вход точки в `input`.

| `input_path` | Что получает задача | Что остаётся в `task.input` |
|---|---|---|
| `$` (по умолчанию) | весь текущий JSON | весь текущий JSON |
| `$.repository` | строка `"example/repo"` | весь объект |
| `$.request` | `{"severity": "high"}` | весь объект |
| `$.missing` | ошибка `TASK_INPUT_MISSING`, задача не ставится | — |

`input_path` — не шаблон и не выражение. Допустимы только `$`, `.field`, `['field']`, `[index]`; wildcard и фильтры запрещены. Путь читается из данных, которые пришли в Task. Если предыдущий Transform заменил корень, путь относится к новому корню.

Пример:

```vflow
step load = task.run(task_id: "repo.load", input_path: "$");
step review = task.run(task_id: "code.review", input_path: "$.request");
step combine = transform(operations: [
    {op: "copy", from: "$.task.output.json.repository", to: "$.context.repository"}
]);
```

### 7.4. Transform и If

- `Transform` заменяет данные результатом последней операции. Если нужен прежний контекст, скопируйте его внутрь результата до `select`/`remove`.
- `If` не меняет данные. Он передаёт тот же JSON в выбранный порт.
- После `Task` данные — конверт Task; после `Merge` — конверт Merge. Учитывайте это в путях.

### 7.5. Fork и Merge

`Fork` передаёт каждой ветке независимую логическую копию входного JSON. Изменения в одной ветке не видны другим. `Merge` собирает полные данные каждой ветки и снимок `source` до Fork; он не смешивает поля автоматически.

### 7.6. Практические правила путей

1. После Task используйте `$.task.output.json...` для структурированного ответа.
2. После Merge используйте `$.branches[i].data...` и `$.source...`.
3. Не рассчитывайте на неявное приведение типов: `"12"` и `12` — разные значения.
4. Проверяйте путь через `POST /api/admin/vflow/preview-if` и `preview-transform` до публикации.
5. Не передавайте в Task весь накопленный конверт, если агенту нужен только фрагмент: задайте `input_path`.

---

## 8. Полные примеры

> ID задач в примерах вымышлены (`code.review`, `quality.score` и т. п.). Перед публикацией создайте соответствующие задачи в штатном разделе задач VALERIA. Примеры не содержат секретов и готовы для проверки компилятором.

### 8.1. Последовательный пайплайн с If

```vflow
workflow "sequential_if" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.review", value: {score: 0, status: "new"}}
    ]);

    step score_task = task.run(
        task_id: "quality.score",
        input_path: "$.review",
        result_mode: "strict_json"
    );

    step extract = transform(operations: [
        {op: "copy", from: "$.task.output.json.score", to: "$.review.score"},
        {op: "set", path: "$.review.status", value: "scored"}
    ]);

    step decide = if(
        path: "$.review.score",
        operator: "ge",
        value: 80,
        missing: "error"
    );

    step accept = task.run(task_id: "quality.accept");
    step reject = task.run(task_id: "quality.reject");

    start -> prepare -> score_task -> extract -> decide;
    decide.true -> accept -> end;
    decide.false -> reject -> end;
}
```

Разбор: `prepare` создаёт `review`; Task получает только `review`; `extract` переносит score из конверта Task; If выбирает ровно одну ветку; обе ветки завершаются в `end`.

### 8.2. Fork из трёх задач и Merge

```vflow
workflow "review_three_ways" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.review.started", value: true}
    ]);

    step split = fork(count: 3, join: combined);

    step code = task.run(task_id: "review.code", result_mode: "strict_json");
    step tests = task.run(task_id: "review.tests", result_mode: "strict_json");
    step security = task.run(task_id: "review.security", result_mode: "strict_json");
    step combined = merge();

    step summarize = transform(operations: [
        {op: "set", path: "$.summary.branch_count", value: 3}
    ]);

    start -> prepare -> split;
    split[0] -> code -> combined;
    split[1] -> tests -> combined;
    split[2] -> security -> combined;
    combined -> summarize -> end;
}
```

Все три ветки получают независимые копии данных после `prepare`. `combined` ждёт индексы 0, 1 и 2; результат `branches` упорядочен по индексу, а не по скорости завершения. Если одна из задач падает, Run становится `failed`, а Merge не выпускает частичный результат.

### 8.3. Вложенные Fork/Merge

```vflow
workflow "nested_fork" {
    version: 1;
    input: json;

    step outer = fork(count: 2, join: outer_join);
    step inner = fork(count: 2, join: inner_join);

    step work_a = task.run(task_id: "work.a");
    step work_b = task.run(task_id: "work.b");
    step work_c = task.run(task_id: "work.c");

    step inner_join = merge();
    step outer_join = merge();

    step finish = transform(operations: [
        {op: "set", path: "$.nested", value: true}
    ]);

    start -> outer;
    outer[0] -> inner;
    outer[1] -> work_c -> outer_join;
    inner[0] -> work_a -> inner_join;
    inner[1] -> work_b -> inner_join;
    inner_join -> outer_join;
    outer_join -> finish -> end;
}
```

Внутренний `inner_join` закрывает Fork `inner` и возвращает точку во внешнюю область; затем `outer_join` закрывает `outer`. Попытка прийти в `outer_join` с незакрытым `inner` была бы отклонена.

### 8.4. Цикл с выходом

```vflow
workflow "retry_until_ready" {
    version: 1;
    input: json;

    step prepare = transform(operations: [
        {op: "set", path: "$.ready", value: false}
    ]);

    step work = task.run(task_id: "job.prepare", result_mode: "strict_json");

    step read = transform(operations: [
        {op: "copy", from: "$.task.output.json.ready", to: "$.ready"}
    ]);

    step check = if(
        path: "$.ready",
        operator: "eq",
        value: true,
        missing: "false"
    );

    step finish = transform(operations: [
        {op: "set", path: "$.status", value: "done"}
    ]);

    start -> prepare -> work -> read -> check;
    check.true -> finish -> end;
    check.false -> work;
}
```

Цикл `work -> read -> check -> work` допустим вне открытых Fork/Merge. Каждый повторный вход в Task создаёт **новый** Task Run с новым `visit_seq`/`attempt`; runtime ограничивает число переходов `VFLOW_MAX_TRANSITIONS` и время `VFLOW_MAX_RUN_SECONDS`. Публикация не гарантирует, что бизнес-условие когда-либо станет истинным, поэтому цикл обязан иметь достижимый выход и осмысленный лимит.

### 8.5. Task со строгим JSON и таймаутами

```vflow
workflow "strict_report" {
    version: 1;
    input: json;

    step run = task.run(
        task_id: "report.generate",
        input_path: "$.request",
        result_mode: "strict_json",
        queue_timeout_seconds: 300,
        execution_timeout_seconds: 1800,
        retries: 1
    );

    step shape = transform(operations: [
        {op: "select", path: "$.task.output.json"},
        {op: "set", path: "$.published", value: true}
    ]);

    start -> run -> shape -> end;
}
```

`queue_timeout_seconds` действует только до старта задачи; `execution_timeout_seconds` — после старта. `retries: 1` даёт одну дополнительную попытку при retryable-ошибке или queue timeout, но не при execution timeout. `strict_json` превращает невалидный ответ в `TASK_OUTPUT_NOT_JSON`. `select` заменяет корень чистым JSON ответа, убирая конверт Task.

---

## 9. Правила валидации

Проверки выполняются на нескольких уровнях: парсер (синтаксис), компилятор (структура и типы), сервис validate/publish (наличие задач) и runtime (состояние, лимиты, идемпотентность).

### 9.1. Синтаксис

- Незакрытая строка/блок workflow, неожиданный символ, неверный JSON-литерал, отсутствие `version` или `input`, дублирующийся аргумент — ошибки с `line`/`column`.
- Объявление `step` без `=` или вызова недопустимо.
- Цепочка должна содержать минимум два endpoint-а.
- Индекс ветки — неотрицательное целое.

### 9.2. Структура графа

1. **Один `start` и один достижимый `end`.** Ровно одно ребро из `start`; хотя бы одно ребро в `end`; `end` достижим из `start`.
2. **Уникальные ID.** Повторный `step` отклоняется (`COMPILE_DUPLICATE_STEP`). `start`/`end` зарезервированы.
3. **Известный тип и версия handler-а.** Неизвестный call name или версия — ошибка (`COMPILE_UNKNOWN_NODE_TYPE`, `COMPILE_UNKNOWN_HANDLER_VERSION`).
4. **Все узлы достижимы из `start`.** Висячие узлы — ошибка (`COMPILE_UNREACHABLE_NODES`), а не предупреждение.
5. **Нет дублирующихся рёбер.** `(source, source_port, target)` уникален.
6. **Цель ребра без порта.** Порт допустим только у источника.

### 9.3. Порты и fan-out

1. `Task`, `Transform`, `Merge` имеют ровно одно исходящее ребро `out`; fan-out запрещён (`COMPILE_FAN_OUT`).
2. `If` имеет ровно одно ребро `true` и ровно одно `false` (`COMPILE_IF_PORT_COUNT`); другие порты запрещены.
3. `Fork` имеет либо полный набор `branch[0..N-1]`, либо ровно один `all` (`COMPILE_FORK_PORTS`); `out` запрещён.
4. `Fork.join` обязан ссылаться на существующий узел `merge` (`COMPILE_FORK_JOIN_NOT_MERGE`).
5. `Merge` обязан быть join-ом ровно одного Fork (`COMPILE_MERGE_UNPAIRED`, `COMPILE_MERGE_REUSED`).

### 9.4. Вложенность Fork/Merge

Валидатор обходит все достижимые пути с явным стеком открытых Fork-областей:

- `end` допустим только при пустом стеке; иначе `COMPILE_FORK_BYPASS_MERGE`.
- Merge должен закрывать **верхний** кадр стека; иначе `COMPILE_MERGE_SCOPE_MISMATCH`.
- Повторный вход в тот же Fork до его Merge запрещён (`COMPILE_FORK_REENTRY`).
- Каждая ветка, включая оба исхода If, должна иметь маршрут к своему Merge (`COMPILE_FORK_BRANCH_UNJOINED`).
- Merge без открытого Fork (`COMPILE_MERGE_WITHOUT_FORK`) и вход в середину чужой области отклоняются.
- Вложенные пары не пересекаются; внутренний Merge закрывается раньше внешнего.

### 9.5. Циклы

- Циклы **вне** открытых Fork/Merge разрешены, если у них есть достижимый выход.
- Циклы внутри ветки Fork разрешены только если каждый путь ветки сохраняет маршрут к парному Merge; иначе публикация отклоняется.
- Повторный вход в Fork после закрытия его Merge создаёт **новый** Fork Instance; старые arrivals не смешиваются с новыми.
- Статически доказать завершение произвольного цикла нельзя. Runtime применяет `VFLOW_MAX_TRANSITIONS` и `VFLOW_MAX_RUN_SECONDS`; превышение даёт `LIMIT_TRANSITIONS` / `LIMIT_RUN_SECONDS`.

### 9.6. Задачи и handler-ы

- На этапе `validate`/`publish` сервис проверяет, что каждая ссылка `task_id` существует (`TASK_NOT_FOUND`).
- При создании Run задачи проверяются повторно; отсутствие даёт `TASK_UNAVAILABLE`.
- Требуемые версии handler-ов должны быть зарегистрированы (`required_handlers`); неизвестная версия делает версию непубликуемой или переводит health в `degraded`.
- Нельзя опубликовать граф, который runtime не сможет исполнить.

### 9.7. Типичные диагностические коды

| Код | Причина |
|---|---|
| `PARSE_*` | Синтаксическая ошибка, незакрытый блок/строка, неверный литерал. |
| `COMPILE_DUPLICATE_STEP` | Повторный ID шага. |
| `COMPILE_UNKNOWN_ARGUMENT` | Лишний аргумент вызова. |
| `COMPILE_INVALID_ARGUMENT` | Неверный тип/значение аргумента. |
| `COMPILE_FAN_OUT` | Несколько исходящих рёбер у обычного узла. |
| `COMPILE_IF_PORT_COUNT` | Не ровно одно ребро `true`/`false`. |
| `COMPILE_FORK_PORTS` | Неполный набор branch-портов или смешение с `all`. |
| `COMPILE_MERGE_UNPAIRED` | Merge без Fork. |
| `COMPILE_FORK_BRANCH_UNJOINED` | Ветка не достигает своего Merge. |
| `COMPILE_FORK_BYPASS_MERGE` | Путь в `end` при открытом Fork. |
| `TASK_NOT_FOUND` | Задача не существует на validate/publish. |

---

## 10. Лимиты и конфигурация

### 10.1. Переменные окружения

Значения читаются при старте приложения. После изменения `.env` нужен перезапуск. Некорректное значение приводит к ошибке запуска.

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `VFLOW_ENABLED` | `true` | Главный feature flag. При `false` маршруты VFlow не регистрируются, фоновые циклы не запускаются. |
| `VFLOW_MAX_FORK_N` | `32` | Максимум `count` для Fork, применяется runtime. |
| `VFLOW_MAX_ACTIVE_POINTS` | `128` | Максимум активных точек Run при создании Fork. |
| `VFLOW_MAX_TOTAL_POINTS` | `1000` | Максимум созданных точек Run. |
| `VFLOW_MAX_TRANSITIONS` | `10000` | Максимум переходов Run. |
| `VFLOW_MAX_JSON_BYTES` | `1048576` (1 МиБ) | Максимальный размер JSON-значения. |
| `VFLOW_MAX_JSON_DEPTH` | `64` | Максимальная глубина JSON. |
| `VFLOW_MAX_RUN_SECONDS` | `86400` | Максимальная длительность Run; проверяется при обработке точек. |
| `VFLOW_RUNS_PER_MINUTE` | `30` | Лимит запусков на пользователя в окне. |
| `VFLOW_RUN_WINDOW_SECONDS` | `60` | Окно rate limit. |
| `VFLOW_RETENTION_DAYS` | `30` | Хранение завершённых Run. |

Пример:

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

### 10.2. Где применяется каждый лимит

| Лимит | Момент проверки | Код ошибки |
|---|---|---|
| `VFLOW_MAX_FORK_N` | `SpawnFork` | `LIMIT_FORK_COUNT` |
| `VFLOW_MAX_ACTIVE_POINTS` | `SpawnFork` | `LIMIT_ACTIVE_POINTS` |
| `VFLOW_MAX_TOTAL_POINTS` | `SpawnFork` | `LIMIT_TOTAL_POINTS` |
| `VFLOW_MAX_TRANSITIONS` | Каждый переход | `LIMIT_TRANSITIONS` |
| `VFLOW_MAX_JSON_BYTES` | Вход Run, выход узла, Merge | `LIMIT_JSON_BYTES` |
| `VFLOW_MAX_JSON_DEPTH` | Вход Run, выход узла, Merge | `LIMIT_JSON_DEPTH` |
| `VFLOW_MAX_RUN_SECONDS` | Обработка точки | `LIMIT_RUN_SECONDS` |
| `queue_timeout_seconds` | Task в очереди | `TASK_QUEUE_TIMEOUT` |
| `execution_timeout_seconds` | Task после старта | `TASK_EXECUTION_TIMEOUT` |

Компилятор принимает `fork count` до 32 независимо от `VFLOW_MAX_FORK_N`. Если env-лимит меньше, граф может сохраниться и опубликоваться, но Run завершится `LIMIT_FORK_COUNT`. Держите значение env и per-pipeline policy согласованными с политикой публикации.

### 10.3. Per-pipeline policy: только ужесточение

Настройки определения хранятся в `settings_json`. Политика может **уменьшать** глобальные лимиты, но не увеличивать их.

```http
PUT /api/admin/pipelines/{definition_id}/settings
Content-Type: application/json
x-csrf-token: <csrf-token>

{
  "settings": {
    "limits": {
      "max_fork_n": 8,
      "max_transitions": 2000,
      "max_json_bytes": 65536
    },
    "cancel_own": false
  }
}
```

Правила:

- Допустимые поля: `limits` и `cancel_own`; неизвестные поля отклоняются.
- В `limits` допустимы только имена из `VFLOW_MAX_*` в нижнем регистре (`max_fork_n`, `max_active_points`, `max_total_points`, `max_transitions`, `max_json_bytes`, `max_json_depth`, `max_run_seconds`).
- Значения — положительные целые; значение больше глобального cap отклоняется.
- Указанные поля переопределяют глобальные; неуказанные наследуются.
- Эффективные лимиты фиксируются в Run при создании (`limits_json`) и не меняются задним числом для уже начатого Run.
- `cancel_own` (по умолчанию `true`) разрешает инициатору отменять свой Run.

### 10.4. Rate limit и retention

- Rate limit действует на `POST /api/pipelines/{identifier}/runs`, считается на пользователя в скользящем окне. При превышении — HTTP `429 VFLOW_RATE_LIMITED` с `Retry-After`.
- Rate limiter process-local; при нескольких процессах/репликах он не является общим.
- Retention удаляет только завершённые Run (`succeeded`, `failed`, `cancelled`) старше `VFLOW_RETENTION_DAYS`. Определения, версии, активные Run и аудит не удаляются.
- Фоновый retention запускается при старте и далее раз в 24 часа. Перед первым запуском на большой БД сделайте backup и dry-run.

---

## 11. Ошибки и наблюдаемость

### 11.1. Диагностика компиляции

Ошибки парсера и компилятора содержат `code`, `message`, `line`, `column`, `node_id`, `edge_id` (если применимо). API возвращает их в конверте:

```json
{
  "error": {
    "code": "VFLOW_VALIDATION_ERROR",
    "message": "Workflow validation failed",
    "details": {
      "diagnostics": [
        {
          "code": "COMPILE_FORK_BRANCH_UNJOINED",
          "message": "Branch 0 of Fork 'split' has no route to Merge 'combined'",
          "line": 12,
          "column": 5,
          "node_id": "split",
          "edge_id": null
        }
      ]
    },
    "request_id": "req-or-uuid"
  }
}
```

`POST /api/admin/pipelines/{id}/validate` возвращает `200` с `{"valid": false, "diagnostics": [...]}`; `compile` и `publish` при ошибках дают `422 VFLOW_VALIDATION_ERROR`.

### 11.2. Runtime-ошибки

При неустранимой ошибке Run переходит в `failed`. Активные точки и открытые Fork Instance переводятся в `cancelled`, незавершённые Task links — в `failed`, пишется событие `RUN_FAILED`.

Публичный статус Run содержит только безопасные поля:

```json
{
  "status": "failed",
  "error": {
    "code": "TASK_OUTPUT_NOT_JSON",
    "message": "Task output is not valid JSON"
  }
}
```

Внутренний traceback, prompt-ы и промежуточный JSON пользователю не выдаются. Администратор видит точки и события через `GET /api/admin/vflow/runs/{run_id}`.

### 11.3. Режимы результата Task

| `result_mode` | Поведение | `task.output.json` |
|---|---|---|
| `auto` | Пытается разобрать весь текст как JSON; ошибка разбора не валит Run. | Объект/массив/число и т. п. или `null`. |
| `strict_json` | Требует валидный JSON; иначе `TASK_OUTPUT_NOT_JSON`. | Распарсенное значение. |
| `none` | Не разбирает JSON; текст сохраняется. | `null`. |

`strict_json` не извлекает JSON из markdown-блока или произвольного текста: разбирается весь финальный ответ. `none` полезен, когда downstream-узлу нужен только текст.

### 11.4. Повторы и таймауты Task

- `retries` — число **дополнительных** попыток. Каждая попытка — новый Task Run, новый `attempt`, новый `vflow_task_links`.
- `queue_timeout_seconds` применяется, пока задача в `queued` и ещё не стартовала. При превышении задача отменяется; если `attempt <= retries`, создаётся новая попытка, иначе Run падает `TASK_QUEUE_TIMEOUT`.
- `execution_timeout_seconds` применяется после старта. Задача отменяется, Run падает `TASK_EXECUTION_TIMEOUT`; автоматического повтора нет, чтобы не повторять side effects.
- `TASK_CHANGED`, `TASK_UNAVAILABLE`, `TASK_CANCELLED` не ретраятся.
- Ошибка сети при опросе задачи не равна ошибке задачи: ожидание продолжается с backoff; новая задача не создаётся.

### 11.5. Отмена

- Инициатор может отменить свой Run, если `cancel_own` не запрещён; администратор может отменить любой Run.
- Отмена переводит Run в `cancelled`, активные точки и открытые Fork Instance — в `cancelled`; новые Task не ставятся.
- Текущая реализация **не вызывает** `gateway.cancel` для уже поставленных Task. Внешняя задача может продолжить работу и side effects; для остановки агента используйте штатные средства очереди/админки VALERIA.
- Отмена идемпотентна: повторный запрос для терминального Run не создаёт новых переходов.

### 11.6. События, health и metrics

Run-события: `RUN_STARTED`, `POINT_ADVANCED`, `TASK_ENQUEUED`, `TASK_RETRY`, `TASK_QUEUE_TIMEOUT`, `TASK_EXECUTION_TIMEOUT`, `FORK_SPAWNED`, `MERGE_ARRIVED`, `MERGE_COMPLETED`, `MERGE_DUPLICATE_ARRIVAL`, `MERGE_STALE_ARRIVAL`, `MERGE_RECOVERED`, `RUN_SUCCEEDED`, `RUN_FAILED`, `RUN_CANCELLED`.

События содержат структурные ID, порт, branch index, count, но не input Run и не prompt-ы. Payload проходит redaction. Административные точки (`data`) и события доступны только через admin API.

| Endpoint | Что показывает |
|---|---|
| `GET /api/admin/vflow/health` | Состояние БД, handler-версий, активных Run, leases, outbox. |
| `GET /api/admin/vflow/metrics` | Агрегаты Run/points/outbox и top ошибок по кодам. |
| `GET /api/admin/vflow/retention` | Параметры retention и кандидаты на удаление. |
| `GET /api/admin/vflow/runs/{id}` | Точки, `fork_stack`, `data`, события, task snapshots. |

`health = degraded` обычно означает, что активный Run ссылается на отсутствующую handler-версию или схема БД неполная. Startup-проверка логирует warning, но не блокирует запуск приложения.

---

## 12. Авторский процесс в UI и API

### 12.1. Создание задач

VFlow не создаёт определения Task. Сначала создайте и настройте задачу в **штатном разделе задач VALERIA** (prompt, модель, очередь, workspace-политика). Затем используйте её `task_id` в `task.run`. Администратор может выбрать в палитре VFlow и неопубликованную задачу: публикация пайплайна делегирует право запуска в его контексте. Пользователь не получает прямого доступа к задаче.

### 12.2. Создание определения

В административном разделе «Редактор пайплайнов» (`#/admin-pipelines`, `#/admin-pipeline-editor`) администратор создаёт определение: slug, title, description. API:

```http
POST /api/admin/pipelines
Content-Type: application/json
x-csrf-token: <csrf-token>

{"slug": "review-changes", "title": "Review changes", "description": "Three-way review"}
```

### 12.3. Граф или код

- Вкладка «Граф» редактирует канонический Graph IR: узлы, рёбра, порты, позиции.
- Вкладка «VFlow-код» редактирует DSL.
- Переключение граф → код генерирует канонический DSL; код → граф возможно только после успешной компиляции.
- Если при сохранении переданы и `source`, и `graph`, их канонические графы обязаны совпадать; иначе `400`.
- Комментарии, пробелы и форматирование при round-trip могут нормализоваться; семантика должна сохраняться.
- Исходник сохраняется в черновике. При ошибке парсинга предыдущий валидный IR не перезаписывается.

### 12.4. Проверка и публикация

1. **Validate** — `POST /api/admin/pipelines/{id}/validate`: синтаксис, структура, Fork/Merge, handler-версии, наличие задач.
2. **Publish** — `POST /api/admin/pipelines/{id}/publish`: только для валидного черновика; создаёт неизменяемую версию и делает её опубликованной.
3. **Rollback** — `POST /api/admin/pipelines/{id}/rollback` с `version_id`: возвращает прежнюю опубликованную версию.
4. **Archive** — прекращает новые запуски, но не отменяет активные Run.

Черновик обновляется через `PUT /api/admin/pipelines/{id}/draft`. Если черновик уже существует, обязателен `If-Match: "<etag>"`; иначе `412 VFLOW_PRECONDITION_FAILED`. Это защищает от перезаписи параллельных правок.

### 12.5. Аудитория, схемы и политика

- **Аудитория:** `{"mode": "all"}` или `{"mode": "selected", "user_ids": ["..."]}`. Скрытый пайплайн для пользователя выглядит как `404`; аудитория проверяется сервером при каталоге и создании Run.
- **`input_schema`:** безопасное подмножество JSON Schema для формы и серверной проверки входа. Поддерживаются `type` (`object`, `array`, `string`, `boolean`, `integer`, `number`, `null`), `enum`, `required`, `properties`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern` (Python `re.search`), `minimum`, `maximum`. `additionalProperties`, `format`, `oneOf`, `anyOf`, `$ref` игнорируются.
- **`public_output_schema`:** allowlist-проекция публичного результата. Пустая схема или `null` означает status-only. `GET /api/pipeline-runs/{id}/result` отдаёт только успешный Run и только разрешённые поля. Это не полная валидация, а безопасная проекция.
- **`settings`:** per-pipeline policy, только ужесточение лимитов и `cancel_own` (см. раздел 10.3).

### 12.6. Запуск

Пользователь открывает `#/pipelines`, выбирает опубликованный доступный пайплайн, заполняет форму по `input_schema` и запускает:

```http
POST /api/pipelines/review-changes/runs
Content-Type: application/json
x-csrf-token: <csrf-token>
Idempotency-Key: <uuid>

{"input": {"repository": "example/repo", "pr": 24}}
```

Ответ `202` содержит `id`, `status`, `pipeline` и `progress`. Повтор с тем же `Idempotency-Key` и тем же входом возвращает существующий Run; тот же ключ с другим входом даёт `409 IDEMPOTENCY_CONFLICT`. `workspace_id` берётся из профиля пользователя, а не из входного JSON.

### 12.7. Основные API-маршруты

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/api/pipelines` | Каталог доступных опубликованных пайплайнов. |
| `GET` | `/api/pipelines/{identifier}` | Публичные метаданные + `input_schema`. |
| `POST` | `/api/pipelines/{identifier}/runs` | Создать Run (`202`). |
| `GET` | `/api/pipeline-runs` | Своя история. |
| `GET` | `/api/pipeline-runs/{run_id}` | Свой Run: статус, прогресс, безопасная ошибка. |
| `GET` | `/api/pipeline-runs/{run_id}/result` | Публичный результат по allowlist-схеме. |
| `POST` | `/api/pipeline-runs/{run_id}/cancel` | Отменить свой Run. |
| `GET` | `/api/admin/pipelines` | Все определения. |
| `POST` | `/api/admin/pipelines` | Создать определение. |
| `PUT` | `/api/admin/pipelines/{id}/draft` | Сохранить черновик (`If-Match`). |
| `POST` | `/api/admin/pipelines/{id}/validate` | Проверить. |
| `POST` | `/api/admin/pipelines/{id}/publish` | Опубликовать версию. |
| `POST` | `/api/admin/pipelines/{id}/rollback` | Откатить публикацию. |
| `PUT` | `/api/admin/pipelines/{id}/audience` | Задать аудиторию. |
| `PUT` | `/api/admin/pipelines/{id}/settings` | Задать policy. |
| `GET` | `/api/admin/vflow/block-registry` | Типы блоков и схемы. |
| `GET` | `/api/admin/vflow/tasks-catalog` | Задачи VALERIA для палитры. |
| `POST` | `/api/admin/vflow/compile` | DSL → IR + диагностика. |
| `POST` | `/api/admin/vflow/format` | IR → канонический DSL. |
| `POST` | `/api/admin/vflow/preview-transform` | Предпросмотр Transform без Task Run. |
| `POST` | `/api/admin/vflow/preview-if` | Предпросмотр If (`result`, `port`). |

---

## 13. Безопасность и лучшие практики

### 13.1. Разделение ролей

- **Администратор** создаёт задачи, определения, черновики, проверяет, публикует, настраивает аудиторию/схемы/policy и видит диагностику.
- **Пользователь** (`user`, `tasks_only` при наличии доступа) видит только опубликованные доступные пайплайны, запускает их, смотрит свои Run и безопасный прогресс.
- Пользователь не видит Graph IR, DSL, `node_id`, `task_id`, `task_run_id`, `fork_stack`, промежуточный JSON и prompt-ы.
- Все проверки выполняются сервером; скрытие кнопки в UI не является защитой.

### 13.2. Ограничения языка

- Нет `eval`, `exec`, `import`, shell, SQL, динамических вызовов и пользовательских функций.
- JSONPath — строго ограниченное подмножество над JSON-данными; ключи не являются атрибутами Python/JS.
- `Transform` работает только с JSON и не имеет сетевых/файловых side effects.
- Новые системные блоки добавляет **только разработчик VALERIA** через код, Registry, тесты и деплой. Администратор не загружает плагины и не исполняет код через веб-форму.

### 13.3. Task и workspace

- `Task` ставится через существующую очередь от имени инициатора Run и в его разрешённом workspace.
- Публикация версии делегирует право выполнить конкретные Task в контексте пайплайна; пользователь не получает прямого доступа к скрытой задаче.
- Снимок Task фиксирует версию на старте Run; изменение задачи после старта даёт `TASK_CHANGED`.
- Workspace не является полноценной песочницей. Несколько веток Fork могут одновременно менять одни файлы; предупреждайте авторов о возможных конфликтах и используйте изоляцию/очередь VALERIA.

### 13.4. Данные, результат и секреты

- По умолчанию публичный результат — только статус. Включайте `public_output_schema` осознанно и только как allowlist.
- Не выдавайте пользователям промежуточный JSON, prompt-ы, reasoning, tool-сообщения и task snapshots.
- Не помещайте токены, пароли, ключи и персональные данные в `input`, `config`, `task_id`, `public_output_schema`, события, audit или документацию.
- Task snapshots содержат только метаданные (`task_id`, title, updated_at, agent/provider/model, queue_id), без prompt/content.
- Административная диагностика точек может содержать чувствительный JSON; ограничьте доступ к admin API и backup БД.
- Для документации и тестов используйте вымышленные ID, `example/repo` и placeholder-токены.

### 13.5. Практические рекомендации автора

1. Сначала создайте и проверьте Task в штатном разделе.
2. Начинайте с последовательного графа; добавляйте Fork только для реально независимых работ.
3. У каждой ветки Fork должен быть явный маршрут к парному Merge, включая оба исхода If.
4. Не делайте fan-out обычного узла; используйте Fork.
5. Проверяйте `input_path` и пути Transform на реальном входе через preview.
6. Для структурированных ответов используйте `strict_json`; для свободного текста — `none`.
7. Задавайте queue/execution timeouts и retries осознанно; execution timeout не ретраится.
8. Оставляйте цикл с достижимым выходом и рассчитывайте на transition limit.
9. Публикуйте новую версию вместо изменения активной; active Run закреплён за старой версией.
10. Сначала проверяйте аудиторию и публичную схему на тестовом пользователе.

---

## 14. Краткая справка

### 14.1. Вызовы узлов и порты

| DSL-вызов | IR type | Вход | Выходы | Обязательные аргументы | Основные необязательные |
|---|---|---|---|---|---|
| `task.run(...)` | `valeria.task@1.0` | `in` | `out` | `task_id` | `input_path`, `input`, `result_mode`, `queue_timeout_seconds`, `execution_timeout_seconds`, `retries` |
| `transform(...)` | `core.transform@1.0` | `in` | `out` | `operations` | `create_missing` |
| `if(...)` | `core.if@1.0` | `in` | `true`, `false` | `path`, `operator` | `value`, `missing` |
| `fork(...)` | `core.fork@1.0` | `in` | `branch[0..N-1]` или `all` | `count`, `join` | — |
| `merge(...)` | `core.merge@1.0` | `in` (N рёбер) | `out` | — | `mode` (только `all`) |

### 14.2. Синтаксис рёбер

| Запись | Значение |
|---|---|
| `a -> b -> c` | `a -> b`, `b -> c`. |
| `start -> a` | Единственный вход Run — узел `a`. |
| `a -> end` | Завершить точку на терминале. |
| `decision.true -> a` | Переход по true-порту If. |
| `decision.false -> b` | Переход по false-порту If. |
| `split[0] -> a` | Явная ветка Fork с индексом 0. |
| `split -> a` | Сокращение `all`: все ветки идут в `a`. |
| `a.true -> b` | Ошибка, если `a` — не If. |
| `a -> b.true` | Ошибка: порт у цели запрещён. |

### 14.3. Быстрые контракты

| После узла | Форма данных |
|---|---|
| `Task` | `{"input": <полный вход>, "task": {"task_id", "run_id", "status", "output": {"text", "json"}}}` |
| `Transform` | Результат последней операции (новая копия). |
| `If` | Тот же JSON без изменений. |
| `Fork` | N независимых копий входного JSON. |
| `Merge` | `{"source": <вход Fork>, "branches": [{"index": i, "data": <данные ветки>}]}` |

### 14.4. Минимальный рабочий каркас

```vflow
workflow "hello" {
    version: 1;
    input: json;

    step echo = transform(operations: [
        {op: "set", path: "$.ok", value: true}
    ]);

    start -> echo -> end;
}
```

---

## Ссылки

- [Техническая спецификация VFlow](VALERIA_VFlow_Technical_Specification_RU.md)
- [ADR-001…004](VALERIA_VFlow_ADR_RU.md)
- [Эксплуатационная документация VFlow](VALERIA_VFlow_Operations_RU.md)
- [VFlow Extension SDK](VALERIA_VFlow_Extension_SDK_RU.md)
- [План тестирования VFlow](VALERIA_VFlow_QA_Test_Plan_RU.md)
- [Совместимость блоков](VALERIA_VFlow_Block_Compatibility_RU.md)
- [Implementation Backlog](VALERIA_VFlow_Implementation_Backlog_RU.md)
- [Execution Plan](VALERIA_VFlow_Execution_Plan_RU.md)
- [`.env.example`](../.env.example)
- [English version](VALERIA_VFlow_Language_Guide_EN.md)

Исходники, задающие нормативное поведение:

- `src/opencode_web/vflow/parser.py` — лексер и парсер.
- `src/opencode_web/vflow/compiler.py` — компиляция и структурная валидация.
- `src/opencode_web/vflow/logic.py` — Transform и If.
- `src/opencode_web/vflow/handlers.py` — планирование действий Task/Transform/If/Fork/Merge.
- `src/opencode_web/vflow/runtime.py` — точки, переходы, Fork/Merge, Task, лимиты.
- `src/opencode_web/vflow/policy.py`, `config.py`, `limits.py` — policy и лимиты.
- `src/opencode_web/vflow/service.py`, `api.py` — validate/publish, аудитория, схемы, HTTP API.
