import { expect, test } from '@playwright/test'
import { activeWorkspaceButton, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test: the Connect dialog is a modal sheet whose focus
 * trap, inert background and 44px touch targets only a real engine can judge,
 * and bare digit keys are routed by a window listener that jsdom cannot
 * reproduce faithfully.
 */
const OVERVIEW = '/settings/workspace'
const READY = '.settings-main .section-title'

const CONNECTED = {
  installed: true,
  binary_path: '/usr/local/bin/gws',
  cli_available: true,
  profiles: [{
    name: 'alpha',
    label: 'Alpha Google account',
    purpose: '',
    examples: [],
    configured: true,
    credentials_present: true,
    client_secret_present: true,
    config_dir: '/synthetic/alpha/secrets/gws-alpha',
    workspaces: ['alpha'],
    setup_command: '',
    headless_auth_command: '',
    email: 'alpha@example.com',
    token_valid: true,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
  }],
}

test.describe('workspace google account', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workspace-google-${testInfo.workerIndex}`)
  })

  test('none state opens the dialog, traps focus, closes on Escape and returns focus', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, OVERVIEW, READY)

    const connect = page.getByRole('button', { name: 'Connect Google account' })
    await expect(connect).toBeVisible()
    await connect.click()

    const dialog = page.getByRole('dialog', { name: 'Connect Google account' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByLabel('Display name')).toBeFocused()

    for (let i = 0; i < 15; i++) {
      await page.keyboard.press('Tab')
      const inside = await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'))
      expect(inside, `Tab ${i + 1} left the dialog`).toBe(true)
    }

    // A bare digit with focus on a dialog button must not switch workspace
    // (ChatLayout keeps the digit shortcuts inert while Settings is open).
    const before = await activeWorkspaceButton(page).textContent()
    await dialog.locator('.gws-connect-foot').getByRole('button', { name: 'Close' }).focus()
    await page.keyboard.press('2')
    await expect(activeWorkspaceButton(page)).toHaveText(before ?? '')

    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(connect).toBeFocused()
  })

  test('connected state menu', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.route('**/api/integrations/gws*', (route) => route.fulfill({ json: CONNECTED }))
    await boot(page, OVERVIEW, READY)

    await expect(page.locator('.gws-account')).toContainText('alpha@example.com')
    await expect(page.locator('.gws-account')).toContainText('Connected')
    await page.getByRole('button', { name: 'Actions for Google account' }).click()
    const items = page.getByRole('menuitem')
    await expect(items).toHaveText(['Re-authenticate', 'Advanced / headless setup', 'Disconnect and remove'])
    await page.keyboard.press('Escape')
    await expect(items.first()).toBeHidden()
    await expect(page).toHaveURL(/\/settings\/workspace$/)
  })
})

test.describe('workspace google account on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workspace-google-phone-${testInfo.workerIndex}`)
  })

  test('phone width', async ({ page }) => {
    await boot(page, OVERVIEW, READY)
    await page.getByRole('button', { name: 'Connect Google account' }).click()
    const dialog = page.getByRole('dialog', { name: 'Connect Google account' })
    await expect(dialog).toBeVisible()

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)

    const box = await dialog.boundingBox()
    const viewport = page.viewportSize()!
    expect(Math.round(box?.width ?? 0)).toBe(viewport.width)
    expect(Math.round(box?.height ?? 0)).toBe(viewport.height)

    // Radios are reached through their label rows, so those carry the 44px.
    const controls = dialog.locator('button, input:not([type="radio"]):not([type="file"]), label.gws-radio')
    const count = await controls.count()
    expect(count).toBeGreaterThan(4)
    for (let i = 0; i < count; i++) {
      const el = controls.nth(i)
      if (!(await el.isVisible())) continue
      const b = await el.boundingBox()
      expect(b?.height ?? 0, `control ${i}: ${(await el.textContent())?.trim()}`).toBeGreaterThanOrEqual(44)
    }
  })
})
