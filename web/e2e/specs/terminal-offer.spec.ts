import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * The suggested-command card, in a real browser.
 *
 * The command is the assistant's, not the engine's, and the user runs it or
 * does not: the card shows it, copies it, opens the terminal beside it and
 * dismisses it, and nothing in this journey runs anything. jsdom cannot
 * answer the half of it that matters here — a real layout at a phone width,
 * a real clipboard, a real 44px touch target — so it is a browser spec.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const COMMAND = 'ciao service start --workspace /Users/me/Ciao'
const TITLE = 'Suggested command — review before running'
const TOUCH_MIN = 44

/**
 * A command that carries markup in it: a bold tag, an image with an event
 * handler, a markdown link and a bare URL. The card shows characters, so
 * none of these may become an element or a link.
 */
const LITERAL_COMMAND = [
  'ciao service start --workspace /Users/me/Ciao',
  '<b>bold</b> <img src=x onerror="alert(1)">',
  '[link](https://example.invalid/run-now) https://example.invalid/run-now',
].join('\n')

/** A command with no short breaks in it: it has to wrap, not scroll sideways. */
const LONG_COMMAND = 'npm install --global @company/internal-cli@latest --registry https://registry.example.invalid/npm --prefix /Users/somebody/Applications/Utilities --legacy-peer-deps --ignore-scripts'

/** Offer a suggested command to one chat, before the app boots. */
async function offerCommand(page: Page, chatId: string, command: string): Promise<void> {
  const res = await page.request.post('/__fixture__/terminal-offer', {
    data: { chat_id: chatId, command },
  })
  expect(res.ok(), 'the fixture refused the offer').toBe(true)
  const body = await res.json() as { offer: { id: string, command: string } }
  // A fresh random id every time, the way the engine mints one.
  expect(body.offer.id).toMatch(/^[0-9a-f]{32}$/)
  expect(body.offer.command).toBe(command)
}

/** How many shells this browser context has started. */
async function terminalSessions(page: Page): Promise<number> {
  return page.evaluate(async () => {
    const res = await fetch('/__fixture__/terminal')
    const body = await res.json() as { sessions: unknown[] }
    return body.sessions.length
  })
}

/** The exact text of an element, without Playwright's whitespace normalising. */
async function exactText(page: Page, selector: string): Promise<string> {
  return page.locator(selector).evaluate(el => el.textContent ?? '')
}

/** Focus the last control before the card, so the next Tab lands on the card. */
async function focusBeforeCard(page: Page): Promise<void> {
  await page.evaluate(() => {
    const card = document.querySelector('.terminal-offer')
    if (!card) throw new Error('the card is not on the page')
    const focusable = Array.from(document.querySelectorAll(
      'a[href], button:not([disabled]), input:not([type=hidden]), textarea, select, [tabindex]:not([tabindex="-1"])',
    )).filter((el): el is HTMLElement => el instanceof HTMLElement && el.offsetParent !== null)
    const before = focusable.filter(el => Boolean(
      card.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING,
    ))
    const last = before[before.length - 1]
    if (!last) throw new Error('nothing focusable before the card')
    last.focus()
  })
}

test.describe('the suggested command', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('is shown verbatim and copied exactly', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-copy-${testInfo.workerIndex}`)
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
    await offerCommand(page, CHAT_ID, LITERAL_COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.terminal-offer')
    await expect(card).toBeVisible()
    // The warning is fixed: the command is unverified, and the card says so.
    await expect(card.locator('.terminal-offer-title')).toHaveText(TITLE)
    // The command is the command, to the character.
    expect(await exactText(page, '.terminal-offer-command code')).toBe(LITERAL_COMMAND)
    // Nothing in it became markup: no element, no link.
    await expect(card.locator('.terminal-offer-command a')).toHaveCount(0)
    await expect(card.locator('.terminal-offer-command img')).toHaveCount(0)
    await expect(card.locator('.terminal-offer-command b')).toHaveCount(0)
    await expect(card.locator('a')).toHaveCount(0)
    await expect(card.locator('img')).toHaveCount(0)

    await card.getByRole('button', { name: 'Copy', exact: true }).click()
    await expect(card.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
    const clipboard = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboard).toBe(LITERAL_COMMAND)

    // Looking at a suggestion starts nothing.
    expect(await terminalSessions(page)).toBe(0)
    await expect(page.locator('.terminal-pane')).toHaveCount(0)
  })

  test('is reachable and operable from the keyboard', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-keyboard-${testInfo.workerIndex}`)
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
    await offerCommand(page, CHAT_ID, COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.terminal-offer')
    await expect(card).toBeVisible()
    const copy = card.getByRole('button', { name: 'Copy', exact: true })
    const open = card.getByRole('button', { name: 'Open in Terminal' })
    const dismiss = card.getByRole('button', { name: 'Dismiss' })

    // The keyboard reaches the three controls in reading order. The command
    // itself is a stop before them: a scrollable region has to be
    // reachable, or a long command could not be read with the keyboard.
    await focusBeforeCard(page)
    let reached = false
    for (let i = 0; i < 4 && !reached; i += 1) {
      await page.keyboard.press('Tab')
      reached = await copy.evaluate((el) => el === document.activeElement)
    }
    expect(reached, 'the copy control is not reached by Tab').toBe(true)
    const ring = await copy.evaluate((el) => {
      const style = getComputedStyle(el)
      return {
        matches: el.matches(':focus-visible'),
        outlineStyle: style.outlineStyle,
        outlineWidth: style.outlineWidth,
        boxShadow: style.boxShadow,
      }
    })
    expect(ring.matches, 'the focused control is not focus-visible').toBe(true)
    expect(ring.outlineStyle !== 'none' || ring.boxShadow !== 'none', JSON.stringify(ring)).toBe(true)

    await page.keyboard.press('Tab')
    await expect(open).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dismiss).toBeFocused()

    // Back to Copy, and the keyboard alone copies the command.
    await page.keyboard.press('Shift+Tab')
    await page.keyboard.press('Shift+Tab')
    await expect(copy).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(card.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
    const clipboard = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboard).toBe(COMMAND)

    // And the keyboard alone dismisses it. The card unmounts, so the focus
    // must not fall to the page: it goes to the composer, where the next
    // word goes.
    await page.keyboard.press('Tab')
    await expect(open).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dismiss).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.locator('.terminal-offer')).toHaveCount(0)
    await expect(page.locator(COMPOSER)).toBeFocused()
  })

  test('opens the terminal for its own chat and starts no shell', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-open-${testInfo.workerIndex}`)
    await offerCommand(page, CHAT_ID, COMMAND)
    const terminalCalls: string[] = []
    page.on('request', request => {
      const url = new URL(request.url())
      if (url.pathname.startsWith('/api/terminal/')) {
        terminalCalls.push(`${request.method()} ${url.pathname}${url.search}`)
      }
    })
    await boot(page, CHAT_PATH, COMPOSER)

    await page.locator('.terminal-offer').getByRole('button', { name: 'Open in Terminal' }).click()
    const pane = page.locator('.terminal-pane')
    await expect(pane).toBeVisible()

    // The terminal is scoped to the chat that made the suggestion.
    await expect.poll(() => terminalCalls.join(' ')).toContain(`chat_id=${CHAT_ID}`)
    // Opening the panel starts no shell. It does fetch the browser's own
    // terminal grant — the pane asks the engine for it, because the
    // signed-in session is the terminal's trust boundary — and that is the
    // only POST it makes.
    expect(terminalCalls.filter(call => call.startsWith('POST'))).toEqual([
      'POST /api/terminal/unlock',
    ])
    expect(await terminalSessions(page)).toBe(0)
    await expect(pane.locator('.tp-session')).toHaveCount(0)
  })

  test('is dismissed by the user', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-dismiss-${testInfo.workerIndex}`)
    await offerCommand(page, CHAT_ID, COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.terminal-offer')
    await expect(card).toBeVisible()
    const dismissal = page.waitForRequest(request => (
      request.method() === 'PATCH' && request.url().includes(`/api/chats/${CHAT_ID}`)
    ))
    await card.getByRole('button', { name: 'Dismiss' }).click()
    const request = await dismissal

    // The body is exactly the offer it means, and nothing else: a
    // dismissal never rides along with a pin, a title or any other write.
    const body = request.postDataJSON() as { terminal_offer: { expected_id: string } }
    expect(Object.keys(body)).toEqual(['terminal_offer'])
    expect(Object.keys(body.terminal_offer)).toEqual(['expected_id'])
    expect(body.terminal_offer.expected_id).toMatch(/^[0-9a-f]{32}$/)

    await expect(page.locator('.terminal-offer')).toHaveCount(0)
  })

  test('a stale dismissal shows the newer command and says so', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-conflict-${testInfo.workerIndex}`)
    // The client must not hear about the replacement before the dismissal:
    // the engine announces an accepted change over the socket, and this
    // fixture's replacement is silent. The 15-second list poll would carry
    // it, so the polls are held back — a failed poll is a failed refresh,
    // and the store carries on with what it knows.
    let listReads = 0
    await page.route(/\/api\/chats\?active_only=1/, route => {
      listReads += 1
      if (listReads === 1) return route.fallback()
      return route.abort()
    })
    await offerCommand(page, CHAT_ID, COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.terminal-offer')
    await expect(card).toBeVisible()

    // A newer suggestion arrives silently: the card still shows the old one.
    const newer = 'ciao service start --workspace /Users/me/Other'
    await offerCommand(page, CHAT_ID, newer)
    expect(await exactText(page, '.terminal-offer-command code')).toBe(COMMAND)

    // The dismissal names the command that was on screen, so the engine
    // answers with a conflict and its own current offer.
    const conflict = page.waitForResponse(res => (
      res.request().method() === 'PATCH'
      && res.url().includes(`/api/chats/${CHAT_ID}`)
      && res.status() === 409
    ))
    await card.getByRole('button', { name: 'Dismiss' }).click()
    const answer = await conflict
    expect(await answer.json()).toMatchObject({ error: 'terminal_offer_conflict' })

    const notice = card.locator('.terminal-offer-notice')
    await expect(notice).toBeVisible()
    await expect(notice).toContainText('the suggestion changed — the newer one is shown')
    // The newer command is the one on screen now.
    expect(await exactText(page, '.terminal-offer-command code')).toBe(newer)
    // And nothing was run.
    expect(await terminalSessions(page)).toBe(0)
  })

  test('a long command wraps at 200% zoom without pushing the page sideways', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-zoom-${testInfo.workerIndex}`)
    await offerCommand(page, CHAT_ID, LONG_COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    await expect(page.locator('.terminal-offer')).toBeVisible()
    // Chromium's page zoom is not scriptable, but it is indistinguishable
    // from halving the CSS viewport: at 200% a 1440px window lays out as
    // 720 CSS px. Re-measuring after the resize is what makes this a zoom
    // test rather than a second narrow-viewport test.
    await page.setViewportSize({ width: 720, height: 450 })
    await expect(page.locator('.terminal-offer')).toBeVisible()

    const box = await page.locator('.terminal-offer-command').evaluate(el => ({
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
      height: el.getBoundingClientRect().height,
    }))
    // The command wraps inside the box instead of scrolling sideways.
    expect(box.scrollWidth, 'the command scrolls sideways inside its box').toBeLessThanOrEqual(box.clientWidth + 1)
    // And the box stays bounded, so a long command cannot take the chat.
    expect(box.height).toBeLessThanOrEqual(260)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `zoomed layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 375, height: 667 }, hasTouch: true, isMobile: true })

  test('shows the command, wraps it, and offers 44px controls', async ({ page }, testInfo) => {
    await isolate(page, `terminal-offer-phone-${testInfo.workerIndex}`)
    await offerCommand(page, CHAT_ID, LONG_COMMAND)
    await boot(page, CHAT_PATH, COMPOSER)

    // A coarse pointer, so the touch rules are the ones under test.
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)

    const card = page.locator('.terminal-offer')
    await expect(card).toBeVisible()
    expect(await exactText(page, '.terminal-offer-command code')).toBe(LONG_COMMAND)

    // The command wraps: the box is no wider than its column, and the
    // document does not scroll sideways.
    const box = await page.locator('.terminal-offer-command').evaluate(el => ({
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
      height: el.getBoundingClientRect().height,
    }))
    expect(box.scrollWidth, 'the command scrolls sideways inside its box').toBeLessThanOrEqual(box.clientWidth + 1)
    expect(box.height).toBeLessThanOrEqual(260)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `the phone layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    // Every control in the card is a 44px target.
    const controls = card.locator('button')
    const count = await controls.count()
    expect(count).toBe(3)
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }

    // Nothing ran, and nothing was copied: the card is a review surface.
    expect(await terminalSessions(page)).toBe(0)
    await expect(page.locator('.terminal-pane')).toHaveCount(0)
  })
})
