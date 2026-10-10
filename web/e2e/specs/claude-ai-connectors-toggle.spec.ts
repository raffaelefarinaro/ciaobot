import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

const SHOTS = '/private/tmp/claude-501/-Users-raffaelefarinaro-repos-ciaobot/d6b99252-f939-4dd7-b1fb-0de2ae7b0ad8/scratchpad/i8shots'

async function openEditor(page: Page, patches: Array<Record<string, unknown>>) {
  await page.route('**/api/workspaces/alpha', async (route) => {
    if (route.request().method() === 'PATCH') {
      patches.push(route.request().postDataJSON())
      await route.fulfill({ json: { ok: true } })
      return
    }
    await route.fallback()
  })
  await boot(page, '/settings/workspaces', '[aria-label="Edit workspace alpha"]')
  await page.locator('[aria-label="Edit workspace alpha"]').click()
  const toggle = page.getByRole('switch', { name: 'claude.ai connectors' })
  await toggle.scrollIntoViewIfNeeded()
  await expect(toggle).toBeVisible()
  return toggle
}

test.describe('claude.ai connectors switch', () => {
  test('keyboard, click, save body, focus ring', async ({ page }, testInfo) => {
    await isolate(page, `connectors-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 1280, height: 900 })
    const patches: Array<Record<string, unknown>> = []
    const toggle = await openEditor(page, patches)
    await expect(toggle).toHaveAttribute('aria-checked', 'true')

    // Reachable by Tab: walk forward from the preceding select.
    await page.locator('.settings-switch-row').first().locator('xpath=preceding::select[1]').focus()
    let reached = false
    for (let i = 0; i < 4 && !reached; i++) {
      await page.keyboard.press('Tab')
      reached = await toggle.evaluate((el) => el === document.activeElement)
    }
    expect(reached, 'switch not reached by Tab').toBe(true)

    const ring = await toggle.evaluate((el) => {
      const s = getComputedStyle(el)
      return { matches: el.matches(':focus-visible'), outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth, boxShadow: s.boxShadow }
    })
    expect(ring.matches).toBe(true)
    expect(ring.outlineStyle !== 'none' || ring.boxShadow !== 'none', JSON.stringify(ring)).toBe(true)

    await page.keyboard.press('Space')
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await page.screenshot({ path: `${SHOTS}/desktop-off-focused.png` })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'false')

    await page.locator('.workspace-save').click()
    await expect.poll(() => patches.length).toBeGreaterThan(0)
    expect(patches[0]).toHaveProperty('claude_ai_connectors', false)
  })

  test('mobile hit target', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
    const page = await context.newPage()
    await isolate(page, `connectors-m-${testInfo.workerIndex}`)
    const toggle = await openEditor(page, [])
    const box = await toggle.boundingBox()
    console.log(`MOBILE hit target: ${box!.width}x${box!.height}`)
    await page.screenshot({ path: `${SHOTS}/mobile.png` })
    expect(box!.width).toBeGreaterThanOrEqual(44)
    expect(box!.height).toBeGreaterThanOrEqual(44)
    await context.close()
  })

  test('200% zoom does not overflow the row', async ({ page }, testInfo) => {
    await isolate(page, `connectors-z-${testInfo.workerIndex}`)
    // 200% page zoom == halving the CSS viewport (see browser-zoom.spec.ts).
    await page.setViewportSize({ width: 640, height: 450 })
    const toggle = await openEditor(page, [])
    const m = await toggle.evaluate((el) => {
      const row = el.closest('.settings-switch-row') as HTMLElement
      const r = row.getBoundingClientRect()
      const b = el.getBoundingClientRect()
      return { rowScroll: row.scrollWidth, rowClient: row.clientWidth, docScroll: document.documentElement.scrollWidth, docClient: document.documentElement.clientWidth, btnRight: b.right, rowRight: r.right }
    })
    console.log(`ZOOM: ${JSON.stringify(m)}`)
    expect(m.rowScroll).toBeLessThanOrEqual(m.rowClient)
    expect(m.btnRight).toBeLessThanOrEqual(m.rowRight + 1)
    expect(m.docScroll).toBeLessThanOrEqual(m.docClient)
    await page.screenshot({ path: `${SHOTS}/zoomed.png` })
  })
})
