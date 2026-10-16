import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The composer's "Move to background" chip (#1413) is a phone-first control.
 * jsdom has no layout engine, so nothing there establishes whether the chip
 * fits beside Stop at 390px, whether it is a 44px tap target, or where focus
 * actually comes to rest when a tap unmounts the chip that had it — the focus
 * path that took four review rounds, tested in vitest only against jsdom's
 * rendering model.
 *
 * The fixture is deliberately silent on a chat's own socket, so these specs
 * speak on it directly (`POST /__fixture__/chat-frame`) with the frames a broker
 * sends for an OpenCode foreground shell: a `tool_use` carrying `backgroundable`,
 * and the `tool_result` plus `result` that end it.
 */
const PHONE = { width: 390, height: 844 }
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const MOVE_ID = 'toolu_e2e_move'
const CHIP = '.composer-move-bg'
const STOP = '.send-btn.is-stop'

type ChipWindow = Window & { __chipAt?: number | null }

/** The `tool_use` a broker emits per foreground shell call. */
function toolUse(backgroundable: boolean) {
  return {
    type: 'tool_use',
    tool_use_id: MOVE_ID,
    tool_name: 'Bash',
    tool_input: JSON.stringify({ command: 'sleep 300 && echo done' }),
    ...(backgroundable ? { backgroundable: true } : {}),
  }
}

/** What a successful move causes: the call ends, and with it the turn. */
const END_OF_TURN = [{ type: 'tool_result', tool_use_id: MOVE_ID }, { type: 'result' }]

async function speak(page: Page, frames: unknown[]) {
  const status = await page.evaluate(async ({ chatId, payload }) => {
    const r = await fetch('/__fixture__/chat-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, frames: payload }),
    })
    return r.status
  }, { chatId: CHAT_ID, payload: frames })
  expect(status).toBe(200)
}

/** Wait for the frame to have been *processed*, not merely sent: only a frame renders Stop. */
async function expectStreaming(page: Page) {
  await expect(page.locator(STOP)).toBeVisible()
}

/**
 * Note when the chip first appears, in page time.
 *
 * The chip is offered by a 1s tick over `Date.now()`, so the 3s rule is a fact
 * about when it appeared, and reading a clock around waits makes the runner or
 * the browser's own scheduling part of the assertion — a stalled thread would
 * fail a correct build. An observer for the one event, read against a start
 * taken before the frame was ever sent, cannot: see `chipDelayMs`.
 */
async function watchForChip(page: Page) {
  await page.evaluate((selector) => {
    ;(window as ChipWindow).__chipAt = null
    const mark = () => {
      if ((window as ChipWindow).__chipAt == null && document.querySelector(selector)) {
        ;(window as ChipWindow).__chipAt = performance.now()
      }
    }
    new MutationObserver(mark).observe(document.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['class'],
    })
    mark()
  }, CHIP)
}

/** The request that carries the call starts the clock; the call itself starts later still. */
async function requestNow(page: Page): Promise<number> {
  return page.evaluate(() => performance.now())
}

/** How long the chip took, or null if it never appeared. */
async function chipDelayMs(page: Page, since: number): Promise<number | null> {
  const appeared = await page.evaluate(() => (window as ChipWindow).__chipAt ?? null)
  if (appeared === null) return null
  return Math.round(appeared - since)
}

/** The chip is a phone control: it must clear --touch and sit inside the layout. */
async function expectTapTargetBesideStop(page: Page) {
  const chip = await page.locator(CHIP).boundingBox()
  const stop = await page.locator('.send-btn').boundingBox()
  const viewport = page.viewportSize()!
  expect(chip).not.toBeNull()
  expect(stop).not.toBeNull()
  expect(chip!.height).toBeGreaterThanOrEqual(43.5)
  expect(chip!.width).toBeGreaterThanOrEqual(43.5)
  // Contained in the viewport. Being ordered left of Stop says nothing on its
  // own: both controls can be off-screen together and still be ordered.
  expect(chip!.x).toBeGreaterThanOrEqual(0)
  expect(chip!.x + chip!.width).toBeLessThanOrEqual(viewport.width)
  expect(chip!.y).toBeGreaterThanOrEqual(0)
  expect(chip!.y + chip!.height).toBeLessThanOrEqual(viewport.height)
  // ...on Stop's row, to its left, not overlapping it.
  expect(chip!.x + chip!.width).toBeLessThanOrEqual(stop!.x + 1)
  const chipMiddle = chip!.y + chip!.height / 2
  expect(chipMiddle).toBeGreaterThan(stop!.y)
  expect(chipMiddle).toBeLessThan(stop!.y + stop!.height)
}

/**
 * Tap the chip with the reply held open, then end the turn, then answer.
 *
 * The order matters and is the whole point: a real move ends the reply, so the
 * chip's `tool_result` retracts the chip and the turn's `result` ends the stream
 * *while the HTTP request is still out*. Focus is therefore restored after the
 * element that held it has unmounted, and `document.activeElement` has fallen to
 * `<body>` — the case that took review rounds 2 and 3. Fulfilling the route
 * eagerly instead would race the two and quietly test the easy path.
 *
 * Returns the requests seen, so a caller can check the count once the whole
 * action has settled rather than only while its first response is held.
 */
async function tapChipAndEndTurn(page: Page): Promise<string[]> {
  let release = () => {}
  const held = new Promise<void>((resolve) => { release = resolve })
  const posted: string[] = []
  await page.route('**/api/chats/*/move-to-background', async (route) => {
    posted.push(route.request().url())
    await held
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ moved: true }) })
  })

  await page.locator(CHIP).click()
  await expect.poll(() => posted.length).toBe(1)

  await speak(page, END_OF_TURN)
  await expect(page.locator(CHIP)).toHaveCount(0)
  await expect(page.locator(STOP)).toHaveCount(0)

  release()
  return posted
}

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `move-bg-${testInfo.workerIndex}`)
})

test.describe('move a running call to the background (#1413), on a phone', () => {
  test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

  test('the chip is offered only once the call has run 3s, as a 44px target by Stop', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await watchForChip(page)
    const requested = await requestNow(page)
    await speak(page, [toolUse(true)])
    await expectStreaming(page)

    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })
    await expectTapTargetBesideStop(page)

    // The delay is the feature, and this asserts it directly. The clock starts
    // before the frame is sent, so the call it dates can only start later, and
    // the chip cannot be early without the measurement being late: the number
    // can be too large, never too small. A build with no delay lands inside one
    // tick of the frame — under a second — and fails here.
    const delay = await chipDelayMs(page, requested)
    expect(delay).not.toBeNull()
    expect(delay!).toBeGreaterThanOrEqual(2_900)

    await page.screenshot({ path: 'test-results/move-bg-phone.png' })
  })

  test('a call that is not backgroundable never offers it, however long it runs', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(false)])

    // Prove the frame landed before asserting anything is absent: without this
    // the absence below would also hold when nothing arrived at all, and the
    // spec would pass against a gate as broken as always offering the chip.
    await expectStreaming(page)
    // Past the 3s threshold plus a whole 1s tick, so a gate that ignored the
    // flag would have rendered the chip by now.
    await page.waitForTimeout(4_500)

    await expect(page.locator(CHIP)).toHaveCount(0)
    await expect(page.locator(STOP)).toBeVisible()
  })

  test('tapping it posts once, and focus survives the chip unmounting', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(true)])
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    const posted = await tapChipAndEndTurn(page)

    // Focus must come to rest inside the composer, on a target that names
    // itself: the bar is the only one that is always focusable, never disabled,
    // and never raises the on-screen keyboard.
    await expect.poll(async () => page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      return {
        inComposer: !!el?.closest('.composer-bar'),
        role: el?.getAttribute('role') ?? null,
        label: el?.getAttribute('aria-label') ?? null,
      }
    })).toEqual({ inComposer: true, role: 'group', label: 'Message composer' })

    // Counted again now the action has settled: what was seen while the first
    // response was held cannot see a second request sent after it.
    expect(posted.length).toBe(1)

    await page.screenshot({ path: 'test-results/move-bg-after-tap.png' })
  })
})

test.describe('move a running call to the background (#1413), on a desktop width', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('tapping it hands focus to the textarea, not the composer bar', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(true)])
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    const posted = await tapChipAndEndTurn(page)

    // Above COMPOSER_FOCUS_MIN_WIDTH the keyboard is not a concern, so the next
    // message can be typed straight away — the other half of the focus rule.
    await expect.poll(() => page.evaluate(() => document.activeElement?.className ?? null))
      .toContain('chat-input')

    expect(posted.length).toBe(1)
  })
})
