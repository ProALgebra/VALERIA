import { randomBytes } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

// Smoke suite against the real FastAPI backend serving the committed production build from /static.
// The backend runs on 127.0.0.1 with a temp SQLite database, docs and debug off, and a fake OpenCode
// server (real/fake-opencode) so a question can make the full round trip. Run: `bun run e2e:real`.
const PORT = Number(process.env['E2E_BACKEND_PORT'] || 8084);
// Generated once in the runner; workers inherit the environment.
process.env['E2E_ADMIN_PASSWORD'] ||= randomBytes(18).toString('base64url');
process.env['E2E_USER_PASSWORD'] ||= randomBytes(18).toString('base64url');

export default defineConfig({
    testDir: './real',
    fullyParallel: false,
    workers: 1,
    retries: 0,
    timeout: 90_000,
    expect: { timeout: 15_000 },
    reporter: [['list']],
    outputDir: './.results/real',
    use: {
        baseURL: `http://127.0.0.1:${PORT}`,
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        locale: 'ru-RU',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
    projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
    webServer: {
        command: 'python3 e2e/real/serve_backend.py',
        cwd: '..',
        env: { E2E_BACKEND_PORT: String(PORT) },
        wait: { stdout: /VALERIA READY/ },
        timeout: 180_000,
        gracefulShutdown: { signal: 'SIGTERM', timeout: 40_000 },
        reuseExistingServer: false,
        stdout: 'pipe',
        stderr: 'pipe',
    },
});
