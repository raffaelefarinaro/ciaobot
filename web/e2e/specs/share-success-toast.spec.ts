import { devices, expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Synthetic share-success journey. The fixture answers one reviewed share
 * preview and one accept; nothing here reads a vault or a provider.
 */
const CASE = {
  git: {
    title: 'Published to Team',
    body: 'The note was pushed to https://example.test/team.git on main and the remote commit was confirmed.',
  },
  folder: {
    title: 'Saved locally to Team folder',
    body: 'The note was saved to notes/new.md on this computer. Cloud delivery and current availability are unknown.',
  },
} as const

async function runShare(page: Page, kind: keyof typeof CASE, viaKeyboard: boolean) {
  await page.request.post('/__fixture__/share-success', { data: { kind } })
  await boot(page, '/memory/review?show=suggested', '.pr-row')
  const row = page.locator('.pr-row').filter({ hasText: `opaque-${kind}` })
  await expect(row).toBeVisible()
  await row.locator('.pr-row-action').click()
  await expect(page.locator('.pr-card')).toBeVisible()
  const publish = page.locator('.pr-card button.btn-primary')
  await expect(publish).toHaveText('Publish')
  if (viaKeyboard) {
    await publish.focus()
    await page.keyboard.press('Enter')
  } else {
    await publish.click()
  }
  await expect(page.locator('.pr-card')).toHaveCount(0)
  const toast = page.locator('.toast').filter({ hasText: CASE[kind].title })
  await expect(toast).toBeVisible()
  await expect(toast).toHaveAttribute('role', 'status')
  await expect(toast).toContainText(CASE[kind].body)
  return toast
}

test.describe('desktop share success toast', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `share-toast-desktop-${testInfo.workerIndex}`)
  })

  for (const kind of ['git', 'folder'] as const) {
    test(`${kind} success survives panel close, keyboard activation and 200% zoom`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 900 })
      const toast = await runShare(page, kind, true)
      await page.setViewportSize({ width: 640, height: 450 })
      await expect(toast).toBeVisible()
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(overflow, `zoomed success toast overflows: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    })
  }
})

test.describe('mobile share success toast', () => {
  const { defaultBrowserType: _browser, ...iphone } = devices['iPhone 13']
  test.use(iphone)
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `share-toast-mobile-${testInfo.workerIndex}`)
  })

  for (const kind of ['git', 'folder'] as const) {
    test(`${kind} success survives panel close at phone width`, async ({ page }) => {
      const toast = await runShare(page, kind, false)
      await expect(toast).toBeVisible()
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(overflow, `phone success toast overflows: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
      const dismiss = toast.locator('.toast-close')
      const box = await dismiss.boundingBox()
      expect(box).not.toBeNull()
      // Device-scale rounding reports 43.999998 for a 44px CSS box.
      expect(box!.width).toBeGreaterThanOrEqual(43.5)
      expect(box!.height).toBeGreaterThanOrEqual(43.5)
    })
  }
})

test('the review page stays reachable after a reload', async ({ page }) => {
  await isolate(page, 'share-toast-reload')
  await page.request.post('/__fixture__/share-success', { data: { kind: 'git' } })
  await boot(page, '/memory/review?show=suggested', '.pr-row')
  await expect(page.locator('.pr-row').filter({ hasText: 'opaque-git' })).toBeVisible()
  await page.reload()
  await expect(page.locator('.pr-row').filter({ hasText: 'opaque-git' })).toBeVisible()
})
