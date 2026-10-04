import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

// Document overflow cannot detect controls clipped by the Automations pane.
// Check all four corners against the viewport AND the browser's hit-test tree.
async function expectReachable(control: Locator) {
  await expect(control).toBeVisible()
  await expect.poll(() => control.evaluate((el) => {
    const rect = el.getBoundingClientRect()
    const points = [
      [rect.left + 8, rect.top + 8], [rect.right - 8, rect.top + 8],
      [rect.left + 8, rect.bottom - 8], [rect.right - 8, rect.bottom - 8],
    ]
    return rect.width >= 44 && rect.height >= 44
      && rect.left >= 0 && rect.right <= window.innerWidth
      && rect.top >= 0 && rect.bottom <= window.innerHeight
      && points.every(([x, y]) => el.contains(document.elementFromPoint(x!, y!)))
  }), `fully visible and hittable: ${await control.getAttribute('aria-label') ?? await control.textContent()}`).toBe(true)
}

async function mockTrigger(page: Page) {
  await page.route('**/api/webhooks?*', (route) => route.fulfill({ json: {
    triggers: [{
      trigger_id: 'trg_phone', name: 'New issue from GitHub', workspace: 'alpha',
      project_id: null, instructions: 'Triage the issue.', enabled: true,
      mode: 'auto', input_policy: 'event_text', revision: 1,
      created_at: '2026-10-04T09:00:00+00:00', updated_at: '2026-10-04T09:00:00+00:00',
    }],
  } }))
  await page.route('**/api/webhooks/trg_phone/receipts?*', (route) => route.fulfill({ json: {
    workspace: 'alpha', trigger_id: 'trg_phone', receipts: [], limit: 50,
  } }))
}

for (const scenario of ['desktop', 'phone', '200% page zoom reflow'] as const) {
  test.describe(scenario, () => {
    test.use({ hasTouch: true, viewport: scenario === 'desktop'
      ? { width: 1280, height: 900 } : { width: 390, height: 844 } })

    test('every webhook action stays reachable with history closed and open', async ({ page }, testInfo) => {
      await isolate(page, `webhook-actions-${testInfo.testId}`)
      await mockTrigger(page)
      if (scenario === '200% page zoom reflow') {
        await page.setViewportSize({ width: 780, height: 1688 })
      }
      await boot(page, '/schedules', '.wh-row')
      // Follow browser-zoom.spec.ts: 200% page zoom halves the CSS viewport.
      // Resize AFTER boot to exercise reflow, not just initial phone layout.
      if (scenario === '200% page zoom reflow') {
        await page.setViewportSize({ width: 390, height: 844 })
      }
      const actions = page.locator('.wh-row-actions')
      for (const open of [false, true]) {
        const history = actions.getByRole('button', { name: open ? 'Hide history' : 'History', exact: true })
        await expect(history).toHaveAttribute('aria-expanded', String(open))
        for (const control of await actions.getByRole('button').all()) await expectReachable(control)
        if (!open) await page.screenshot({ path: testInfo.outputPath('history-closed.png') })
        await actions.getByRole('button', { name: 'More actions for New issue from GitHub' }).tap()
        await expectReachable(page.getByRole('menuitem', { name: 'Delete trigger…' }))
        await page.locator('.wh-state').tap()
        await expect(page.getByRole('menuitem', { name: 'Delete trigger…' })).toHaveCount(0)
        if (!open) {
          await history.focus()
          await page.keyboard.press('Enter')
        }
      }
      await page.screenshot({ path: testInfo.outputPath('history-open.png') })
      await actions.getByRole('button', { name: 'Hide history', exact: true }).tap()
      await expect(page.locator('.wh-history')).toHaveCount(0)
    })
  })
}
