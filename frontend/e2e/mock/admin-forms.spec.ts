import type { Locator, Page } from '@playwright/test';
import { expect, openAs, test, type MockControl } from '../fixtures';

// SPEC §16 item 10 — admin forms: field error under the field, alert above the form,
// leaving a changed form, two-admin conflict (SPEC §10.2–10.4, §11.1 «Синхронная форма», «Предупреждение об уходе…»,
// «Одновременная работа»).

/** The kbq-form-field that hosts `control`. */
const fieldOf = (control: Locator) => control.locator('xpath=ancestor::kbq-form-field[1]');

/** A form field in the Koobiq error state with `text` in its `<kbq-error>`. */
async function expectFieldError(control: Locator, text: string): Promise<void> {
    const field = fieldOf(control);
    await expect(field).toHaveClass(/kbq-form-field_invalid/);
    await expect(field.locator('kbq-error')).toHaveText(text);
    await expect(field.locator('kbq-error')).toBeVisible();
}

/** The alert sits above the form: above the first form section. */
async function expectAboveForm(page: Page, alert: Locator): Promise<void> {
    await expect(alert).toBeVisible();
    const a = await alert.boundingBox();
    const f = await page.locator('fieldset').first().boundingBox();
    expect(a && f && a.y + a.height <= f.y + 1, 'alert is above the first form section').toBeTruthy();
}

const leaveModal = (page: Page) =>
    page.locator('.kbq-modal-container').filter({ hasText: 'Несохраненные данные будут потеряны.' });

/**
 * Types into a Koobiq number input. kbqNumberInput hands the value to the form in a `setTimeout(0)` after
 * `input`; a page-side zero timeout queued after it resolves once the model has the value (not a fixed sleep).
 */
async function fillNumber(page: Page, input: Locator, value: string): Promise<void> {
    await input.fill(value);
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
}

async function failNext(mock: MockControl, path: string, status = 500): Promise<void> {
    await mock.session({ fail: path, fail_status: status, fail_times: 1 });
}

test.describe('field errors under the field', () => {
    test('user create: a taken login is shown under «Логин», input is kept', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/new');
        const login = page.locator('#v-login');
        await login.fill('p.volkov');
        await page.locator('#v-pass').fill('Valeria-2026-tmp');
        await page.locator('#v-ws').fill('qa/net-utils');
        await page.getByRole('button', { name: 'Создать', exact: true }).click();
        await expectFieldError(login, 'Логин p.volkov уже занят');
        await expect(login).toHaveValue('p.volkov');
        await expect(page.locator('#v-pass')).toHaveValue('Valeria-2026-tmp');
        await expect(page).toHaveURL(/\/admin\/users\/new$/);
    });

    test('user edit: an invalid task limit is shown under «Лимит задач»', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/p.volkov/edit');
        const limit = page.locator('#v-limit');
        await limit.fill('42');
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        await expectFieldError(limit, 'От 1 до 10');
        await expect(page).toHaveURL(/\/admin\/users\/p\.volkov\/edit$/);
    });

    test('task form: a duplicate title shows «Такое название уже есть» under «Название»', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/new');
        const title = page.locator('#t-title');
        // Uniqueness ignores case and surrounding spaces (SPEC §11.1 «Названия объектов»).
        await title.fill('  ревью MERGE request ');
        await page.locator('#t-content').fill('Проверь MR.');
        await page.getByRole('button', { name: 'Сохранить черновик' }).click();
        await expectFieldError(title, 'Такое название уже есть');
        await expect(page).toHaveURL(/\/admin\/tasks\/new$/);
    });

    test('queue edit: an invalid worker limit shows an error hint under the number field', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/queue/settings/edit');
        const workers = page.getByRole('textbox', { name: 'Воркеров, не больше: Задачи' });
        await fillNumber(page, workers, '');
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        // Number inputs are not form-field controls: the error is a kbq-hint color="error" in the same cell.
        const cell = workers.locator('xpath=ancestor::div[contains(concat(" ", @class, " "), " ag-cell ")][1]');
        await expect(cell.locator('kbq-hint[color="error"]')).toHaveText('От 1 до 10');
        await expect(page).toHaveURL(/\/admin\/queue\/settings\/edit$/);
    });
});

test.describe('server error: alert above the form', () => {
    test('user create: 500 shows «Не удалось создать пользователя» above the form, input kept', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/new');
        await page.locator('#v-login').fill('n.novikov');
        await page.locator('#v-pass').fill('Valeria-2026-tmp');
        await page.locator('#v-ws').fill('qa/novikov');
        await failNext(mock, '/api/admin/users');
        await page.getByRole('button', { name: 'Создать', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Не удалось создать пользователя' });
        await expectAboveForm(page, alert);
        await expect(page.locator('#v-login')).toHaveValue('n.novikov');
        await expect(page).toHaveURL(/\/admin\/users\/new$/);
    });

    test('task edit: 500 shows «Не удалось сохранить» above the form', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/t-ci');
        await page.locator('#t-desc').fill('Новое описание.');
        await failNext(mock, '/api/admin/tasks/t-ci');
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Не удалось сохранить' });
        await expectAboveForm(page, alert);
        await expect(page.locator('#t-desc')).toHaveValue('Новое описание.');
        await expect(page).toHaveURL(/\/admin\/tasks\/t-ci$/);
    });

    test('queue edit: 500 shows «Не удалось сохранить» above the form', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/queue/settings/edit');
        await fillNumber(page, page.getByRole('textbox', { name: 'Воркеров, не больше: Задачи' }), '3');
        await failNext(mock, '/api/admin/queues/tasks');
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Не удалось сохранить' });
        await expectAboveForm(page, alert);
        await expect(page).toHaveURL(/\/admin\/queue\/settings\/edit$/);
    });
});

test.describe('leaving a changed form', () => {
    test('user edit: «Остаться» keeps the edits, «Все равно перейти» leaves', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/p.volkov/edit');
        const mm = page.locator('#v-mm');
        await mm.fill('pavel.volkov');

        await page.getByRole('link', { name: 'Новый запрос' }).click();
        await expect(leaveModal(page)).toBeVisible();
        await leaveModal(page).getByRole('button', { name: 'Остаться' }).click();
        await expect(leaveModal(page)).toBeHidden();
        await expect(page).toHaveURL(/\/admin\/users\/p\.volkov\/edit$/);
        await expect(mm).toHaveValue('pavel.volkov');

        await page.getByRole('link', { name: 'Новый запрос' }).click();
        await leaveModal(page).getByRole('button', { name: 'Все равно перейти' }).click();
        await expect(page).toHaveURL((u) => u.pathname === '/');
    });

    test('task form: «Отмена» asks first; confirming leaves to the list', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/t-ci');
        await page.locator('#t-desc').fill('Черновик правки');
        await page.getByRole('link', { name: 'Отмена' }).click();
        await expect(leaveModal(page)).toBeVisible();
        await leaveModal(page).getByRole('button', { name: 'Остаться' }).click();
        await expect(page).toHaveURL(/\/admin\/tasks\/t-ci$/);
        await expect(page.locator('#t-desc')).toHaveValue('Черновик правки');

        await page.getByRole('link', { name: 'Отмена' }).click();
        await leaveModal(page).getByRole('button', { name: 'Все равно перейти' }).click();
        await expect(page).toHaveURL(/\/admin\/tasks$/);
    });

    test('an unchanged form leaves without asking', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/queue/settings/edit');
        await expect(page.getByRole('textbox', { name: 'Воркеров, не больше: Задачи' })).toBeVisible();
        await page.getByRole('link', { name: 'Отмена' }).click();
        await expect(page).toHaveURL(/\/admin\/queue\/settings$/);
        await expect(leaveModal(page)).toHaveCount(0);
    });

    test('queue edit: closing the tab with edits triggers beforeunload; dismissing keeps the page', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/queue/settings/edit');
        const workers = page.getByRole('textbox', { name: 'Воркеров, не больше: Задачи' });
        // A real click + typing: Chromium shows beforeunload only after a user gesture.
        await workers.click();
        await page.keyboard.press('ControlOrMeta+a');
        await page.keyboard.type('3');
        await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
        const dialog = page.waitForEvent('dialog');
        await page.close({ runBeforeUnload: true });
        const d = await dialog;
        expect(d.type()).toBe('beforeunload');
        await d.dismiss();
        expect(page.isClosed()).toBe(false);
        await expect(workers).toHaveValue('3');
    });
});

test.describe('two admins: conflict and deletion', () => {
    test('user edit: another admin saved first → «Объект изменён пользователем p.volkov», «Перезаписать» saves', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/a.nikitina/edit');
        await page.locator('#v-mm').fill('anna.nikitina');
        await mock.patch({ collection: 'users', id: 'u-anikitina', set: {}, by: 'p.volkov' });
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Объект изменён пользователем p.volkov' });
        await expectAboveForm(page, alert);
        await expect(page.locator('#v-mm')).toHaveValue('anna.nikitina');
        await alert.getByRole('button', { name: 'Перезаписать' }).click();
        await expect(page).toHaveURL(/\/admin\/users\?focus=a\.nikitina/);
        // The card of the saved user shows the overwritten Mattermost.
        await expect(page.locator('kbq-content-panel').getByText('@anna.nikitina')).toBeVisible();
    });

    test('user edit: another admin deleted the user → «Объект удалён пользователем p.volkov»', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/users/a.nikitina/edit');
        await page.locator('#v-mm').fill('anna.nikitina');
        await mock.patch({ collection: 'users', id: 'u-anikitina', delete: true, by: 'p.volkov' });
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Объект удалён пользователем p.volkov' });
        await expectAboveForm(page, alert);
        await expect(alert.getByRole('button', { name: 'Перезаписать' })).toHaveCount(0);
        await expect(page).toHaveURL(/\/admin\/users\/a\.nikitina\/edit$/);
    });

    test('task edit: conflict alert with «Перезаписать»; overwriting saves and returns to the list', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/t-ci');
        await page.locator('#t-desc').fill('Моя правка описания.');
        await mock.patch({ collection: 'tasks', id: 't-ci', set: { description: 'Правка Волкова' }, by: 'p.volkov' });
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Объект изменён пользователем p.volkov' });
        await expectAboveForm(page, alert);
        await expect(page.locator('#t-desc')).toHaveValue('Моя правка описания.');
        await alert.getByRole('button', { name: 'Перезаписать' }).click();
        await expect(page).toHaveURL(/\/admin\/tasks$/);
    });

    test('task edit: deleted by another admin → «Объект удалён пользователем p.volkov» with «Создать объект»', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/t-ci');
        await page.locator('#t-desc').fill('Моя правка описания.');
        await mock.patch({ collection: 'tasks', id: 't-ci', delete: true, by: 'p.volkov' });
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Объект удалён пользователем p.volkov' });
        await expectAboveForm(page, alert);
        await expect(alert.getByRole('button', { name: 'Создать объект' })).toBeVisible();
        await expect(page).toHaveURL(/\/admin\/tasks\/t-ci$/);
    });

    test('queue edit: conflict on a queue another admin changed → «Объект изменён пользователем p.volkov»', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/queue/settings/edit');
        await fillNumber(page, page.getByRole('textbox', { name: 'Воркеров, не больше: Задачи' }), '3');
        await mock.patch({ collection: 'queues', id: 'tasks', set: { priority: 150 }, by: 'p.volkov' });
        await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        const alert = page.locator('kbq-alert').filter({ hasText: 'Объект изменён пользователем p.volkov' });
        await expectAboveForm(page, alert);
        await alert.getByRole('button', { name: 'Перезаписать' }).click();
        await expect(page).toHaveURL(/\/admin\/queue\/settings$/);
    });
});

test.describe('first field is focused on open', () => {
    for (const [path, first] of [
        ['/admin/users/new', '#v-login'],
        ['/admin/users/i.sokolova/edit', 'kbq-radio-group input[type="radio"]:checked'],
        ['/admin/tasks/new', '#t-title'],
        ['/admin/tasks/t-ci', '#t-title'],
        ['/admin/queue/settings/edit', '#q-limit'],
    ] as const) {
        test(path, async ({ page, mock }) => {
            await mock.reset('admin');
            await openAs(page, 'admin', path);
            await expect(page.locator(first)).toBeFocused();
        });
    }
});

test.describe('task form: «Кто видит» user search', () => {
    test('filters as you type: any case, accents, several words; keeps the picked users', async ({ page, mock }) => {
        await mock.reset('admin');
        await openAs(page, 'admin', '/admin/tasks/new');
        await page.locator('kbq-radio-button').filter({ hasText: 'Выбранные' }).click();
        const select = page.locator('kbq-select[aria-label="Пользователи"]');
        await select.click();
        const search = page.getByRole('textbox', { name: 'Поиск пользователей' });
        await expect(search).toBeFocused();
        const options = page.locator('kbq-option');

        await search.fill('S');
        await expect(options).toHaveText(['i.sokolova', 's.egorova']);
        await search.fill('  SÓKOL ');
        await expect(options).toHaveText(['i.sokolova']);
        await options.first().click();
        await search.fill('v ova');
        await expect(options).toHaveText(['i.sokolova', 's.egorova']);
        await search.fill('bel');
        await expect(options).toHaveText(['m.belov']);
        await options.first().click();
        await search.fill('zzz');
        await expect(options).toHaveCount(0);
        await expect(page.locator('[kbq-select-search-empty-result]')).toHaveText('Ничего не найдено');

        await page.keyboard.press('Escape');
        await page.keyboard.press('Escape');
        await expect(select.locator('kbq-tag')).toHaveText(['i.sokolova', 'm.belov']);
    });
});
