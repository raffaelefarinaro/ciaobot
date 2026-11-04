import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Layout claims about the phone chat header need a real engine: jsdom lays
 * nothing out. At 390px the close X and the code badge give
 * their room to the title rail (Back and the menu leave the chat; Chat details
 * carries the code mode switch), and the bell's count hangs off the bell icon
 * at every button size.
 */
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('phone chat header', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `phone-header-${testInfo.workerIndex}`)
  })

  test('drops the close X and code badge, widens the title, and anchors the bell badge to the icon', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.chat-title-row .close-btn')).toBeHidden()
    await expect(page.locator('.chat-code-badge')).toHaveCount(0)
    await expect(page.locator('.chat-title-row .chat-title')).toBeVisible()

    const rail = (await page.locator('.chat-title-row .title-rail').boundingBox())!
    expect(rail.width).toBeGreaterThan(150)
    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)

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

  test('a pinned file never moves the header: its opener sits under the header, not above it', async ({ page }) => {
    // alpha-chat-2 gets a pinned file; alpha-chat-1 has none. The opener used to sit
    // above the header, under the status bar, and pushed the header down.
    await page.route(/\/api\/chats(\/alpha-chat-2)?(\?.*)?$/, async route => {
      const response = await route.fetch()
      const body = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
      const pin = (c: Record<string, unknown>) => c.chat_id === 'alpha-chat-2' ? { ...c, pinned_file_path: 'notes/plan.md' } : c
      await route.fulfill({ response, json: Array.isArray(body) ? body.map(pin) : pin(body) })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const header = page.locator('.chat-panel .pane-header')
    const plainTop = await header.evaluate(el => el.getBoundingClientRect().top)

    await page.goto('/chat/alpha-chat-2')
    const opener = page.locator('.narrow-pin-opener')
    await expect(opener).toBeVisible()
    expect(await header.evaluate(el => el.getBoundingClientRect().top)).toBe(plainTop)
    const headerBottom = await header.evaluate(el => el.getBoundingClientRect().bottom)
    expect(await opener.evaluate(el => el.getBoundingClientRect().top)).toBeGreaterThanOrEqual(headerBottom - 1)
  })
})
