import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test';

/** Prototype "now" the mock seed is built around with `--fixed-now` (2026-10-24 14:30 local). */
export const PROTO_NOW = new Date(2026, 9, 24, 14, 30);
export const PASSWORD = 'valeria-demo-2026';

export type Persona = 'user' | 'tasks' | 'admin' | 'none';

/** Thin client for the mock's `/__mock/*` control routes (see mock/SCENARIOS.md). */
export class MockControl {
    constructor(private readonly request: APIRequestContext) {}

    async reset(as: Persona = 'user', extra: Record<string, unknown> = {}): Promise<void> {
        const r = await this.request.post('/__mock/reset', { data: { as, ...extra } });
        expect(r.ok(), await r.text()).toBeTruthy();
    }
    async session(body: Record<string, unknown>): Promise<void> {
        const r = await this.request.post('/__mock/session', { data: body });
        expect(r.ok(), await r.text()).toBeTruthy();
    }
    async state(): Promise<any> {
        return (await this.request.get('/__mock/state')).json();
    }
    async emit(event: Record<string, unknown>): Promise<void> {
        const r = await this.request.post('/__mock/emit', { data: event });
        expect(r.ok(), await r.text()).toBeTruthy();
    }
    async play(id: string): Promise<void> {
        const r = await this.request.post('/__mock/play', { data: { id } });
        expect(r.ok(), await r.text()).toBeTruthy();
    }
    async patch(body: Record<string, unknown>): Promise<void> {
        const r = await this.request.post('/__mock/patch', { data: body });
        expect(r.ok(), await r.text()).toBeTruthy();
    }
}

/** Signs a persona in through the browser (cookie `mock_scenario`) and opens `path`. */
export async function openAs(page: Page, as: Persona, path = '/', query = ''): Promise<void> {
    const q = new URLSearchParams({ as, redirect: path });
    await page.goto(`/__mock/session?${q}${query ? `&${query}` : ''}`);
    await page.waitForURL((u) => u.pathname === path.split('?')[0]);
}

export const test = base.extend<{ mock: MockControl }>({
    mock: async ({ page }, use) => {
        // The browser clock starts at the prototype "now" and keeps ticking.
        await page.clock.install({ time: PROTO_NOW });
        const mock = new MockControl(page.request);
        await mock.reset('user');
        await use(mock);
    },
});

export { expect };
