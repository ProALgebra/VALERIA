import { expect, openAs, test } from '../fixtures';
import type { Locator, Page } from '@playwright/test';

// SPEC §16 item 4: question flow, clarification, follow-up; the menu status changes over SSE without reload.

const LIVE = { timeout: 30_000 };
const ANSWER_HEAD = 'Таймаут OTA задаётся в двух местах:';
const CLARIFY_QUESTION = 'В коде два механизма обновления. О каком речь?';

/** Nav row of a chat (SPEC §6.1), found by its route. */
const navRow = (page: Page, href: string): Locator => page.locator(`v-nav a.v-nav__chat[href="${href}"]`);

/** Heading of the nav block the row sits in: «Активные», «Готово», «Отложенные» or «Архив». */
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

/** Counts full document loads after the call: the test must never reload. */
function countReloads(page: Page): () => number {
    let loads = 0;
    page.on('load', () => loads++);
    return () => loads;
}

test('ask on home → clarification → answer → follow-up, menu follows over SSE', async ({ page, mock: _mock }) => {
    const question = 'Как настроить таймаут OTA для медленных 2G-каналов в ota-agent?';
    await openAs(page, 'user', '/');
    const reloads = countReloads(page);

    await page.locator('kbq-button-toggle', { hasText: 'Я только спросить' }).click();
    await page.getByRole('textbox', { name: 'Что нужно сделать?' }).fill(question);
    await page.getByRole('button', { name: 'Спросить', exact: true }).click();

    await page.waitForURL(/\/questions\/[^/]+$/);
    const href = new URL(page.url()).pathname;
    await expect(page.getByText(question, { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Только чтение', { exact: true })).toBeVisible();

    // «Valeria уточняет»: the chat waits for the user, the menu shows it in «Активные».
    const card = page.locator('v-clarify-card');
    await expect(card).toBeVisible(LIVE);
    await expect(card.getByText('Valeria уточняет')).toBeVisible();
    await expect(card.getByText(CLARIFY_QUESTION)).toBeVisible();
    const row = navRow(page, href);
    await expectInMenu(row, 'Активные', 'Ждёт вашего ответа');

    const send = card.getByRole('button', { name: 'Отправить ответ' });
    await expect(send).toBeDisabled();
    await card.locator('kbq-radio-button', { hasText: 'OTA прошивки устройства' }).click();
    await expect(send).toBeEnabled();
    await send.click();

    await expect(card).toBeHidden(LIVE);
    await expect(page.getByText('OTA прошивки устройства', { exact: true })).toBeVisible();

    // Answer: markdown, «Копировать ответ», the menu moves the chat to «Готово».
    const answers = page.locator('v-bot-message', { hasText: ANSWER_HEAD });
    await expect(answers).toHaveCount(1, LIVE);
    await expect(page.getByRole('button', { name: 'Копировать ответ' }).first()).toBeVisible();
    await expectInMenu(row, 'Готово', 'Ответ готов');

    // Follow-up in the same chat.
    const followUp = 'А где задаётся число повторных попыток загрузки?';
    const composer = page.getByRole('textbox', { name: 'Задать следующий вопрос' });
    await expect(composer).toBeEnabled();
    await composer.fill(followUp);
    // Ctrl/⌘ Enter sends; the click on «Спросить» has its own test below.
    await composer.press('ControlOrMeta+Enter');
    await expect(page.getByText(followUp, { exact: true })).toBeVisible();
    await expect(answers).toHaveCount(2, LIVE);
    await expectInMenu(row, 'Готово', 'Ответ готов');
    await expect(page).toHaveURL(new RegExp(`${href}$`));

    expect(reloads(), 'the page must not be reloaded').toBe(0);
});

test('queued question: «В очереди», then the menu changes over SSE while on another screen', async ({ page, mock }) => {
    await openAs(page, 'user', '/questions/q-queued');
    const reloads = countReloads(page);

    await expect(page.locator('v-status-line').getByText(/^В очереди/)).toBeVisible();
    const row = navRow(page, '/questions/q-queued');
    await expectInMenu(row, 'Активные', 'В очереди');

    // Leave the chat (SPA navigation), then let the job run.
    await page.getByRole('link', { name: 'Новый запрос' }).click();
    await expect(page).toHaveURL(/\/$/);
    await mock.play('q-queued');

    await expectInMenu(row, 'Готово', 'Ответ готов');

    await row.click();
    await expect(page).toHaveURL(/\/questions\/q-queued$/);
    await expect(page.locator('v-bot-message', { hasText: ANSWER_HEAD })).toBeVisible(LIVE);
    expect(reloads(), 'the page must not be reloaded').toBe(0);
});

test('a seeded clarification (q-clarify) is answered and the answer arrives', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/questions/q-clarify');
    const reloads = countReloads(page);
    const row = navRow(page, '/questions/q-clarify');
    await expectInMenu(row, 'Активные', 'Ждёт вашего ответа');

    const card = page.locator('v-clarify-card');
    await expect(card.getByText(CLARIFY_QUESTION)).toBeVisible();
    await card.locator('kbq-radio-button', { hasText: 'Обновление конфигурации по MQTT' }).click();
    await card.getByRole('button', { name: 'Отправить ответ' }).click();

    await expect(page.locator('v-bot-message', { hasText: ANSWER_HEAD })).toBeVisible(LIVE);
    await expectInMenu(row, 'Готово', 'Ответ готов');
    expect(reloads(), 'the page must not be reloaded').toBe(0);
});

test('the composer send button can be clicked after typing a follow-up', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/questions/q-answer');
    const composer = page.getByRole('textbox', { name: 'Задать следующий вопрос' });
    await composer.fill('А где задаётся число повторных попыток загрузки?');
    await page.getByRole('button', { name: 'Спросить', exact: true }).click({ timeout: 5_000 });
    await expect(page.getByText('А где задаётся число повторных попыток загрузки?', { exact: true })).toBeVisible();
});
