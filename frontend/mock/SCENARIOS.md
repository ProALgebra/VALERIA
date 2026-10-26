# VALERIA API mock: scenarios

Start: `~/.bun/bin/bun frontend/mock/server.ts --port 4301 [--speed 4] [--fixed-now]`.

- **`--speed N`** makes lifecycle timers N× faster.
- **`--fixed-now`** keeps the seed on prototype dates. Prototype "now" is 2026-10-24 14:30 local. The mock clock starts there and runs on in real time: `now = PROTO_NOW + time since start or last /__mock/reset`. Set the browser clock to PROTO_NOW too (Playwright `page.clock.install({ time: PROTO_NOW })`). Without the flag, the seed is shifted so that "now" is the real clock at reset.
- **Clock and seed ages:** unfinished seeded jobs (and their chats) keep their age against the mock clock. For example, `r-running` always started 31 min ago, even after the server has run for hours. Unfinished stats runs follow the clock the same way. Jobs freeze once they finish.
- **Slots (as in `claim_next_job`):** dispatching, running, waiting_permission and cancelling sessions count against the global limit; waiting_question does not. Queue `max_workers` also applies. The seeded override is 4 (the prototype's «3 из 4»). With `feed=all` it is raised to 8 so the extra state fixtures fit. A live job that finds no free slot finishes the oldest frozen seeded running/dispatching job (extra state fixtures first, never jobs started by `/__mock/play`). If no such job exists, it stays `queued` and retries every 500 ms.
- **Tests:** `cd frontend && ~/.bun/bin/bun test ./mock/` (`./` keeps bun away from `e2e/mock/*.spec.ts`).

Demo password for every seed user: `valeria-demo-2026`.

## Scenario controls

| Control | Effect |
|---|---|
| `?as=user\|tasks\|admin\|none` | Signs in a persona automatically and persists it in the `mock_scenario` cookie. The query can be on the API request, on the page URL (read from `Referer`), or in the `x-mock-as` header. Personas: `user` = i.sokolova; `tasks` = i.sokolova with `tasks_only`; `admin` = d.orlov. `none` = logged out. |
| `&folder=0` | The persona has no assigned folder (`workspace_path: null`). |
| `&first=1` | First login: the persona has no chats, questions or runs. |
| `&feed=proto` | Only the chats the prototype shows. Drops the extra state fixtures `q-answer q-running q-queued q-failed f-planning f-plan f-running f-done f-denied r-plan`. |
| `?delay=ms`, header `x-mock-delay: ms` | Adds latency (for example 300 ms for the loader). This also works as a page-URL query via `Referer`. |
| `POST /__mock/session {"delay":300,"delay_path":"/api/me/items"}` | Sets a persistent delay, optionally limited to paths containing a substring. |
| header `x-mock-fail: 503` | Forces this request to fail with `{"detail":"Mock forced error"}`. |
| `POST /__mock/session {"fail":"/api/tasks","fail_status":500,"fail_times":1}` | Forced-error toggle: matching requests fail `fail_times` times. Use `"*"` to fail every request, and `null` to turn the toggle off. `"fail":"batch:u-pvolkov"` makes that id return `reason:"invalid"` in a batch (the backend's rejected-update reason). |
| `GET /__mock/session?as=admin&redirect=/admin` | Switches persona and answers 302 to the redirect with one `ocw_session` cookie (a link for the browser). |
| `GET /__mock/state` | Returns the profile, scenario, every item id with its status, queue positions, users, tasks and tombstones. |
| `POST /__mock/reset [{"as":"user"}]` | Reseeds everything and signs in again. With `as`, the profile starts from the defaults (folder assigned, not first login, feed all) plus the body. |
| `POST /__mock/emit {...}` | Publishes a raw SSE event. Special forms: `{"type":"chat.updated","chat_id":"q-gcc13","snoozed_until":null}` updates and emits the real item; `{"type":"job.updated","job":"r-queued","status":"running"}` moves a job. A `chat.updated` / `task.title.updated` / `chat.deleted` without `item` needs an existing `chat_id` (404 otherwise) and is delivered as the backend does (owner only, admins get `owner_user_id`, `tasks_only` never gets folders). `{"type":"opencode.disconnected"}` without `workspace` fans out one event per user folder. |
| `POST /__mock/play {"id":"r-queued"}` | Starts the timed lifecycle of a frozen seeded job or chat. |
| `POST /__mock/patch {"collection":"tasks","id":"t-ci","set":{"title":"…"},"by":"p.volkov"}` | Simulates another admin's edit: bumps `updated_at` and sets `updated_by`, so your next PATCH gets 409. Add `"delete":true` to get 404 `deleted`. |

Every 403 is printed to stdout as `[mock 403] METHOD path user=login detail=…`.

### Message tags (live lifecycles)

These tags make lifecycles branch:

- **Consultations:** the first message gets a clarifying question unless it contains `#direct`. A follow-up asks a question only with `#question`. `#fail` makes it fail.
- **Task `user_input`:** `#permission`, `#question` and `#fail` work. A `plan_first` task goes plan → build automatically.
- **Folder message:** `#question` and `#fail` work. Agent `plan` produces a plan text. Agent `build` makes edits, then asks permission, then finishes. `reject` gives the "denied" ending.

Stopping works by job type:

- **Questions and folders:** `POST /api/chats/:id/abort`.
- **Tasks:** `POST /api/task-runs/:id/stop`.
- **Queued jobs:** `DELETE /api/jobs/:id`.
- **Admin:** `POST /api/admin/jobs/:id/stop`.

## Prototype step · state → setup → URL / actions

The prototype is `valeria-ui/prototype.html` (`STEPS` + `STEPS_FIND`). The setup column applies on top of a fresh `POST /__mock/reset`.

| # | Step · state | Persona / setup | API calls and actions (ids are stable) |
|---|---|---|---|
| 1 | home · feed «Я только спросить» | `?as=user` | `GET /api/me/items`, `GET /api/tasks`, `GET /api/consultations`. Ask: `POST /api/consultations {message}` + `Idempotency-Key` |
| 2 | home · folder «Сделать» | `?as=user` (folder assigned) | `GET /api/config/public` (workspace), `GET /api/options`, `GET /api/templates`. Send: `POST /api/chats {message, agent:"plan"\|"build", model}` → `{chat, job}` |
| 3 | home · preset «Выбрана задача» (ci) | `?as=user` | `GET /api/tasks` → `t-ci` with `input_hint` «Ссылка на пайплайн или job GitLab» (composer placeholder). Run: `POST /api/tasks/t-ci/runs {user_input}` → 202 public_task_run |
| 4 | home · duplicate «Такой запуск уже идёт» (mr + MR !1932) | `?as=user` | Pick `t-mr` and enter `https://gitlab.example.com/fw/firmware-sdk/-/merge_requests/1932`. `GET /api/task-runs` contains `r-running` with `task_id:"t-mr"` and that exact `user_input`, status `running` → the UI matches on `task_id` + `user_input` |
| 5 | home · first «Первый вход» | `?as=user&first=1` | `GET /api/me/items` → `[]`, `/api/task-runs` → `[]` |
| 6 | ask · queued | `?as=user` | `q-queued`: `GET /api/consultations/q-queued` status `queued`; `/api/queue/me` position 3. Live: `POST /__mock/play {"id":"q-queued"}` |
| 7 | ask · running «Готовит ответ» | `?as=user` | `q-running` (status `running`). Live: a new `POST /api/consultations` goes queued → dispatching → running over SSE `consultation.updated` |
| 8 | ask · clarify «Valeria уточняет» | `?as=user` | `q-clarify` waiting_question: `GET /api/consultations/q-clarify/questions`, then reply `POST …/questions/:qid/reply {answers:[["OTA прошивки устройства"]]}` → resume → completed |
| 9 | ask · answer «Ответ готов» | `?as=user` | `q-answer` (completed; `messages` include the assistant answer) |
| 10 | ask · failed | `?as=user` | `q-failed` (failed). Live: `POST /api/consultations {"message":"… #fail"}` |
| 11 | run · queued | `?as=user` | `r-queued` (`GET /api/task-runs/r-queued`; `/api/queue/me` position 3) |
| 12 | run · running | `?as=user` | `r-running`; plan_first: `r-plan` (phase `plan`). Live: `POST /api/tasks/t-ci/runs` → SSE `task.updated` ×4 + `task.title.updated` |
| 13 | run · done «Готово: итог» | `?as=user` | `r-done`: `GET /api/task-runs/r-done/result` → `{status:"available", text}` |
| 14 | run · failed | `?as=user` | `r-failed` (`/result` → `status:"unavailable"`) |
| 15 | run · cancel «Подтверждение отмены» | `?as=user` | UI dialog over `r-running`. Confirm: `POST /api/task-runs/r-running/stop` → 202 (status `cancelling`) |
| 16 | run · cancelled | `?as=user` | `r-cancelled`. Live: after #15, SSE `task.updated` `cancelled` ~1 s later (÷ speed) |
| 17 | folder · planning «Составляет план» | `?as=user` | `f-planning` (running, agent plan; `GET /api/chats/f-planning/messages` shows running read/grep tools) |
| 18 | folder · plan «План готов» | `?as=user` | `f-plan` (idle; last assistant message is the plan text). Continue: `POST /api/chats/f-plan/messages {message, agent:"build"}` |
| 19 | folder · running «Выполняет (Build)» | `?as=user` | `f-running` (running, edits). Live: `POST /__mock/play {"id":"f-running"}` → permission |
| 20 | folder · permission «Просит разрешение» | `?as=user` | `f-permission` waiting_permission: `GET /api/chats/f-permission/permissions`, `POST …/permissions/:pid {response:"once"\|"always"\|"reject"}` |
| 21 | folder · done «Готово» | `?as=user` | `f-done` (allowed ending); `f-denied` = rejected ending |
| 22 | find · cmds «Команды» | `?as=user` | Client-side; data from `GET /api/me/items` + `GET /api/tasks` |
| 23 | find · results «Найдено» (conan) | `?as=user` | Client-side search over the same data. «conan» matches `f-*` (Миграция net-utils на Conan 2), `f-conan-profiles`, `r-plan` |
| 24 | find · none (zigbee) | `?as=user` | Client-side, no matches |
| 25 | stats · week | `?as=admin` | `GET /api/admin/statistics/runs?from=<now-7d>` (+ `GET /api/admin/statistics?timezone=…`) |
| 26 | stats · day «24 часа» | `?as=admin` | `…/statistics/runs?from=<now-24h>` (includes today's prototype runs + live runs) |
| 27 | stats · task (ci, 30 days) | `?as=admin` | `…/statistics/runs?from=<now-30d>&type=t-ci` |
| 28 | stats · empty (2026-10-18, questions) | `?as=admin`, start with `--fixed-now` | `…/statistics/runs?from=2026-10-18T00:00:00+03:00&to=2026-10-18T23:59:00+03:00&type=question` → `items: []` (a Sunday in the prototype calendar; without `--fixed-now` the calendar shifts) |
| 29 | users · list «Список и карточка» | `?as=admin` | `GET /api/admin/users`. Card: `/api/admin/users/:id/chats`, `/task-runs`, `/tasks` |
| 30 | users · selected (3 users) | `?as=admin` | Client-side selection of `u-pvolkov`, `u-anikitina`, `u-mbelov` |
| 31 | users · block «Подтверждение блокировки» | `?as=admin` | `POST /api/admin/users/batch {ids:[…], status:"blocked"}` → per-id `{ok, skipped, reason}`. Partial: include `u-dorlov` (self), `u-segorova` (already), or set `fail:"batch:<id>"` |
| 32 | users · reset «Сброс пароля» | `?as=admin` | `POST /api/admin/users/u-pvolkov/reset-password {password ≥12}` → 204 |
| 33 | users · edit | `?as=admin` | `PATCH /api/admin/users/:id {…, updated_at}`. Conflict: `/__mock/patch {"collection":"users","id":"u-anikitina","by":"p.volkov"}` first → 409 |
| 34 | users · ws-conflict «Папку не сменить» | `?as=admin` | `PATCH /api/admin/users/u-pvolkov {"workspace_path":"qa/net-utils"}` → 409 «Workspace cannot change while jobs are queued or active» (`f-volkov-hal` is running) |
| 35 | users · create | `?as=admin` | `POST /api/admin/users {login, password, workspace_path}` → 201 |
| 36 | users · create-error «Логин занят» | `?as=admin` | `POST /api/admin/users {"login":"p.volkov","password":"Valeria-2026-tmp",…}` → 409 «Login or Mattermost user already exists» |
| 37 | atask · list | `?as=admin` | `GET /api/admin/tasks` |
| 38 | atask · edit «Редактор: черновик» | `?as=admin` | `t-conan` is published to 3 users as in the prototype, so first make it a draft: `/__mock/patch {"collection":"tasks","id":"t-conan","set":{"is_published":0}}`. Then (draft, plan_first, queue `long`, next `t-mr`): `PATCH /api/admin/tasks/t-conan {…, updated_at}`. Conflict: `/__mock/patch` → 409 `{code:"conflict", updated_by}`. Deleted: `PATCH /api/admin/tasks/t-old-notes` → 404 `{code:"deleted", deleted_by:"d.orlov"}` |
| 39 | atask · published | `?as=admin` | `t-ci` (published). Audience: `PUT /api/admin/tasks/t-mr/audience` |
| 40 | queue · live «Сейчас» | `?as=admin` | `GET /api/admin/queue` (limits, queue rows with active/queued counts, jobs); SSE `job.updated`. `&feed=proto` → limit 4, as in the prototype; `feed=all` → limit 8 |
| 41 | queue · stop | `?as=admin` | `POST /api/admin/jobs/j-f-volkov-hal/stop` → 202 job row; or `/api/admin/jobs/r-belov-notes/stop` (queued → cancelled at once) |
| 42 | queue · settings «Очереди и лимиты» | `?as=admin` | `GET /api/admin/queues`, `PATCH /api/admin/settings/concurrency {value}`, `DELETE …/concurrency/override`, `PUT /api/admin/settings/default-chat-queue` |
| 43 | queue · edit | `?as=admin` | `PATCH /api/admin/queues/:id {name, priority, max_workers, updated_at}` (409/404 as for tasks) |
| 44 | queue · add | `?as=admin` | `POST /api/admin/queues {name, priority, max_workers}` → 201 |
| 45 | queue · archive-blocked «Очередь занята» | `?as=admin` | `DELETE /api/admin/queues/tasks` → 409 «Queue is still assigned or contains unfinished jobs» |
| 46 | aconsult · list «Все запросы» | `?as=admin` | `GET /api/admin/requests` |
| 47 | aconsult · user (i.sokolova) | `?as=admin` | `GET /api/admin/requests?user=i.sokolova` |
| 48 | aconsult · tasks (ci, 30 days) | `?as=admin` | `GET /api/admin/requests?type=task&task=t-ci&from=<now-30d>` |
| 49 | aconsult · none (zigbee) | `?as=admin` | `GET /api/admin/requests?q=zigbee` → `[]` |
| 50 | aconsult · view «Просмотр вопроса» | `?as=admin` | `GET /api/admin/consultations/q-volkov-hal` (audit `consultation.admin_view`); waiting question: `q-nikitina-debug` |
| 51 | aconsult · view-run «Просмотр запуска» | `?as=admin` | `GET /api/admin/task-runs/r-nikitina-dup/messages` + `GET /api/task-runs/r-nikitina-dup/result` |
| 52 | audit · list «Журнал» | `?as=admin` | `GET /api/admin/audit?limit=50` → `{items, next_cursor}`; next page `&cursor=<next_cursor>`; filters `from,to,user,action(prefix with "."),q`. Object ids resolve to fixtures: job `j-f-volkov-hal` (p.volkov's folder), task_run `r-queued` |
| 53 | audit · detail «Событие» | `?as=admin` | A row from #52 (`metadata_json`, logins) |
| 54 | audit · none (zigbee) | `?as=admin` | `GET /api/admin/audit?q=zigbee` → `{items:[], next_cursor:null}` |

**Coverage: 54 prototype step·states found (home 5, ask 5, run 6, folder 5, find 3, stats 4, users 8, atask 3, queue 6, aconsult 6, audit 3) and 54 covered.** #22–24, #30 and #53 are client-only states; the mock supplies their data.

## Extra states (not prototype steps)

| State | Setup / call |
|---|---|
| Logged out → login screen | `?as=none` → `GET /api/auth/me` 401. Then `POST /api/auth/login {login:"i.sokolova", password:"valeria-demo-2026"}`; wrong password → 401 «Неверный логин или пароль»; `s.egorova` is blocked → 401 |
| No folder assigned | `?as=user&folder=0` → `config.workspace: null`; `POST /api/chats` → 422 «No workspace assigned»; task run → 422 |
| tasks_only account | `?as=tasks` → `/api/options`, `/api/chats`, `/api/templates` → 403 «This account can only use tasks and consultations»; `/api/me/items` has no folders; `/api/queue/me` is allowed (questions + tasks with `position`) |
| Duplicate run in progress | see #4 (`r-running`) |
| Unknown ids | `/api/task-runs/r-nope` → 404 «Task run not found»; `/api/consultations/x` → 404 «Диалог не найден»; `/api/chats/x` → 404 «Chat not found» |
| Loader (300 ms) | `?delay=300` on the page URL or the request, or `x-mock-delay: 300` |
| Forced error / retry | `POST /__mock/session {"fail":"/api/me/items","fail_status":503,"fail_times":1}` |
| Chat actions | `POST /api/chats/:id/{archive,unarchive,snooze,unsnooze}` and `DELETE /api/chats/:id` work for every kind and every role. Snoozed item: `q-gcc13` (until +2 days). Archived: `q-ota-logs`, `q-armv8`, `r-mr1904`, `r-ci46102`, `f-conan-profiles`. Expiry: snooze with `until` a few seconds ahead → SSE `chat.updated` with `snoozed_until:null` |
| Snooze cleared by attention | A snoozed chat whose job goes to waiting_question/permission is unsnoozed (`chat.updated`) |
| Live plan_first run | `POST /api/tasks/t-conan/runs` (`?as=user`) → phase `plan` → `build` → completed |
| Mattermost profile | `PATCH /api/profile {"mattermost_username":"p.volkov"}` → 409 «Mattermost user is already assigned» |

## Contract notes (BRIEF §7, §10)

- `public_task_run` returns the real status (including `waiting_question` and `waiting_permission`) and also `user_input`, `error_code` and `started_at`. `/api/me/items` task items use the same status. Live waiting status: put `#permission` or `#question` in `user_input`.
- `public_task` (`/api/tasks`) and the admin task routes return `input_hint`; admin create and PATCH accept it (max 500 characters, otherwise 422). The seed hints are the prototype's texts. `t-yt` is not in the prototype's task list, so its hint is `null`.
