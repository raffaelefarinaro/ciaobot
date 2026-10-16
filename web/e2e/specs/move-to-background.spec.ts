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

interface Timings {
  /** When the app's own socket delivered the tool call, page time. */
  toolUse: number | null
  /** When the chip first appeared, page time. */
  chip: number | null
}
type TimingWindow = Window & { __timings?: Timings }

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

/**
 * Note when the app's own socket receives the call.
 *
 * This is the instant the store dates the call, so the 3s rule can be measured
 * from it and nothing else: not from when the test asked for the frame, which
 * would count transport latency as part of the delay, and not from when Stop
 * rendered, which is the browser's scheduling rather than the app's clock.
 * Installed before the app loads, since the socket it hooks is created at
 * startup.
 */
async function watchFrameArrival(page: Page, moveId: string) {
  await page.addInitScript((wanted) => {
    const Native = window.WebSocket
    const timings: Timings = { toolUse: null, chip: null }
    ;(window as TimingWindow).__timings = timings
    window.WebSocket = class extends Native {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols)
        this.addEventListener('message', (event: MessageEvent) => {
          if (timings.toolUse !== null || typeof event.data !== 'string') return
          try {
            const frame = JSON.parse(event.data) as { type?: unknown; tool_use_id?: unknown }
            // The events socket carries a snapshot too, and a loose match on the
            // text would let anything in it — a transcript entry, say — start the
            // clock early and hide a missing delay.
            if (frame.type === 'tool_use' && frame.tool_use_id === wanted) {
              timings.toolUse = performance.now()
            }
          } catch {
            /* not a frame this test cares about */
          }
        })
      }
    } as unknown as typeof WebSocket
  }, moveId)
}

/** Note when the chip first appears, in page time. */
async function watchForChip(page: Page) {
  await page.evaluate((selector) => {
    const timings = (window as TimingWindow).__timings
    if (!timings) throw new Error('the frame-arrival hook did not install')
    const mark = () => {
      if (timings.chip === null && document.querySelector(selector)) timings.chip = performance.now()
    }
    new MutationObserver(mark).observe(document.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['class'],
    })
    mark()
  }, CHIP)
}

/** How long the chip took to follow the call, or null if either never happened. */
async function chipDelayAfterCallMs(page: Page): Promise<number | null> {
  const timings = await page.evaluate(() => (window as TimingWindow).__timings ?? null)
  if (!timings || timings.toolUse === null || timings.chip === null) return null
  return Math.round(timings.chip - timings.toolUse)
}

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
 * Returns the requests seen; callers check the count again after a quiet window,
 * since a count read while the first reply is held cannot see a later one.
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

  // The chip disables itself while the request is out: that is the guard that
  // keeps one tap to one request, so assert it rather than only the count. A
  // second activation is attempted in-page rather than as a real tap, because a
  // real tap on a disabled control moves focus to the composer bar, and this
  // test is about where focus lands afterwards.
  await expect(page.locator(CHIP)).toBeDisabled()
  await page.locator(CHIP).evaluate((el) => (el as HTMLElement).click())
  expect(posted.length).toBe(1)

  await speak(page, END_OF_TURN)
  await expect(page.locator(CHIP)).toHaveCount(0)
  await expect(page.locator(STOP)).toHaveCount(0)

  release()
  return posted
}

/** One request, and none in a quiet window afterwards however the action ended. */
async function expectNoFurtherMove(page: Page, posted: string[]) {
  await page.waitForTimeout(750)
  expect(posted.length).toBe(1)
}

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `move-bg-${testInfo.workerIndex}`)
})

test.describe('move a running call to the background (#1413), on a phone', () => {
  test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

  test('the chip is offered only once the call has run 3s, as a 44px target by Stop', async ({ page }) => {
    await watchFrameArrival(page, MOVE_ID)
    await boot(page, CHAT_PATH, COMPOSER)
    await watchForChip(page)
    await speak(page, [toolUse(true)])
    await expectStreaming(page)

    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })
    await expectTapTargetBesideStop(page)

    // The delay is the feature, and this is the assertion of it: measured from
    // the moment the app's socket delivered the call, to the moment the chip
    // appeared. A build with no delay offers it within one tick of that arrival
    // and fails; counting the test's own request, or Stop's rendering, would
    // have let transport latency or a stalled thread decide the number.
    const delay = await chipDelayAfterCallMs(page)
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

    await expectNoFurtherMove(page, posted)
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

    await expectNoFurtherMove(page, posted)
  })
})
