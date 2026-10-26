import { defineConfig, devices } from '@playwright/test';

// E2E against the mock API (SPEC §16). One worker: every test resets the shared mock state.
// Ports: Angular dev server 4204, mock 4304 (override with E2E_APP_PORT / E2E_MOCK_PORT to run two suites at once).
// Run: `bun run e2e` (frontend/), one file: `bun run e2e mock/roles.spec.ts`.
const APP_PORT = Number(process.env['E2E_APP_PORT'] || 4204);
const MOCK_PORT = Number(process.env['E2E_MOCK_PORT'] || 4304);
const MOCK = `http://127.0.0.1:${MOCK_PORT}`;
const APP = `http://127.0.0.1:${APP_PORT}`;

export default defineConfig({
    testDir: './mock',
    fullyParallel: false,
    workers: 1,
    retries: 0,
    timeout: 60_000,
    expect: { timeout: 10_000 },
    reporter: [['list']],
    outputDir: `./.results/${APP_PORT}`,
    use: {
        baseURL: APP,
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        locale: 'ru-RU',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
    projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
    webServer: [
        {
            command: `${process.env.HOME}/.bun/bin/bun mock/server.ts --port ${MOCK_PORT} --fixed-now --speed 4`,
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
