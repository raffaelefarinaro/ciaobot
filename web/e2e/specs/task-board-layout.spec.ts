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
      // Done is collapsed by default; this spec reads all four lanes in one row.
      await page.addInitScript(() => localStorage.setItem('ciao.tasks.doneShown', '1'))
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
      // Lanes are a fixed 310px, so four of them outgrow the pane at every width
      // tested here (1440 leaves about 1036px) and the board scrolls sideways.
      expect(await page.locator('.task-lanes').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
      // Reordering has no handle: the card menu's Move up / Move down is the
      // keyboard and touch path, and every card has it.
      await expect(page.locator('.task-card .drag-grip')).toHaveCount(0)
      await expect(page.locator('.task-card .task-more')).toHaveCount(4)
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
      // Reordering has no handle: the card menu's Move up / Move down is the
      // keyboard and touch path, and every card has it.
      await expect(page.locator('.task-card .drag-grip')).toHaveCount(0)
      await expect(page.locator('.task-card .task-more')).toHaveCount(4)
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    })
  })
}
