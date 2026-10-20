import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

for (const width of [1440, 390]) {
  test.describe(`task editor at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: width === 390 })

    test('contains resized text fields and closes after completion with keyboard focus on the board', async ({ page }, testInfo) => {
      await isolate(page, `task-editor-${width}`)
      await page.route('**/api/tasks/*/complete', async route => {
        const read = await page.request.get(route.request().url().replace('/complete', ''))
        const payload = await read.json()
        await route.fulfill({ json: { ...payload, task: {
          ...payload.task, status: 'done', revision: 'b'.repeat(64),
          completed_at: new Date().toISOString(), resolution: '', completions: [],
        } } })
      })
      // Done is collapsed by default; this spec follows focus to the completed card in its lane.
      await page.addInitScript(() => localStorage.setItem('ciao.tasks.doneShown', '1'))
      await boot(page, '/tasks', '.task-lanes')
      await page.locator('.task-open').first().focus()
      await page.keyboard.press('Enter')
      await expect(page.locator('#task-detail-name')).toBeFocused()
      await page.locator('.task-body').click()
      const field = page.locator('#task-detail-body')
      await expect(field).toBeFocused()
      // Simulate an extreme resize; CSS must clamp both dimensions.
      await field.evaluate(el => {
        el.style.width = '2000px'
        el.style.height = '2000px'
      })
      const bounds = await field.boundingBox()
      const form = await page.locator('.task-form').boundingBox()
      expect(bounds!.width).toBeLessThan(form!.width)
      expect(bounds!.height).toBeLessThanOrEqual(450)
      expect(await field.evaluate(el => getComputedStyle(el).resize)).toBe('vertical')
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath('editor-light.png'), fullPage: true })
      await page.locator('html').evaluate(el => el.classList.remove('theme-light'))
      await page.screenshot({ path: testInfo.outputPath('editor-dark.png'), fullPage: true })
      await page.getByRole('radio', { name: 'Done', exact: true }).click()
      await expect(page.locator('#task-resolution-text')).toBeFocused()
      if (width === 390) {
        expect((await page.getByRole('button', { name: 'Complete without note' }).boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
      await page.getByRole('button', { name: 'Complete without note' }).click()
      await expect(page.locator('#task-detail-name')).toHaveCount(0)
      await expect(page.locator('.task-card--done .task-open')).toBeFocused()
      await expect(page).toHaveURL(/\/tasks$/)
      // 200% zoom's equivalent CSS viewport also keeps the field in the sheet.
      await page.setViewportSize({ width: Math.round(width / 2), height: 450 })
      await page.keyboard.press('Enter')
      await page.locator('.task-body').click()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    })
  })
}
