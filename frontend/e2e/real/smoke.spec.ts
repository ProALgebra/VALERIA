import { expect, test, type Page } from '@playwright/test';

const ADMIN = { login: 'admin', password: process.env['E2E_ADMIN_PASSWORD']! };
const USER = { login: 'smoke-user', password: process.env['E2E_USER_PASSWORD']! };

/** Collects console errors, CSP violations and failed API calls; the smoke fails if any appear. */
async function watchErrors(page: Page): Promise<string[]> {
    const errors: string[] = [];
    await page.addInitScript(() =>
        document.addEventListener('securitypolicyviolation', (e) =>
            console.error(`csp violation: ${e.effectiveDirective} ${e.blockedURI}`),
        ),
    );
    page.on('console', (m) => m.type() === 'error' && errors.push(`console: ${m.text()}`));
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('response', (r) => {
        if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.request().method()} ${r.url()}`);
    });
    return errors;
}

/** Signs in through the login form; the 401 from the anonymous /api/auth/me probe is cleared from `errors`. */
async function signIn(page: Page, who: { login: string; password: string }, errors: string[]): Promise<void> {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login/);
    await page.locator('input[formcontrolname="login"]').fill(who.login);
    await page.locator('input[formcontrolname="password"]').fill(who.password);
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(page).not.toHaveURL(/\/login/);
    errors.length = 0;
}

test('security basics: docs are off, the favicon and the SPA come from /static', async ({ page, request }) => {
    // /docs and /redoc are page URLs, so they fall through to the app shell; the schema is a plain 404.
    const shell = await (await request.get('/static/index.html')).text();
    for (const path of ['/docs', '/redoc']) {
        const response = await request.get(path);
        expect(await response.text(), path).toBe(shell);
    }
    expect((await request.get('/openapi.json')).status()).toBe(404);
    expect((await request.get('/static/favicon.svg')).ok()).toBeTruthy();
    expect((await request.get('/static/app.js')).status()).toBe(404);
    const shellResponse = await page.goto('/');
    // Defense in depth: the shell only runs scripts from our origin.
    expect(shellResponse?.headers()['content-security-policy']).toContain("script-src 'self' 'unsafe-eval';");
    await expect(page.locator('link[rel="icon"]')).toHaveAttribute('href', '/static/favicon.svg');
    await expect(page).toHaveURL(/\/login/);
});

test('login: a wrong password shows the error, then the admin signs in', async ({ page }) => {
    const errors = await watchErrors(page);
    await page.goto('/login');
    await page.locator('input[formcontrolname="login"]').fill('admin');
    await page.locator('input[formcontrolname="password"]').fill('wrong-password');
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(page.locator('.v-login__error')).toBeVisible();
    errors.length = 0; // the 401 above is expected
    await page.locator('input[formcontrolname="password"]').fill(ADMIN.password);
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(page).toHaveURL('/');
    await expect(page.getByText('Я только спросить')).toBeVisible();
    // The session lives only in the HttpOnly cookie: nothing token-like in Web Storage.
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    expect(stored).not.toMatch(/csrf|token|session/i);
    expect(errors).toEqual([]);
});

test('admin pages open without errors', async ({ page }) => {
    const errors = await watchErrors(page);
    await signIn(page, ADMIN, errors);
    const pages: [string, string][] = [
        ['/admin/statistics', 'Статистика'],
        ['/admin/users', 'Пользователи'],
        ['/admin/tasks', 'Настройка задач'],
        ['/admin/queue', 'Очередь'],
        ['/admin/requests', 'Запросы пользователей'],
        ['/admin/audit', 'Аудит'],
    ];
    for (const [path, title] of pages) {
        await page.goto(path);
        await expect(page.locator('h1.v-screen-header__title'), path).toHaveText(title);
        await expect(page.locator('.v-page-state__error'), path).toHaveCount(0);
    }
    // A custom period is formatted by Koobiq's DateFormatter (messageformat), which must work under the CSP.
    await page.goto('/admin/audit?period=custom&from=2026-01-01T00:00:00.000Z&to=2026-01-02T00:00:00.000Z');
    await expect(page.locator('h1.v-screen-header__title')).toHaveText('Аудит');
    await expect(page.locator('kbq-filter-bar').getByText(/янв/i).first()).toBeVisible();
    await page.goto('/admin/users');
    await expect(page.getByText('smoke-user').first()).toBeVisible();
    expect(errors).toEqual([]);
});

test('user: new request screen, then a question with a clarification makes the round trip', async ({ page }) => {
    const errors = await watchErrors(page);
    await signIn(page, USER, errors);
    await expect(page.getByText('Я только спросить')).toBeVisible();
    await page.getByText('Я только спросить').click();
    const composer = page.locator('textarea').last();
    await composer.fill('Как прошить плату?');
    const send = page.getByRole('button', { name: 'Спросить', exact: true });
    await expect(send).toBeEnabled();
    await send.click();
    await expect(page).toHaveURL(/\/questions\//, { timeout: 20_000 });

    // The fake agent asks «Для какой платы?»; the status reaches the screen over SSE.
    await expect(page.getByText('Для какой платы?')).toBeVisible({ timeout: 45_000 });
    await page.getByText('Плата A', { exact: true }).click();
    await page.getByRole('button', { name: 'Отправить ответ' }).click();
    await expect(page.getByText('Ответ для: Плата A')).toBeVisible({ timeout: 45_000 });
    // The menu moves the chat into the «Готово» group once the SSE status arrives.
    await expect(page.getByText(/^готово$/i).first()).toBeVisible();
    expect(errors).toEqual([]);
});
