import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/** The flag the page-side observer sets; declared here so the spec needs no `any`. */
interface ChatRiseWindow { __sawChatRise?: boolean }
/** The arriving tile's opacity when the class first appeared, latched by the page-side observer. */
interface TileArrivalWindow { __tileArrival?: { opacity: number } }

/**
 * Motion that only a real layout can show: a chat opened from Home rises (the
 * reverse of pulling it down), archiving moves to the next chat rather than Home,
 * and the phone drawer slides out on close the way it slides in on open.
 */
/**
 * A chat opened from Home rises into place only once its real panel has arrived
 * (#3086). The ChatPanel chunk is held on the wire, so the skeleton stays on
 * screen past the whole 340 ms window and the page clock proves it: nothing is
 * marked while the panel is absent, and the rise runs its full window after the
 * panel mounts. The class is latched in the page, since it lives only for that
 * window and a polled read can miss it on a loaded runner.
 */
test('a chat opened from Home rises into place when its panel arrives, then settles', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await isolate(page, 'motion-rise')
  // Held before Home boots, so a request on Home would be caught.
  let chunkRequests = 0
  let markRequested!: () => void
  const requested = new Promise<void>(resolve => { markRequested = resolve })
  let releaseChunk!: () => void
  const gate = new Promise<void>(resolve => { releaseChunk = resolve })
  await page.route(/\/assets\/ChatPanel-[^/]+\.js(\?.*)?$/, async route => {
    chunkRequests += 1
    markRequested()
    await gate
    await route.continue()
  })
  try {
    await boot(page, '/', '.empty-shell:not(.home-boot)')
    expect(chunkRequests).toBe(0)

    // The rise waits 800 ms after bootstrap on the app's clock: install the page clock now and move it past that.
    await page.clock.install({ time: new Date() })
    await page.clock.runFor(900)
    // Latch the class in the page before the click: the observer sees it the moment a chat panel carries it.
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
    await requested
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)

    // Held: the skeleton is immediate, and no real panel or rise exists, before and after 1000 ms more.
    await page.clock.pauseAt(new Date(Date.now() + 1000))
    const skeleton = page.locator('[role="status"][aria-label="Loading conversation"]')
    await expect(skeleton).toBeVisible()
    await expect(page.locator('.chat-panel')).toHaveCount(0)
    await page.clock.runFor(1000)
    await expect(skeleton).toBeVisible()
    await expect(page.locator('.chat-panel')).toHaveCount(0)
    expect(await page.evaluate(() => (window as unknown as ChatRiseWindow).__sawChatRise === true)).toBe(false)

    // Release the chunk: the real panel mounts and rises for the full window.
    releaseChunk()
    const panel = page.locator('.chat-panel').first()
    const arrived = expect(panel).toBeVisible()
    await page.clock.runFor(16)
    await arrived
    await expect.poll(() => page.evaluate(() => (window as unknown as ChatRiseWindow).__sawChatRise === true)).toBe(true)
    await page.clock.runFor(340)
    await expect(page.locator('.chat-panel.chat-rise')).toHaveCount(0)
  } finally {
    // Never leave the held chunk stuck if an assertion above failed.
    releaseChunk()
  }
})

/**
 * Under reduced motion the chat arrives without travel: the panel is usable, no
 * arrival class or timer starts, and its computed animation is none.
 */
test('under reduced motion a chat opened from Home arrives without the rise', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await isolate(page, 'motion-rise-reduced')
  await boot(page, '/', '.empty-shell:not(.home-boot)')
  await page.clock.install({ time: new Date() })
  await page.clock.runFor(900)
  await page.locator('.chat-item', { hasText: 'alpha first conversation' }).click()
  await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
  await expect(page.locator('.chat-panel').first()).toBeVisible()
  await expect(page.locator('.chat-panel.chat-rise')).toHaveCount(0)
  const motion = await page.locator('.chat-panel').first().evaluate(el => {
    const style = getComputedStyle(el)
    return { animationName: style.animationName, transform: style.transform }
  })
  expect(motion.animationName).toBe('none')
  expect(['none', 'matrix(1, 0, 0, 1, 0, 0)']).toContain(motion.transform)
})

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
  // The arriving class lives only for the ~560 ms tile animation, and a loaded
  // runner can return from the click after it is gone. Latch it in the page,
  // before the click, and read its opacity in the same task that adds it.
  await page.evaluate(() => {
    const w = window as unknown as TileArrivalWindow
    new MutationObserver(() => {
      if (w.__tileArrival) return
      const el = document.querySelector('.chat-split-side--arriving')
      if (el) w.__tileArrival = { opacity: Number(getComputedStyle(el).opacity) }
    }).observe(document.body, { attributes: true, attributeFilter: ['class'], childList: true, subtree: true })
  })
  await page.locator('.chat-item', { hasText: 'alpha second conversation' }).click()
  // Still hidden while the chat slides in.
  await expect.poll(() => page.evaluate(() => (window as unknown as TileArrivalWindow).__tileArrival?.opacity ?? null)).not.toBeNull()
  expect(await page.evaluate(() => (window as unknown as TileArrivalWindow).__tileArrival!.opacity)).toBeLessThan(0.5)
  await expect(page.locator('.chat-split-side--arriving')).toHaveCount(0)
  await expect(page.locator('.chat-split-side')).toBeVisible()
})
