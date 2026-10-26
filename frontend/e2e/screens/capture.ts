import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Page, TestInfo } from '@playwright/test';
import { expect, test as base } from '../fixtures';
import { OUT_DIR, RUNS_DIR } from './paths';
import type { ScreenState, Theme } from './states';

export const THEMES: readonly Theme[] = ['light', 'dark'];

/**
 * Picks the theme the way the app stores it (UiStateService, key `valeria.theme`, schema v1) before any app
 * script runs, so the first paint is already in that theme. The OS scheme is set to match.
 */
export async function useTheme(page: Page, theme: Theme): Promise<void> {
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript((value) => {
        try {
            localStorage.setItem('valeria.theme', JSON.stringify({ v: 1, value }));
        } catch {
            /* about:blank has no storage */
        }
    }, theme);
}

/** Tracks in-flight requests except the SSE stream, which never finishes. */
export function trackNetwork(page: Page): { idle(quietMs?: number, maxMs?: number): Promise<void> } {
    const pending = new Set<unknown>();
    let last = Date.now();
    const skip = (url: string) => url.includes('/api/events') || url.startsWith('data:');
    page.on('request', (r) => {
        if (skip(r.url())) return;
        pending.add(r);
        last = Date.now();
    });
    const done = (r: unknown) => {
        if (pending.delete(r)) last = Date.now();
    };
    page.on('requestfinished', done);
    page.on('requestfailed', done);
    return {
        async idle(quietMs = 500, maxMs = 15_000) {
            const start = Date.now();
            while (Date.now() - start < maxMs) {
                if (pending.size === 0 && Date.now() - last >= quietMs) return;
                await new Promise((r) => setTimeout(r, 50));
            }
        },
    };
}

/** Waits for fonts, finite CSS/Web animations (dialogs, overlays) and two frames. */
async function settleRendering(page: Page): Promise<void> {
    await page.evaluate(async () => {
        await document.fonts.ready;
        const finite = document
            .getAnimations()
            .filter((a) => {
                const it = a.effect?.getComputedTiming().iterations;
                return it !== Infinity && a.playState === 'running';
            })
            .map((a) => a.finished.catch(() => undefined));
        await Promise.race([Promise.all(finite), new Promise((r) => setTimeout(r, 2000))]);
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
}

export const livePath = (s: ScreenState, theme: Theme) => join(OUT_DIR, s.group, `live-${s.state}-${theme}.png`);

function writeRun(s: ScreenState, theme: Theme, info: { captured: boolean; reason?: string }): void {
    mkdirSync(RUNS_DIR, { recursive: true });
    writeFileSync(
        join(RUNS_DIR, `${s.group}__${s.state}__${theme}.json`),
        JSON.stringify({ group: s.group, state: s.state, theme, ...info, at: new Date().toISOString() }, null, 2),
    );
}

export const test = base;

/** Registers one test per state × theme for a group. */
export function captureGroup(states: readonly ScreenState[]): void {
    for (const s of states) {
        for (const theme of THEMES) {
            const title = `${s.group} · ${s.state} · ${theme}`;
            if (s.skip) {
                test(title, async () => {
                    writeRun(s, theme, { captured: false, reason: s.skip });
                    test.skip(true, s.skip);
                });
                continue;
            }
            test(title, async ({ page, mock }, testInfo: TestInfo) => {
                const net = trackNetwork(page);
                await useTheme(page, theme);
                try {
                    await s.run({ page, mock, theme });
                    await net.idle();
                    await expect(page.locator('body')).toHaveClass(theme === 'dark' ? /kbq-dark/ : /kbq-light/);
                    await settleRendering(page);
                    // Park the pointer in the bottom-right corner (empty content), so no row keeps a hover state.
                    if (!s.keepPointer) await page.mouse.move(1439, 789);
                    await net.idle(300);
                    const path = livePath(s, theme);
                    mkdirSync(join(OUT_DIR, s.group), { recursive: true });
                    await page.screenshot({ path, animations: 'disabled', caret: 'hide', scale: 'device' });
                    writeRun(s, theme, { captured: true });
                } catch (err) {
                    const msg = String((err as Error)?.message ?? err).split('\n')[0].slice(0, 200);
                    writeRun(s, theme, {
                        captured: false,
                        reason: `test failed (attempt ${testInfo.retry + 1}): ${msg}`,
                    });
                    throw err;
                }
            });
        }
    }
}
