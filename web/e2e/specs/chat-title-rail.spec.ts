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

  test('centres the title with the neighbours off-screen, puts the state track on the header\'s bottom edge, and slides to the next chat with the arrow key', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const current = page.locator('.title-rail-item[aria-current="page"]')
    await expect(current).toContainText('alpha first conversation')
    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)
    await expect(page.locator('.chat-code-badge')).toHaveCount(0)

    const header = page.locator('.chat-panel .pane-header')
    expect(await header.evaluate(el => el.getBoundingClientRect().height)).toBe(61)

    const box = (selector: string) => page.locator(selector).first().evaluate(el => {
      const b = el.getBoundingClientRect()
      return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, centreX: b.left + b.width / 2, centreY: b.top + b.height / 2 }
    })
    const close = await box('.chat-title-row .close-btn')
    const title = await box('.title-rail-item[aria-current="page"] .chat-title')
    const block = await box('.title-rail-item[aria-current="page"]')
    const strip = await box('.title-rail-strip')
    const track = await box('.title-rail-track')
    const head = await box('.chat-panel .pane-header')
    // Centred in the rail, with close on the block's vertical centre.
    expect(Math.abs(title.centreX - strip.centreX)).toBeLessThanOrEqual(2)
    expect(Math.abs(close.centreY - block.centreY)).toBeLessThanOrEqual(2)
    // The neighbour is off-screen until a swipe or step brings it in.
    const next = await box('.title-rail-item:not([aria-current]) .title-rail-title')
    expect(next.left).toBeGreaterThanOrEqual(strip.right - 1)
    // The track sits on the header's bottom edge, centred under the title, below the project line.
    expect(Math.abs(track.bottom - head.bottom)).toBeLessThanOrEqual(2)
    expect(Math.abs(track.centreX - strip.centreX)).toBeLessThanOrEqual(2)
    const project = await box('.title-rail-item[aria-current="page"] .title-rail-project')
    expect(track.top).toBeGreaterThan(project.bottom)
    await expect(page.locator('.title-rail-track .title-rail-seg')).toHaveCount(2)

    await current.focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    await expect(page.locator('.title-rail-item[aria-current="page"]')).toContainText('alpha second conversation')
  })

  test('with 13 chats the track keeps one segment per chat inside the rail and the header stays 61px', async ({ page }) => {
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

    await expect(page.locator('.title-rail-track .title-rail-seg')).toHaveCount(13)
    expect(await page.locator('.chat-panel .pane-header').evaluate(el => el.getBoundingClientRect().height)).toBe(61)
    const [strip, track] = await page.locator('.title-rail-strip, .title-rail-track').evaluateAll(
      els => els.map(el => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right } }),
    )
    expect(track.left).toBeGreaterThanOrEqual(strip.left)
    expect(track.right).toBeLessThanOrEqual(strip.right)
  })

  test('the chat shortcut is inert while typing, and steps the rail with Option+brackets otherwise', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await page.locator(COMPOSER).focus()
    await page.keyboard.press('Alt+BracketRight')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)

    await page.locator(COMPOSER).evaluate(el => (el as HTMLElement).blur())
    await page.keyboard.press('Alt+BracketRight')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    // Opening a chat focuses its composer on mount: wait for that focus before
    // blurring, or the blur can land first and the next press hits the typing gate.
    await expect(page.locator(COMPOSER)).toBeFocused()
    await page.locator(COMPOSER).evaluate(el => (el as HTMLElement).blur())
    await page.keyboard.press('Alt+BracketLeft')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
  })
})

test.describe('chat title rail, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `title-rail-phone-${testInfo.workerIndex}`)
  })

  test('has no arrow buttons, snaps the strip, switches on a finished swipe, and never scrolls the document sideways', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)
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
