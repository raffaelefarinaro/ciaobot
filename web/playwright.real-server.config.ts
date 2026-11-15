import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e/real-server',
  testMatch: '*.spec.ts',
  retries: 0,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 7_000 },
  use: { ...devices['Desktop Chrome'], trace: 'retain-on-failure' },
})
