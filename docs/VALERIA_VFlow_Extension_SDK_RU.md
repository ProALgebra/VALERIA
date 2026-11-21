# VFlow Extension SDK (RU)

**Статус:** code-only SDK для разработчиков VALERIA
**Аудитория:** разработчики, сопровождающие backend VFlow и системные блоки
**Связанные документы:** `docs/VALERIA_VFlow_Block_Compatibility_RU.md`, `docs/VALERIA_VFlow_Technical_Specification_RU.md` (§14), `docs/VALERIA_VFlow_ADR_RU.md` (ADR-004)

## 1. Когда использовать SDK

SDK нужен, когда новый системный оператор нельзя выразить существующими блоками `core.transform`, `core.if`, `core.fork`, `core.merge` или задачей `valeria.task`, но его поведение детерминировано, безопасно и должно выполняться самим VFlow.

Подходящие случаи:

- детерминированная обработка JSON без внешних side effects;
- новый управляющий оператор с собственными портами и строгой валидацией конфигурации;
- системная интеграция, которой нужны явно объявленные capabilities и отдельный security review;
- внутренний блок, поставляемый вместе с VALERIA и доступный администратору после деплоя.

SDK **не** предназначен для:

- загрузки кода пользователем, администратором или через веб-форму;
- запуска произвольного Python/JavaScript, `eval`, `exec`, shell, SQL или импорта по имени;
- обхода runtime, прав, workspace-политик, лимитов и аудита;
- замены `valeria.task`, если задача уже решает задачу через обычную очередь.

## 2. Границы безопасности

1. **Только доверенный код из поставки.** Новый блок — это Python-модуль в репозитории VALERIA, прошедший code review и тесты. Динамического discovery, пользовательских плагинов и hot-reload нет.
2. **Handler — чистый планировщик.** Он получает неизменяемый `PlanContext` (узел, точка, граф, безопасный статус задачи) и возвращает один типизированный `Action`. Он не получает `Database`, gateway, HTTP-клиент, filesystem, shell или ORM-сессию.
3. **Persistence принадлежит runtime.** Создание/удаление точек, задачи, fork/merge, лимиты, сериализация JSON, авторизация и транзакции выполняются общим `VFlowRuntime` после проверки Action.
4. **Никакого пользовательского кода.** Запрещены `eval`, `exec`, `compile`, импорт по строке, вызовы shell, динамический SQL и загрузка JS/Python из UI. Новые блоки не должны содержать такие конструкции даже внутри handler-а.
5. **Capabilities — декларация, а не sandbox.** Они фиксируют минимально необходимые side effects и помогают ревьюить изменения. Блок с `external_io`, `secrets` или `workspace` требует отдельного аудита; блок без этих capability не должен получать соответствующий доступ.
6. **Секреты не попадают в граф.** Конфигурация узла и IR хранятся в БД и доступны администратору. Токены, пароли и ключи нельзя записывать в `config`, `ui` или output; для них нужен отдельный механизм с capability `secrets`.
7. **Неизвестный тип/версия — ошибка.** Компилятор и runtime не подменяют отсутствующий handler заглушкой или pass-through.

## 3. Состав SDK

### 3.1. `NodeSpec`

`NodeSpec` — доверенное описание контракта блока. Оно попадает в Registry и в манифест для schema-driven UI.

| Поле | Назначение |
|---|---|
| `type` | Стабильное namespace-имя, например `custom.echo` |
| `version` | Версия контракта, например `1.0` |
| `title` | Название в палитре |
| `category` | Категория палитры (`operator`, `task`, `custom`, …) |
| `description` | Краткое описание |
| `inputs` / `outputs` | Имена портов |
| `config_schema` | JSON Schema конфигурации для generic inspector и ориентир для проверки; handler обязан безопасно отклонять некорректный config |
| `call_names` | Имена DSL-вызовов, например `("custom.echo", "echo")` |
| `capabilities` | Явные capability-токены |

`NodeSpec.__post_init__` проверяет capabilities: неизвестный или повторяющийся токен отклоняется; `pure` нельзя комбинировать с side-effecting capability.

### 3.2. `NodeHandler`

Handler реализует протокол:

```python
from typing import ClassVar

from opencode_web.vflow.actions import Action
from opencode_web.vflow.handlers import PlanContext


class MyHandler:
    node_type: ClassVar[str] = "custom.my_block"
    node_version: ClassVar[str] = "1.0"
    capabilities: ClassVar[tuple[str, ...]] = ("pure", "deterministic")

    def plan(self, context: PlanContext) -> Action:
        ...
```

`register_block` проверяет совпадение `node_type`, `node_version` (если объявлена) и capabilities с `NodeSpec` до изменения Registry. `VFlowRuntime` дополнительно сверяет объявленные handler-ом capabilities с Registry при создании, если spec доступен.

### 3.3. Runtime Actions

Handler возвращает только один из типов `Action`:

| Action | Смысл |
|---|---|
| `Advance(port, data)` | Передать данные по выходному порту |
| `EnqueueTask(...)` | Поставить обычную задачу VALERIA (только для встроенного Task-адаптера) |
| `Suspend(kind, wake_condition)` | Долговечное ожидание; поддерживаемые kinds определяет runtime |
| `SpawnFork(...)` / `ArriveMerge(...)` | Ветвление/барьер |
| `Finish(data)` | Завершить Run |
| `Fail(code, message)` | Безопасная ошибка выполнения |

Пример блока не должен напрямую писать в БД или вызывать gateway. Любая такая интеграция — отдельный доверенный системный блок и отдельный runtime-контракт.

## 4. Пример: `custom.echo@1.0`

Пример находится в `src/opencode_web/vflow/blocks/example.py`. Это немедленный transform-like блок:

- без `tag` возвращает вход без изменений (глубокая копия);
- с `tag` возвращает `{"tag": "<tag>", "value": <input>}`;
- не имеет доступа к БД, gateway, сети, filesystem или секретам;
- объявляет `capabilities=("pure", "deterministic")`.

Сокращённый контракт:

```python
from opencode_web.vflow.actions import Advance, Fail
from opencode_web.vflow.capabilities import Capability
from opencode_web.vflow.registry import NodeSpec

ECHO_SPEC = NodeSpec(
    type="custom.echo",
    version="1.0",
    title="Echo (example)",
    category="custom",
    description="Безопасный пример SDK",
    inputs=("in",),
    outputs=("out",),
    config_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tag": {"type": "string", "maxLength": 200, "default": ""},
        },
    },
    capabilities=(Capability.PURE, Capability.DETERMINISTIC),
    call_names=("custom.echo", "echo"),
)
```

```python
import copy
from typing import ClassVar

from opencode_web.vflow.actions import Advance, Fail
from opencode_web.vflow.handlers import PlanContext


class EchoHandler:
    node_type: ClassVar[str] = "custom.echo"
    node_version: ClassVar[str] = "1.0"
    capabilities: ClassVar[tuple[str, ...]] = ECHO_SPEC.capabilities

    def plan(self, context: PlanContext) -> Advance | Fail:
        tag = context.node.config.get("tag", "")
        if not isinstance(tag, str) or len(tag) > 200:
            return Fail("ECHO_INVALID_TAG", "Echo tag must be a string")

        data = copy.deepcopy(context.point.data)
        if tag:
            data = {"tag": tag, "value": data}
        return Advance(port="out", data=data)
```

DSL-вызовы:

```vflow
step copy = custom.echo(tag: "sdk");
start -> copy -> end
```

или алиас:

```vflow
step copy = echo();
start -> copy -> end
```

## 5. Регистрация в коде

### 5.1. Ручная регистрация на своём Registry

```python
from opencode_web.vflow.blocks.example import (
    ECHO_SPEC,
    EchoHandler,
    register_example_block,
)
from opencode_web.vflow.handlers import DEFAULT_HANDLERS
from opencode_web.vflow.registry import NodeRegistry
from opencode_web.vflow.sdk import register_block

registry = NodeRegistry()            # встроенные блоки
handlers = dict(DEFAULT_HANDLERS)    # встроенные handler-ы

register_example_block(registry, handlers)
assert registry.get("custom.echo", "1.0") is ECHO_SPEC
assert handlers["custom.echo"].node_type == "custom.echo"

# Эквивалентная явная форма:
# register_block(registry, handlers, ECHO_SPEC, EchoHandler())
```

`register_block` атомарен: сначала проверяются тип, версия, capabilities и занятость handler-таблицы; только затем изменяются Registry и mapping. Повторная регистрация отклоняется.

### 5.2. Демонстрационная композиция

```python
from opencode_web.vflow.blocks.example import (
    build_example_handlers,
    build_example_registry,
)

registry = build_example_registry()   # builtins + custom.echo
handlers = build_example_handlers()   # builtins + EchoHandler
```

`EXAMPLE_REGISTRY`/`EXAMPLE_HANDLERS` — готовые демо/тестовые композиции; `DEMO_REGISTRY`/`DEMO_HANDLERS` — их алиасы. `DEFAULT_REGISTRY` намеренно **не** содержит `custom.echo`: доступность блока в production — отдельное решение деплоя.

### 5.3. Подключение к сервису и runtime

```python
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

service = VFlowService(database, registry=registry)
runtime = VFlowRuntime(database, registry=registry, handlers=handlers)
```

В работающем HTTP-приложении Registry и handlers должны передаваться в точке сборки приложения (тот же registry — в `VFlowService` для компиляции/манифеста и в `VFlowRuntime` для выполнения). Без этой явной проводки блок не появится в production-палитре, даже если модуль импортирован.

### 5.4. Чего нельзя делать

- сканировать каталоги и импортировать модули по имени из пользовательского ввода;
- регистрировать handler по запросу API;
- принимать Python/JS-код через `config_schema`;
- выполнять SQL/shell из handler-а;
- добавлять блок в `DEFAULT_REGISTRY` без code review, тестов и решения о деплое.

## 6. Версионирование и совместимость

- `type` стабилен; `version` меняется при изменении семантики, конфигурации или портов.
- Узел IR хранит точную `type_version`; публикация фиксирует `required_handlers_json` и `graph_hash`.
- Новый Run использует IR опубликованной версии; активный Run не переключается на новую реализацию.
- Нельзя менять смысл уже выпущенного `@1.0`; для нового поведения нужен `@1.1`/`@2.0`.
- Старый handler остаётся в деплое, пока есть активные Run/публикации на его версии.
- Удаление типа с зависимостями блокируется или требует явной миграционной процедуры.
- Полная политика: `docs/VALERIA_VFlow_Block_Compatibility_RU.md`.

## 7. Capabilities

Поддерживаемые токены (`opencode_web.vflow.capabilities.Capability`):

| Capability | Значение |
|---|---|
| `pure` | Нет side effects; читается только immutable snapshot |
| `deterministic` | Одинаковые input/config дают одинаковый output |
| `external_io` | Сеть, filesystem, БД, gateway или очередь |
| `secrets` | Чтение секретов/токенов |
| `workspace` | Делегированный доступ к workspace VALERIA |
| `queue` | Постановка в очередь задач VALERIA (встроенный Task) |

Правила:

- неизвестные и повторяющиеся токены отклоняются при создании `NodeSpec`;
- `pure` нельзя комбинировать с `external_io`, `secrets`, `workspace` или `queue`;
- `deterministic` можно комбинировать с `pure`;
- handler может объявить `capabilities`; при регистрации они должны точно совпадать со spec;
- capabilities не заменяют проверку кода: они лишь делают контракт явным.

Текущая матрица встроенных блоков:

| Блок | Capabilities |
|---|---|
| `core.transform@1.0` | `pure`, `deterministic` |
| `core.if@1.0` | `pure`, `deterministic` |
| `core.fork@1.0` | `pure`, `deterministic` |
| `core.merge@1.0` | `pure`, `deterministic` |
| `valeria.task@1.0` | `queue`, `external_io`, `workspace` |
| `custom.echo@1.0` | `pure`, `deterministic` |

## 8. Schema-driven UI

Общий UI не требует правок для нового стандартного блока. Манифест `GET /api/admin/vflow/block-registry` возвращает для каждого `NodeSpec`:

```json
{
  "type": "custom.echo",
  "version": "1.0",
  "title": "Echo (example)",
  "category": "custom",
  "description": "Безопасный пример SDK",
  "inputs": ["in"],
  "outputs": ["out"],
  "config_schema": {
    "type": "object",
    "properties": {
      "tag": {"type": "string", "maxLength": 200, "default": ""}
    }
  },
  "capabilities": ["pure", "deterministic"],
  "call_names": ["custom.echo", "echo"]
}
```

Палитра берёт `type`/`title`/`description`, inspector строит форму по `config_schema` (string, number, boolean, enum, array, object). Если стандартной формы недостаточно, разработчик может добавить **доверенный frontend-компонент в поставку VALERIA**, но не загружать сторонний JS через веб-форму. Для примера `custom.echo` достаточно generic-формы.

Требования к схеме:

- `type: "object"` на верхнем уровне;
- `additionalProperties: false` для новых блоков;
- явные `required`, `default`, `minimum`/`maximum`, `maxLength`, `enum`;
- никаких секретов в `default`/`description`;
- схема должна быть JSON-сериализуемой.

## 9. Тестирование

Минимальный набор для нового блока:

1. Регистрация: `register_block`/`register_*`, разрешение call names, отказ при дубликатах и drift capabilities.
2. Компиляция DSL и IR: `compile_source`/`compile_graph`, `required_handlers`.
3. Handler: ожидаемый `Action`, отсутствие мутации входа, безопасный `Fail` на неверной конфигурации.
4. Manifest: наличие `type/version/title/category/config_schema/call_names/capabilities`.
5. Demo Run: публикация версии и выполнение через `VFlowRuntime` с тем же registry/handlers.

Пример targeted-проверки:

```bash
/tmp/vflow-venv/bin/ruff check src/opencode_web/vflow tests/test_vflow_sdk.py
/tmp/vflow-venv/bin/pytest -q \
  tests/test_vflow_sdk.py \
  tests/test_vflow_registry.py \
  tests/test_vflow_handlers.py
```

Перед релизом также прогоняются compiler/API/runtime-тесты, затрагивающие изменённый контракт, и проверяется health snapshot.

## 10. Деплой

1. Добавить модуль блока и тесты в репозиторий.
2. Пройти code review, включая security review capabilities.
3. Убедиться, что Registry и handlers подключены в composition root приложения.
4. Задеплоить код и перезапустить процесс; hot-loading отсутствует.
5. Проверить `GET /api/admin/vflow/block-registry` и health-check (`handlers` без `missing_handlers`).
6. Убедиться, что старые handler-версии остаются доступными для активных Run.
7. Иметь план отката: вернуть предыдущий релиз, не удаляя старые версии handler-ов.

После деплоя новый блок доступен администратору для новых версий пайплайнов. Уже опубликованные версии и активные Run продолжают работать по закреплённому контракту.

## 11. Справочник файлов

| Файл | Назначение |
|---|---|
| `src/opencode_web/vflow/capabilities.py` | Capability-модель и валидация |
| `src/opencode_web/vflow/registry.py` | `NodeSpec`, Registry, call forms |
| `src/opencode_web/vflow/handlers.py` | `NodeHandler`, `PlanContext`, встроенные handlers |
| `src/opencode_web/vflow/actions.py` | Типизированные Runtime Actions |
| `src/opencode_web/vflow/sdk.py` | `register_block` |
| `src/opencode_web/vflow/blocks/example.py` | Пример `custom.echo@1.0` |
| `tests/test_vflow_sdk.py` | Тесты SDK, Registry, manifest и demo Run |

## 12. Чек-лист разработчика

- [ ] `type` и `version` не конфликтуют с существующими.
- [ ] `config_schema` описывает все поля и ограничения.
- [ ] `call_names` добавлены в `NodeSpec`, парсер не изменён.
- [ ] Handler использует только `PlanContext` и `Action`.
- [ ] Нет `eval`/`exec`/shell/SQL/динамического импорта.
- [ ] Capabilities минимальны и совпадают у spec и handler.
- [ ] Есть тесты регистрации, компиляции, plan, manifest и demo Run.
- [ ] Старые версии handler-ов не удалены, пока есть активные Run.
- [ ] Документация и план отката обновлены.
