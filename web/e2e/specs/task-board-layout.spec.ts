import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

const labels = ['To do', 'In progress', 'In review', 'Done']

for (const width of [1440, 900, 390]) {
  test.describe(`task groups at ${width}px`, () => {
    test.use({ viewport: { width, height: 1000 }, hasTouch: width === 390 })

    test('keeps the four lanes in board order, in one row, and filters to one group', async ({ page }, testInfo) => {
      await isolate(page, `task-groups-${width}`)
      await page.route(/\/api\/tasks(?:\?|$)/, async route => {
        const response = await route.fetch()
        const payload = await response.json()
        const base = payload.tasks[0]
        await route.fulfill({ response, json: { ...payload, tasks:
          ['backlog', 'in_progress', 'in_review', 'done'].map((status, index) => ({
            ...base,
            id: String(index + 1).repeat(32),
            title: `${labels[index]} example`,
            status,
            updated_at: new Date().toISOString(),
            // Done keeps to tasks completed today, so the done card needs a stamp.
            completed_at: status === 'done' ? new Date().toISOString() : null,
          })),
        } })
      })
      await boot(page, '/tasks', '.task-lanes')
      const lanes = page.locator('.task-lane')
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      // Side by side at every width: one row, the lanes running left to right.
      const boxes = await Promise.all((await lanes.all()).map(lane => lane.boundingBox()))
      for (let index = 1; index < boxes.length; index++) {
        expect(Math.abs(boxes[index]!.y - boxes[0]!.y)).toBeLessThan(1)
        expect(boxes[index]!.x).toBeGreaterThan(boxes[index - 1]!.x)
      }
      // One horizontal scroller holds the row, and the board scrolls it, not the page.
      await expect(page.locator('.task-lanes')).toHaveCount(1)
      // A wide pane fits all four lanes; a narrower one scrolls them sideways.
      expect(await page.locator('.task-lanes').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(width < 1440)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
      await expect(page.locator('.task-card[draggable="true"]')).toHaveCount(4)
      await page.screenshot({ path: testInfo.outputPath('status-groups.png'), fullPage: true, animations: 'disabled' })
      await page.locator('html').evaluate(el => el.classList.remove('theme-light'))
      await page.screenshot({ path: testInfo.outputPath('status-groups-dark.png'), fullPage: true, animations: 'disabled' })
      await page.locator('.task-chip', { hasText: /^In review/ }).click()
      await expect(page.locator('.task-lane-label')).toHaveText(['In review'])
      await expect(page.locator('.task-card')).toHaveCount(1)
      await page.locator('.task-chip', { hasText: /^All / }).click()
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      // As in browser-zoom.spec.ts, 200% page zoom halves the CSS viewport.
      // CSS zoom alone would leave the sidebar's window breakpoint unchanged.
      await page.setViewportSize({ width: Math.round(width / 2), height: 500 })
      await expect(page.locator('.task-card[draggable="true"]')).toHaveCount(4)
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    })
  })
}
