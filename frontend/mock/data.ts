// Demo data for the VALERIA dev mock. Russian texts are copied verbatim from
// valeria-ui/prototype.html; times are prototype wall-clock (Sat 2026-10-24 14:30)
// and are shifted to "real now" by the store (see store.ts / at()).

export const PROTO_NOW = new Date(2026, 9, 24, 14, 30).getTime();

/** Prototype wall-clock: day offset from 2026-10-24 and "HH:MM". */
export function protoTime(dayOffset: number, hm: string): number {
  const [h, m] = hm.split(":").map(Number);
  return new Date(2026, 9, 24 + dayOffset, h, m).getTime();
}
/** Absolute prototype date (month is 1-based). */
export function protoDate(month: number, day: number, hm = "10:00"): number {
  const [h, m] = hm.split(":").map(Number);
  return new Date(2026, month - 1, day, h, m).getTime();
}

export const DEMO_PASSWORD = "valeria-demo-2026";
export const WORKSPACE_ROOT = "/home/valeria/workspaces";
export const ws = (rel: string) => `${WORKSPACE_ROOT}/${rel}`;

export const ENV_LIMIT = 2;
export const HARD_LIMIT = 10;
export const CURRENT_LIMIT = 4;
export const USER_JOBS_HARD_LIMIT = 10;
export const AGENTS = ["build", "plan"];
export const DEFAULT_AGENT = "build";
export const DEFAULT_MODEL = "anthropic/claude-sonnet-4-5";

export const MODELS = [
  { id: "anthropic/claude-sonnet-4-5", name: "claude-sonnet-4-5", provider: "anthropic" },
  { id: "openai/gpt-5-codex", name: "gpt-5-codex", provider: "openai" },
  { id: "qwen/qwen3-coder-480b", name: "qwen3-coder-480b", provider: "qwen" },
];

// ---------------------------------------------------------------- users
export interface SeedUser {
  id: string; login: string; role: "user" | "admin"; tasks_only: 0 | 1;
  mattermost_username: string | null; workspace: string | null;
  status: "active" | "disabled"; max_active_jobs: number | null;
  last_login_at: number | null; created_at: number;
}
export const USERS: SeedUser[] = [
  { id: "u-dorlov", login: "d.orlov", role: "admin", tasks_only: 0, mattermost_username: "d.orlov",
    workspace: "admin/sandbox", status: "active", max_active_jobs: null,
    last_login_at: protoTime(0, "09:12"), created_at: protoDate(9, 2) },
  { id: "u-isokolova", login: "i.sokolova", role: "user", tasks_only: 0, mattermost_username: "i.sokolova",
    workspace: "qa/firmware-sdk", status: "active", max_active_jobs: null,
    last_login_at: protoTime(0, "13:50"), created_at: protoDate(9, 4) },
  { id: "u-pvolkov", login: "p.volkov", role: "user", tasks_only: 0, mattermost_username: "p.volkov",
    workspace: "fw/hal-stm32", status: "active", max_active_jobs: 2,
    last_login_at: protoTime(0, "11:02"), created_at: protoDate(9, 4, "10:30") },
  { id: "u-anikitina", login: "a.nikitina", role: "user", tasks_only: 1, mattermost_username: "a.nikitina",
    workspace: "qa/firmware-sdk-ci", status: "active", max_active_jobs: null,
    last_login_at: protoTime(-1, "17:40"), created_at: protoDate(9, 15) },
  { id: "u-mbelov", login: "m.belov", role: "user", tasks_only: 1, mattermost_username: null,
    workspace: "docs/release-notes", status: "active", max_active_jobs: null,
    last_login_at: protoDate(10, 21, "09:00"), created_at: protoDate(10, 1) },
  { id: "u-segorova", login: "s.egorova", role: "user", tasks_only: 0, mattermost_username: "s.egorova",
    workspace: "fw/bootloader/secure-boot-armv8-experimental", status: "disabled", max_active_jobs: null,
    last_login_at: protoDate(10, 3, "16:00"), created_at: protoDate(9, 9) },
  { id: "u-youtrack", login: "youtrack-bot", role: "user", tasks_only: 1, mattermost_username: null,
    workspace: "integrations/youtrack", status: "active", max_active_jobs: null,
    last_login_at: null, created_at: protoTime(-1, "16:20") },
];
export const ACCOUNT = { user: "u-isokolova", tasks: "u-isokolova", admin: "u-dorlov" } as const;

// ---------------------------------------------------------------- queues
export const QUEUES = [
  { id: "questions", name: "Вопросы", priority: 200, max_workers: 1, is_system: 0, created_at: protoDate(9, 2, "10:05") },
  { id: "tasks", name: "Задачи", priority: 100, max_workers: 2, is_system: 0, created_at: protoDate(9, 2, "10:06") },
  { id: "default", name: "Основная", priority: 0, max_workers: 4, is_system: 1, created_at: protoDate(9, 2, "10:00") },
  { id: "long", name: "Долгие сборки на трёх тулчейнах", priority: -10, max_workers: 1, is_system: 0, created_at: protoDate(10, 2, "12:40") },
];
export const CONSULTATION_QUEUE = "questions";
export const DEFAULT_CHAT_QUEUE = "default";

// ---------------------------------------------------------------- tasks
const ITOG = "\nИтог — короткий отчёт без логов.";
export const TASKS = [
  { id: "t-ci", title: "Разбор упавшего CI-пайплайна",
    description: "Найдёт причину падения пайплайна или job в GitLab и предложит исправление.",
    hint: "Ссылка на пайплайн или job GitLab", model: "anthropic/claude-sonnet-4-5", queue_id: "tasks",
    is_published: 1, visibility: "all", audience: [] as string[], plan_first: 0, next: [] as string[],
    created_at: protoDate(9, 20), updated_at: protoDate(10, 14, "12:00") },
  { id: "t-mr", title: "Ревью merge request",
    description: "Проверит изменения в MR: ошибки, стиль, тесты. Замечания придут итоговым отчётом.",
    hint: "Ссылка на merge request", model: "openai/gpt-5-codex", queue_id: "tasks",
    is_published: 1, visibility: "selected", audience: ["u-isokolova", "u-pvolkov", "u-anikitina"], plan_first: 0, next: [],
    created_at: protoDate(9, 21), updated_at: protoDate(10, 9, "12:00") },
  { id: "t-notes", title: "Release notes по задачам версии",
    description: "Соберёт закрытые задачи YouTrack по версии и подготовит черновик release notes.",
    hint: "Версия, например 4.12", model: "anthropic/claude-sonnet-4-5", queue_id: "tasks",
    is_published: 1, visibility: "selected", audience: ["u-isokolova", "u-pvolkov", "u-anikitina", "u-mbelov"], plan_first: 0, next: [],
    created_at: protoDate(9, 22), updated_at: protoDate(10, 11, "12:00") },
  { id: "t-dup", title: "Поиск дубликатов задачи в YouTrack",
    description: "Найдёт похожие задачи и объяснит, чем они отличаются.",
    hint: "Ссылка на задачу YouTrack", model: "qwen/qwen3-coder-480b", queue_id: "tasks",
    is_published: 1, visibility: "all", audience: [], plan_first: 0, next: [],
    created_at: protoDate(9, 23), updated_at: protoDate(10, 2, "12:00") },
  { id: "t-yt", title: "Ответ на упоминание @Valeria в YouTrack",
    description: "Ответит на упоминание @Valeria в комментарии YouTrack.",
    hint: null as string | null, model: "anthropic/claude-sonnet-4-5", queue_id: "tasks", // the prototype has no hint for this task
    is_published: 1, visibility: "selected", audience: ["u-youtrack"], plan_first: 0, next: [],
    created_at: protoDate(9, 24), updated_at: protoDate(9, 28, "12:00") },
  { id: "t-conan", title: "Обновление зависимости Conan во всех пакетах прошивки с проверкой сборки на трёх тулчейнах",
    description: "Сначала составит план, затем обновит conanfile.py и проверит сборку на gcc 12, gcc 13 и clang 17.",
    hint: "Пакет и версия, например openssl/3.3.2", model: "anthropic/claude-sonnet-4-5", queue_id: "long",
    // Published to 3 users: the prototype's user list has 5 tasks («Все варианты · 8» with 3 templates)
    // and its publish step says «Её видят 3 пользователя».
    is_published: 1, visibility: "selected", audience: ["u-isokolova", "u-pvolkov", "u-anikitina"], plan_first: 1, next: ["t-mr"],
    content: "Ты — инженер сборки. Обнови зависимость, указанную пользователем, во всех пакетах репозитория firmware-sdk.\n1. Найди все conanfile.py, где она используется.\n2. Обнови версию и lock-файлы.\n3. Собери каждый пакет в профилях armv8-gcc12, x86_64-gcc13, x86_64-clang17.\nИтог: таблица «пакет → результат сборки», без логов.",
    created_at: protoDate(10, 20), updated_at: protoTime(0, "13:40") },
].map((t) => ({ ...t, content: (t as { content?: string }).content ?? t.description + ITOG }));

// ---------------------------------------------------------------- templates
export const TEMPLATES = [
  { id: "tpl-review", title: "Ревью перед MR",
    content: "Проверь изменения в текущей ветке относительно develop: ошибки, утечки ресурсов, отсутствие тестов. Ничего не исправляй, только список замечаний." },
  { id: "tpl-conan2", title: "Миграция пакета на Conan 2",
    content: "Переведи пакет net-utils на Conan 2: conanfile.py, профили, CI. Сначала покажи план." },
  { id: "tpl-tests", title: "Покрыть модуль тестами",
    content: "Напиши unit-тесты на GoogleTest для модуля ota-agent/src/download.cpp. Запусти их и покажи результат." },
];

// ---------------------------------------------------------------- texts
export const QUESTION_TEXT = "Где в коде обрабатывается таймаут OTA-обновления и как его увеличить для медленных каналов?";
export const CLARIFY = {
  question: "В коде два механизма обновления. О каком речь?",
  multiple: false,
  custom: true,
  options: [
    { label: "OTA прошивки устройства", description: "модуль ota-agent: загрузка и применение образа" },
    { label: "Обновление конфигурации по MQTT", description: "модуль cfg-sync: доставка настроек на устройство" },
  ],
};
export const ANSWER = [
  "Таймаут OTA задаётся в двух местах:",
  "",
  "1. `ota-agent/src/download.cpp:142`, константа `kDownloadTimeout = 300s`. Когда она истекает, загрузка прерывается и агент отправляет `OTA_FAILED(timeout)`.",
  "2. `ota-agent/config/defaults.yaml`, параметр `ota.download_timeout_sec`. Если он задан, он перекрывает константу (см. `Config::load`, `config.cpp:88`).",
  "",
  "Для медленных каналов меняйте `defaults.yaml` или профиль устройства, пересобирать агент не нужно. Похожий случай разбирали в FW-2381 «OTA обрывается на 2G-модемах», там подняли значение до 900 с.",
].join("\n");
export const CI_REPORT = [
  "#### Причина",
  "",
  "Job `build:armv8-gcc12` упал на линковке: `undefined reference to EVP_MD_fetch`. В MR !1928 openssl обновлён до 3.3 в `conanfile.py`, а `conan.lock` по-прежнему фиксирует `openssl/1.1.1w` для профиля armv8.",
  "",
  "#### Как исправить",
  "",
  "```",
  "conan lock create . -pr:h=profiles/armv8-gcc12 --lockfile-out=conan.lock",
  "```",
  "",
  "Закоммитьте обновлённый `conan.lock` в ветку MR. Код менять не нужно.",
  "",
  "#### Что проверено",
  "",
  "- На x86_64 сборка проходит, ошибка только в профилях с lock-файлом.",
  "- Последний зелёный пайплайн #48176, до слияния !1928.",
].join("\n");
export const PLAN = [
  "План перевода `net-utils` на Conan 2:",
  "",
  "1. `conanfile.py`: импорты из `conan` вместо `conans`, `layout()` через `cmake_layout`, генераторы `CMakeToolchain` и `CMakeDeps`.",
  "2. Профили `profiles/armv8-gcc12` и `x86_64-gcc13`: секция `[conf]` вместо `[env]`.",
  "3. `CMakeLists.txt`: убрать `conan_basic_setup()`, подключать зависимости через `find_package`.",
  "4. `.gitlab-ci.yml`: образ с Conan 2, `conan create . -pr:h=…`.",
  "5. Собрать пакет локально для armv8 и показать результат.",
].join("\n");
export const DONE_OK = [
  "`net-utils` собирается на Conan 2.",
  "",
  "- Изменено 4 файла: `conanfile.py`, 2 профиля, `.gitlab-ci.yml`.",
  "- `conan create` для armv8-gcc12 прошёл за 2 мин 14 с, тесты пакета 37 из 37.",
  "- Изменения не закоммичены. Проверьте `git diff` и закоммитьте сами.",
].join("\n");
export const DONE_DENIED = [
  "Файлы обновлены. Сборку я не проверила, потому что вы отклонили `conan create`.",
  "",
  "- Изменено 4 файла: `conanfile.py`, 2 профиля, `.gitlab-ci.yml`.",
  "- Проверьте сборку сами: `conan create . -pr:h=profiles/armv8-gcc12 --build=missing`.",
].join("\n");
export const FOLDER_PROMPT = "Переведи пакет net-utils на Conan 2: conanfile.py, профили, CI. Сначала покажи план.";
export const FOLDER_BUILD_PROMPT = "Выполни план";
export const PERMISSION_COMMAND = "conan create . -pr:h=profiles/armv8-gcc12 --build=missing";
export const PERMISSION_WHY = "собрать пакет для armv8 и проверить, что изменения в conanfile.py и профилях работают.";
export const RUN_FAILED_TEXT = "Агент запросил разрешение, а у задачи его нет. Проверьте, открывается ли ссылка в дополнении, и запустите ещё раз. Подробности запуска есть у администратора.";
export const CI_URL = "https://gitlab.example.com/fw/firmware-sdk/-/pipelines/48211";
export const MR_URL = "https://gitlab.example.com/fw/firmware-sdk/-/merge_requests/1932";
export const MCP_SERVERS = [
  { name: "gitlab-readonly", status: "connected" },
  { name: "memory", status: "connected" },
  // The prototype shows «vision · переподключается»; OpenCode has no "reconnecting"
  // MCP state, the closest status the backend can pass through is "failed".
  { name: "vision", status: "failed" },
];

// ---------------------------------------------------------------- REQS answers (verbatim)
export const R = {
  ci48211: "Линковка падает на armv8: libcrypto собрана без -fPIC в профиле armv8-gcc12. Исправление — добавить openssl:fPIC=True в profiles/armv8-gcc12 и пересобрать зависимость.",
  gcc13: "В gcc 13 по умолчанию включено -Werror=dangling-reference. Предупреждение срабатывает в net-utils/src/buffer.cpp:57, а nightly собирается с -Werror. Варианты: исправить ссылку на временный объект или отключить флаг в профиле x86_64-gcc13.",
  notesFailed: "YouTrack не ответил за 60 с. Задача остановилась на этапе сбора задач версии.",
  belov412: "Таких задач три: FW-2398, FW-2402 и FW-2417. Все три в статусе «На проверке», исполнитель — p.volkov.",
  hal: "DMA передаёт буфер без участия ядра и даёт одно прерывание в конце. IT вызывает прерывание на каждый байт. На 921600 бод IT занимает до 18% времени ядра, DMA — меньше 1%.",
  dup2388: "Похожих задач две: FW-2310 (тот же стек вызовов, закрыта в 4.11) и FW-2371 (другой модуль, совпадает только текст ошибки).",
  otaLogs: "Задайте log.level: debug в ota-agent/config/defaults.yaml или передайте --log-level=debug при запуске.",
  mr1904: "Замечаний четыре, два из них блокирующие: таймаут переподключения не сбрасывается после успешного соединения, тестов на обрыв канала нет.",
  ci46102: "Тест test_download_resume флакает из-за жёсткого таймаута 2 с. Предложено заменить ожидание на событие окончания загрузки.",
  armv8: "Используйте профиль armv8-gcc12 из репозитория profiles: в нём уже указан тулчейн aarch64-none-linux-gnu 12.3.",
  clarNikitina: "Для какой платформы собираете: armv8 или x86_64?",
};

// ---------------------------------------------------------------- audit (prototype AUDIT)
export const AUDIT = [
  { t: protoTime(0, "14:21"), who: "u-dorlov", as: null, action: "concurrency.update", type: "setting", obj: "max_active_sessions", meta: { value: 4 } },
  { t: protoTime(0, "14:18"), who: "u-dorlov", as: "u-pvolkov", action: "job.stop.request", type: "job", obj: "j-f-volkov-hal", meta: { job_id: "j-f-volkov-hal", status: "running" } },
  { t: protoTime(0, "14:15"), who: "u-isokolova", as: null, action: "task.run", type: "task_run", obj: "r-queued", meta: { run_id: "r-queued", queue_id: "tasks" } },
  { t: protoTime(0, "13:52"), who: "u-dorlov", as: "u-anikitina", action: "consultation.admin_view", type: "consultation", obj: "q-nikitina-debug", meta: {} },
  { t: protoTime(0, "13:50"), who: "u-isokolova", as: null, action: "auth.login", type: "user", obj: "u-isokolova", meta: {} },
  { t: protoTime(0, "13:40"), who: "u-dorlov", as: null, action: "task.update", type: "task", obj: "t-conan", meta: { fields: ["content", "next_task_ids"] } },
  { t: protoTime(0, "12:10"), who: "u-dorlov", as: "u-isokolova", action: "user.update", type: "user", obj: "u-isokolova", meta: { mattermost_username: "i.sokolova" } },
  { t: protoTime(0, "11:47"), who: "u-dorlov", as: "u-pvolkov", action: "permission.admin_response", type: "permission", obj: "per_7Hq2", meta: { permission_id: "per_7Hq2", response: "once" } },
  { t: protoTime(0, "11:30"), who: "u-dorlov", as: null, action: "queue.update", type: "queue", obj: "questions", meta: { priority: 200, max_workers: 1 } },
  { t: protoTime(0, "10:05"), who: "u-dorlov", as: "u-mbelov", action: "user.password_reset", type: "user", obj: "u-mbelov", meta: {} },
  { t: protoTime(-1, "18:02"), who: "u-dorlov", as: "u-anikitina", action: "task_access.update", type: "user", obj: "u-anikitina", meta: { task_ids: ["t-ci", "t-mr", "t-notes", "t-dup"] } },
  { t: protoTime(-1, "16:20"), who: "u-dorlov", as: null, action: "user.create", type: "user", obj: "u-youtrack", meta: { role: "user", tasks_only: true } },
  { t: protoDate(10, 21, "09:31"), who: "u-anikitina", as: null, action: "auth.login", type: "user", obj: "u-anikitina", meta: {} },
  { t: protoDate(10, 19, "17:05"), who: "u-dorlov", as: "u-segorova", action: "user.update", type: "user", obj: "u-segorova", meta: { status: "disabled" } },
  { t: protoDate(10, 2, "12:40"), who: "u-dorlov", as: null, action: "queue.update", type: "queue", obj: "long", meta: { priority: -10, max_workers: 1 } },
];
