import { expect, openAs, test } from '../fixtures';
import type { Locator, Page } from '@playwright/test';

// SPEC §16 item 7 / §6.2: the chat menu «…» for a normal, a snoozed and an archived chat; an action on the open chat
// returns to home.

const MENU_LABEL = 'Действия с чатом';
const SNOOZE_ITEMS = ['На 1 час', 'До 10 утра завтра', 'До 10 утра понедельника'];

const navRow = (page: Page, href: string): Locator => page.locator(`v-nav a.v-nav__chat[href="${href}"]`);
const fold = (page: Page, name: 'Отложенные' | 'Архив'): Locator => page.locator('v-nav button.v-nav__fold', { hasText: name });
/** The open dropdown panel (Koobiq renders it in the CDK overlay). */
const menuPanel = (page: Page): Locator => page.locator('.cdk-overlay-container .kbq-dropdown__panel');
const menuItems = (page: Page): Locator => menuPanel(page).locator('.kbq-dropdown-item');

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

async function openFold(page: Page, name: 'Отложенные' | 'Архив'): Promise<void> {
    const button = fold(page, name);
    if ((await button.getAttribute('aria-expanded')) !== 'true') await button.click();
    await expect(button).toHaveAttribute('aria-expanded', 'true');
}

/** Opens «…» of a nav row (it shows on hover). */
async function openRowMenu(page: Page, row: Locator): Promise<void> {
    await row.hover();
    await row.getByRole('button', { name: MENU_LABEL }).click();
    await expect(menuPanel(page)).toBeVisible();
}

async function openHeaderMenu(page: Page): Promise<void> {
    await page.locator('v-screen-header').getByRole('button', { name: MENU_LABEL }).click();
    await expect(menuPanel(page)).toBeVisible();
}

async function expectMenu(page: Page, caption: string | null, items: string[]): Promise<void> {
    await expect(menuItems(page)).toHaveText(items);
    const header = menuPanel(page).locator('.kbq-optgroup-label');
    if (caption) await expect(header).toHaveText([caption]);
    else await expect(header).toHaveCount(0);
}

const toast = (page: Page, title: string): Locator => page.locator('kbq-toast', { hasText: title });

test('normal chat: «Отложить» items, «В архив», «Удалить» — in the row and in the header', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/');
    const row = navRow(page, '/questions/q-answer');
    await expect.poll(() => navGroup(row)).toBe('Готово');

    await openRowMenu(page, row);
    await expectMenu(page, 'Отложить', [...SNOOZE_ITEMS, 'В архив', 'Удалить']);
    await page.keyboard.press('Escape');
    await expect(menuPanel(page)).toBeHidden();

    await row.click();
    await expect(page).toHaveURL(/\/questions\/q-answer$/);
    await openHeaderMenu(page);
    await expectMenu(page, 'Отложить', [...SNOOZE_ITEMS, 'В архив', 'Удалить']);
});

test('snoozed chat: «Вернуть из отложенных», «В архив», «Удалить»; unsnooze returns it to its group', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/');
    await openFold(page, 'Отложенные');
    const row = navRow(page, '/questions/q-gcc13');
    await expect.poll(() => navGroup(row)).toBe('Отложенные');
    await expect(row.locator('.v-nav__when')).toHaveText('пн, 10:00');

    await openRowMenu(page, row);
    await expectMenu(page, 'Отложить', ['Вернуть из отложенных', 'В архив', 'Удалить']);

    // Same menu in the header of the open snoozed chat.
    await page.keyboard.press('Escape');
    await expect(menuPanel(page)).toBeHidden();
    await row.click();
    await expect(page).toHaveURL(/\/questions\/q-gcc13$/);
    await openHeaderMenu(page);
    await expectMenu(page, 'Отложить', ['Вернуть из отложенных', 'В архив', 'Удалить']);

    await menuItems(page).filter({ hasText: 'Вернуть из отложенных' }).click();
    await expect(toast(page, 'Возвращено из отложенных')).toBeVisible();
    await expect.poll(() => navGroup(row)).toBe('Готово');
});

test('archived chat: «Вернуть из архива», «Удалить»; restore returns it to its group', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/');
    await openFold(page, 'Архив');
    const row = navRow(page, '/questions/q-ota-logs');
    await expect.poll(() => navGroup(row)).toBe('Архив');

    await openRowMenu(page, row);
    await expectMenu(page, null, ['Вернуть из архива', 'Удалить']);
    await page.keyboard.press('Escape');
    await expect(menuPanel(page)).toBeHidden();

    await row.click();
    await expect(page).toHaveURL(/\/questions\/q-ota-logs$/);
    await openHeaderMenu(page);
    await expectMenu(page, null, ['Вернуть из архива', 'Удалить']);

    await menuItems(page).filter({ hasText: 'Вернуть из архива' }).click();
    await expect(toast(page, 'Возвращено из архива')).toBeVisible();
    await expect.poll(() => navGroup(row)).toBe('Готово');
});

test('«В архив» on the open chat (header menu) returns to home', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/questions/q-answer');
    await openHeaderMenu(page);
    await menuItems(page).filter({ hasText: 'В архив' }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('heading', { name: 'Что нужно сделать?' })).toBeVisible();
    await expect(toast(page, 'Перенесено в архив')).toBeVisible();
    await openFold(page, 'Архив');
    await expect.poll(() => navGroup(navRow(page, '/questions/q-answer'))).toBe('Архив');
});

test('«На 1 час» on the open chat (row menu) returns to home and moves it to «Отложенные»', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/tasks/runs/r-done');
    const row = navRow(page, '/tasks/runs/r-done');
    await openRowMenu(page, row);
    await menuItems(page).filter({ hasText: 'На 1 час' }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(toast(page, 'Чат отложен')).toBeVisible();
    await openFold(page, 'Отложенные');
    await expect.poll(() => navGroup(row)).toBe('Отложенные');
    await expect(row.locator('.v-nav__when')).toHaveText(/^15:3\d$/);
});

test('«Удалить» on the open chat: confirmation, Esc keeps it, «Удалить» deletes and returns to home', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/folder/f-done');
    const row = navRow(page, '/folder/f-done');
    await openRowMenu(page, row);
    await menuItems(page).filter({ hasText: 'Удалить' }).click();

    const dialog = page.locator('.kbq-modal-container');
    await expect(dialog.getByText('Удалить чат?')).toBeVisible();
    await expect(
        dialog.getByText('Чат «Миграция net-utils на Conan 2» удалится вместе с ответами. Восстановить его не получится.'),
    ).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Отмена' })).toBeFocused();
    // Koobiq handles Esc only after the opening animation ends (kbqAfterOpen).
    await expect(dialog).not.toHaveClass(/zoom-enter/);
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(page).toHaveURL(/\/folder\/f-done$/);
    await expect(row).toHaveCount(1);

    await openHeaderMenu(page);
    await menuItems(page).filter({ hasText: 'Удалить' }).click();
    await page.locator('.kbq-modal-container').getByRole('button', { name: 'Удалить' }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(toast(page, 'Чат удалён')).toBeVisible();
    await expect(row).toHaveCount(0);
    const state = await _mock.state();
    expect(state.items.some((i: { id: string }) => i.id === 'f-done')).toBe(false);
});
