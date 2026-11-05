import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * The needs-you card, in a real browser.
 *
 * An OpenCode MCP server that needs a sign-in puts a card above the composer.
 * The user signs in in a new tab, the card shows the wait and a working link,
 * and Not now takes the card away. jsdom cannot answer the layout, the pop-up
 * or the touch targets, so this is a browser spec.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const TOUCH_MIN = 44

/** Put an `mcp_auth` action on the chat, before the app boots. */
async function raiseSignIn(page: Page): Promise<void> {
  const res = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: CHAT_ID, kind: 'mcp_auth', params: { server: 'linear', provider: 'opencode' } },
  })
  expect(res.ok(), 'the fixture refused the action').toBe(true)
}

/** Record the pop-ups the page opens, instead of opening them. */
async function recordPopups(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const opened: unknown[][] = []
    ;(window as Window & { __opened?: unknown[][] }).__opened = opened
    window.open = ((...args: unknown[]) => {
      opened.push(args)
      return null
    }) as typeof window.open
  })
}

async function popups(page: Page): Promise<unknown[][]> {
  return page.evaluate(() => (window as Window & { __opened?: unknown[][] }).__opened ?? [])
}

test.describe('the needs-you card', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('asks for a sign-in, waits for it, and goes when dismissed', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-${testInfo.workerIndex}`)
    await recordPopups(page)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.needs-you-card')
    await expect(card).toBeVisible()
    await expect(card.locator('.needs-you-card-title')).toHaveText('Sign in to linear')
    // The card sits above the composer, where the user is about to type.
    const cardBox = await card.boundingBox()
    const composerBox = await page.locator(COMPOSER).boundingBox()
    expect(cardBox!.y + cardBox!.height).toBeLessThanOrEqual(composerBox!.y + 1)

    await card.getByRole('button', { name: 'Sign in', exact: true }).click()

    // The page opened in a new tab, with no opener, at the address the engine sent.
    await expect.poll(() => popups(page)).toHaveLength(1)
    const [call] = await popups(page)
    const [url, target, features] = call as string[]
    expect(url).toMatch(/^https:\/\/auth\.example\.invalid\/authorize/)
    expect(target).toBe('_blank')
    expect(features).toBe('noopener,noreferrer')

    // The wait is shown, with a link the user can use again.
    await expect(card.locator('.needs-you-card-title')).toHaveText('Waiting for linear to connect…')
    await expect(card.getByRole('link', { name: 'Open the sign-in page again' })).toHaveAttribute('target', '_blank')

    await card.getByRole('button', { name: 'Not now', exact: true }).click()
    await expect(page.locator('.needs-you-card')).toHaveCount(0)
  })
})

test.describe('the needs-you card on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('offers 44px controls and does not scroll sideways', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-phone-${testInfo.workerIndex}`)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    // A coarse pointer, so the touch rules are the ones under test.
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)

    const card = page.locator('.needs-you-card')
    await expect(card).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `the phone layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    const controls = card.locator('button')
    const count = await controls.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }
  })
})
