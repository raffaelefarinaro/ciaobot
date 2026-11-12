import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

// The engine's trim catalog, injected on GET /api/workspaces. The fixture does
// not carry one, so the card only renders here.
const CATALOG = [
  { key: 'tool:CronCreate', kind: 'tool', name: 'CronCreate', label: 'Cron schedules', reason: 'Schedules runs.', group: 'scheduling', default_trimmed: true, locked: true },
  { key: 'tool:NotebookEdit', kind: 'tool', name: 'NotebookEdit', label: 'Notebook editing', reason: 'Edits Jupyter notebooks.', group: 'features', default_trimmed: true, locked: false },
  { key: 'tool:ReportFindings', kind: 'tool', name: 'ReportFindings', label: 'Report findings', reason: 'Reports review findings.', group: 'review', default_trimmed: false, locked: false },
  { key: 'skill:a', kind: 'skill', name: 'a', label: 'Skill A', reason: 'Bundled skill A.', group: 'skills', default_trimmed: true, locked: false },
  { key: 'skill:b', kind: 'skill', name: 'b', label: 'Skill B', reason: 'Bundled skill B.', group: 'skills', default_trimmed: true, locked: false },
  { key: 'skill:c', kind: 'skill', name: 'c', label: 'Skill C', reason: 'Bundled skill C.', group: 'skills', default_trimmed: false, locked: false },
  { key: 'skill:d', kind: 'skill', name: 'd', label: 'Skill D', reason: 'Bundled skill D.', group: 'skills', default_trimmed: false, locked: false },
  { key: 'setting:disableWorkflows', kind: 'setting', name: 'disableWorkflows', label: 'Workflows', reason: 'Turns off workflows.', group: 'features', default_trimmed: false, locked: false },
]

async function openCard(page: Page, id: string, patches: Array<Record<string, unknown>>) {
  await isolate(page, id)
  await page.route('**/api/workspaces', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const json = await response.json()
    await route.fulfill({ response, json: { ...json, claude_trim_options: CATALOG } })
  })
  await page.route('**/api/workspaces/alpha', async (route) => {
    if (route.request().method() === 'PATCH') {
      patches.push(route.request().postDataJSON())
      await route.fulfill({ json: { ok: true } })
      return
    }
    await route.fallback()
  })
  await boot(page, '/settings/workspace', '.trim-card')
}

test.describe('Claude context trimming card', () => {
  test('keyboard, click, save body, focus ring', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const patches: Array<Record<string, unknown>> = []
    await openCard(page, `trim-${testInfo.workerIndex}`, patches)
    const notebook = page.getByRole('switch', { name: 'Notebook editing' })
    await expect(notebook).toHaveAttribute('aria-checked', 'false')

    // Reachable by Tab: walk forward from the claude.ai connectors switch above the card.
    await page.locator('.trim-card').locator('xpath=preceding::button[@role="switch"][1]').focus()
    let reached = false
    for (let i = 0; i < 6 && !reached; i++) {
      await page.keyboard.press('Tab')
      reached = await notebook.evaluate((el) => el === document.activeElement)
    }
    expect(reached, 'Notebook editing switch not reached by Tab').toBe(true)

    const ring = await notebook.evaluate((el) => {
      const s = getComputedStyle(el)
      return { matches: el.matches(':focus-visible'), outlineStyle: s.outlineStyle, boxShadow: s.boxShadow }
    })
    expect(ring.matches).toBe(true)
    expect(ring.outlineStyle !== 'none' || ring.boxShadow !== 'none', JSON.stringify(ring)).toBe(true)

    await page.keyboard.press('Space')
    await expect(notebook).toHaveAttribute('aria-checked', 'true')

    await page.locator('.workspace-save').click()
    await expect.poll(() => patches.length).toBeGreaterThan(0)
    expect(patches[0]).toHaveProperty('claude_trim', { 'tool:NotebookEdit': false })
  })

  test('mobile hit targets', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
    const page = await context.newPage()
    try {
      await openCard(page, `trim-m-${testInfo.workerIndex}`, [])
      const targets = [
        page.getByRole('switch', { name: 'Notebook editing' }),
        page.locator('.trim-disclosure > summary').first(),
        page.locator('.trim-reset'),
        page.locator('#trim-extra-tools-alpha'),
      ]
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded()
        const box = await target.boundingBox()
        expect(box!.width).toBeGreaterThanOrEqual(44)
        expect(box!.height).toBeGreaterThanOrEqual(44)
      }
    } finally {
      await context.close()
    }
  })

  test('200% zoom does not overflow the card', async ({ page }, testInfo) => {
    // 200% page zoom == halving the CSS viewport (see browser-zoom.spec.ts).
    await page.setViewportSize({ width: 640, height: 450 })
    await openCard(page, `trim-z-${testInfo.workerIndex}`, [])
    await page.locator('.trim-disclosure > summary').first().click()
    const m = await page.evaluate(() => {
      const card = document.querySelector('.trim-card') as HTMLElement
      const cardRect = card.getBoundingClientRect()
      const switchesRight = Array.from(card.querySelectorAll<HTMLElement>('.settings-switch'))
        .map((el) => el.getBoundingClientRect().right)
      return {
        cardScroll: card.scrollWidth,
        cardClient: card.clientWidth,
        maxSwitchRight: Math.max(...switchesRight),
        cardRight: cardRect.right,
        docScroll: document.documentElement.scrollWidth,
        docClient: document.documentElement.clientWidth,
      }
    })
    expect(m.cardScroll).toBeLessThanOrEqual(m.cardClient)
    expect(m.maxSwitchRight).toBeLessThanOrEqual(m.cardRight + 1)
    expect(m.docScroll).toBeLessThanOrEqual(m.docClient)
  })
})
