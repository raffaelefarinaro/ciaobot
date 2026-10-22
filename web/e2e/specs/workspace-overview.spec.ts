import { expect, test } from '@playwright/test'
import { activeWorkspaceButton, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test: the Overview page lives under a container-query
 * rail, a segmented control with arrow-key semantics and a sidebar switcher
 * that changes what the page shows. jsdom has no layout, so only a real engine
 * can say whether the cards fit at phone width and the keys reach the control.
 */
const OVERVIEW = '/settings/workspace'
const READY = '.settings-main .section-title'

test.describe('workspace overview', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workspace-overview-${testInfo.workerIndex}`)
  })

  test('names the workspace, lists its cards and follows the switcher', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, OVERVIEW, READY)

    await expect(page.locator('.page-tag')).toContainText('Overview')
    const rail = page.locator('.settings-toc-item')
    await expect(rail.first()).toBeVisible()
    const labels = await rail.allTextContents()
    for (const title of ['Workspace', 'Google account', 'Memory handover', 'Update task history']) {
      expect(labels).toContain(title)
    }
    const name = page.locator('#workspace-name-input')
    const first = await name.inputValue()

    await activeWorkspaceButton(page).click()
    const other = page.locator('#workspace-scope-menu [role="menuitem"]').filter({ hasNotText: new RegExp(first, 'i') }).first()
    await other.click()
    await expect(name).not.toHaveValue(first)
  })

  test('the memory handover radiogroup takes arrow keys', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    // The fixture persists neither the workspace PATCH nor the handover, so
    // the save would silently revert and the second arrow would have nothing
    // to move from. Keep the chosen value here and answer both routes from it.
    let handover = 'off'
    await page.route('**/api/proposals/handover*', async (route) => {
      const real = await (await page.request.get(route.request().url())).json()
      await route.fulfill({ json: { ...real, handover } })
    })
    await page.route('**/api/workspaces/*', async (route) => {
      if (route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON() as { memory_handover?: string } | null
        if (body?.memory_handover) handover = body.memory_handover
        // Answer with the registry itself: the store replaces its list from the reply.
        await route.fulfill({ json: await (await page.request.get('/api/workspaces')).json() })
        return
      }
      await route.fallback()
    })
    await boot(page, OVERVIEW, READY)

    const group = page.getByRole('radiogroup', { name: 'Memory handover' })
    await group.scrollIntoViewIfNeeded()
    const off = group.getByRole('radio', { name: 'Off' })
    await off.focus()
    await expect(off).toHaveAttribute('aria-checked', 'true')
    await page.keyboard.press('ArrowRight')
    const dismiss = group.getByRole('radio', { name: 'Dismiss', exact: true })
    await expect(dismiss).toBeFocused()
    // Focus moves at once; the save is still in flight, and the group ignores
    // arrows until it settles (no second write mid-save). Wait for the save,
    // then the next arrow moves on with focus intact.
    await expect(dismiss).not.toHaveAttribute('aria-disabled', 'true')
    await page.keyboard.press('ArrowLeft')
    await expect(off).toBeFocused()
  })

  for (const [width, height] of [[390, 844], [640, 450]] as const) {
    test(`does not scroll sideways at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      await boot(page, OVERVIEW, READY)
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    })
  }
})
