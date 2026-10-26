import { expect, openAs, test } from '../fixtures';

test('the app shell opens for a signed-in persona', async ({ page, mock: _mock }) => {
    await openAs(page, 'admin', '/admin/users');
    await expect(page.locator('v-shell')).toBeVisible();
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Пользователи');
});

test('a 502 from /api/auth/me at startup shows the error state, retry opens the app (SPEC §7)', async ({ page, mock }) => {
    await openAs(page, 'user', '/');
    await mock.session({ fail: '/api/auth/me', fail_status: 502, fail_times: 1 });
    await page.goto('/search');
    const error = page.locator('.v-page-state__error');
    await expect(error).toBeVisible();
    await expect(error.getByText('Не удалось загрузить данные')).toBeVisible();
    await expect(page.locator('v-shell')).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe('/search');

    await error.getByRole('button', { name: 'Попробовать снова' }).click();
    await expect(page.locator('v-shell')).toBeVisible();
    await expect(error).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe('/search');
});
