import { expect, openAs, test } from '../fixtures';
import type { Locator, Page } from '@playwright/test';

// SPEC §16 item 6: work in a folder: plan → «Выполнить план в режиме Build» → permission → done.

const LIVE = { timeout: 30_000 };
const PLAN_HEAD = 'План перевода net-utils на Conan 2:';
const COMMAND = 'conan create . -pr:h=profiles/armv8-gcc12 --build=missing';
const DONE_OK = 'net-utils собирается на Conan 2.';

const navRow = (page: Page, href: string): Locator => page.locator(`v-nav a.v-nav__chat[href="${href}"]`);

async function navGroup(row: Locator): Promise<string | null> {
    if (!(await row.count())) return null;
    return row.evaluate((el) => {
        for (let n = el.previousElementSibling; n; n = n.previousElementSibling) {
            if (n.classList.contains('v-nav__section')) return (n.textContent ?? '').trim();
            if (n.classList.contains('v-nav__fold')) return (n.querySelector('kbq-navbar-title')?.textContent ?? '').trim();
        }
        return null;
    });
}

async function expectInMenu(row: Locator, group: string, status: string): Promise<void> {
    await expect.poll(() => navGroup(row), LIVE).toBe(group);
    await expect(row.locator('.v-nav__st')).toHaveAttribute('aria-label', status, LIVE);
}

test('«Сделать» with Plan → plan → Build → permission allowed once → done', async ({ page, mock: _mock }) => {
    const prompt = 'Переведи пакет net-utils на Conan 2: conanfile.py, профили, CI. Сначала покажи план, e2e.';
    await openAs(page, 'user', '/');

    await page.locator('kbq-button-toggle', { hasText: 'Сделать' }).click();
    await page.locator('kbq-button-toggle-group[aria-label="Агент"]').locator('kbq-button-toggle', { hasText: 'Plan' }).click();
    await page.getByRole('textbox', { name: 'Что нужно сделать?' }).fill(prompt);
    await page.getByRole('button', { name: 'Начать', exact: true }).click();

    await page.waitForURL(/\/folder\/[^/]+$/);
    const href = new URL(page.url()).pathname;
    await expect(page.getByText(prompt, { exact: true })).toBeVisible();

    // The plan is ready: plan text + «Выполнить план в режиме Build»; the chat is «Готово» in the menu.
    await expect(page.getByText(PLAN_HEAD)).toBeVisible(LIVE);
    const runPlan = page.getByRole('button', { name: 'Выполнить план в режиме Build' });
    await expect(runPlan).toBeVisible();
    await expect(page.getByText('или напишите, что поправить')).toBeVisible();
    const row = navRow(page, href);
    await expectInMenu(row, 'Готово', 'Готово');

    await runPlan.click();
    await expect(runPlan).toBeHidden(LIVE);
    await expect(page.getByText('Выполни план', { exact: true })).toBeVisible();

    // The agent asks a permission: the card replaces the composer.
    const perm = page.getByRole('group', { name: 'Valeria просит разрешение на команду' });
    await expect(perm).toBeVisible(LIVE);
    await expect(perm.getByText(COMMAND, { exact: true })).toBeVisible();
    await expect(perm.getByText('Зачем:')).toBeVisible();
    await expect(perm.getByRole('button', { name: 'Всегда разрешать conan create' })).toBeVisible();
    await expect(perm.getByRole('button', { name: 'Отклонить' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Сообщение агенту' })).toHaveCount(0);
    await expectInMenu(row, 'Активные', 'Ждёт разрешения');

    await perm.getByRole('button', { name: 'Разрешить один раз' }).click();
    await expect(perm).toBeHidden(LIVE);

    // Done.
    await expect(page.getByText(DONE_OK)).toBeVisible(LIVE);
    await expectInMenu(row, 'Готово', 'Готово');
    await expect(page.getByRole('textbox', { name: 'Сообщение агенту' })).toBeEnabled();
});

test('a seeded permission (f-permission) rejected → the "denied" ending', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/folder/f-permission');
    const perm = page.getByRole('group', { name: 'Valeria просит разрешение на команду' });
    await expect(perm.getByText(COMMAND, { exact: true })).toBeVisible();
    await perm.getByRole('button', { name: 'Отклонить' }).click();
    await expect(page.getByText('Файлы обновлены. Сборку я не проверила', { exact: false })).toBeVisible(LIVE);
    await expectInMenu(navRow(page, '/folder/f-permission'), 'Готово', 'Готово');
});

test('a working folder chat offers «Остановить» in place of «Отправить», not in the header', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/folder/f-running');

    const composer = page.locator('v-chat-composer');
    const stop = composer.getByRole('button', { name: 'Остановить' });
    await expect(stop).toBeVisible(LIVE);
    await expect(composer.getByRole('button', { name: 'Отправить' })).toHaveCount(0);
    await expect(page.locator('v-screen-header').getByRole('button', { name: 'Остановить' })).toHaveCount(0);

    await stop.click();
    await expect(composer.getByRole('button', { name: 'Отправить' })).toBeVisible(LIVE);
    await expect(stop).toHaveCount(0);
});
