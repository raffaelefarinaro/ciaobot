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

const token = (page: Page, name: string) =>
  page.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name)
const dialog = (page: Page) => page.getByRole('dialog', { name: 'Custom colours' })
const hexInput = (page: Page, label: string) => dialog(page).getByRole('textbox', { name: label })
const openCustomise = async (page: Page) => {
  await page.getByRole('button', { name: 'Customise…' }).click()
  await expect(dialog(page)).toBeVisible()
}

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

  test('body font follows the choice and code stays monospace', async ({ page }) => {
    await boot(page, '/settings/appearance', SETTINGS)
    const family = (selector: string) =>
      page.evaluate((sel) => getComputedStyle(document.querySelector(sel) as Element).fontFamily, selector)
    const token = (name: string) =>
      page.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name)
    const group = page.getByRole('radiogroup', { name: 'Body font' })
    // Code-like surfaces keep --font-mono whatever the body font is. A probe
    // with the token gives the browser-normalised mono stack to compare with;
    // no .badge renders on this page, so one is attached to measure its rule.
    const monoSurfaces = () => page.evaluate(() => {
      const make = (cls: string, style = '') => {
        const el = document.createElement('span')
        el.className = cls
        el.style.cssText = style
        document.body.appendChild(el)
        const family = getComputedStyle(el).fontFamily
        el.remove()
        return family
      }
      return {
        probe: make('', 'font-family: var(--font-mono)'),
        badge: make('badge'),
        wordmark: getComputedStyle(document.querySelector('.wordmark') as Element).fontFamily,
      }
    })
    const expectMonoSurfaces = async () => {
      const found = await monoSurfaces()
      expect(found.badge).toBe(found.probe)
      expect(found.wordmark).toBe(found.probe)
    }

    await group.getByRole('radio', { name: 'Serif' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-body-font', 'serif')
    await expect.poll(() => family('body')).toMatch(/Charter|Georgia/)
    expect(await family('.settings-main .routine-name')).toBe(await family('body'))
    expect(await family('.settings-main .routine-detail')).toBe(await family('body'))
    await expectMonoSurfaces()

    await group.getByRole('radio', { name: 'Mono' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-body-font', 'mono')
    await expect.poll(() => family('body')).toBe(await token('--font-mono'))
    await expectMonoSurfaces()

    await group.getByRole('radio', { name: 'System' }).click()
    await expect(page.locator('html')).not.toHaveAttribute('data-body-font', /.+/)
    // Chromium rewrites BlinkMacSystemFont, so compare the face, not the string.
    expect(await family('body')).toMatch(/sans-serif$/)
    expect(await family('body')).not.toMatch(/Charter|Georgia/)
  })

  test('body font survives a reload from the engine', async ({ page }) => {
    await boot(page, '/settings/appearance', SETTINGS)
    await page.getByRole('radiogroup', { name: 'Body font' }).getByRole('radio', { name: 'Serif' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-body-font', 'serif')
    await page.evaluate(() => localStorage.removeItem('ciao-appearance'))
    await page.reload()
    await expect(page.locator(SETTINGS)).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('data-body-font', 'serif')
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

  test('a refused custom pair keeps Apply disabled and says why, a passing one applies and Reset clears it', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'dark'))
    await boot(page, '/settings/appearance', SETTINGS)
    await openCustomise(page)
    await hexInput(page, 'Dark theme text hex').fill('#5a5a70')
    await expect(dialog(page).getByRole('status').first()).toHaveText(/4\.5:1/)
    await expect(dialog(page).getByRole('button', { name: 'Apply' })).toBeDisabled()

    await hexInput(page, 'Dark theme text hex').fill('#e8e8f0')
    await hexInput(page, 'Dark theme background hex').fill('#101018')
    await expect(dialog(page).getByRole('status').first()).toHaveText(/Readable/)
    await expect(dialog(page).getByRole('button', { name: 'Apply' })).toBeEnabled()
    await dialog(page).getByRole('button', { name: 'Apply' }).click()
    await expect(dialog(page)).toBeHidden()
    await expect(page.locator('html')).toHaveAttribute('data-custom-colors', 'dark')
    expect(await token(page, '--bg')).toBe('#101018')

    // Light has no pair, so flipping Theme drops the dark tokens live.
    await page.getByRole('radio', { name: 'Light', exact: true }).click()
    await expect(page.locator('html')).toHaveClass(/theme-light/)
    await expect.poll(() => token(page, '--bg')).toBe('#f4f4fa')
    await page.getByRole('radio', { name: 'Dark', exact: true }).click()
    await expect.poll(() => token(page, '--bg')).toBe('#101018')

    await page.locator('.setting-row', { hasText: 'Custom colours' }).getByRole('button', { name: 'Reset' }).click()
    await expect(page.locator('html')).not.toHaveAttribute('data-custom-colors', /.+/)
    await expect.poll(() => token(page, '--bg')).toBe('#1a1a2e')
  })

  test('the dialog in the light theme', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'light'))
    await boot(page, '/settings/appearance', SETTINGS)
    await openCustomise(page)
    await expect(hexInput(page, 'Light theme background hex')).toHaveValue('#f4f4fa')
    await dialog(page).getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog(page)).toBeHidden()
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

    await page.getByRole('radiogroup', { name: 'Body font' }).getByRole('radio', { name: 'Mono' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-body-font', 'mono')
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)

    await page.getByRole('button', { name: 'Customise…' }).click()
    await expect(dialog(page)).toBeVisible()
    const dialogSmall = await page.evaluate((min) => {
      const bad: string[] = []
      document.querySelectorAll('[role=dialog] button, [role=dialog] input').forEach((el) => {
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) return
        if (r.width < min - 0.5 || r.height < min - 0.5) {
          bad.push(`${el.tagName} ${(el.textContent || '').trim().slice(0, 20)} ${Math.round(r.width)}x${Math.round(r.height)}`)
        }
      })
      return bad
    }, TOUCH_MIN)
    expect(dialogSmall).toEqual([])
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await hexInput(page, 'Dark theme text hex').fill('#5a5a70')
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
