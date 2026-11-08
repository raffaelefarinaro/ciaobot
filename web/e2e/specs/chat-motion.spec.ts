import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/** The flag the page-side observer sets; declared here so the spec needs no `any`. */
interface ChatRiseWindow { __sawChatRise?: boolean }

/**
 * Motion that only a real layout can show: a chat opened from Home rises (the
 * reverse of pulling it down), archiving moves to the next chat rather than Home,
 * and the phone drawer slides out on close the way it slides in on open.
 */
test('a chat opened from Home rises into place, then settles', async ({ page }) => { await page.setViewportSize({ width: 1280, height: 800 }); await isolate(page, 'motion-rise'); await boot(page, '/', '.empty-shell:not(.home-boot)')
  // The rise only starts 800 ms after bootstrap (bootSettledAt in ChatLayout.vue), so wait for bootstrap first.
  await page.waitForTimeout(900)
  // The class lives only for the 320 ms rise, so polling for it can miss it on a loaded
  // runner. Latch it in the page the moment any chat panel carries it, before the click.
  await page.evaluate(() => {
    const w = window as unknown as ChatRiseWindow
    const isRising = (el: Element) => el.matches('.chat-panel.chat-rise') || el.querySelector('.chat-panel.chat-rise') !== null
    new MutationObserver(records => {
      for (const r of records) {
        if (r.target instanceof Element && r.type === 'attributes' && isRising(r.target)) w.__sawChatRise = true
        for (const n of r.addedNodes) if (n instanceof Element && isRising(n)) w.__sawChatRise = true
      }
    }).observe(document.body, { attributes: true, attributeFilter: ['class'], childList: true, subtree: true })
  })
  await page.locator('.chat-item', { hasText: 'alpha first conversation' }).click()
  await expect.poll(() => page.evaluate(() => (window as unknown as ChatRiseWindow).__sawChatRise === true)).toBe(true)
  await expect(page.locator('.chat-panel.chat-rise')).toHaveCount(0) })
test('archiving a chat opens the next one in the title rail order instead of Home', async ({ page }) => { await page.setViewportSize({ width: 1280, height: 800 }); await isolate(page, 'motion-archive'); await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.getByRole('button', { name: /^Archive/ }).first().click()
  await page.getByRole('button', { name: 'Archive', exact: true }).last().click()
  await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/) })
test.describe('phone', () => { test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
test('closing the drawer slides it out with its rows still in it, then collapses it', async ({ page }) => { await isolate(page, 'motion-drawer'); await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.getByRole('button', { name: /open sidebar|menu/i }).first().click()
  await expect(page.locator('.sidebar-backdrop')).toBeVisible()
  await page.locator('.sidebar-backdrop').click({ position: { x: 370, y: 400 } })
  await expect(page.locator('.sidebar.sidebar--closing')).toHaveCount(1)
  await expect(page.locator('.sidebar.sidebar--closing .project-list')).toBeVisible()
  await page.waitForTimeout(300); await expect(page.locator('.sidebar.collapsed')).toHaveCount(1); await expect(page.locator('.sidebar-backdrop')).toHaveCount(0) }) })

test('closing Chat details keeps it closed in the next chat', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 }); await isolate(page, 'motion-details'); await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await expect(page.locator('.chat-panel--rail')).toHaveCount(1)
  await page.getByRole('button', { name: 'Hide chat details' }).click()
  await expect(page.locator('.chat-panel--rail')).toHaveCount(0)
  await page.locator('.chat-item', { hasText: 'alpha second conversation' }).click()
  await expect(page).toHaveURL(/alpha-chat-2$/)
  await expect(page.locator('.chat-panel')).toHaveCount(1)
  await expect(page.locator('.chat-panel--rail')).toHaveCount(0)
})

test('switching to a chat with a pinned file slides the chat first, then the file', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 }); await isolate(page, 'motion-tile')
  await page.route(/\/api\/chats(\/alpha-chat-2)?(\?.*)?$/, async route => {
    const response = await route.fetch()
    const body = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
    const pin = (c: Record<string, unknown>) => c.chat_id === 'alpha-chat-2' ? { ...c, pinned_file_path: 'notes/plan.md' } : c
    await route.fulfill({ response, json: Array.isArray(body) ? body.map(pin) : pin(body) })
  })
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.locator('.chat-item', { hasText: 'alpha second conversation' }).click()
  // Still hidden while the chat slides in. The arriving class lives only for the
  // ~520 ms tile animation, so find it and read its opacity in the same frame:
  // a separate count check and evaluate can straddle its removal on a loaded runner.
  const arriving = await page.waitForFunction(() => {
    const el = document.querySelector('.chat-split-side--arriving')
    // Wrapped in an object: a bare opacity of 0 is falsy and would keep waiting.
    return el ? { opacity: Number(getComputedStyle(el).opacity) } : null
  }, undefined, { polling: 'raf' })
  expect((await arriving.jsonValue())!.opacity).toBeLessThan(0.5)
  await expect(page.locator('.chat-split-side--arriving')).toHaveCount(0)
  await expect(page.locator('.chat-split-side')).toBeVisible()
})
