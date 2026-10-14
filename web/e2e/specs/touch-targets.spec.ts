import { devices, expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * DESIGN.md requires every interactive target to be at least 44x44 CSS px on
 * touch layouts. jsdom applies no CSS, so only a real engine under touch
 * emulation can measure that.
 */
const TOUCH_MIN = 44
const TABS = [
  '/settings',
  '/settings/workspaces',
  '/settings/models',
  '/settings/skills',
  '/settings/subagents',
  '/settings/commands',
  '/settings/mcp',
]

test.use({ ...devices['iPhone 13'], defaultBrowserType: undefined })

test.describe('Settings touch targets', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `touch-${testInfo.workerIndex}`)
    await page.route('**/api/package/status', (route) => route.fulfill({ json: {
      current_version: '1.1.0', latest_version: '1.2.0', update_available: true, mode: 'installer',
    } }))
    await page.route('**/api/update/status', (route) => route.fulfill({ json: {
      install_mode: 'installer', can_update: true, operation: null, running: false,
    } }))
  })

  test('touch emulation is a coarse pointer', async ({ page }) => {
    await boot(page)
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
  })

  test('every Settings control is at least 44×44 on touch', async ({ page }) => {
    const small: string[] = []
    for (const tab of TABS) {
      await boot(page, tab, '.settings-main')
      const found = await page.evaluate((min) => {
        const out: string[] = []
        const sel = '.settings-main :is(button, a[href], [role=button], [role=switch], select)'
        for (const el of Array.from(document.querySelectorAll(sel))) {
          const parent = el.parentElement
          const inline = el.tagName === 'A' && parent && ['P', 'LI'].includes(parent.tagName)
            && !el.classList.contains('settings-help-link')
          if (inline) continue
          const r = el.getBoundingClientRect()
          if (r.width === 0 || r.height === 0) continue
          if (r.height < min - 0.5 || r.width < min - 0.5) {
            const label = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('title') || '')
              .trim().replace(/\s+/g, ' ').slice(0, 60)
            out.push(`"${label}" <${el.tagName.toLowerCase()} class="${el.className}"> ${Math.round(r.height)}×${Math.round(r.width)} (h×w)`)
          }
        }
        return out
      }, TOUCH_MIN)
      for (const f of found) small.push(`${tab}: ${f}`)
    }
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })
})
