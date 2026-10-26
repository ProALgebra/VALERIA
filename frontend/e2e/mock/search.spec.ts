import type { Locator, Page } from '@playwright/test';
import { expect, openAs, test } from '../fixtures';

// SPEC §12, §16 item 9: one search algorithm everywhere — case-insensitive, from the first character, NFKD
// diacritics both ways, whitespace tokens with AND, a "quoted phrase" is one token, trim at the edges only.
//
// The seed has no diacritics, so each test first renames fixtures through `/__mock/patch` (the mock's
// "someone else changed it" control) before the page loads; the app reads them as ordinary API data.

const LAFITE = 'Château Lafite: парсер'; // f-config, a finished folder chat
const MARGAUX = 'Chateau Margaux: логи'; // q-ota-logs, an archived question (palette searches the archive too)

/** The rows shown, in any order (lists are sorted by activity / last login). */
async function expectSet(rows: Locator, expected: string[], message?: string): Promise<void> {
    await expect
        .poll(async () => (await rows.allInnerTexts()).map((t) => t.trim()).sort(), { message })
        .toEqual([...expected].sort());
}

/** Types the query into a search input the way a user does (input events), replacing what was there. */
async function search(input: Locator, query: string): Promise<void> {
    await input.fill('');
    await input.fill(query);
}

test.describe('palette «Поиск и команды»', () => {
    async function openPalette(page: Page) {
        await page.getByRole('link', { name: 'Поиск и команды' }).click();
        const dialog = page.getByRole('dialog', { name: 'Поиск и команды' });
        await expect(dialog).toBeVisible();
        const input = dialog.getByRole('combobox', { name: 'Найти чат или выбрать команду' });
        const chats = dialog.getByRole('group', { name: 'Чаты' }).locator('.v-palette__title');
        const nothing = dialog.getByText('Ничего не нашлось', { exact: true });
        return { input, chats, nothing };
    }

    test('case, first character, diacritics both ways, tokens, quotes and trim', async ({ page, mock }) => {
        await mock.patch({ collection: 'chats', id: 'f-config', set: { title: LAFITE } });
        await mock.patch({ collection: 'chats', id: 'q-ota-logs', set: { title: MARGAUX } });
        await openAs(page, 'user', '/');
        const { input, chats, nothing } = await openPalette(page);

        // From the first character, case-insensitive: "l" finds «Lafite» (its only l is a capital).
        await input.pressSequentially('l');
        await expect(chats.filter({ hasText: LAFITE })).toHaveCount(1);
        await expect(nothing).toHaveCount(0);

        // Diacritics: plain query finds accented data, accented query finds plain data; composed and decomposed.
        for (const q of ['chateau', 'CHATEAU', 'château', 'CHÂTEAU', 'cha\u0302teau' /* decomposed â */]) {
            await search(input, q);
            await expectSet(chats, [LAFITE, MARGAUX], q);
        }

        // Tokens with AND, in any order.
        await search(input, 'chateau логи');
        await expectSet(chats, [MARGAUX]);
        await search(input, 'логи château');
        await expectSet(chats, [MARGAUX]);
        await search(input, 'château логи zigbee');
        await expect(nothing).toBeVisible();

        // A quoted phrase is one token.
        await search(input, '"chateau margaux"');
        await expectSet(chats, [MARGAUX]);
        await search(input, '"margaux chateau"');
        await expect(nothing).toBeVisible();
        await search(input, 'margaux chateau');
        await expectSet(chats, [MARGAUX]);

        // Trim at the edges, not inside.
        await search(input, '   chateau   ');
        await expectSet(chats, [LAFITE, MARGAUX]);
        await search(input, '  "château lafite"  ');
        await expectSet(chats, [LAFITE]);
        await search(input, '"château  lafite"');
        await expect(nothing).toBeVisible();
    });
});

test.describe('admin «Пользователи» table (login, folder, Mattermost)', () => {
    test('case, first character, diacritics both ways, tokens, quotes and trim', async ({ page, mock }) => {
        // Signing in re-applies the seed folders (mock applyProfile), so patch after the persona switch, then reload.
        await openAs(page, 'admin', '/admin/users');
        // Accented data: m.belov's Mattermost. Plain data searched with an accented query: youtrack-bot's folder.
        await mock.patch({ collection: 'users', id: 'u-mbelov', set: { mattermost_username: 'michaël.belov' } });
        await mock.patch({
            collection: 'users',
            id: 'u-youtrack',
            set: { workspace_path: '/home/valeria/workspaces/integrations/cafe' },
        });
        await page.reload();
        const input = page.getByRole('textbox', { name: 'Логин, папка или Mattermost' });
        const logins = page.locator('.ag-center-cols-container .ag-row [col-id="user"] .v-login');
        const nobody = page.getByText('Никого не нашли', { exact: true });
        await expect(logins).toHaveCount(7);

        // First character, upper case query vs lower case data.
        await input.pressSequentially('Y');
        await expectSet(logins, ['youtrack-bot']);

        // Login, folder, Mattermost fields.
        await search(input, 'P.VOLKOV');
        await expectSet(logins, ['p.volkov']);
        await search(input, 'hal-stm32');
        await expectSet(logins, ['p.volkov']);
        await search(input, 'michael');
        await expectSet(logins, ['m.belov']);
        await search(input, 'MICHAËL');
        await expectSet(logins, ['m.belov']);
        await search(input, 'CAFÉ');
        await expectSet(logins, ['youtrack-bot']);

        // Tokens with AND across fields.
        await search(input, 'qa firmware');
        await expectSet(logins, ['i.sokolova', 'a.nikitina']);
        await search(input, 'firmware nikitina');
        await expectSet(logins, ['a.nikitina']);

        // Quoted phrase (account type «Задачи и вопросы»).
        await search(input, 'и задачи');
        await expectSet(logins, ['a.nikitina', 'm.belov', 'youtrack-bot']);
        await search(input, '"и задачи"');
        await expect(nobody).toBeVisible();

        // Trim at the edges, not inside.
        await search(input, '  "задачи и"  ');
        await expectSet(logins, ['a.nikitina', 'm.belov', 'youtrack-bot']);
        await search(input, '"задачи  и"');
        await expect(nobody).toBeVisible();
    });
});

test.describe('admin «Запросы пользователей» list', () => {
    test('diacritics, tokens, quotes and trim over title and user', async ({ page, mock }) => {
        const RESUME = 'Какие задачи YouTrack вошли в версию 4.12 — résumé';
        await mock.patch({ collection: 'chats', id: 'q-belov-412', set: { title: RESUME } });
        await openAs(page, 'admin', '/admin/requests');
        const input = page.getByRole('textbox', { name: 'Текст запроса или логин' });
        const titles = page.locator('.ag-center-cols-container .ag-row .v-title');
        const none = page.getByText('Запросов не нашли', { exact: true });
        await expect(titles.filter({ hasText: RESUME })).toHaveCount(1);

        for (const q of ['resume', 'RÉSUMÉ', 'Résumé']) {
            await search(input, q);
            await expectSet(titles, [RESUME], q);
        }

        // Tokens: user login AND a word of the title.
        await search(input, 'volkov hal');
        await expectSet(titles, [
            'Рефакторинг HAL для STM32H7',
            'Чем отличается HAL_UART_Transmit_DMA от HAL_UART_Transmit_IT на STM32H7 при высокой нагрузке?',
        ]);

        await search(input, '"HAL для"');
        await expectSet(titles, ['Рефакторинг HAL для STM32H7']);
        await search(input, '"для HAL"');
        await expect(none).toBeVisible();

        await search(input, '   "hal для"   ');
        await expectSet(titles, ['Рефакторинг HAL для STM32H7']);
        await search(input, '"hal  для"');
        await expect(none).toBeVisible();
    });
});
