import type { Locator, Page } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// Modal title follows the prototype: plural(n, 'пользователя', 'пользователей', 'пользователей').
// SPEC §16 item 11 — bulk block in «Пользователи» with partial success (SPEC §10.2, §11.1 «Массовые операции — атомарность»).

const row = (page: Page, login: string): Locator =>
    page.locator('.ag-center-cols-container .ag-row').filter({ has: page.locator('.v-login', { hasText: new RegExp(`^${login.replace('.', '\\.')}$`) }) });
const check = (page: Page, login: string) => row(page, login).locator('.ag-selection-checkbox input').click();
const blockedBadge = (page: Page, login: string) => row(page, login).locator('kbq-badge', { hasText: 'Заблокирован' });
const actionsPanel = (page: Page) => page.getByRole('toolbar', { name: 'Действия с отмеченными' });
const blockModal = (page: Page) => page.locator('.kbq-modal-container');
const toast = (page: Page, text: string) => page.locator('kbq-toast').filter({ hasText: text });

async function statusOf(mock: { state(): Promise<any> }, id: string): Promise<string> {
    const state = await mock.state();
    return state.users.find((u: { id: string }) => u.id === id)?.status;
}

test('partial success: «Заблокированы 2 из 3: не удалось заблокировать p.volkov», table shows the new statuses', async ({ page, mock }) => {
    await mock.reset('admin');
    await mock.session({ fail: 'batch:u-pvolkov' });
    await openAs(page, 'admin', '/admin/users');
    for (const login of ['p.volkov', 'a.nikitina', 'm.belov']) await check(page, login);

    await expect(actionsPanel(page)).toContainText('Выбрано: 3');
    await actionsPanel(page).getByRole('button', { name: 'Заблокировать' }).click();

    const modal = blockModal(page);
    await expect(modal.locator('.kbq-modal-title')).toHaveText('Заблокировать 3 пользователей?');
    await expect(modal).toContainText('p.volkov, a.nikitina, m.belov');
    await modal.getByRole('button', { name: 'Заблокировать' }).click();
    await expect(modal).toBeHidden();

    await expect(toast(page, 'Заблокированы 2 из 3: не удалось заблокировать p.volkov')).toBeVisible();

    // The table reflects what the server did.
    await expect(blockedBadge(page, 'a.nikitina')).toBeVisible();
    await expect(blockedBadge(page, 'm.belov')).toBeVisible();
    await expect(blockedBadge(page, 'p.volkov')).toHaveCount(0);
    expect(await statusOf(mock, 'u-anikitina')).toBe('disabled');
    expect(await statusOf(mock, 'u-mbelov')).toBe('disabled');
    expect(await statusOf(mock, 'u-pvolkov')).toBe('active');
});

test('full success: own account and already blocked are not counted, «Заблокированы 2 пользователя»', async ({ page, mock }) => {
    await mock.reset('admin');
    await openAs(page, 'admin', '/admin/users');
    for (const login of ['d.orlov', 's.egorova', 'p.volkov', 'm.belov']) await check(page, login);

    await expect(actionsPanel(page)).toContainText('Выбрано: 4');
    await actionsPanel(page).getByRole('button', { name: 'Заблокировать' }).click();

    const modal = blockModal(page);
    await expect(modal.locator('.kbq-modal-title')).toHaveText('Заблокировать 2 пользователей?');
    await expect(modal).toContainText('Вашу учётку d.orlov блокировка не затронет.');
    await expect(modal).toContainText('Уже заблокированы: s.egorova.');
    await modal.getByRole('button', { name: 'Заблокировать' }).click();

    await expect(toast(page, 'Заблокированы 2 пользователя')).toBeVisible();
    await expect(blockedBadge(page, 'p.volkov')).toBeVisible();
    await expect(blockedBadge(page, 'm.belov')).toBeVisible();
    await expect(blockedBadge(page, 'd.orlov')).toHaveCount(0);
    expect(await statusOf(mock, 'u-dorlov')).toBe('active');
});

test('every target fails: «Не удалось заблокировать p.volkov», nothing changes', async ({ page, mock }) => {
    await mock.reset('admin');
    await mock.session({ fail: 'batch:u-pvolkov' });
    await openAs(page, 'admin', '/admin/users');
    await check(page, 'p.volkov');
    await actionsPanel(page).getByRole('button', { name: 'Заблокировать' }).click();
    await expect(blockModal(page).locator('.kbq-modal-title')).toHaveText('Заблокировать 1 пользователя?');
    await blockModal(page).getByRole('button', { name: 'Заблокировать' }).click();
    await expect(toast(page, 'Не удалось заблокировать p.volkov')).toBeVisible();
    await expect(blockedBadge(page, 'p.volkov')).toHaveCount(0);
    expect(await statusOf(mock, 'u-pvolkov')).toBe('active');
});

test('«Заблокировать» is disabled when every checked user is already blocked', async ({ page, mock }) => {
    await mock.reset('admin');
    await openAs(page, 'admin', '/admin/users');
    await check(page, 's.egorova');
    await expect(actionsPanel(page).getByRole('button', { name: 'Заблокировать' })).toBeDisabled();
    await expect(actionsPanel(page).getByRole('button', { name: 'Разблокировать' })).toBeEnabled();
});
