import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * The needs-you card, in a real browser.
 *
 * A Google sign-in that expired shows a card above the composer.
 * jsdom cannot answer the layout, the pop-up or touch targets.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const TOUCH_MIN = 44

/** Put a `gws_auth` action on the chat, before the app boots. */
async function raiseSignIn(page: Page, state?: 'waiting'): Promise<void> {
  const params: Record<string, string> = { profile: 'alpha' }
  if (state === 'waiting') params.url = 'https://accounts.google.com/o/oauth2/auth?state=waiting'
  const res = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: CHAT_ID, kind: 'gws_auth', state, params },
  })
  expect(res.ok(), 'the fixture refused the action').toBe(true)
}

const STACK_CARD = '.needs-you-stack .needs-you-card'

interface PopupRecord { args: unknown[]; closed: boolean }
type PopupTab = { closed: boolean; opener: unknown; location: { href: string }; close(): void }

/** Record the pop-ups the page opens, instead of opening them. The stub hands back a fake tab. */
async function recordPopups(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const opened: { args: unknown[]; tab: PopupTab }[] = []
    ;(window as Window & { __opened?: typeof opened }).__opened = opened
    window.open = ((...args: unknown[]) => {
      const tab = { closed: false, opener: window as unknown, location: { href: '' }, close() { this.closed = true } }
      opened.push({ args, tab })
      return tab
    }) as unknown as typeof window.open
  })
}

async function popups(page: Page): Promise<PopupRecord[]> {
  return page.evaluate(() => {
    const opened = (window as Window & { __opened?: { args: unknown[]; tab: PopupTab }[] }).__opened ?? []
    return opened.map(({ args, tab }) => ({ args, closed: tab.closed }))
  })
}

test.describe('the Google sign-in card', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('asks for a sign-in, finishes it, and goes when dismissed', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-${testInfo.workerIndex}`)
    await recordPopups(page)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator(STACK_CARD)
    await expect(card).toBeVisible()
    await expect(card.locator('.needs-you-card-title')).toHaveText('Sign in to Google again')
    const composer = (await page.locator(COMPOSER).boundingBox())!
    const box = (await card.boundingBox())!
    expect(box.y + box.height).toBeLessThanOrEqual(composer.y + 0.5)

    await card.getByRole('button', { name: 'Sign in with Google', exact: true }).click()

    // The tab opened in the click is closed again: the fixture sent no Google address.
    await expect.poll(() => popups(page)).toEqual([{ args: ['', '_blank'], closed: true }])
    await expect(card.locator('.needs-you-card-title')).toHaveText('Signed in to Google')

    await card.getByRole('button', { name: 'Done', exact: true }).click()
    await expect(page.locator(STACK_CARD)).toHaveCount(0)
  })
})

test.describe('the Google sign-in card on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('offers 44px controls and does not scroll sideways', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-phone-${testInfo.workerIndex}`)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    // A coarse pointer, so the touch rules are the ones under test.
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)

    const card = page.locator(STACK_CARD)
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

  test('keeps the waiting link 44px', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-phone-waiting-${testInfo.workerIndex}`)
    await raiseSignIn(page, 'waiting')
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator(STACK_CARD)
    await expect(card).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `the phone layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    const link = card.locator('a.needs-you-btn')
    const linkBox = (await link.boundingBox())!
    expect(linkBox.height, `the sign-in link is ${linkBox.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    expect(linkBox.width, `the sign-in link is ${linkBox.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)

    const controls = card.locator('button')
    const count = await controls.count()
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }
  })
})
