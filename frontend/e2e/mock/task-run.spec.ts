import { expect, openAs, test } from '../fixtures';
import type { Page } from '@playwright/test';

// SPEC §16 item 5: task run from home (addition, Mattermost line), cancel with confirmation, «Запустить ещё раз»;
// SCENARIOS #4 duplicate «Такой запуск уже идёт».

const LIVE = { timeout: 30_000 };
const MR_URL = 'https://gitlab.example.com/fw/firmware-sdk/-/merge_requests/1932';
const MATTERMOST = 'Когда задача завершится, Valeria напишет вам в Mattermost @i.sokolova';

const header = (page: Page) => page.locator('v-screen-header');
const aside = (page: Page) => page.locator('v-chat-aside');

async function taskRunIds(mock: { state(): Promise<any> }): Promise<string[]> {
    const state = await mock.state();
    return (state.items as { kind: string; id: string }[]).filter((i) => i.kind === 'task').map((i) => String(i.id));
}

test('run a task from home: no note, Mattermost line, addition on the run screen, the report', async ({ page, mock }) => {
    const pipeline = 'https://gitlab.example.com/fw/firmware-sdk/-/pipelines/48300';
    await openAs(page, 'user', '/');

    await page.locator('.v-home__presets').getByRole('button', { name: 'Разбор упавшего CI-пайплайна' }).click();
    await expect(page).toHaveURL(/\/new\/t-ci$/);

    const addition = page.getByRole('textbox', { name: 'Дополнение к задаче' });
    await expect(addition).toHaveAttribute('placeholder', 'Ссылка на пайплайн или job GitLab');
    // No note under the field: the addition is described by its placeholder only.
    await expect(page.getByText('Текст добавится в конец инструкции задачи.')).toHaveCount(0);
    await expect(page.locator('.v-home__notify')).toHaveText(MATTERMOST);

    await addition.fill(pipeline);
    await page.getByRole('button', { name: 'Запустить' }).click();

    await page.waitForURL(/\/tasks\/runs\/[^/]+$/);
    expect(page.url()).not.toContain('r-done');
    await expect(page.getByText('Задача поставлена в очередь')).toBeVisible();

    // Aside: Задача, Ваше дополнение.
    await expect(aside(page).getByText('Ваше дополнение')).toBeVisible();
    await expect(aside(page).getByText(pipeline, { exact: true })).toBeVisible();
    await expect(aside(page).getByText('Разбор упавшего CI-пайплайна', { exact: true })).toBeVisible();

    // Ends with the report.
    await expect(header(page).locator('kbq-badge')).toHaveText('Завершена', LIVE);
    await expect(page.getByText('Итог', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Копировать итог' })).toBeVisible();
    await expect(aside(page).getByText('Длительность')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Причина' })).toBeVisible();
});

test('cancel asks for confirmation; «Запустить ещё раз» starts a new run with the same addition', async ({ page, mock }) => {
    await openAs(page, 'user', '/tasks/runs/r-running');
    const badge = header(page).locator('kbq-badge');
    await expect(badge).toHaveText('В работе');
    await expect(aside(page).getByText(MR_URL, { exact: true })).toBeVisible();

    // «Не отменять» keeps the run going.
    await header(page).getByRole('button', { name: 'Отменить' }).click();
    const dialog = page.locator('.kbq-modal-container');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Отменить запуск?')).toBeVisible();
    await expect(dialog.getByText('Valeria прервёт задачу «Ревью merge request». Итога не будет, запуск останется в списке со статусом «Отменена».')).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Не отменять' })).toBeFocused();
    await dialog.getByRole('button', { name: 'Не отменять' }).click();
    await expect(dialog).toBeHidden();
    await expect(badge).toHaveText('В работе');
    const before = await mock.state();
    expect(before.queue.find((j: { id: string }) => j.id === 'r-running')?.status).toBe('running');

    // Confirmed: cancelled.
    await header(page).getByRole('button', { name: 'Отменить' }).click();
    await page.locator('.kbq-modal-container').getByRole('button', { name: 'Отменить запуск' }).click();
    await expect(page.locator('.kbq-modal-container')).toBeHidden();
    await expect(badge).toHaveText('Отменена', LIVE);
    await expect(page.getByText(/^Запуск отменён в \d{1,2}:\d{2}$/)).toBeVisible();
    await expect(header(page).getByRole('button', { name: 'Отменить' })).toHaveCount(0);

    // «Запустить ещё раз» → the task preset with the same addition → a new run.
    const runsBefore = await taskRunIds(mock);
    await page.getByRole('button', { name: 'Запустить ещё раз' }).click();
    await expect(page).toHaveURL(/\/new\/t-mr$/);
    await expect(page.getByRole('textbox', { name: 'Дополнение к задаче' })).toHaveValue(MR_URL);
    await page.getByRole('button', { name: 'Запустить' }).click();

    await page.waitForURL((u) => /\/tasks\/runs\/[^/]+$/.test(u.pathname) && !u.pathname.endsWith('/r-running'));
    const newId = new URL(page.url()).pathname.split('/').pop()!;
    expect(runsBefore).not.toContain(newId);
    await expect(aside(page).getByText(MR_URL, { exact: true })).toBeVisible();
    await expect(header(page).locator('kbq-badge')).toHaveText('Завершена', LIVE);
});

test('a failed run offers «Запустить ещё раз» with the same addition', async ({ page, mock: _mock }) => {
    await openAs(page, 'user', '/tasks/runs/r-failed');
    await expect(page.getByText('Задача завершилась с ошибкой')).toBeVisible();
    await header(page).getByRole('button', { name: 'Запустить ещё раз' }).click();
    await expect(page).toHaveURL(/\/new\/t-notes$/);
    await expect(page.getByRole('textbox', { name: 'Дополнение к задаче' })).toHaveValue('4.12');
});

test('duplicate run: «Такой запуск уже идёт» and «Открыть запуск», no new run', async ({ page, mock }) => {
    await openAs(page, 'user', '/');
    const runsBefore = await taskRunIds(mock);

    await page.locator('.v-home__presets').getByRole('button', { name: 'Ревью merge request' }).click();
    await expect(page).toHaveURL(/\/new\/t-mr$/);
    const addition = page.getByRole('textbox', { name: 'Дополнение к задаче' });
    await expect(addition).toHaveAttribute('placeholder', 'Ссылка на merge request');
    await addition.fill(MR_URL);
    await page.getByRole('button', { name: 'Запустить' }).click();

    const alert = page.locator('kbq-alert', { hasText: 'Такой запуск уже идёт' });
    await expect(alert).toBeVisible();
    await expect(page).toHaveURL(/\/new\/t-mr$/);
    expect(await taskRunIds(mock)).toEqual(runsBefore);

    await alert.getByRole('button', { name: 'Открыть запуск' }).click();
    await expect(page).toHaveURL(/\/tasks\/runs\/r-running$/);
});
