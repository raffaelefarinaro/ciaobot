import { expect, test, type Page } from '@playwright/test'
import { activeWorkspaceButton, boot, isolate } from '../support/app'

/**
 * Why this is a browser test: reordering is keyboard focus (the handle keeps
 * focus while the row moves under it), a real HTML5 drag, and the 1-9 keys
 * landing on the page the order was changed from. jsdom has no focus routing
 * or drag-and-drop, so only a real engine can say the whole path works.
 */
const MANAGE = '/settings/workspaces'
const READY = '.settings-main .section-title'

const handle = (page: Page, name: string) => page.locator(`[data-workspace-handle="${name}"]`)

async function switcherOrder(page: Page): Promise<string[]> {
  await activeWorkspaceButton(page).click()
  const menu = page.getByRole('menu', { name: 'Choose workspace' })
  const items = menu.locator('.workspace-scope-option:has(.workspace-scope-mark:not(.workspace-scope-mark--new))')
  await expect(items.first()).toBeVisible()
  const names = (await items.locator('.workspace-scope-name').allTextContents()).map(t => t.trim())
  await page.keyboard.press('Escape')
  await expect(menu).toHaveCount(0)
  return names
}

test.describe('workspace reorder', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await isolate(page, `workspace-reorder-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('Alt+ArrowDown on a handle reorders, announces, and the digits and reload follow', async ({ page }) => {
    await boot(page, MANAGE, READY)
    await handle(page, 'alpha').focus()
    await page.keyboard.press('Alt+ArrowDown')

    await expect(page.locator('.settings-main [role="status"][aria-live="polite"]'))
      .toHaveText('Moved Alpha to position 2 of 3')
    await expect(handle(page, 'alpha')).toBeFocused()

    expect(await switcherOrder(page)).toEqual(['Beta', 'Alpha', 'Gamma'])
    // The digits are inert inside Settings, so press them from Home.
    await boot(page, '/')
    await page.keyboard.press('1')
    await expect(activeWorkspaceButton(page)).toContainText('Beta')
    await page.keyboard.press('2')
    await expect(activeWorkspaceButton(page)).toContainText('Alpha')

    await page.reload()
    await expect(activeWorkspaceButton(page)).toBeVisible()
    expect(await switcherOrder(page)).toEqual(['Beta', 'Alpha', 'Gamma'])
  })

  test('dragging a handle onto another row moves it there', async ({ page }) => {
    await boot(page, MANAGE, READY)
    await handle(page, 'gamma').dragTo(page.locator('#workspace-row-beta'))

    await expect(page.locator('.settings-main [role="status"][aria-live="polite"]'))
      .toHaveText('Moved Gamma to position 2 of 3')
    expect(await switcherOrder(page)).toEqual(['Alpha', 'Gamma', 'Beta'])
  })

  test('the row menu Move up reorders and returns focus to the handle', async ({ page }) => {
    await boot(page, MANAGE, READY)
    await page.getByRole('button', { name: 'Actions for workspace gamma' }).click()
    await page.getByRole('menuitem', { name: 'Move workspace gamma up' }).click()

    await expect(page.locator('.settings-main [role="status"][aria-live="polite"]'))
      .toHaveText('Moved Gamma to position 2 of 3')
    await expect(handle(page, 'gamma')).toBeFocused()
    expect(await switcherOrder(page)).toEqual(['Alpha', 'Gamma', 'Beta'])
  })
})
