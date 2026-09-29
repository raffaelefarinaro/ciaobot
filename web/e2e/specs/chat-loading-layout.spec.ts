import { expect, test } from '@playwright/test'
import { isolate } from '../support/app'

test('loading transcript rows stay separated and inside the pane', async ({ page }, testInfo) => {
  await isolate(page, `loading-${testInfo.workerIndex}`)
  let releaseHistory!: () => void
  const historyHeld = new Promise<void>((resolve) => { releaseHistory = resolve })
  await page.route(/\/api\/chats\/[^/]+\/messages(?:\?.*)?$/, async (route) => {
    await historyHeld
    await route.continue()
  })

  try {
    await page.goto('/chat/alpha-chat-1')
    const skeleton = page.locator('.history-skeleton-stack')
    await expect(skeleton).toBeVisible()
    await expect(page.getByRole('heading', { name: /Connecting to Ciaobot/ })).toBeHidden()
    await expect(page.locator('.chat-empty-state')).toBeHidden()

    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 844 })
      if (width === 390) {
        await page.reload()
        await expect(skeleton).toBeVisible()
        await expect(page.getByRole('heading', { name: /Connecting to Ciaobot/ })).toBeHidden()
      }
      const layout = await skeleton.evaluate((stack) => {
        const parent = stack.getBoundingClientRect()
        const rows = [...stack.querySelectorAll('.skel-msg, .skel-trace')]
          .map((row) => row.getBoundingClientRect())
        return {
          count: rows.length,
          gaps: rows.slice(1).map((row, i) => row.top - rows[i].bottom),
          outside: rows.some((row) => row.left < parent.left - 1 || row.right > parent.right + 1),
        }
      })
      expect(layout.count).toBe(4)
      for (const gap of layout.gaps) {
        expect(gap, `overlapping loading rows at ${width}px`).toBeGreaterThanOrEqual(13)
      }
      expect(layout.outside, `loading row escapes the pane at ${width}px`).toBe(false)
    }
  } finally {
    releaseHistory()
  }
})
