import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The Automations card for a Workspace care run (#3648): every state the engine
 * can answer for one exact dispatch, in a real engine at phone width and at 200%
 * zoom. The fixture answers by dispatch id (see CARE_RUNS in the fixture server).
 */

const CARE_ID = 'system-memory-curation@alpha'
const CARD = '.care-run'
const TOUCH_MIN = 44

async function openCare(page: Page, request: import('@playwright/test').APIRequestContext, session: string, dispatchId: string) {
  await isolate(page, session)
  // The fixture's first routine becomes the care routine for this session only.
  await request.post('/__fixture__/inbox', {
    headers: { cookie: `e2e_session=${session}` },
    data: {
      schedules: {
        'alpha-daily-brief': {
          schedule_id: CARE_ID,
          title: 'Workspace care',
          last_run_chat_id: 'chat-care-1',
          last_dispatch_id: dispatchId,
          last_dispatched_at: '2026-09-24T03:00:00Z',
          workspace: 'alpha',
        },
      },
    },
  })
  await boot(page, `/schedules/${CARE_ID}`, CARD)
}

test.describe('care run card', () => {
  test('ok run shows counts, grouped reasons, weekly state and exact links', async ({ page, request }, testInfo) => {
    await openCare(page, request, `care-ok-${testInfo.workerIndex}`, 'd-ok')
    await expect(page.locator(`${CARD} .care-run-counts`)).toHaveText('Planned 6 · completed 2 · deferred 1 · failed 1')
    await expect(page.locator(`${CARD} .care-run-group-title`)).toHaveText([
      'Failed (1)', 'Blocked (1)', 'Deferred by budget (1)', 'Queued for review (1)',
    ])
    await expect(page.locator(`${CARD} .care-run-weekly`)).toContainText('did not advance')
    const review = page.locator(`${CARD} a:has-text("Open in Memory Review")`)
    await expect(review).toHaveCount(1)
    await expect(review).toHaveAttribute('href', /^\/memory\/review\?show=suggested&workspace=alpha&proposal=prv_c{64}$/)
    await expect(page.locator(`${CARD} a:has-text("Open the run chat")`)).toHaveAttribute('href', '/chat/chat-care-1')
    await page.locator(CARD).screenshot({ path: testInfo.outputPath('care-ok-desktop.png') })
  })

  test('no-op and legacy runs say what they are', async ({ page, request }, testInfo) => {
    await openCare(page, request, `care-noop-${testInfo.workerIndex}`, 'd-noop')
    await expect(page.locator(`${CARD} .care-run-noop`)).toHaveText('No change needed.')
    await openCare(page, request, `care-legacy-${testInfo.workerIndex}`, 'd-legacy')
    await expect(page.locator(CARD)).toContainText('Per-item detail was not recorded')
  })

  for (const [dispatch, words] of [
    ['d-pending', 'still running, or its outcome is not recorded yet'],
    ['d-superseded', 'A newer run replaced this one'],
    ['d-ambiguous', 'records for this run disagree'],
    ['d-unavailable', 'cannot be read right now'],
  ] as const) {
    test(`${dispatch} is stated in words`, async ({ page, request }, testInfo) => {
      await openCare(page, request, `care-${dispatch}-${testInfo.workerIndex}`, dispatch)
      await expect(page.locator(CARD)).toContainText(words)
      await expect(page.locator(`${CARD} .care-run-counts`)).toHaveCount(0)
      await page.locator(CARD).screenshot({ path: testInfo.outputPath(`care-${dispatch}.png`) })
    })
  }

  test('a failed read shows an alert and Retry reaches the report', async ({ page, request }, testInfo) => {
    const session = `care-retry-${testInfo.workerIndex}`
    let fail = true
    await page.route('**/care-run?*', async (route) => {
      if (fail) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
      return route.continue()
    })
    await openCare(page, request, session, 'd-ok')
    await expect(page.locator(`${CARD} [role="alert"]`)).toContainText('Could not load the outcome')
    fail = false
    await page.locator(`${CARD} .care-run-retry`).click()
    await expect(page.locator(`${CARD} .care-run-counts`)).toBeVisible()
  })

  test('keyboard reaches the links with a visible focus ring', async ({ page, request }, testInfo) => {
    await openCare(page, request, `care-kbd-${testInfo.workerIndex}`, 'd-ok')
    const link = page.locator(`${CARD} a:has-text("Open in Memory Review")`)
    await link.focus()
    await expect(link).toBeFocused()
    expect(await link.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe('none')
  })

  test.describe('phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

    test('targets are 44px and nothing scrolls sideways, also at 200% zoom', async ({ page, request }, testInfo) => {
      await openCare(page, request, `care-phone-${testInfo.workerIndex}`, 'd-ok')
      for (const sel of [`${CARD} a`]) {
        for (const box of await page.locator(sel).evaluateAll(els => els.map(e => e.getBoundingClientRect().height))) {
          expect(box).toBeGreaterThanOrEqual(TOUCH_MIN)
        }
      }
      expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)
      await page.locator(CARD).screenshot({ path: testInfo.outputPath('care-ok-phone.png') })
      // 200% page zoom is indistinguishable from halving the CSS viewport.
      await page.setViewportSize({ width: 195, height: 422 })
      expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)
      await expect(page.locator(CARD)).toBeVisible()
      expect(await page.locator(CARD).evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
      await page.locator(CARD).screenshot({ path: testInfo.outputPath('care-ok-zoom200.png') })
    })
  })
})
