import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * The chat title rail needs a real layout: jsdom has no scroll snapping, no
 * touch scrolling and no box geometry. The fixture gives workspace alpha two
 * active chats with the same timestamps, both read, so the rail order is
 * [alpha-chat-1, alpha-chat-2].
 */

test.describe('chat title rail, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `title-rail-desktop-${testInfo.workerIndex}`)
  })

  test('shows the current chat centred with its neighbour, keeps the 61px header and the title line, and switches with the arrows', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.title-rail [aria-current="page"]')).toContainText('alpha first conversation')
    await expect(page.getByRole('button', { name: /alpha second conversation/ })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Previous chat' })).toBeDisabled()

    const headerHeight = await page.locator('.chat-panel .pane-header').evaluate(el => el.getBoundingClientRect().height)
    expect(headerHeight).toBe(61)

    const centreY = (selector: string) => page.locator(selector).evaluate(el => {
      const box = el.getBoundingClientRect()
      return box.top + box.height / 2
    })
    const titleY = await centreY('.chat-title-row .chat-title')
    expect(Math.abs((await centreY('.chat-title-row .close-btn')) - titleY)).toBeLessThanOrEqual(2)
    expect(Math.abs((await centreY('.chat-title-row .chat-code-badge')) - titleY)).toBeLessThanOrEqual(2)

    await page.getByRole('button', { name: 'Next chat' }).click()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    await expect(page.locator('.title-rail [aria-current="page"]')).toContainText('alpha second conversation')
  })

  test('with 13 chats the n / N count sits clear of the project line and the header stays 61px', async ({ page }) => {
    // Eleven more chats in alpha, copied from alpha-chat-2 so they share its
    // project. The fixture itself stays as it is for the other specs.
    await page.route(/\/api\/chats(\?.*)?$/, async route => {
      const response = await route.fetch()
      const chats = await response.json() as Array<Record<string, unknown>>
      const base = chats.find(c => c.chat_id === 'alpha-chat-2')!
      const extra = Array.from({ length: 11 }, (_, i) => ({ ...base, chat_id: `alpha-extra-${i}`, title: `alpha extra ${i}` }))
      await route.fulfill({ response, json: [...chats, ...extra] })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.title-rail-count')).toHaveText('1 / 13')
    const headerHeight = await page.locator('.chat-panel .pane-header').evaluate(el => el.getBoundingClientRect().height)
    expect(headerHeight).toBe(61)

    const boxes = await page.locator('.title-rail-item[aria-current] .title-rail-project, .title-rail-count').evaluateAll(
      els => els.map(el => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom } }),
    )
    const [project, count] = boxes
    expect(count.top, 'count must start below the project line').toBeGreaterThanOrEqual(project.bottom)
  })
})

test.describe('chat title rail, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `title-rail-phone-${testInfo.workerIndex}`)
  })

  test('hides the arrows, snaps the strip, switches on a finished swipe, and never scrolls the document sideways', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.getByRole('button', { name: 'Next chat' })).toBeHidden()
    const snap = await page.locator('.title-rail-strip').evaluate(el => getComputedStyle(el).scrollSnapType)
    expect(snap).toContain('x mandatory')

    await page.locator('.title-rail-strip').evaluate(el => {
      el.dispatchEvent(new Event('touchstart'))
      el.dispatchEvent(new Event('touchend'))
      el.scrollLeft = el.scrollWidth
    })
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)

    const overflow = await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)
    expect(overflow).toBe(true)
  })
})
