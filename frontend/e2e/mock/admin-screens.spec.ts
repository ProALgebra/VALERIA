import type { Page } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// Runtime smoke of the admin screens that format dates with the ru-RU locale (LOCALE_ID 'ru', KBQ_DATE_LOCALE
// 'ru-RU', Luxon adapter): each renders without console errors / page errors and shows Russian dates.

const RU_MONTH_SHORT = '(янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)';
/** Prototype dates: «Сегодня, 14:21», «Вчера, 18:02», «21 окт», «21 окт, 09:31», «15 сен 2025», or dd.MM(.yyyy). */
const RU_DATE = new RegExp(`(Сегодня, \\d{2}:\\d{2}|Вчера, \\d{2}:\\d{2}|\\d{1,2} ${RU_MONTH_SHORT}|\\d{2}\\.\\d{2}(\\.\\d{2,4})?)`);
const RU_DATE_OLD = new RegExp(`(^|\\s)(\\d{1,2} ${RU_MONTH_SHORT}|\\d{2}\\.\\d{2}(\\.\\d{2,4})?)`);
const EN_MONTH = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b|\b(AM|PM)\b|\bToday\b|\bYesterday\b/;

/** Collects console errors and uncaught page errors from now on. */
function watchErrors(page: Page): string[] {
    const errors: string[] = [];
    page.on('console', (m) => {
        if (m.type() === 'error') errors.push(`console: ${m.text()}`);
    });
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    return errors;
}

test('«Запросы пользователей»: list renders with Russian dates, no errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/requests');
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Запросы пользователей');
    const created = page.locator('.ag-center-cols-container .ag-row [col-id="created"]');
    await expect(created.first()).toHaveText(RU_DATE);
    // Older requests use the day + Russian month form.
    const texts = await created.allInnerTexts();
    expect(texts.length).toBeGreaterThan(5);
    for (const t of texts) expect(t.trim()).toMatch(RU_DATE);
    expect(texts.some((t) => RU_DATE_OLD.test(t)), `an older date with a Russian month among: ${texts.join(' | ')}`).toBe(true);
    await expect(page.locator('main')).not.toContainText(EN_MONTH);
    expect(errors).toEqual([]);
});

test('request view (q-volkov-hal) renders read-only with Russian dates, no errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/requests/q-volkov-hal');
    await expect(page.getByText('Ответить или написать от его имени отсюда нельзя')).toBeVisible();
    await expect(page.locator('main')).toContainText(RU_DATE);
    await expect(page.locator('main')).not.toContainText(EN_MONTH);
    expect(errors).toEqual([]);
});

test('request opened from the list renders without errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/requests');
    await page.locator('.ag-center-cols-container .ag-row').first().click();
    await expect(page).toHaveURL(/\/admin\/requests\/[^/?]+/);
    await expect(page.getByText('Ответить или написать от его имени отсюда нельзя')).toBeVisible();
    await expect(page.locator('main')).not.toContainText(EN_MONTH);
    expect(errors).toEqual([]);
});

test('«Аудит»: list and event panel render with Russian dates, no errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/audit');
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Аудит');
    const times = page.locator('.ag-center-cols-container .ag-row [col-id="time"]');
    await expect(times.first()).toHaveText(RU_DATE);
    const texts = await times.allInnerTexts();
    for (const t of texts) expect(t.trim()).toMatch(RU_DATE);
    expect(texts.some((t) => RU_DATE_OLD.test(t)), `an older date with a Russian month among: ${texts.join(' | ')}`).toBe(true);
    await page.locator('.ag-center-cols-container .ag-row').first().click();
    await expect(page.locator('kbq-content-panel')).toContainText(RU_DATE);
    await expect(page.locator('main')).not.toContainText(EN_MONTH);
    expect(errors).toEqual([]);
});

test('«Статистика»: renders, the custom period picker shows Russian month names, no errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/statistics');
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Статистика');
    await expect(page.getByRole('img', { name: /^Контрольная карта/ })).toBeVisible();

    await page.getByRole('button', { name: /^Период/ }).click();
    await page.getByRole('option', { name: /^Произвольный период/ }).click();
    const overlay = page.locator('.cdk-overlay-container');
    // The custom range: two calendars («с» / «по») with Russian month and weekday names, «Применить».
    await expect(overlay.getByRole('button', { name: /Применить/ })).toBeVisible();
    await expect(overlay).toContainText(new RegExp(`с\\s*${RU_MONTH_SHORT}\\s*2026`, 'i'));
    await expect(overlay).toContainText(new RegExp(`по\\s*${RU_MONTH_SHORT}\\s*2026`, 'i'));
    await expect(overlay).toContainText(/Пн\s*Вт\s*Ср\s*Чт\s*Пт\s*Сб\s*Вс/);
    await expect(overlay).not.toContainText(EN_MONTH);
    await expect(overlay).not.toContainText(/\b(Mo|Tu|We|Th|Fr|Sa|Su|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/);
    expect(errors).toEqual([]);
});

test('«Очередь» · «Сейчас» renders without errors', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/queue');
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Очередь');
    await expect(page.getByRole('link', { name: 'Сейчас' })).toBeVisible();
    await expect(page.getByText('Активные сессии')).toBeVisible();
    await expect(page.locator('.ag-center-cols-container .ag-row').first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Остановить' }).first()).toBeVisible();
    expect(errors).toEqual([]);
});

/** Holds `/api/admin/tasks` back so the filter bar renders before the task titles arrive (the deep-link race). */
async function delayTasks(page: Page): Promise<void> {
    await page.route('**/api/admin/tasks', async (route) => {
        await new Promise((r) => setTimeout(r, 800));
        await route.continue();
    });
}

test('«Запросы пользователей»: a `?task=` deep link shows the «Задача» chip with the task title', async ({ page, mock }) => {
    await mock.reset('admin');
    await delayTasks(page);
    await openAs(page, 'admin', '/admin/requests?type=task&period=30d&task=t-ci');
    await expect(page.getByRole('button', { name: /^Задача\s*Разбор упавшего CI-пайплайна/ })).toBeVisible();
    await expect(page.locator('.ag-center-cols-container .ag-row')).toHaveCount(4);

    // Clearing and picking the task again still works after the pipe is re-created.
    const chip = page.getByRole('button', { name: /^Задача\s*Разбор упавшего CI-пайплайна/ });
    // Pipe order: «Период», «Тип», «Задача» — the last «Очистить» belongs to «Задача».
    await page.getByRole('button', { name: 'Очистить' }).last().click();
    await expect(page).not.toHaveURL(/[?&]task=/);
    await page.getByRole('button', { name: /^Задача/ }).click();
    await page.locator('.cdk-overlay-container').getByText('Разбор упавшего CI-пайплайна', { exact: true }).click();
    await expect(page).toHaveURL(/[?&]task=t-ci/);
    await expect(chip).toBeVisible();
});

test('«Статистика»: a `?type=<task>` deep link shows the task title in the «Тип» chip', async ({ page, mock }) => {
    await mock.reset('admin');
    await delayTasks(page);
    await openAs(page, 'admin', '/admin/statistics?period=30d&type=t-ci');
    await expect(page.getByRole('button', { name: /^Тип\s*Разбор упавшего CI-пайплайна/ })).toBeVisible();
});

/** Labels of the filter bar chips, e.g. `['Период', 'Пользователь 2', 'Действие']`. */
async function chips(page: Page): Promise<string[]> {
    const names = await page.locator('kbq-filter-bar').getByRole('button').allInnerTexts();
    return names.map((t) => t.replace(/\s+/g, ' ').trim()).filter((t) => t && t !== 'Сбросить');
}

/** Opens a pipe and ticks the given options of its multi-select (the panel stays open between picks). */
async function pick(page: Page, pipe: RegExp, options: string[]): Promise<void> {
    await page.locator('kbq-filter-bar').getByRole('button', { name: pipe }).click();
    const overlay = page.locator('.cdk-overlay-container');
    for (const name of options) {
        await overlay.getByText(name, { exact: true }).click();
    }
    await page.keyboard.press('Escape');
}

test('«Аудит»: several values per filter are OR, filters are AND, «Сбросить» clears everything', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/audit');
    const rows = page.locator('.ag-center-cols-container .ag-row');
    await expect(rows.first()).toBeVisible();

    await pick(page, /^Действие/, ['Входы', 'Задачи и запуски']);
    await expect(page).toHaveURL(/[?&]action=auth,tasks(&|$)/);
    await expect(page.locator('kbq-filter-bar').getByRole('button', { name: /^Действие\s*2$/ })).toBeVisible();
    // Only «Входы» and «Задачи и запуски» rows, and both kinds of them.
    await expect
        .poll(async () => {
            const codes = await rows.locator('[col-id="action"]').allInnerTexts();
            return (
                codes.length > 0 &&
                codes.every((c) => /\b(auth\.|task|template\.)/.test(c)) &&
                codes.some((c) => c.includes('auth.login')) &&
                codes.some((c) => /\btask/.test(c))
            );
        })
        .toBe(true);

    await pick(page, /^Пользователь/, ['a.nikitina', 'i.sokolova']);
    await expect(page).toHaveURL(/[?&]user=a\.nikitina,i\.sokolova(&|$)/);
    // Either login (who acted or on whose behalf), and rows of both.
    await expect
        .poll(async () => {
            const who = await rows.locator('[col-id="actor"]').allInnerTexts();
            return (
                who.length > 0 &&
                who.every((w) => /a\.nikitina|i\.sokolova/.test(w)) &&
                who.some((w) => w.includes('a.nikitina')) &&
                who.some((w) => w.includes('i.sokolova'))
            );
        })
        .toBe(true);

    await page.locator('kbq-filter-reset').getByRole('button', { name: 'Сбросить' }).click();
    await expect(page).toHaveURL(/\/admin\/audit$/);
    await expect.poll(() => chips(page)).toEqual(['Период', 'Пользователь', 'Действие']);
    await expect(page.locator('kbq-filter-reset')).toHaveCount(0);
    // The rows reload unfiltered: actions and people outside the old filters are back.
    await expect(rows.locator('[col-id="action"]').filter({ hasText: /\b(user|queue|chat|concurrency)\./ }).first()).toBeVisible();
    await expect(rows.locator('[col-id="actor"]').filter({ hasText: 'd.orlov' }).first()).toBeVisible();
    expect(errors).toEqual([]);
});

test('«Запросы пользователей»: several tasks are OR, «Сбросить» clears every filter', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/requests');
    const rows = page.locator('.ag-center-cols-container .ag-row');
    await expect(rows.first()).toBeVisible();

    await pick(page, /^Тип/, ['Задача']);
    await expect(page).toHaveURL(/[?&]type=task(&|$)/);
    await pick(page, /^Задача/, ['Разбор упавшего CI-пайплайна', 'Ревью merge request']);
    await expect(page).toHaveURL(/[?&]task=t-ci,t-mr(&|$)/);
    await expect(page.locator('kbq-filter-bar').getByRole('button', { name: /^Задача\s*2$/ })).toBeVisible();
    await expect
        .poll(async () => {
            const kinds = await rows.locator('[col-id="title"]').allInnerTexts();
            return (
                kinds.length > 0 &&
                kinds.every((k) => /Задача · (Разбор упавшего CI-пайплайна|Ревью merge request)/.test(k)) &&
                kinds.some((k) => k.includes('Разбор упавшего CI-пайплайна')) &&
                kinds.some((k) => k.includes('Ревью merge request'))
            );
        })
        .toBe(true);
    await expect(page.locator('v-screen-header kbq-badge')).toContainText(/\d+ из \d+/);

    await page.locator('kbq-filter-reset').getByRole('button', { name: 'Сбросить' }).click();
    await expect(page).toHaveURL(/\/admin\/requests$/);
    await expect.poll(() => chips(page)).toEqual(['Пользователь', 'Период', 'Тип']);
    await expect(page.locator('v-screen-header kbq-badge')).not.toContainText('из');
    await expect(rows.locator('[col-id="title"]').filter({ hasText: 'Вопрос' }).first()).toBeVisible();
    expect(errors).toEqual([]);
});

test('«Статистика»: several types are OR, «Сбросить» brings back the default period and every type', async ({ page, mock }) => {
    await mock.reset('admin');
    const errors = watchErrors(page);
    await openAs(page, 'admin', '/admin/statistics');
    const kpi = page.locator('.v-kpi__v').first();
    await expect.poll(async () => Number(await kpi.innerText())).toBeGreaterThan(0);
    const all = Number(await kpi.innerText());

    await pick(page, /^Тип/, ['Вопросы', 'Разбор упавшего CI-пайплайна']);
    await expect(page).toHaveURL(/[?&]type=question,t-ci(&|$)/);
    await expect(page.locator('kbq-filter-bar').getByRole('button', { name: /^Тип\s*2$/ })).toBeVisible();
    await expect.poll(async () => Number(await kpi.innerText())).toBeLessThan(all);
    const picked = Number(await kpi.innerText());
    expect(picked).toBeGreaterThan(0);

    await page.locator('kbq-filter-reset').getByRole('button', { name: 'Сбросить' }).click();
    await expect(page).toHaveURL(/\/admin\/statistics$/);
    await expect.poll(() => chips(page)).toEqual(['Период Последние 7 дней', 'Тип']);
    await expect.poll(async () => Number(await kpi.innerText())).toBeGreaterThan(picked);
    expect(errors).toEqual([]);
});
