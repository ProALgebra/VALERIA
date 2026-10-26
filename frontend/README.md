# VALERIA frontend

Angular 20 (standalone components, signals) on Koobiq 20.4.2. The product spec is `valeria-ui/SPEC.md`,
the visual reference is `valeria-ui/prototype.html`.

## Commands

Installs go through bun. The Angular CLI needs a real Node on `PATH` (the agents' nvm shim is lazy):

```sh
export PATH="$HOME/.nvm/versions/node/v22.22.3/bin:$HOME/.bun/bin:$PATH"
bun install
```

| Command | What it does |
|---|---|
| `bun run mock` | Mock API on http://127.0.0.1:4301 (`mock/`, see `mock/SCENARIOS.md`) |
| `bun run start` | Dev server on http://localhost:4201; `/api` and `/__mock` are proxied to `VALERIA_API` (default http://127.0.0.1:4301) |
| `bun run build` | Production build into `../src/opencode_web/static/` |
| `bun run test` | Unit tests once, headless (vitest + jsdom through `@angular/build:unit-test`) |
| `bun run e2e` | Placeholder, owned by the integration task |

Against the real backend: `VALERIA_API=http://127.0.0.1:<port> bun run start`. The proxy keeps SSE
(`/api/events`) streaming: no compression, no buffering (`proxy.conf.mjs`).

Mock personas: open `http://localhost:4201/__mock/session?as=user|tasks|admin`, then `/`.
Demo password: `valeria-demo-2026`.

### Production build

The backend serves only this UI. `bun run build` replaces `src/opencode_web/static/` with `index.html`,
hashed assets, `favicon.svg` (from `public/`) and `3rdpartylicenses.txt`. Asset URLs are `/static/…`
(`deployUrl`), base href is `/`; critical CSS inlining is off because the inlined `url(./media/…)` would
resolve outside `/static/`. The Docker image and the systemd deploy do not build the frontend, so the build
output is committed: rebuild and commit `src/opencode_web/static/` together with frontend changes.

Old hash links of the previous UI (`#/consultations/:id`, `#/tasks/runs/:id`, `#/users`…) are rewritten to
the new routes in `main.ts` before the router starts (`core/guards/legacy-hash.ts`).

## Layout

```
src/app/
  app.config.ts, app.routes.ts, app.ts   providers, routes (SPEC §5), root (<router-outlet> + toast host)
  core/
    api/        ApiService (promise wrappers over HttpClient), interceptor: CSRF header, 401 → /login?return=
    auth/       AuthService: user, isAdmin, tasksOnly, roleLabel, login/logout, ensureLoaded
    events/     EventsService: one SSE connection, reconnect backoff, on(type), pollWhileDisconnected
    items/      ItemsApi + ItemsStore: /api/me/items, groups for the menu, archive/snooze/remove, transitions$
    notify/     NotifyService (toasts, bottom-right) + NotifyHost; finalResult() for finished runs
    search/     normalizeForSearch, tokenize, matches (case/diacritics-insensitive, AND tokens, "phrases")
    state/      UiStateService: the only code touching localStorage/sessionStorage (`valeria.*` keys)
    status/     statusLabel/statusTone/statusBadgeColor/statusIcon, KIND_ICON, errorCodeText/describeError
    theme/      AppThemeService: Системная/Светлая/Тёмная, key `valeria.theme`
    time/       snooze moments and labels
    guards/     authMatch/guestMatch, adminMatch/notTasksOnlyMatch, dirtyGuard, legacy hash rewrite
  shared/       page-state (loader/error/empty), access-denied, not-found, placeholder
  shell/        shell layout, nav (side menu), chat-menu («…» + actions), screen-header, aside (resizable)
  features/     screens; only login lives here so far
```

## Adding a screen

1. Create `features/<screen>/<screen>.component.ts` (standalone, OnPush, selector prefix `v-`).
2. In `app.routes.ts`, replace the placeholder `loadComponent` of the route with yours. Keep the path,
   `canMatch` and the denied twin. `screen()`, `admin()` and `denied()` build the route entries;
   `withDirtyGuard()` adds `dirtyGuard` to every screen.
3. Route params, query params and route `data` arrive as component inputs (`withComponentInputBinding`):
   `readonly id = input.required<string>()` for `questions/:id`.

Building blocks:

- **Header**: `<v-screen-header [title] [crumbs] [chat] [asideToggle]>` with slots `[vHeaderBack]`,
  `[vHeaderLead]`, `[vHeaderAfterTitle]`, `[vHeaderActions]`. Pass `[chat]="item"` to get the «…» chat menu.
- **Loading/error/empty**: wrap content in `<v-page-state [loading] [error] (retry)="load()">`. The loader
  appears only after 300 ms; the error state has «Попробовать снова».
- **Right aside** (chat screens): `<v-chat-aside>`; open state and width persist (`chat.aside.*`).
- **Content panel** width: `panel = cpanelWidth('<screen>')`, bind `[vCpanelWidth]="panel"` on the
  `kbq-content-panel-container`; width is saved per screen and squeezes to the minimum on narrow windows.
- **Chat actions**: inject `ChatActionsService` (snooze, archive, remove with the confirm modal);
  `removed$` tells the menu where to move focus.
- **Persisted UI state**: declare a key with `boolKey/rangeKey/enumKey('<screen>.<block>.open', …)` and
  use `inject(UiStateService).state(key)` (a writable signal). Drafts: `readDraft/writeDraft/clearDraft`.
- **Unsaved changes**: implement `DirtyAware.isDirty()` on the routed component, or call
  `inject(DirtyFormService).track(() => dirty)` from a child; navigation and reload then ask first.
- **Live updates**: `inject(EventsService).on('chat.updated')`; `ItemsStore` already applies item events.
- **Toasts**: `NotifyService.success/error/warning/info(title, text?)`, `withOpen(…)` for a toast with
  «Открыть».
- **Errors**: `describeError(err)` returns the Russian text for an `ApiError` (`errorCodeText(code)`).
- **Search**: `matches(fields, tokenize(query))` from `core/search` (rules in `~/.claude/rules`).

Rules: colours only from `--kbq-*` tokens; every icon-only button has `kbqTooltip` and `aria-label`; no
token or session data in storage (the session is an HttpOnly cookie, CSRF lives in memory).

## Koobiq 20.4.2 notes

- `provideAnimations()` is required: dropdown, modal, toast and content panel use `@angular/animations`.
- `KbqVerticalNavbar` collects its items with a content query, so every `kbq-navbar-item` must be declared
  inside the `<kbq-vertical-navbar>` template (an `ng-template` declared there and stamped with
  `ngTemplateOutlet` works; items inside child components do not).
- The navbar is one tab stop with arrow-key roving focus; nested focusable things in a row are
  `tabindex="-1"` and opened from the row (Shift+F10 / context-menu key for the chat menu).
- `kbq-navbar-brand` is focusable; the brand here is a plain `div`.
- A `kbq-navbar-title` in a dropdown-trigger row adds a submenu chevron and is projected right after
  `[kbq-icon]`, ahead of any other content. The user row (avatar first, no chevron) therefore uses a plain span with `kbq-title`.
- The flex rule for `kbq-navbar-container` ships with the horizontal navbar only; vertical containers
  get `display: flex; flex-direction: column` in `nav.component.scss`.
- The navbar item's `::after` state layer sits over its content; interactive children need
  `position: relative; z-index: 1`.
- A dropdown trigger restores focus to itself on close; a trigger hidden on close (row «…») loses focus,
  hence `kbqDropdownTriggerRestoreFocus=false` and a manual return to the row.
- Modal footer callbacks referencing the ref need a typed ref (`const ref: KbqModalRef = …`) under
  `strict`.
- `kbq-dropdown__group-header` is a title; for the prototype's small grey captions («Отложить», «Тема») add
  `v-menu-caption` (`styles.scss`). Dropdown item text is a column; a right-hand hint needs
  `flex-direction: row` on `.kbq-dropdown-item__text` (see the theme items in `nav.component.scss`).
- A dropdown pane is at least as wide as its trigger; full-width navbar rows need the panel inset to line up
  with the visible row (`.v-nav__menu`).
- There is no filled theme-fade button; «Новый запрос» is a transparent theme button with the
  `--kbq-background-theme-fade` background and the matching state tokens.
