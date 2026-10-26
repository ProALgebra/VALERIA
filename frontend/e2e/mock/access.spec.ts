import type { Page, Request } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// SPEC §4, §16 items 2 and 3: roles hide what they can never use, a direct URL to a closed section shows
// «Раздел недоступен для вашей учётки» inside the shell, and a missing workspace disables sending with a reason.

const DENIED_TITLE = 'Раздел недоступен для вашей учётки';
const NO_FOLDER = 'Администратор ещё не назначил вам рабочую папку';
const SYSTEM_ITEMS = ['Статистика', 'Пользователи', 'Настройка задач', 'Очередь', 'Запросы пользователей', 'Аудит'];

/** Accessible name with the icon glyphs (private-use characters) around the text ignored. */
const named = (text: string) =>
    new RegExp(`^[^\\p{L}\\p{N}]*${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^\\p{L}\\p{N}]*$`, 'u');
const systemButton = (page: Page) => page.locator('v-nav').getByRole('button', { name: named('Система') });
const modeToggle = (page: Page, name: string) => page.locator('kbq-button-toggle', { hasText: name });

async function expectDenied(page: Page, path: string): Promise<void> {
    await expect(page.getByText(DENIED_TITLE, { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'На главную', exact: true })).toBeVisible();
    // The shell stays and the address is the one typed.
    await expect(page.locator('v-nav')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Поиск и команды' })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe(path);
}

/** Opens the «Все варианты · N» dropdown on home and returns the overlay panel. */
async function openVariants(page: Page) {
    await page.getByRole('button', { name: /^Все варианты · \d+/ }).click();
    const panel = page.locator('.kbq-dropdown__panel');
    await expect(panel.getByText('Задачи', { exact: true })).toBeVisible();
    return panel;
}

test.describe('roles (§16.2)', () => {
    test('admin sees «Система» with every admin section', async ({ page, mock: _mock }) => {
        await openAs(page, 'admin', '/');
        await expect(systemButton(page)).toBeVisible();
        await systemButton(page).click();
        for (const label of SYSTEM_ITEMS) {
            await expect(page.locator('.kbq-dropdown__panel').getByRole('link', { name: named(label) })).toBeVisible();
        }
        await expect(modeToggle(page, 'Сделать')).toBeVisible();
    });

    test('user: «Сделать» and templates are there, «Система» is not; /admin/users is denied in place', async ({
        page,
        mock: _mock,
    }) => {
        await openAs(page, 'user', '/');
        await expect(page.getByRole('heading', { name: 'Что нужно сделать?' })).toBeVisible();
        await expect(modeToggle(page, 'Я только спросить')).toBeVisible();
        await expect(modeToggle(page, 'Сделать')).toBeVisible();
        await expect(systemButton(page)).toHaveCount(0);
        const panel = await openVariants(page);
        await expect(panel.getByText('Шаблоны для «Сделать»', { exact: true })).toBeVisible();
        await page.keyboard.press('Escape');

        await page.goto('/admin/users');
        await expectDenied(page, '/admin/users');
        await expect(page.locator('h1.v-screen-header__title')).toHaveText('Пользователи');
        await expect(systemButton(page)).toHaveCount(0);

        // «На главную» leads home.
        await page.getByRole('link', { name: 'На главную', exact: true }).click();
        await page.waitForURL((u) => u.pathname === '/');
        await expect(page.getByRole('heading', { name: 'Что нужно сделать?' })).toBeVisible();
    });

    test('tasks: no «Сделать», templates or «Система»; folder and admin URLs are denied; no 403 reaches the UI', async ({
        page,
        mock: _mock,
    }) => {
        const forbidden: string[] = [];
        const got403: string[] = [];
        const watch = (req: Request) => {
            const { pathname } = new URL(req.url());
            if (
                pathname === '/api/chats' ||
                pathname.startsWith('/api/chats/') ||
                pathname === '/api/options' ||
                pathname.startsWith('/api/templates') ||
                pathname.startsWith('/api/admin/')
            ) {
                forbidden.push(`${req.method()} ${pathname}`);
            }
        };
        page.on('request', watch);
        page.on('response', (res) => {
            if (res.status() === 403) got403.push(`${res.request().method()} ${new URL(res.url()).pathname}`);
        });

        await openAs(page, 'tasks', '/');
        await expect(page.getByRole('heading', { name: 'Что нужно сделать?' })).toBeVisible();
        await expect(page.getByRole('button', { name: 'Спросить', exact: true })).toBeVisible();
        await expect(modeToggle(page, 'Сделать')).toHaveCount(0);
        await expect(systemButton(page)).toHaveCount(0);
        const panel = await openVariants(page);
        await expect(panel.getByText('Шаблоны для «Сделать»', { exact: true })).toHaveCount(0);
        await page.keyboard.press('Escape');

        // Palette: no «Сделать» command for this account.
        await page.getByRole('link', { name: 'Поиск и команды' }).click();
        const palette = page.getByRole('dialog', { name: 'Поиск и команды' });
        await expect(palette).toBeVisible();
        await expect(palette.getByRole('option', { name: 'Спросить' })).toBeVisible();
        await expect(palette.getByRole('option', { name: 'Сделать', exact: true })).toHaveCount(0);

        // Folder work («Сделать») by direct URL.
        await page.goto('/folder/f-plan');
        await expectDenied(page, '/folder/f-plan');

        // Admin section by direct URL.
        await page.goto('/admin/users');
        await expectDenied(page, '/admin/users');

        // Screens this account may open still work.
        await page.goto('/tasks/runs/r-done');
        await expect(page.getByText('Готово', { exact: true }).first()).toBeVisible();
        await page.goto('/questions/q-answer');
        await expect(page.locator('h1.v-screen-header__title')).toBeVisible();

        await page.goto('/');
        await expect(page.getByRole('heading', { name: 'Что нужно сделать?' })).toBeVisible();

        expect(forbidden, 'tasks_only must never call these routes').toEqual([]);
        expect(got403, 'no 403 for tasks_only').toEqual([]);
        await expect(page.locator('kbq-toast')).toHaveCount(0);
        await expect(page.getByText('Попробовать снова')).toHaveCount(0);
    });
});

test.describe('no workspace (§16.3)', () => {
    async function expectBlocked(page: Page, label: string): Promise<void> {
        const button = page.getByRole('button', { name: named(label) });
        await expect(button).toBeDisabled();
        // A disabled button can't be hovered: the reason sits on the focusable wrapper.
        const wrap = page.locator('v-send-button .v-send-button__wrap', { has: button });
        await expect(wrap).toHaveAttribute('aria-label', `${label}. ${NO_FOLDER}`);
        await wrap.hover();
        await expect(page.locator('.kbq-tooltip', { hasText: NO_FOLDER })).toBeVisible();
        await page.mouse.move(0, 0);
        await expect(page.locator('.kbq-tooltip', { hasText: NO_FOLDER })).toHaveCount(0);
    }

    test('home shows the alert and «Спросить», «Начать», «Запустить» are disabled with the reason', async ({
        page,
        mock: _mock,
    }) => {
        await openAs(page, 'user', '/', 'folder=0');
        const alert = page.locator('kbq-alert', { hasText: NO_FOLDER });
        await expect(alert).toBeVisible();
        await expect(alert).toContainText(
            'Без неё нельзя задать вопрос, запустить задачу или работать в папке. Напишите администратору.',
        );

        // «Спросить» stays disabled for the folder reason even with text in the field.
        await page.getByRole('textbox', { name: 'Что нужно сделать?' }).fill('Почему падает сборка?');
        await expectBlocked(page, 'Спросить');

        await modeToggle(page, 'Сделать').click();
        await expectBlocked(page, 'Начать');

        await modeToggle(page, 'Я только спросить').click();
        await page.locator('.v-home__chip', { hasText: 'Разбор упавшего CI-пайплайна' }).click();
        await page.waitForURL((u) => u.pathname === '/new/t-ci');
        await page.getByRole('textbox', { name: 'Дополнение к задаче' }).fill('https://gitlab.example.com/x/-/pipelines/1');
        await expectBlocked(page, 'Запустить');
        await expect(page.locator('kbq-alert', { hasText: NO_FOLDER })).toBeVisible();
    });
});
