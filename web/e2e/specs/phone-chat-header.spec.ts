import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Layout claims about the phone chat header need a real engine: jsdom lays
 * nothing out. At 390px the close X and the code badge give their room to the
 * title (Back and the menu leave the chat; Chat details carries the code mode
 * switch), and the bell's count hangs off the bell icon at every button size.
 */
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('phone chat header', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `phone-header-${testInfo.workerIndex}`)
  })

  test('drops the close X and code badge, widens the title, and anchors the bell badge to the icon', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.chat-title-row .close-btn')).toBeHidden()
    await expect(page.locator('.chat-code-badge')).toBeHidden()

    const title = (await page.locator('.chat-title-row .chat-title').boundingBox())!
    expect(title.width).toBeGreaterThan(150)

    const bell = page.locator('.header-trail .notif-bell')
    await expect(bell).toBeVisible()
    const icon = (await bell.locator('svg').boundingBox())!
    const count = bell.locator('.notif-bell-count')
    await expect(count).toBeVisible()
    const badge = (await count.boundingBox())!
    const overlaps = badge.x < icon.x + icon.width && badge.x + badge.width > icon.x
      && badge.y < icon.y + icon.height && badge.y + badge.height > icon.y
    expect(overlaps, `badge ${JSON.stringify(badge)} floats off icon ${JSON.stringify(icon)}`).toBe(true)

    await page.getByRole('button', { name: 'Show chat details' }).click()
    await expect(page.locator('#chat-work-rail .chat-repo-section')).toBeVisible()
    await expect(page.locator('#chat-work-rail .chat-repo-section').getByText('Code mode', { exact: true })).toBeVisible()
  })
})
