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
 *
 * The 3s rule is a rule about the app's own clock, so the test that asserts it
 * runs that clock by hand (`page.clock`), which is what lets it put the
 * boundary where it is instead of inferring it from wall-clock arrivals.
 */
const PHONE = { width: 390, height: 844 }
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const MOVE_ID = 'toolu_e2e_move'
const CHIP = '.composer-move-bg'
const STOP = '.send-btn.is-stop'

/** The `tool_use` a broker emits per foreground shell call. */
function toolUse(id: string, command: string, backgroundable: boolean) {
  return {
    type: 'tool_use',
    tool_use_id: id,
    tool_name: 'Bash',
    tool_input: JSON.stringify({ command }),
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
  // ...and Stop against the bar's right edge. The compact activity cluster is
  // the bar's spacer; when it stopped growing, chip and Stop slid left to sit
  // beside the attach button.
  const bar = await page.locator('.composer-bar').boundingBox()
  expect(bar!.x + bar!.width - (stop!.x + stop!.width)).toBeLessThanOrEqual(4)
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

/** The class of whatever holds focus, which is what the focus log and the guard read. */
const focusedClass = (page: Page) => page.evaluate(() => document.activeElement?.className ?? null)

/**
 * Where the user might have moved focus while the move request was out. One case
 * is enough here: it proves the restore consults the guard with the real
 * elements. The guard's own rule — the chip or the body, nothing else — is a
 * unit in `src/lib/moveToBackground.ts`, because enumerating places was how
 * every review round of this file found one more container the rule could be
 * widened to (composer bar, composer surface, chat layout, the toast beside it,
 * the app root, and the comment box teleported outside it); that argument was
 * never going to end in a test.
 */
type FocusTarget = {
  what: string
  /** The class focus must still be on when the move call returns. */
  focused: string
  move: (page: Page) => Promise<void>
}

const DRAFT_FOCUS: FocusTarget = {
  what: 'the draft the user is writing',
  focused: 'chat-input',
  move: (page) => page.locator('.chat-input').fill('next message'),
}

/**
 * The chip is retracted before the response comes back — the order a call that
 * ends on its own produces — with the user having moved focus meanwhile. Focus
 * must still be where they put it once the move call has returned.
 */
async function expectMoveLeavesFocusAlone(page: Page, target: FocusTarget) {
  await boot(page, CHAT_PATH, COMPOSER)

  // The chip is unmounted by the time this restore runs, so the chip cannot
  // witness it: record every focus change instead. Recording starts before the
  // tap, so focus from the boot is not in the log.
  await page.evaluate(() => {
    const w = window as unknown as { __focusLog: string[] }
    w.__focusLog = []
    document.addEventListener('focusin', (e) => {
      w.__focusLog.push((e.target as HTMLElement).className || 'body')
    }, true)
  })

  await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
  await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

  // Hold the move open, tap the chip to arm the restore, then move focus
  // somewhere of the user's own choosing.
  let release = () => {}
  const held = new Promise<void>((resolve) => { release = resolve })
  const posted: string[] = []
  await page.route('**/api/chats/*/move-to-background', async (route) => {
    posted.push(route.request().url())
    await held
    // The call had already ended, so there was nothing left to move. The guard
    // does not turn on the result, only on where focus is.
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ moved: false }) })
  })

  await page.locator(CHIP).focus()
  await expect.poll(() => focusedClass(page)).toContain('composer-move-bg')
  await page.locator(CHIP).click()
  await expect.poll(() => posted.length).toBe(1)
  await expect(page.locator(CHIP)).toBeDisabled()

  await target.move(page)
  expect(await focusedClass(page)).toContain(target.focused)
  // Whatever the user's own move logged is theirs. From here nothing may take
  // focus — and the chip unmounting cannot, since it is not the focused element
  // at this point.
  const loggedWhenTheyMoved = await page.evaluate(() => (window as unknown as { __focusLog: string[] }).__focusLog.length)

  await speak(page, END_OF_TURN)
  await expect(page.locator(CHIP)).toHaveCount(0)
  await expect(page.locator(STOP)).toHaveCount(0)
  release()

  // "Nothing to move" is the app's own word that the move call has returned, and
  // the restore runs in the continuation after it. The toast's DOM is only
  // observable from a later task, once the whole microtask drain that ran the
  // restore has finished, so this is a signal rather than a settle.
  await expect(page.locator('.toast-title', { hasText: 'Nothing to move' })).toBeVisible()

  const focusLog = await page.evaluate(() => (window as unknown as { __focusLog: string[] }).__focusLog)
  expect(focusLog.slice(loggedWhenTheyMoved)).toEqual([])
  expect(await focusedClass(page)).toContain(target.focused)
}

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `move-bg-${testInfo.workerIndex}`)
})

test.describe('move a running call to the background (#1413), on a phone', () => {
  test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

  test('the chip is offered only once the call has run 3s, as a 44px target by Stop', async ({ page }) => {
    await page.clock.install()
    await page.clock.pauseAt(Date.now())
    await boot(page, CHAT_PATH, COMPOSER)
    // A frozen clock does not fire the timer the app opens its chat socket on.
    await page.clock.runFor(300)

    // A turn is already running when the call arrives — which is the real case,
    // since a foreground shell shows up after the reply has started — and that
    // is what puts the call *between* two of the composer's 1s ticks rather
    // than on one. The tick is what decides when the chip appears, so a call
    // landing on a tick would hide a delay one tick too short: both thresholds
    // would be answered by that same tick. 200ms in, the ticks fall at 800,
    // 1800, 2800 and 3800ms of call age, so 2800ms is a tick that a 3s rule
    // must refuse and a 2.5s rule would have taken.
    await speak(page, [toolUse('toolu_e2e_open', 'echo first', false)])
    await expectStreaming(page)
    await page.clock.runFor(200)
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
    // Prove the call landed before asserting anything is absent: without this
    // the absence below would also hold when nothing arrived at all.
    await expect(page.locator('.activity-line', { hasText: 'sleep 300' })).toHaveCount(1)

    // And it stays unoffered to 2810ms — past the tick at 2800ms, short of 3s.
    // Checked right through rather than only at the end, so a build that offered
    // it early and withdrew it again cannot read as never offering it. A build
    // with no delay, or one a tick short, offers it on or before the 2800ms tick
    // and fails here; measuring arrival-to-render in wall time instead let
    // thread scheduling and transport latency decide the number.
    for (let age = 0; age < 2_810; age += 100) {
      const step = Math.min(100, 2_810 - age)
      await page.clock.runFor(step)
      await expect(page.locator(CHIP), `the chip was offered ${age + step}ms into the call`).toHaveCount(0)
    }
    // The next tick, at 3800ms of call age.
    await page.clock.runFor(1_000)
    await expect(page.locator(CHIP)).toBeVisible()

    await expectTapTargetBesideStop(page)
    await page.screenshot({ path: 'test-results/move-bg-phone.png' })
  })

  test('a call that is not backgroundable never offers it, however long it runs', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', false)])

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
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
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

  test('focus the user moved while the request was out is left where they put it', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    // Hold the move open so the user can move focus while it is in flight —
    // the case the comment in moveToBackground() describes: they go to the
    // textarea to write the next message.
    let release = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    const posted: string[] = []
    await page.route('**/api/chats/*/move-to-background', async (route) => {
      posted.push(route.request().url())
      await held
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ moved: true }) })
    })

    // The tap arms the restore by taking focus, and the chip disables itself the
    // moment it is clicked — a disabled button cannot hold focus, so by the time
    // the request is out the chip's focus has already fallen to <body>. Arm the
    // restore explicitly and assert it before the click, since a chip that was
    // never focused is a chip with nothing to give back.
    await page.locator(CHIP).focus()
    await expect.poll(() => page.evaluate(() => document.activeElement?.className ?? null))
      .toContain('composer-move-bg')
    await page.locator(CHIP).click()
    await expect.poll(() => posted.length).toBe(1)
    await expect(page.locator(CHIP)).toBeDisabled()

    await page.locator('.chat-input').fill('next message')
    release()

    // The chip re-enabling is the request coming back, and the restore runs in
    // the same continuation that re-enables it — so once this passes the
    // restore has had its say and the check below is not racing it. On this
    // width an unguarded restore lands on the composer bar, taking the cursor
    // out of the draft mid-sentence, which is what round 2 of the review of
    // #1413 was about.
    await expect(page.locator(CHIP)).toBeEnabled()
    await expect.poll(() => page.evaluate(() => document.activeElement?.className ?? null))
      .toContain('chat-input')
    await expect(page.locator('.chat-input')).toHaveValue('next message')
  })

  test(`a call that ends while the move is out does not steal ${DRAFT_FOCUS.what}`, async ({ page }) => {
    await expectMoveLeavesFocusAlone(page, DRAFT_FOCUS)
  })
})

test.describe('move a running call to the background (#1413), in a narrow window', () => {
  // A composer bar under 560px with room to spare: the compact activity cluster
  // is the bar's only spacer here, so this is where Send drifted left.
  test.use({ viewport: { width: 560, height: 900 } })

  test('the chip and Stop stay against the bar\'s right edge', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    const bar = await page.locator('.composer-bar').boundingBox()
    expect(bar!.width).toBeLessThan(560)
    await expectTapTargetBesideStop(page)
    await page.screenshot({ path: 'test-results/move-bg-narrow.png' })
  })
})

test.describe('move a running call to the background (#1413), on a desktop width', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('tapping it hands focus to the textarea, not the composer bar', async ({ page }) => {
    await boot(page, CHAT_PATH, COMPOSER)
    await speak(page, [toolUse(MOVE_ID, 'sleep 300 && echo done', true)])
    await expect(page.locator(CHIP)).toBeVisible({ timeout: 8_000 })

    const posted = await tapChipAndEndTurn(page)

    // Above COMPOSER_FOCUS_MIN_WIDTH the keyboard is not a concern, so the next
    // message can be typed straight away — the other half of the focus rule.
    await expect.poll(() => page.evaluate(() => document.activeElement?.className ?? null))
      .toContain('chat-input')

    await expectNoFurtherMove(page, posted)
  })
})
