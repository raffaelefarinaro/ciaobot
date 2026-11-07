import { execFileSync } from 'node:child_process'
import { defineConfig, devices } from '@playwright/test'

/**
 * The fixture server's port. A fixed default let two runs on one machine (two
 * agent sessions, or a session and a person) share one server: the second run
 * reused the first run's fixture, serving the other checkout's build, and lost
 * every request once that run stopped it. So each run takes a free port of its
 * own, unless `CIAO_E2E_PORT` pins one.
 *
 * Picked synchronously, because this file is a plain module, and written back
 * to the environment: Playwright loads this config again in every worker, and
 * the workers and the fixture server inherit the environment, so all of them
 * agree on the port picked here once.
 */
function freePort(): number {
  return Number(execFileSync(process.execPath, ['-e', `
    const s = require('node:net').createServer()
    s.listen(0, '127.0.0.1', () => { process.stdout.write(String(s.address().port)); s.close() })
  `]).toString())
}
const PINNED = Boolean(process.env.CIAO_E2E_PORT)
if (!PINNED) process.env.CIAO_E2E_PORT = String(freePort())
const PORT = Number(process.env.CIAO_E2E_PORT)

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
    // Only a pinned port can name a server someone started on purpose.
    reuseExistingServer: PINNED && !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 30_000,
  },
})
