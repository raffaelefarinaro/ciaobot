import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

const labels = ['To do', 'In progress', 'In review', 'Done']

for (const width of [1440, 900, 390]) {
  test.describe(`task groups at ${width}px`, () => {
    test.use({ viewport: { width, height: 1000 }, hasTouch: width === 390 })

    test('keeps status groups in board order and filters to one group', async ({ page }, testInfo) => {
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
          })),
        } })
      })
      await boot(page, '/tasks', '.task-lanes')
      const lanes = page.locator('.task-lane')
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      const boxes = await Promise.all((await lanes.all()).map(lane => lane.boundingBox()))
      for (let index = 1; index < boxes.length; index++) {
        if (width === 1440) expect(boxes[index]!.x).toBeGreaterThan(boxes[index - 1]!.x)
        else expect(boxes[index]!.y).toBeGreaterThanOrEqual(boxes[index - 1]!.y + boxes[index - 1]!.height)
      }
      await expect(page.locator('.task-card[draggable="true"]')).toHaveCount(width === 1440 ? 4 : 0)
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
      await expect(page.locator('.task-card[draggable="true"]')).toHaveCount(0)
      await expect(page.locator('.task-lane-label')).toHaveText(labels)
      expect(await page.locator('.task-lanes').evaluate(el => el.scrollWidth - el.clientWidth)).toBe(0)
    })
  })
}
