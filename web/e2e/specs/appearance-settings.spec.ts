import { devices, expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Appearance is engine-wide: the engine holds the value, the browser keeps a
 * paint cache. jsdom has no CSS, so only a real engine can say what --fg
 * resolves to and how large a control is.
 */
const SETTINGS = '.settings-main'
const TOUCH_MIN = 44

const fg = (page: Page) =>
  page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--fg').trim())

const contrastGroup = (page: Page) => page.getByRole('radiogroup', { name: 'Text contrast' })

test.describe('Settings › Appearance', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `appearance-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('soft text contrast changes the body colour in both themes', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'dark'))
    await boot(page, '/settings/appearance', SETTINGS)
    expect(await fg(page)).toBe('#e8e8f0')

    await contrastGroup(page).getByRole('radio', { name: 'Soft' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-text-contrast', 'soft')
    await expect.poll(() => fg(page)).toBe('#d2d2de')

    await page.getByRole('radio', { name: 'Light', exact: true }).click()
    await expect(page.locator('html')).toHaveClass(/theme-light/)
    await expect.poll(() => fg(page)).toBe('#2e2f46')

    await contrastGroup(page).getByRole('radio', { name: 'Normal' }).click()
    await expect.poll(() => fg(page)).toBe('#1a1a2e')
    await page.getByRole('radio', { name: 'Dark', exact: true }).click()
    await expect.poll(() => fg(page)).toBe('#e8e8f0')
  })

  test('settings persist across reload from the engine', async ({ page }) => {
    await boot(page, '/settings/appearance', SETTINGS)
    await contrastGroup(page).getByRole('radio', { name: 'Soft' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-text-contrast', 'soft')

    // The paint cache is gone, so only the engine can restore it.
    await page.evaluate(() => localStorage.removeItem('ciao-appearance'))
    await page.reload()
    await expect(page.locator(SETTINGS)).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('data-text-contrast', 'soft')
    await expect(contrastGroup(page).getByRole('radio', { name: 'Soft' })).toHaveAttribute('aria-checked', 'true')
  })

  test('theme is engine-wide: a browser with no local choice follows it', async ({ page }) => {
    await boot(page, '/settings/appearance', SETTINGS)
    await page.getByRole('radio', { name: 'Light', exact: true }).click()
    await expect(page.locator('html')).toHaveClass(/theme-light/)

    // A different device: nothing stored locally, so system (dark here) would win.
    await page.evaluate(() => localStorage.clear())
    await page.reload()
    await expect(page.locator(SETTINGS)).toBeVisible()
    await expect(page.locator('html')).toHaveClass(/theme-light/)
  })
})

test('on a touch device every control is at least 44px and the page does not scroll sideways', async ({ browser }, testInfo) => {
  const context = await browser.newContext({ ...devices['Pixel 5'], baseURL: testInfo.project.use.baseURL })
  const page = await context.newPage()
  try {
    await isolate(page, `appearance-touch-${testInfo.workerIndex}`)
    await boot(page, '/settings/appearance', SETTINGS)
    await expect(contrastGroup(page)).toBeVisible()
    const small = await page.evaluate((min) => {
      const card = document.querySelector('.settings-main .card')
      const bad: string[] = []
      card?.querySelectorAll('button, input, [role=radio]').forEach((el) => {
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) return
        if (r.width < min - 0.5 || r.height < min - 0.5) {
          bad.push(`${el.tagName} ${(el.textContent || '').trim().slice(0, 20)} ${Math.round(r.width)}x${Math.round(r.height)}`)
        }
      })
      return bad
    }, TOUCH_MIN)
    expect(small).toEqual([])
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  } finally {
    await context.close()
  }
})

test('the more-colours swatch reveals the extra accent presets', async ({ page }, testInfo) => {
  await isolate(page, `accent-more-${testInfo.workerIndex}`)
  await boot(page, '/settings/workspaces', '.settings-main .section-title')
  await page.getByRole('button', { name: 'New workspace' }).click()
  const swatches = page.locator('#new-workspace .workspace-color-swatch[role=radio]')
  await expect(swatches).toHaveCount(5)
  await page.getByRole('button', { name: 'More colours' }).click()
  await expect(swatches).toHaveCount(9)
  await page.getByRole('radio', { name: 'Deep Teal' }).click()
  await expect(page.getByRole('radio', { name: 'Deep Teal' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByRole('button', { name: 'Show fewer colours' })).toHaveAttribute('aria-expanded', 'true')
})

test('on a touch device every accent swatch, including the plus, is at least 44px', async ({ browser }, testInfo) => {
  const context = await browser.newContext({ ...devices['Pixel 5'], baseURL: testInfo.project.use.baseURL })
  const page = await context.newPage()
  try {
    await isolate(page, `accent-touch-${testInfo.workerIndex}`)
    await boot(page, '/settings/workspaces', '.settings-main .section-title')
    await page.getByRole('button', { name: 'New workspace' }).click()
    await page.getByRole('button', { name: 'More colours' }).click()
    const sizes = await page.$$eval('#new-workspace .workspace-color-swatch', (els) =>
      els.map((el) => { const r = el.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)] }),
    )
    expect(sizes).toHaveLength(10)
    for (const [w, h] of sizes) {
      expect(w).toBeGreaterThanOrEqual(TOUCH_MIN)
      expect(h).toBeGreaterThanOrEqual(TOUCH_MIN)
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  } finally {
    await context.close()
  }
})
