import { devices, expect, test, type Page } from '@playwright/test'
import { BOOTED, COMPOSER, boot, isolate } from '../support/app'

/**
 * DESIGN.md requires every interactive target to be at least 44x44 CSS px on
 * touch layouts. jsdom applies no CSS, so only a real engine under touch
 * emulation can measure that.
 */
const TOUCH_MIN = 44
const TABS = [
  '/settings',
  '/settings/appearance',
  '/settings/access',
  '/settings/advanced',
  '/settings/workspace',
  '/settings/workspaces',
  '/settings/providers',
  '/settings/skills',
  '/settings/subagents',
  '/settings/commands',
  '/settings/mcp',
]

// Content selectors, not shell ones: a surface must have rendered its data
// before it is measured. Automations and Import have no fixture rows, so they
// wait on their heading.
const MAIN_SURFACES: Array<[string, string]> = [
  ['/', '.home-setup'],
  ['/tasks', '.task-card'],
  ['/automations', '.schedule-panel .page-tag'],
  ['/memory/review', '.pr-select-toggle'],
  ['/memory/map', '.mm-toolbar-stale'],
  ['/memory/categories', '.cat-h'],
  ['/memory/import', '.import-title'],
  ['/memory/retired', '.guide-budget-link'],
  ['/memory/history', '.ph-seg'],
  ['/project/alpha-general', '.text-action'],
  ['/schedules/alpha-daily-brief', '.text-link'],
  ['/tasks/0123456789abcdef0123456789abcdef', '.task-status-opt'],
]

// Surfaces whose menus, popovers and dialogs are opened and measured.
const POPUP_SURFACES: Array<[string, string]> = [
  ...MAIN_SURFACES,
  ['/chat/alpha-chat-1', COMPOSER],
  ['/settings/workspace', '.settings-main'],
  ['/settings/workspaces', '.settings-main'],
  ['/settings/providers', '.settings-main'],
  ['/settings/advanced', '.settings-main'],
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

/** Visible popup triggers: menus, popovers, disclosure buttons and Settings' New form. */
const TRIGGER = '[aria-haspopup], [aria-expanded="false"]'
const TRIGGER_CAP = 80
// Opening these proves the run reached each kind of popup, not just some DOM change.
const KNOWN_TRIGGERS = [
  'Model for the new chat',
  '/settings/workspaces › New workspace',
  '/settings/workspaces › Actions for workspace',
  'Show chat details',
]
// Triggers a headless run may legitimately fail to click (covered by another element).
const MAX_SKIPPED = 3

/**
 * Measure every open `.field-info-panel`: report those whose box leaves the
 * viewport or sits more than 12px from the visible glyph's bottom, and name
 * the ones measured so the caller can prove the check ran.
 */
async function panelProblems(page: Page) {
  return page.evaluate(() => {
    const out: string[] = []
    const measured: string[] = []
    const w = window.innerWidth
    for (const el of Array.from(document.querySelectorAll('.field-info-panel'))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0) continue
      const summary = el.parentElement?.querySelector('summary')
      measured.push(summary?.getAttribute('aria-label') ?? '')
      if (r.left < -0.5 || r.right > w + 0.5) out.push(`panel overflows viewport: left ${Math.round(r.left)} right ${Math.round(r.right)} of ${w}`)
      const s = summary?.getBoundingClientRect()
      // The 20px glyph sits centred in the 44px hit area, so its bottom is the
      // summary's bottom minus the 12px inset on touch.
      const glyphBottom = s ? s.bottom - 12 : 0
      if (s && Math.abs(r.top - glyphBottom) > 12.5) out.push(`panel ${Math.round(r.top - glyphBottom)}px from the glyph`)
    }
    return { out, measured }
  })
}

/** Tag every visible popup trigger with its index, so it can be found again after the DOM churns. */
async function tagTriggers(page: Page): Promise<number> {
  return page.evaluate((sel) => {
    document.querySelectorAll('[data-touch-trigger]').forEach((e) => e.removeAttribute('data-touch-trigger'))
    let n = 0
    for (const el of Array.from(document.querySelectorAll(sel))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0 || getComputedStyle(el).visibility === 'hidden') continue
      el.setAttribute('data-touch-trigger', String(n))
      n += 1
    }
    return n
  }, TRIGGER)
}

/** Resolve once the DOM has changed from `before` elements and then held still for a few frames. */
async function settled(page: Page, before: number): Promise<boolean> {
  // Reset the stability counter first: values left by the previous trigger
  // could otherwise satisfy the check on the first frame, before anything opened.
  await page.evaluate(() => {
    const w = window as unknown as { __touchLast?: number; __touchStable?: number }
    w.__touchLast = undefined
    w.__touchStable = 0
  })
  return page.waitForFunction((was) => {
    const w = window as unknown as { __touchLast?: number; __touchStable?: number }
    const now = document.getElementsByTagName('*').length
    if (now === was) return false
    w.__touchStable = w.__touchLast === now ? (w.__touchStable ?? 0) + 1 : 0
    w.__touchLast = now
    return w.__touchStable >= 5
  }, before, { polling: 'raf', timeout: 2000 }).then(() => true, () => false)
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

  test('the code badge and the Repository section are at least 44×44 on touch', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const badge = page.locator('.chat-code-badge')
    await expect(badge).toBeVisible()
    const box = await badge.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    expect(box!.height).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    await badge.click()
    await expect(page.locator('#chat-work-rail .chat-rail-repo')).toBeFocused()
    const found = await measure(page, '#chat-work-rail .chat-repo-section')
    expect(found.total, 'no controls measured in the Repository section').toBeGreaterThan(0)
    expect(found.bad, `controls under ${TOUCH_MIN}px:\n${found.bad.join('\n')}`).toEqual([])
  })

  test('controls inside menus and dialogs are at least 44×44 on touch', async ({ page }) => {
    const small: string[] = []
    const panels: string[] = []
    const measuredPanels = new Set<string>()
    const opened: string[] = []
    const skipped: string[] = []
    for (const [route, ready] of POPUP_SURFACES) {
      await boot(page, route, ready)
      let count = await tagTriggers(page)
      expect(count, `${route}: trigger cap of ${TRIGGER_CAP} would hide triggers`).toBeLessThan(TRIGGER_CAP)
      const baseline = new Set((await measure(page)).bad)
      for (let i = 0; i < count; i += 1) {
        let trigger = page.locator(`[data-touch-trigger="${i}"]`)
        if ((await trigger.count()) !== 1) {
          // A re-render replaced it, or measure() opened a <details> that hid it: start clean.
          await boot(page, route, ready)
          count = await tagTriggers(page)
          trigger = page.locator(`[data-touch-trigger="${i}"]`)
        }
        const label = (await trigger.first().evaluate((el) => (el.getAttribute('aria-label') || el.textContent || '')
          .trim().replace(/\s+/g, ' ').slice(0, 40)).catch(() => '')) || `#${i}`
        const name = `${route} › ${label}`
        const before = await page.evaluate(() => document.getElementsByTagName('*').length)
        try {
          await trigger.click({ timeout: 2000 })
        } catch (err) {
          skipped.push(`${name}: ${String(err).split('\n')[0]}`)
          await boot(page, route, ready)
          count = await tagTriggers(page)
          continue
        }
        if (!(await settled(page, before))) continue // nothing opened
        opened.push(name)
        const found = await measure(page)
        for (const f of found.bad) if (!baseline.has(f)) small.push(`${name}: ${f}`)
        const p = await panelProblems(page)
        p.measured.forEach((m) => measuredPanels.add(m))
        for (const m of p.out) panels.push(`${name}: ${m}`)
        await page.keyboard.press('Escape')
        // Escape leaves forms open on Settings, and a trigger may navigate: start those clean.
        if (route.startsWith('/settings') || new URL(page.url()).pathname !== route) {
          await boot(page, route, ready)
          count = await tagTriggers(page)
        }
      }
    }
    for (const known of KNOWN_TRIGGERS) {
      expect(opened.some((o) => o.includes(known)), `trigger "${known}" never opened; opened:\n${opened.join('\n')}\nskipped:\n${skipped.join('\n')}`).toBe(true)
    }
    expect(skipped.length, `triggers that could not be clicked:\n${skipped.join('\n')}`).toBeLessThanOrEqual(MAX_SKIPPED)
    for (const label of ['About Google Workspace integration', 'About GWS profiles']) {
      expect(measuredPanels.has(label), `panel "${label}" was never measured`).toBe(true)
    }
    expect.soft(panels, `popup panels out of place:\n${panels.join('\n')}`).toEqual([])
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })
})
