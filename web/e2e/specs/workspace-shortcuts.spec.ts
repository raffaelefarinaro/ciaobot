import { expect, test } from '@playwright/test'
import { COMPOSER, activeWorkspaceButton, boot, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The rule in CLAUDE.md — unmodified 1-9 map to the *visible* sidebar order and
 * stay inert while a text field is focused — is entirely about `document
 * .activeElement`. jsdom will happily report a focused textarea that no real
 * keystroke is routed to, and `@vue/test-utils` dispatches synthetic
 * KeyboardEvents straight at a handler, so a mounted test can pass while the
 * shortcut in a real browser is either dead or stealing digits from the
 * composer. Only a real browser decides where a typed character lands.
 */
test.describe('workspace shortcuts', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `shortcuts-${testInfo.workerIndex}`)
  })

  test('digits follow the visible sidebar order', async ({ page }) => {
    await boot(page)

    // The scope control and its menu are the visible workspace order.
    const scope = activeWorkspaceButton(page)
    await expect(scope).toContainText('Alpha')
    await scope.click()
    const menu = page.getByRole('menu', { name: 'Choose workspace' })
    const options = menu.getByRole('menuitem')
    await expect(options.nth(0)).toContainText('Alpha')
    await expect(options.nth(1)).toContainText('Beta')
    await expect(options.nth(2)).toContainText('Gamma')

    await page.keyboard.press('3')
    await expect(activeWorkspaceButton(page)).toContainText('Gamma')

    await page.keyboard.press('1')
    await expect(activeWorkspaceButton(page)).toContainText('Alpha')
  })

  test('a digit typed into the composer stays in the composer', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const composer = page.locator(COMPOSER)
    await composer.click()
    await expect(composer).toBeFocused()

    await page.keyboard.type('2')

    // Both halves matter: the workspace must not move, and the character must
    // actually reach the field. A handler that merely returns early would pass
    // the first assertion and fail the second.
    await expect(composer).toHaveValue('2')
    await expect(activeWorkspaceButton(page)).toContainText('Alpha')

    // Focus leaves the field, and the same key now means "switch workspace".
    await composer.blur()
    await page.keyboard.press('2')
    await expect(activeWorkspaceButton(page)).toContainText('Beta')
  })

  test('Tab reaches the primary navigation without a mouse', async ({ page }) => {
    await boot(page)

    // Walk forward from the top of the document and record what focus lands
    // on. A control that is only reachable by click (no tabindex, or a div
    // with a @click handler) never appears here — which is the regression this
    // guards, and one a mounted component test cannot see at all.
    const reached = new Set<string>()
    for (let i = 0; i < 40; i += 1) {
      await page.keyboard.press('Tab')
      const label = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null
        return el ? (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase() : ''
      })
      if (label) reached.add(label.split('\n').pop() as string)
    }

    // "Home" is the visible label for the home route.
    for (const item of ['home', 'automations', 'memory', 'settings']) {
      expect(reached, `"${item}" should be reachable by Tab`).toContain(item)
    }
  })
})
