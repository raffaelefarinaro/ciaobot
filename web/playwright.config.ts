import { defineConfig, devices } from '@playwright/test'

const PORT = Number(process.env.CIAO_E2E_PORT || 4599)

/**
 * Browser regression suite. Small on purpose: it covers only what mounting
 * components in jsdom provably cannot establish — real focus order, real
 * layout at a narrow viewport, real browser zoom, and a real WebSocket
 * reconnect. Everything else stays in vitest, which is an order of magnitude
 * faster and cannot go flaky.
 *
 * `retries: 0` everywhere, including CI: a retried browser test hides exactly
 * the flakiness this config is trying not to introduce. If a spec here needs a
 * retry to go green it is a broken spec, and it should fail loudly.
 */
export default defineConfig({
  testDir: './e2e/specs',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 30_000,
  expect: { timeout: 7_000 },
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'node e2e/fixture/server.mjs',
    url: `http://127.0.0.1:${PORT}/api/status`,
    reuseExistingServer: !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 30_000,
  },
})
