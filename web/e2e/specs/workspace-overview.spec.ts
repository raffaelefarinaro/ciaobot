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
    for (const title of ['Workspace', 'Shared brains', 'All workspaces']) {
      expect(labels).toContain(title)
    }
    expect(labels).not.toContain('Google account')
    const name = page.locator('#workspace-name-input')
    const first = await name.inputValue()

    await activeWorkspaceButton(page).click()
    const other = page.locator('#workspace-scope-menu [role="menuitem"]').filter({ hasNotText: new RegExp(first, 'i') }).first()
    await other.click()
    await expect(name).not.toHaveValue(first)
  })

  test('the switcher lists the settings entries and Workspace settings opens Overview', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, OVERVIEW, READY)
    await expect(page.locator('.page-tag')).toContainText('Overview')
    // Workspaces is not a rail item any more.
    await expect(page.locator('.settings-nav, nav').getByRole('link', { name: 'Workspaces', exact: true })).toHaveCount(0)

    await activeWorkspaceButton(page).click()
    const items = page.locator('#workspace-scope-menu [role="menuitem"]')
    const texts = (await items.allTextContents()).map(t => t.replace(/^\+\s*/, '').trim())
    expect(texts.slice(-2)).toEqual(['Workspace settings', 'New workspace'])
    expect(texts).not.toContain('Manage workspaces')
    await page.locator('#workspace-scope-menu').getByRole('menuitem', { name: 'Workspace settings' }).click()
    await expect(page).toHaveURL(/\/settings\/workspace$/)
    await expect(page.locator('.page-tag')).toContainText('Overview')
  })

  test('Workspace settings in the switcher opens Overview from another settings page', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, '/settings/skills', READY)
    await activeWorkspaceButton(page).click()
    await page.locator('#workspace-scope-menu').getByRole('menuitem', { name: 'Workspace settings' }).click()
    await expect(page).toHaveURL(/\/settings\/workspace$/)
    await expect(page.locator('.page-tag')).toContainText('Overview')
  })

  test('Rename in All workspaces focuses the name field', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, OVERVIEW, READY)
    await page.getByRole('button', { name: /^Actions for workspace /i }).nth(1).click()
    await page.getByRole('menuitem', { name: /^Rename workspace /i }).click()
    await expect(page).toHaveURL(/\/settings\/workspace#workspace-name$/)
    await expect(page.locator('#workspace-name-input')).toBeFocused()
  })

  test('the All workspaces card ends the page and Settings \u2191 returns to the top', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 700 })
    await boot(page, OVERVIEW, READY)
    const link = page.getByRole('button', { name: /^Settings for workspace / }).first()
    await link.scrollIntoViewIfNeeded()
    const body = page.locator('.pane-body')
    expect(await body.evaluate(el => el.scrollTop)).toBeGreaterThan(0)
    const label = (await link.getAttribute('aria-label')) ?? ''
    const name = label.replace('Settings for workspace ', '')
    await link.click()
    await expect(page.locator('.page-tag')).toContainText(new RegExp(name, 'i'))
    await expect.poll(() => body.evaluate(el => el.scrollTop)).toBe(0)
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

test.describe('workspace switcher entries on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workspace-switcher-phone-${testInfo.workerIndex}`)
  })

  test('every menu item is at least 44px tall', async ({ page }) => {
    await boot(page, OVERVIEW, READY)
    await page.getByRole('button', { name: 'Open sidebar' }).click()
    await activeWorkspaceButton(page).click()
    const items = page.locator('.workspace-scope-menu [role="menuitem"]')
    const count = await items.count()
    expect(count).toBeGreaterThanOrEqual(5)
    for (let i = 0; i < count; i++) {
      const box = await items.nth(i).boundingBox()
      expect(box?.height ?? 0, `menu item ${i}`).toBeGreaterThanOrEqual(44)
    }
  })

  test('the merged Overview does not scroll sideways', async ({ page }) => {
    await boot(page, OVERVIEW, READY)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
  })
})
