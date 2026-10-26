import { defineConfig, devices } from '@playwright/test';

// Screenshot capture against the mock: one live shot per prototype state × theme (see screens/states.ts).
// Not part of `bun run e2e` (that config only looks in ./mock). Run: `bun run e2e:screens` (frontend/),
// one group: `bun run e2e:screens screens/admin-config.spec.ts`, one state: `-g "queue-stop"`.
// Output: <shodan>/tmp/new-ui/screens/final/<group>/live-<state>-<theme>.png + proto copies + INDEX.md
// (override with SCREENS_OUT / SCREENS_PROTO).
// Ports default to 4251 / 4351 so it can run next to the default e2e suite (4204 / 4304).
const APP_PORT = Number(process.env['E2E_APP_PORT'] || 4251);
const MOCK_PORT = Number(process.env['E2E_MOCK_PORT'] || 4351);
const MOCK = `http://127.0.0.1:${MOCK_PORT}`;
const APP = `http://127.0.0.1:${APP_PORT}`;

export default defineConfig({
    testDir: './screens',
    fullyParallel: false,
    workers: 1,
    retries: 1,
    timeout: 60_000,
    expect: { timeout: 10_000 },
    reporter: [['list']],
    outputDir: `./.results/screens-${APP_PORT}`,
    globalTeardown: './screens/index-teardown.ts',
    use: {
        baseURL: APP,
        ...devices['Desktop Chrome'],
        // Same frame as the prototype shots: 1440×790 CSS px at DPR 2 (2880×1580 PNG).
        viewport: { width: 1440, height: 790 },
        deviceScaleFactor: 2,
        locale: 'ru-RU',
        timezoneId: 'Europe/Moscow',
        trace: 'retain-on-failure',
        screenshot: 'off',
    },
    projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
    webServer: [
        {
            command: `${process.env.HOME}/.bun/bin/bun mock/server.ts --port ${MOCK_PORT} --fixed-now`,
            cwd: '..',
            url: `${MOCK}/api/health`,
            reuseExistingServer: false,
            stdout: 'ignore',
            stderr: 'pipe',
        },
        {
            command: `./node_modules/.bin/ng serve --port ${APP_PORT} --host 127.0.0.1`,
            cwd: '..',
            env: { VALERIA_API: MOCK, NG_CLI_ANALYTICS: 'false' },
            url: APP,
            timeout: 180_000,
            reuseExistingServer: false,
            stdout: 'ignore',
            stderr: 'pipe',
        },
    ],
});
