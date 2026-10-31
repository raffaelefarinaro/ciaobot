import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test('Home menu follows a safe-area inset that settles after startup', async ({ page }) => {
  await isolate(page, 'safe-area-header')
  const session = await page.context().newCDPSession(page)
  await session.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: 0 } })
  await boot(page)

  // iOS may initially report zero, then expose the status-bar inset without
  // rotating the device. The header must follow CSS env(), not a cached pixel.
  await session.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: 59 } })
  const menu = page.getByRole('button', { name: 'Open sidebar', exact: true })
  await expect.poll(async () => (await menu.boundingBox())?.y ?? -1).toBeGreaterThanOrEqual(59)
  await expect(page.locator('.header-trail .notif-bell')).toBeInViewport()
  await menu.click()
  await expect(page.locator('.chat-layout')).toHaveClass(/sidebar-open/)
})
