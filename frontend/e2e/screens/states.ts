import type { Locator, Page } from '@playwright/test';
import { expect, openAs, type MockControl, type Persona } from '../fixtures';

export type Theme = 'light' | 'dark';
export type Group = 'foundation' | 'user-start' | 'user-chat' | 'admin-people' | 'admin-config';

export interface ScreenState {
    group: Group;
    /** Live file name part; also the proto name unless `proto` says otherwise. */
    state: string;
    /** Proto file stem without the theme suffix, when it differs from `proto-<state>` ('' = `proto-light.png`). */
    proto?: string;
    /** «How reached» for INDEX.md: URL + steps. */
    how: string;
    /** Not reproducible with mock data: the reason (no test body runs). */
    skip?: string;
    /** Leave the pointer where the last step put it (hover-dependent states). */
    keepPointer?: boolean;
    run(ctx: { page: Page; mock: MockControl; theme: Theme }): Promise<void>;
}

const CI_URL = 'https://gitlab.example.com/fw/firmware-sdk/-/pipelines/48211';
const MR_URL = 'https://gitlab.example.com/fw/firmware-sdk/-/merge_requests/1932';

/** Fresh seed, persona signed in through the browser, page opened. `feed=proto` keeps only the prototype's chats. */
async function open(
    page: Page,
    mock: MockControl,
    as: Persona,
    path: string,
    opts: { feed?: 'proto' | 'all'; first?: boolean } = {},
): Promise<void> {
    const feed = opts.feed ?? 'proto';
    await mock.reset(as, { feed, ...(opts.first ? { first: 1 } : {}) });
    const q = new URLSearchParams({ feed, ...(opts.first ? { first: '1' } : {}) });
    await openAs(page, as, path, q.toString());
}

const header = (page: Page) => page.locator('v-screen-header');
const modal = (page: Page) => page.locator('.kbq-modal-container');
const palette = (page: Page) => page.getByRole('dialog', { name: 'Поиск и команды' });
const paletteInput = (page: Page) =>
    palette(page).getByRole('combobox', { name: 'Найти чат или выбрать команду' });
const userRow = (page: Page, login: string): Locator =>
    page
        .locator('.ag-center-cols-container .ag-row')
        .filter({ has: page.locator('.v-login', { hasText: new RegExp(`^${login.replace('.', '\\.')}$`) }) });
const card = (page: Page) => page.locator('kbq-content-panel');

async function openPalette(page: Page): Promise<void> {
    await page.getByRole('link', { name: 'Поиск и команды' }).click();
    await expect(palette(page)).toBeVisible();
}

async function checkUsers(page: Page, logins: string[]): Promise<void> {
    for (const login of logins) await userRow(page, login).locator('.ag-selection-checkbox input').click();
    await expect(page.getByRole('toolbar', { name: 'Действия с отмеченными' })).toContainText(
        `Выбрано: ${logins.length}`,
    );
}

/** Waits until the main area shows something other than a loader. */
async function mainReady(page: Page): Promise<void> {
    await expect(page.locator('main').first()).toBeVisible();
}

const goto = (group: Group, state: string, as: Persona, path: string, extra: Partial<ScreenState> = {}, feed: 'proto' | 'all' = 'proto'): ScreenState => ({
    group,
    state,
    how: `\`?as=${as}${feed === 'all' ? '' : '&feed=proto'}\` → \`${path}\``,
    run: async ({ page, mock }) => {
        await open(page, mock, as, path, { feed });
        await mainReady(page);
    },
    ...extra,
});

// ---------------------------------------------------------------- foundation
const foundation: ScreenState[] = [
    goto('foundation', 'shell', 'user', '/', { proto: '' }),
    {
        group: 'foundation',
        state: 'user-menu',
        how: '`?as=user&feed=proto` → `/`, click the user button at the bottom of the nav',
        keepPointer: true,
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/');
            await page.locator('.v-nav__user').click();
            await expect(page.locator('.kbq-dropdown__panel, .v-nav__menu').getByText('Выйти')).toBeVisible();
            await page.mouse.move(1439, 789);
        },
    },
];

// ---------------------------------------------------------------- user-start (home, find, run)
const userStart: ScreenState[] = [
    goto('user-start', 'home-feed', 'user', '/'),
    {
        group: 'user-start',
        state: 'home-folder',
        how: '`?as=user&feed=proto` → `/`, mode toggle «Сделать»',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/');
            await page.locator('kbq-button-toggle', { hasText: 'Сделать' }).click();
            await expect(page.locator('[aria-label="Агент"]')).toBeVisible();
        },
    },
    {
        group: 'user-start',
        state: 'home-preset',
        how: `\`?as=user&feed=proto\` → \`/new/t-ci\`, type \`${CI_URL}\` into «Дополнение к задаче»`,
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/new/t-ci');
            const addition = page.getByRole('textbox', { name: 'Дополнение к задаче' });
            await addition.fill(CI_URL);
            await expect(addition).toHaveValue(CI_URL);
        },
    },
    {
        group: 'user-start',
        state: 'home-duplicate',
        how: `\`?as=user&feed=proto\` → \`/new/t-mr\`, type \`${MR_URL}\`, «Запустить» (r-running has the same input)`,
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/new/t-mr');
            await page.getByRole('textbox', { name: 'Дополнение к задаче' }).fill(MR_URL);
            await page.getByRole('button', { name: 'Запустить' }).click();
            await expect(page.getByText('Такой запуск уже идёт')).toBeVisible();
        },
    },
    {
        group: 'user-start',
        state: 'home-first',
        how: '`?as=user&feed=proto&first=1` → `/`',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/', { first: true });
            await mainReady(page);
        },
    },
    {
        group: 'user-start',
        state: 'find-cmds',
        how: '`?as=user&feed=proto` → `/`, click «Поиск и команды» in the nav',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/');
            await openPalette(page);
        },
    },
    {
        group: 'user-start',
        state: 'find-results',
        how: '`?as=user&feed=proto` → `/`, «Поиск и команды», type `conan`',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/');
            await openPalette(page);
            await paletteInput(page).pressSequentially('conan');
            await expect(palette(page).getByRole('group', { name: 'Чаты' })).toBeVisible();
        },
    },
    {
        group: 'user-start',
        state: 'find-none',
        how: '`?as=user&feed=proto` → `/`, «Поиск и команды», type `zigbee`',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/');
            await openPalette(page);
            await paletteInput(page).pressSequentially('zigbee');
            await expect(palette(page).getByText('Ничего не нашлось', { exact: true })).toBeVisible();
        },
    },
    goto('user-start', 'run-queued', 'user', '/tasks/runs/r-queued'),
    goto('user-start', 'run-running', 'user', '/tasks/runs/r-running'),
    goto('user-start', 'run-done', 'user', '/tasks/runs/r-done'),
    goto('user-start', 'run-failed', 'user', '/tasks/runs/r-failed'),
    {
        group: 'user-start',
        state: 'run-cancel',
        how: '`?as=user&feed=proto` → `/tasks/runs/r-running`, header «Отменить»',
        run: async ({ page, mock }) => {
            await open(page, mock, 'user', '/tasks/runs/r-running');
            await header(page).getByRole('button', { name: 'Отменить' }).click();
            await expect(modal(page).getByText('Отменить запуск?')).toBeVisible();
        },
    },
    goto('user-start', 'run-cancelled', 'user', '/tasks/runs/r-cancelled'),
];

// ---------------------------------------------------------------- user-chat (ask, folder)
// q-queued / q-running / q-answer / q-failed / f-planning / f-plan / f-running / f-done are extra state fixtures
// that `feed=proto` drops, so those states use the full feed.
const userChat: ScreenState[] = [
    goto('user-chat', 'ask-queued', 'user', '/questions/q-queued', {}, 'all'),
    goto('user-chat', 'ask-running', 'user', '/questions/q-running', {}, 'all'),
    goto('user-chat', 'ask-clarify', 'user', '/questions/q-clarify'),
    goto('user-chat', 'ask-answer', 'user', '/questions/q-answer', {}, 'all'),
    goto('user-chat', 'ask-failed', 'user', '/questions/q-failed', {}, 'all'),
    goto('user-chat', 'folder-planning', 'user', '/folder/f-planning', {}, 'all'),
    goto('user-chat', 'folder-plan', 'user', '/folder/f-plan', {}, 'all'),
    goto('user-chat', 'folder-running', 'user', '/folder/f-running', {}, 'all'),
    goto('user-chat', 'folder-permission', 'user', '/folder/f-permission'),
    goto('user-chat', 'folder-done', 'user', '/folder/f-done', {}, 'all'),
];

// ---------------------------------------------------------------- admin-people (users, requests, audit)
const SELECTED = ['p.volkov', 'a.nikitina', 'm.belov'];
const adminPeople: ScreenState[] = [
    {
        group: 'admin-people',
        state: 'users-list',
        how: '`?as=admin&feed=proto` → `/admin/users?focus=i.sokolova`',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users?focus=i.sokolova');
            await expect(card(page)).toContainText('i.sokolova');
        },
    },
    {
        group: 'admin-people',
        state: 'users-selected',
        how: `\`/admin/users?focus=i.sokolova\`, check ${SELECTED.join(', ')}`,
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users?focus=i.sokolova');
            await expect(card(page)).toContainText('i.sokolova');
            await checkUsers(page, SELECTED);
        },
    },
    {
        group: 'admin-people',
        state: 'users-block',
        how: `\`/admin/users?focus=i.sokolova\`, check ${SELECTED.join(', ')}, «Заблокировать» in the actions panel`,
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users?focus=i.sokolova');
            await expect(card(page)).toContainText('i.sokolova');
            await checkUsers(page, SELECTED);
            await page
                .getByRole('toolbar', { name: 'Действия с отмеченными' })
                .getByRole('button', { name: 'Заблокировать' })
                .click();
            await expect(modal(page).locator('.kbq-modal-title')).toHaveText('Заблокировать 3 пользователей?');
        },
    },
    {
        group: 'admin-people',
        state: 'users-reset',
        how: '`/admin/users?focus=i.sokolova`, «Сбросить пароль» in the card',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users?focus=i.sokolova');
            await card(page).getByRole('button', { name: 'Сбросить пароль' }).click();
            await expect(modal(page)).toContainText('Сбросить пароль i.sokolova?');
        },
    },
    goto('admin-people', 'users-edit', 'admin', '/admin/users/i.sokolova/edit'),
    {
        group: 'admin-people',
        state: 'users-ws-conflict',
        how: '`/admin/users/i.sokolova/edit`, «Рабочая папка» = `qa/net-utils`, «Сохранить» → 409 (r-running, r-queued are active)',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users/i.sokolova/edit');
            await page.locator('#v-ws').fill('qa/net-utils');
            await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
            await expect(page.locator('kbq-alert').first()).toBeVisible();
        },
    },
    goto('admin-people', 'users-create', 'admin', '/admin/users/new'),
    {
        group: 'admin-people',
        state: 'users-create-error',
        how: '`/admin/users/new`, login `p.volkov`, password `Valeria-2026-tmp`, «Создать» → 409',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/users/new');
            await page.locator('#v-login').fill('p.volkov');
            await page.locator('#v-pass').fill('Valeria-2026-tmp');
            await page.getByRole('button', { name: 'Создать', exact: true }).click();
            await expect(page.getByText('Логин p.volkov уже занят')).toBeVisible();
        },
    },
    goto('admin-people', 'aconsult-list', 'admin', '/admin/requests'),
    goto('admin-people', 'aconsult-user', 'admin', '/admin/requests?user=i.sokolova'),
    goto('admin-people', 'aconsult-tasks', 'admin', '/admin/requests?type=task&period=30d&task=t-ci', {
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/requests?type=task&period=30d&task=t-ci');
            await mainReady(page);
            await expect(page.getByRole('button', { name: /^Задача\s*Разбор упавшего CI-пайплайна/ })).toBeVisible();
        },
    }),
    goto('admin-people', 'aconsult-none', 'admin', '/admin/requests?q=zigbee'),
    goto('admin-people', 'aconsult-view', 'admin', '/admin/requests/q-gcc13'),
    goto('admin-people', 'aconsult-view-run', 'admin', '/admin/requests/r-done'),
    goto('admin-people', 'audit-list', 'admin', '/admin/audit'),
    {
        group: 'admin-people',
        state: 'audit-detail',
        how: '`/admin/audit`, click the 2nd row (prototype `arow: 1`)',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/audit');
            await page.locator('.ag-center-cols-container .ag-row').nth(1).click();
            await expect(page).toHaveURL(/[?&]event=/);
            await expect(card(page)).toBeVisible();
        },
    },
    goto('admin-people', 'audit-none', 'admin', '/admin/audit?q=zigbee'),
];

// ---------------------------------------------------------------- admin-config (stats, tasks, queue)
const EMPTY_DAY = '2026-10-18T00:00:00+03:00..2026-10-18T23:59:00+03:00';
const adminConfig: ScreenState[] = [
    goto('admin-config', 'stats-week', 'admin', '/admin/statistics?period=7d'),
    goto('admin-config', 'stats-day', 'admin', '/admin/statistics?period=24h'),
    goto('admin-config', 'stats-task', 'admin', '/admin/statistics?period=30d&type=t-ci'),
    goto('admin-config', 'stats-empty', 'admin', `/admin/statistics?period=${encodeURIComponent(EMPTY_DAY)}&type=question`),
    goto('admin-config', 'atask-list', 'admin', '/admin/tasks'),
    {
        group: 'admin-config',
        state: 'atask-edit',
        how: '`/__mock/patch tasks t-conan {is_published:0}` (draft, as in the prototype) → `/admin/tasks/t-conan`',
        run: async ({ page, mock }) => {
            await mock.reset('admin', { feed: 'proto' });
            await mock.patch({ collection: 'tasks', id: 't-conan', set: { is_published: 0 } });
            await openAs(page, 'admin', '/admin/tasks/t-conan', 'feed=proto');
            await expect(page.locator('#t-title')).not.toHaveValue('');
        },
    },
    goto('admin-config', 'atask-published', 'admin', '/admin/tasks/t-ci'),
    goto('admin-config', 'queue-live', 'admin', '/admin/queue'),
    {
        group: 'admin-config',
        state: 'queue-stop',
        how: '`/admin/queue`, «Остановить» on p.volkov\'s «Рефакторинг HAL для STM32H7»',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/queue');
            await page
                .locator('.ag-center-cols-container .ag-row')
                .filter({ hasText: 'p.volkov' })
                .first()
                .getByRole('button', { name: 'Остановить' })
                .click();
            await expect(modal(page)).toBeVisible();
        },
    },
    goto('admin-config', 'queue-settings', 'admin', '/admin/queue/settings'),
    goto('admin-config', 'queue-edit', 'admin', '/admin/queue/settings/edit'),
    {
        group: 'admin-config',
        state: 'queue-add',
        how: '`/admin/queue/settings/edit`, «Добавить очередь»',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/queue/settings/edit');
            await page.getByRole('button', { name: 'Добавить очередь' }).click();
            await expect(page.getByRole('button', { name: 'Добавить', exact: true })).toBeVisible();
        },
    },
    {
        group: 'admin-config',
        state: 'queue-archive-blocked',
        how: '`/admin/queue/settings/edit`, «Архивировать» on «Задачи» → 409',
        run: async ({ page, mock }) => {
            await open(page, mock, 'admin', '/admin/queue/settings/edit');
            await page
                .locator('.ag-center-cols-container .ag-row')
                .filter({ hasText: 'Задачи' })
                .first()
                .getByRole('button', { name: 'Архивировать' })
                .click();
            await expect(page.locator('kbq-alert').first()).toBeVisible();
        },
    },
];

export const STATES: readonly ScreenState[] = [...foundation, ...userStart, ...userChat, ...adminPeople, ...adminConfig];
export const statesOf = (group: Group) => STATES.filter((s) => s.group === group);
