import type { Page } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// Cmd+K (Ctrl+K off a Mac) opens «Поиск и команды» from any signed-in screen, even while typing in a field,
// and never stacks a second dialog. `ControlOrMeta` follows the same platform rule as the app.

const HOTKEY = 'ControlOrMeta+KeyK';

const palette = (page: Page) => page.getByRole('dialog', { name: 'Поиск и команды' });

async function expectOnePalette(page: Page): Promise<void> {
    await expect(palette(page)).toBeVisible();
    await expect(palette(page)).toHaveCount(1);
    await expect(page).toHaveURL(/\/search$/);
}

test('opens from home while typing in the field, and a second press keeps one dialog', async ({ page }) => {
    await openAs(page, 'user', '/');
    const field = page.getByRole('textbox', { name: 'Что нужно сделать?' });
    await expect(field).toBeFocused();
    await field.pressSequentially('черновик');

    await page.keyboard.press(HOTKEY);
    await expectOnePalette(page);
    // The search field has focus, so the next press comes from inside the dialog.
    await expect(palette(page).getByRole('combobox', { name: 'Найти чат или выбрать команду' })).toBeFocused();

    await page.keyboard.press(HOTKEY);
    await expectOnePalette(page);

    // Escape closes it and returns to home with the draft still there.
    await page.keyboard.press('Escape');
    await expect(palette(page)).toHaveCount(0);
    await expect(field).toHaveValue('черновик');
});

test('opens from an admin screen', async ({ page }) => {
    await openAs(page, 'admin', '/admin/audit');
    await expect(page.getByRole('heading', { name: 'Аудит' })).toBeVisible();

    await page.keyboard.press(HOTKEY);
    await expectOnePalette(page);
});

test('the menu row shows the shortcut', async ({ page }) => {
    await openAs(page, 'user', '/');
    const row = page.getByRole('link', { name: 'Поиск и команды' });
    await expect(row).toHaveAttribute('aria-keyshortcuts', /^(Meta|Control)\+K$/);
    await expect(row.locator('.kbq-badge')).toHaveText(/^(⌘ K|Ctrl \+ K)$/);
});
