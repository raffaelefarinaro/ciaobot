import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate, settled, TOUCH_FLOOR } from '../support/app'

/**
 * A chat's own loop shows as a chip in the composer bar, beside the model. The
 * fixture has no loops of its own, so the spec turns the routine
 * `alpha-daily-brief` into a loop of `alpha-chat-1` through the inbox seed: it
 * is then the chat's only loop, and the chip is the one place it shows there.
 */

const CHAT = 'alpha-chat-1'
const LOOP = 'alpha-daily-brief'
const CHIP = '.composer-bar button.chat-loop-chip'
const BAR = '.composer-bar'

async function seedLoop(request: APIRequestContext, session: string) {
  const expires = new Date(Date.now() + 6 * 60 * 60 * 1000).toISOString()
  await request.post('/__fixture__/inbox', {
    headers: { cookie: `e2e_session=${session}` },
    data: {
      schedules: {
        [LOOP]: {
          title: 'Watch the build',
          frequency: 'interval',
          interval_minutes: 5,
          daily_time_utc: '',
          web_project_id: null,
          web_chat_id: CHAT,
          owner_chat_id: CHAT,
          expires_at: expires,
          max_runs: 10,
          run_count: 3,
          context_label: 'Build chat',
        },
      },
    },
  })
}

async function deletesFor(request: APIRequestContext, session: string, path: string): Promise<number> {
  const res = await request.get('/__fixture__/writes', { headers: { cookie: `e2e_session=${session}` } })
  const { writes } = (await res.json()) as { writes: Array<{ method: string, path: string }> }
  return writes.filter(w => w.method === 'DELETE' && w.path === path).length
}

/** The chip's box sits inside the composer bar: nothing wraps out or overflows. */
async function expectInsideBar(page: Page) {
  const chip = (await page.locator(CHIP).boundingBox())!
  const bar = (await page.locator(BAR).boundingBox())!
  expect(chip.x).toBeGreaterThanOrEqual(bar.x - 1)
  expect(chip.x + chip.width).toBeLessThanOrEqual(bar.x + bar.width + 1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
}

test.describe('chat loop chip, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('shows the short form inside the composer bar, with a 44px touch target', async ({ page, request }, testInfo) => {
    const session = `chat-loop-phone-${testInfo.workerIndex}`
    await isolate(page, session)
    await seedLoop(request, session)
    await boot(page, `/chat/${CHAT}`, COMPOSER)
    const chip = page.locator(CHIP)
    await expect(chip).toBeVisible()
    await settled(chip)
    await expect(chip).toContainText('Loop · 5m')
    await expect(chip).toHaveAttribute('aria-label', /every 5 min · ends .* · 7 runs left/)
    await expectInsideBar(page)
    const box = (await chip.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR)
  })

  test('Stop removes the chip and deletes the loop exactly once', async ({ page, request }, testInfo) => {
    const session = `chat-loop-stop-phone-${testInfo.workerIndex}`
    await isolate(page, session)
    await seedLoop(request, session)
    await boot(page, `/chat/${CHAT}`, COMPOSER)
    await page.locator(CHIP).tap()
    await page.getByRole('menuitem', { name: 'Stop loop' }).tap()
    await expect(page.locator(CHIP)).toHaveCount(0)
    expect(await deletesFor(request, session, `/api/schedules/${LOOP}`)).toBe(1)
  })
})

test.describe('chat loop chip, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test('names the loop in full, and Open in Automations goes to its page', async ({ page, request }, testInfo) => {
    const session = `chat-loop-desktop-${testInfo.workerIndex}`
    await isolate(page, session)
    await seedLoop(request, session)
    await boot(page, `/chat/${CHAT}`, COMPOSER)
    const chip = page.locator(CHIP)
    await expect(chip).toContainText('Loop · every 5 min · ends')
    await expectInsideBar(page)
    await chip.click()
    await page.getByRole('menuitem', { name: 'Open in Automations' }).click()
    await expect(page).toHaveURL(new RegExp(`/schedules/${LOOP}$`))
  })

  test('a 200% zoom (a 640px CSS viewport) keeps the chip reachable inside the bar', async ({ page, request }, testInfo) => {
    const session = `chat-loop-zoom-${testInfo.workerIndex}`
    await isolate(page, session)
    await seedLoop(request, session)
    await page.setViewportSize({ width: 640, height: 800 })
    await boot(page, `/chat/${CHAT}`, COMPOSER)
    const chip = page.locator(CHIP)
    await expect(chip).toBeVisible()
    await expect(chip).toContainText('Loop · 5m')
    await chip.focus()
    await expect(chip).toBeFocused()
    await expectInsideBar(page)
  })

  test('Stop from the menu removes the chip and deletes the loop once', async ({ page, request }, testInfo) => {
    const session = `chat-loop-stop-desktop-${testInfo.workerIndex}`
    await isolate(page, session)
    await seedLoop(request, session)
    await boot(page, `/chat/${CHAT}`, COMPOSER)
    await page.locator(CHIP).click()
    await page.getByRole('menuitem', { name: 'Stop loop' }).click()
    await expect(page.locator(CHIP)).toHaveCount(0)
    expect(await deletesFor(request, session, `/api/schedules/${LOOP}`)).toBe(1)
  })
})
