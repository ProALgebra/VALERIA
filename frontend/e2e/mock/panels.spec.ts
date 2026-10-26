import type { Locator, Page } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// SPEC §6.4, §16 item 8: side panels resize within bounds by their left-edge handle, and the width, the chat
// aside visibility and the «Отложенные» / «Архив» folds survive a reload. The main menu has no handle.

const ASIDE = { min: 240, max: 560, fallback: 320 };
const CPANEL = { min: 360, max: 760, fallback: 480 };

async function width(locator: Locator): Promise<number> {
    const box = await locator.boundingBox();
    expect(box, 'panel is rendered').not.toBeNull();
    return Math.round(box!.width);
}

/** Drags a handle horizontally by `dx` px with real mouse events. */
async function drag(page: Page, handle: Locator, dx: number): Promise<void> {
    await handle.scrollIntoViewIfNeeded();
    const box = (await handle.boundingBox())!;
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    const steps = 10;
    for (let i = 1; i <= steps; i++) await page.mouse.move(x + (dx * i) / steps, y);
    await page.mouse.up();
}

/** Reads a `valeria.*` key the way UiStateService stores it (`{ v, value }`). */
async function stored(page: Page, key: string): Promise<unknown> {
    return page.evaluate((k) => {
        const raw = localStorage.getItem(k);
        return raw === null ? null : (JSON.parse(raw) as { value: unknown }).value;
    }, key);
}

test('chat aside: resizes within 240–560, width and visibility survive a reload', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/tasks/runs/r-done');
    const panel = page.locator('v-chat-aside aside');
    const handle = page.locator('v-chat-aside .v-chat-aside__resizer');
    await expect(panel).toBeVisible();
    expect(await width(panel)).toBe(ASIDE.fallback);
    await expect(handle).toHaveCSS('cursor', 'ew-resize');

    // The handle is on the left edge: dragging left widens.
    await drag(page, handle, -100);
    await expect.poll(() => width(panel)).toBe(ASIDE.fallback + 100);

    await drag(page, handle, -1000);
    await expect.poll(() => width(panel)).toBe(ASIDE.max);
    await drag(page, handle, 1000);
    await expect.poll(() => width(panel)).toBe(ASIDE.min);

    await drag(page, handle, -150);
    const chosen = await width(panel);
    expect(chosen).toBeGreaterThan(ASIDE.min);
    expect(chosen).toBeLessThan(ASIDE.max);
    await expect.poll(() => stored(page, 'valeria.chat.aside.width')).toBe(chosen);

    await page.reload();
    await expect(panel).toBeVisible();
    expect(await width(panel)).toBe(chosen);

    // Visibility: «Скрыть панель» hides it, and it stays hidden after a reload.
    await page.getByRole('button', { name: 'Скрыть панель' }).click();
    await expect(panel).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole('button', { name: 'Показать панель' })).toBeVisible();
    await expect(panel).toHaveCount(0);
    await page.getByRole('button', { name: 'Показать панель' }).click();
    await expect(panel).toBeVisible();
    expect(await width(panel)).toBe(chosen);
});

const CPANEL_KEY = 'valeria.cpanel.users.width';

async function openUserCard(page: Page) {
    await openAs(page, 'admin', '/admin/users?focus=p.volkov');
    const container = page.locator('kbq-content-panel-container');
    const panel = container.locator('kbq-content-panel');
    // The resizer sizes this wrapper; `kbq-content-panel` sits inside it.
    const wrapper = container.locator('.kbq-content-panel-container__panel');
    const handle = container.locator('.kbq-content-panel-container__panel-resizer');
    await expect(panel.getByText('p.volkov', { exact: true }).first()).toBeVisible();
    return { panel, wrapper, handle };
}

test('content panel (users card): resizes, stops at 360, width survives a reload', async ({ page, mock: _mock }) => {
    const { panel, wrapper, handle } = await openUserCard(page);
    expect(await width(wrapper)).toBe(CPANEL.fallback);

    // The handle is on the left edge: dragging left widens. Each step waits for the debounced save.
    await drag(page, handle, -120);
    await expect.poll(() => width(wrapper)).toBe(CPANEL.fallback + 120);
    await expect.poll(() => stored(page, CPANEL_KEY)).toBe(CPANEL.fallback + 120);

    await drag(page, handle, 1000);
    await expect.poll(() => width(wrapper)).toBe(CPANEL.min);
    await expect.poll(() => stored(page, CPANEL_KEY)).toBe(CPANEL.min);

    await drag(page, handle, -200);
    await expect.poll(() => width(wrapper)).toBe(CPANEL.min + 200);
    await expect.poll(() => stored(page, CPANEL_KEY)).toBe(CPANEL.min + 200);
    const panelWidth = await width(panel);

    await page.reload();
    await expect(panel.getByText('p.volkov', { exact: true }).first()).toBeVisible();
    await expect.poll(() => width(wrapper)).toBe(CPANEL.min + 200);
    expect(await width(panel)).toBe(panelWidth);
});

test('content panel (users card): stops at 760 and keeps it after a reload (wide window)', async ({
    page,
    mock: _mock,
}) => {
    // 1920 px: 760 + 480 px of content fit next to the menu, so no squeezing is involved.
    await page.setViewportSize({ width: 1920, height: 1000 });
    const { wrapper, handle } = await openUserCard(page);
    await drag(page, handle, -1000);
    await expect.poll(() => width(wrapper)).toBe(CPANEL.max);
    await expect.poll(() => stored(page, CPANEL_KEY)).toBe(CPANEL.max);
    await page.reload();
    await expect.poll(() => width(wrapper)).toBe(CPANEL.max);
});

test('content panel at 1440 px: a drag stops where the content keeps 480 px and stays there', async ({ page, mock: _mock }) => {
    // SPEC §6.4: content keeps 480 px next to the panel, so at 1440 px the drag ends at 1440 - 240 - 480 = 720.
    const limit = 1440 - 240 - 480;
    const { wrapper, handle } = await openUserCard(page);
    await drag(page, handle, -1000);
    await expect.poll(() => width(wrapper)).toBe(limit);
    await expect.poll(() => stored(page, CPANEL_KEY)).toBe(limit);
    await expect.poll(() => width(wrapper), { timeout: 3000 }).toBe(limit);
    // A drag after that is still saved.
    await drag(page, handle, 200);
    await expect.poll(() => stored(page, CPANEL_KEY), { timeout: 3000 }).toBe(limit - 200);
});

test('«Отложенные» and «Архив» folds survive a reload; the main menu has no resize handle', async ({
    page,
    mock: _mock,
}) => {
    await openAs(page, 'user', '/');
    const nav = page.locator('v-nav');
    const snoozed = nav.getByRole('button', { name: /Отложенные/ });
    const archive = nav.getByRole('button', { name: /Архив \d+/ });
    const snoozedRow = nav.getByRole('link', { name: /Почему nightly-сборка firmware-sdk падает только на gcc 13\?/ });
    const archivedRow = nav.getByRole('link', { name: /Как включить отладочные логи в ota-agent\?/ });

    // Default: both folded.
    await expect(snoozed).toHaveAttribute('aria-expanded', 'false');
    await expect(archive).toHaveAttribute('aria-expanded', 'false');
    await expect(snoozedRow).toHaveCount(0);
    await expect(archivedRow).toHaveCount(0);

    await snoozed.click();
    await archive.click();
    await expect(snoozedRow).toBeVisible();
    await expect(archivedRow).toBeVisible();

    await page.reload();
    await expect(snoozed).toHaveAttribute('aria-expanded', 'true');
    await expect(archive).toHaveAttribute('aria-expanded', 'true');
    await expect(snoozedRow).toBeVisible();
    await expect(archivedRow).toBeVisible();

    await snoozed.click();
    await expect(snoozed).toHaveAttribute('aria-expanded', 'false');
    await page.reload();
    await expect(snoozed).toHaveAttribute('aria-expanded', 'false');
    await expect(archive).toHaveAttribute('aria-expanded', 'true');
    await expect(snoozedRow).toHaveCount(0);

    // No resize handle anywhere in the main menu, and its width does not change on a drag at its right edge.
    const navbar = nav.locator('kbq-vertical-navbar');
    const resizeCursors = await navbar.evaluate((root) => {
        const found: string[] = [];
        for (const el of [root, ...Array.from(root.querySelectorAll('*'))]) {
            const cursor = getComputedStyle(el).cursor;
            if (/(ew|col|e|w)-resize/.test(cursor) || /resiz/i.test(el.className?.toString() ?? '')) {
                found.push(`${el.tagName.toLowerCase()}.${el.className} (${cursor})`);
            }
        }
        return found;
    });
    expect(resizeCursors).toEqual([]);
    const before = await width(navbar);
    const box = (await navbar.boundingBox())!;
    await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width + 150, box.y + box.height / 2, { steps: 5 });
    await page.mouse.up();
    expect(await width(navbar)).toBe(before);
});
