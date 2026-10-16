import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The composer's "Move to background" chip (#1413) is a phone-first control.
 * jsdom has no layout engine, so nothing there establishes whether the chip
 * fits beside Stop at 390px, whether it is a 44px tap target, or where focus
 * actually lands after a tap that ends the turn — the focus path that took four
 * review rounds to get right, tested only against jsdom's rendering model.
 */
const PHONE = { width: 390, height: 844 }

test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const CHIP = '.composer-move-bg'

/** Hand the open chat one synthetic tool call over its socket. */
async function deliverToolFrame(page: Page, opts: { backgroundable?: boolean; id?: string } = {}) {
  const payload = {
    chat_id: CHAT_ID,
    tool_use_id: opts.id ?? 'toolu_e2e_move',
    backgroundable: opts.backgroundable !== false,
  }
  const status = await page.evaluate(async (body) => {
    const r = await fetch('/__fixture__/tool-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    return r.status
  }, payload)
  expect(status).toBe(200)
}

test.describe('move a running call to the background (#1413)', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `move-bg-${testInfo.workerIndex}`)
  })

  test('a backgroundable call offers the chip by Stop after 3s, as a 44px tap target', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await expect(page.locator(CHIP)).toBeHidden()

    await deliverToolFrame(page)

    // The chip is offered only once the call has run MOVE_TO_BACKGROUND_DELAY_MS.
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })
    // A live turn is the other half of the gate: Stop is what it sits beside.
    await expect(page.locator('.send-btn.is-stop')).toBeVisible()

    const box = await page.locator(CHIP).boundingBox()
    expect(box).not.toBeNull()
    // --touch, the design token every phone control has to clear.
    expect(box!.height).toBeGreaterThanOrEqual(43.5)
    expect(box!.width).toBeGreaterThanOrEqual(43.5)
    // It must fit inside the viewport, beside the button, not push past it.
    const stop = await page.locator('.send-btn').boundingBox()
    expect(box!.x + box!.width).toBeLessThanOrEqual(stop!.x + 1)

    await page.screenshot({ path: 'test-results/move-bg-phone.png' })
  })

  test('a call that is not backgroundable never offers it', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)

    await deliverToolFrame(page, { backgroundable: false })
    // Past the delay, so the absence is the gate and not the clock.
    await page.waitForTimeout(4_000)

    await expect(page.locator(CHIP)).toHaveCount(0)
    // ...and the turn really is live, so the composer is showing Stop.
    await expect(page.locator('.send-btn.is-stop')).toBeVisible()
  })

  test('tapping it posts once and leaves focus inside the composer', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await deliverToolFrame(page)
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    const posted: string[] = []
    await page.route('**/api/chats/*/move-to-background', async (route) => {
      posted.push(route.request().url())
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
    })

    const chip = page.locator(CHIP)
    await chip.click()
    await expect.poll(() => posted.length).toBe(1)
    expect(posted[0]).toContain(`/api/chats/${CHAT_ID}/move-to-background`)

    // Focus must stay in the composer, on a target that identifies itself: the
    // bar is the only stable destination (the send button is disabled once the
    // turn ends with an empty draft, and the textarea would raise the keyboard).
    // Polled, not read once: the restoration runs after the store's request
    // resolves, so a single read can catch the chip still holding focus.
    await expect.poll(async () => page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      return {
        inComposer: !!el?.closest('.composer-bar'),
        role: el?.getAttribute('role') ?? null,
        label: el?.getAttribute('aria-label') ?? null,
      }
    })).toEqual({ inComposer: true, role: 'group', label: 'Message composer' })

    await page.screenshot({ path: 'test-results/move-bg-after-tap.png' })
  })
})
