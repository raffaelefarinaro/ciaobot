import { devices, expect, test, type Page } from '@playwright/test'
import { BOOTED, boot, isolate } from '../support/app'

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

// Content selectors, not shell ones: a surface must have rendered its data
// before it is measured. Automations and Import have no fixture rows, so they
// wait on their heading.
const MAIN_SURFACES: Array<[string, string]> = [
  ['/', '.home-setup-link'],
  ['/tasks', '.task-card'],
  ['/automations', '.schedule-panel .page-tag'],
  ['/memory/review', '.pr-select-toggle'],
  ['/memory/map', '.mm-toolbar-stale'],
  ['/memory/categories', '.cat-h'],
  ['/memory/import', '.import-title'],
  ['/memory/retired', '.guide-budget-link'],
  ['/memory/history', '.ph-seg'],
]

/**
 * Open every <details>, then list the interactive controls under `scope`
 * that are smaller than TOUCH_MIN in either dimension.
 */
async function measure(page: Page, scope = 'body') {
  return page.evaluate(({ min, scope }) => {
    document.querySelectorAll('details').forEach((d) => { d.open = true })
    const bad: string[] = []
    let total = 0
    const sel = 'button, a[href], [role=button], [role=switch], [role=tab], [role=checkbox], '
      + '[role=radio], [role=menuitem], [role=option], input:not([type=hidden]), select, summary'
    const seen = new Set<Element>()
    for (const raw of Array.from(document.querySelectorAll(`${scope} :is(${sel})`))) {
      // A native checkbox/radio is tapped through its label.
      const el = raw instanceof HTMLInputElement && (raw.type === 'checkbox' || raw.type === 'radio')
        ? (raw.closest('label') ?? raw)
        : raw
      if (seen.has(el)) continue
      seen.add(el)
      const parent = el.parentElement
      // Inline prose link (WCAG 2.5.8 exception): sits among sentence text.
      const inline = el.tagName === 'A' && parent && ['P', 'LI'].includes(parent.tagName)
        && !el.classList.contains('settings-help-link')
        && Array.from(parent.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== '')
      if (inline) continue
      const cs = getComputedStyle(el)
      // Visually hidden keyboard controls (e.g. the task card's `.task-open`):
      // the whole card is the pointer target, so the control is not tapped.
      if (cs.clip === 'rect(0px, 0px, 0px, 0px)' || cs.clipPath === 'inset(50%)') continue
      if (cs.visibility === 'hidden') continue
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      total += 1
      if (r.height < min - 0.5 || r.width < min - 0.5) {
        const label = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('title') || '')
          .trim().replace(/\s+/g, ' ').slice(0, 60)
        bad.push(`"${label}" <${el.tagName.toLowerCase()} class="${el.className}"> ${Math.round(r.height)}×${Math.round(r.width)} (h×w)`)
      }
    }
    return { total, bad }
  }, { min: TOUCH_MIN, scope })
}

test.use({ ...devices['iPhone 13'], defaultBrowserType: undefined })

test.describe('touch targets', () => {
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
      const found = await measure(page, '.settings-main')
      expect(found.total, `no controls measured on ${tab}`).toBeGreaterThan(0)
      for (const f of found.bad) small.push(`${tab}: ${f}`)
    }
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })

  test("every control on the app's main surfaces is at least 44×44 on touch", async ({ page }) => {
    const small: string[] = []
    for (const [route, ready] of MAIN_SURFACES) {
      await boot(page, route, ready)
      const found = await measure(page)
      expect(found.total, `no controls measured on ${route}`).toBeGreaterThan(0)
      for (const f of found.bad) small.push(`${route}: ${f}`)
    }
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })

  test('every control in the empty chat view is at least 44×44 on touch', async ({ page }) => {
    await boot(page)
    await page.click(BOOTED)
    await expect(page.locator('.chat-empty-knowledge')).toBeVisible()
    const found = await measure(page)
    expect(found.total, 'no controls measured in chat').toBeGreaterThan(0)
    expect(found.bad, `controls under ${TOUCH_MIN}px:\n${found.bad.join('\n')}`).toEqual([])
  })
})
