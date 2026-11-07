import { expect, test } from '@playwright/test'
import { COMPOSER, INBOX_DIALOG, boot, isolate, openChatDetails, openInbox, seedInbox } from '../support/app'

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
    // A memory pass never reaches the inbox, so seed the ordinary unread chat
    // this layout claim needs (the same way notification-inbox.spec.ts does).
    await seedInbox(page, {
      chats: {
        'alpha-chat-2': {
          last_activity_at: '2026-01-02T10:00:00Z',
          last_read_at: '2026-01-01T09:05:00Z',
          last_snippet: 'Done',
        },
      },
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.chat-title-row .close-btn')).toBeHidden()
    await expect(page.locator('.chat-code-badge')).toHaveCount(0)
    await expect(page.locator('.chat-title-row .chat-title')).toBeVisible()

    const rail = (await page.locator('.chat-title-row .title-rail').boundingBox())!
    expect(rail.width).toBeGreaterThan(220)
    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)

    // The bell and Back moved into the ⋯ menu, so the header holds ☰, the title and ⋯.
    await expect(page.locator('.header-trail .notif-bell')).toBeHidden()
    await expect(page.locator('.header-back')).toBeHidden()
    const more = page.getByRole('button', { name: /^More chat actions/ })
    await expect(more).toBeVisible()
    const icon = (await more.locator('svg').boundingBox())!
    const dot = page.locator('.chat-more-dot')
    await expect(dot).toBeVisible()
    const badge = (await dot.boundingBox())!
    const overlaps = badge.x < icon.x + icon.width && badge.x + badge.width > icon.x
      && badge.y < icon.y + icon.height && badge.y + badge.height > icon.y
    expect(overlaps, `dot ${JSON.stringify(badge)} floats off the ⋯ icon ${JSON.stringify(icon)}`).toBe(true)

    await openChatDetails(page)
    await expect(page.locator('#chat-work-rail .chat-repo-section')).toBeVisible()
    await expect(page.locator('#chat-work-rail .chat-repo-section').getByText('Code mode', { exact: true })).toBeVisible()
  })

  test('the ⋯ menu holds Chat details, Notifications, Archive and Back', async ({ page }) => {
    // Back needs in-app history, so reach the chat from Home through the inbox
    // (its row navigates), not by a full load.
    await seedInbox(page, {
      chats: {
        'alpha-chat-2': {
          last_activity_at: '2026-01-02T10:00:00Z',
          last_read_at: '2026-01-01T09:05:00Z',
          last_snippet: 'Done',
        },
      },
    })
    await boot(page)
    const inbox = await openInbox(page, '.header-trail .notif-bell')
    await inbox.locator('.notif-row').first().click()
    await expect(page.locator('.chat-panel .pane-header')).toBeVisible()

    const more = page.getByRole('button', { name: /^More chat actions/ })
    await more.click()
    const menu = page.getByRole('menu')
    const names = menu.getByRole('menuitem')
    await expect(names.filter({ hasText: 'Chat details' })).toBeVisible()
    await expect(names.filter({ hasText: 'Notifications' })).toBeVisible()
    await expect(names.filter({ hasText: 'Archive' })).toBeVisible()
    await expect(names.filter({ hasText: /^Back to/ })).toBeVisible()
    for (const item of await names.all()) {
      expect((await item.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }

    await names.filter({ hasText: 'Notifications' }).click()
    await expect(page.locator(INBOX_DIALOG)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator(INBOX_DIALOG)).toBeHidden()
    await expect(more).toBeFocused()

    // Chat details opened from the menu return focus to ⋯ however they close.
    const details = page.locator('#chat-work-rail')
    await more.click()
    await page.getByRole('menuitem', { name: 'Chat details' }).click()
    await expect(details).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(details).toBeHidden()
    await expect(more).toBeFocused()

    await more.click()
    await page.getByRole('menuitem', { name: 'Chat details' }).click()
    await expect(details).toBeVisible()
    await page.getByRole('button', { name: 'Hide chat details' }).click()
    await expect(details).toBeHidden()
    await expect(more).toBeFocused()
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
