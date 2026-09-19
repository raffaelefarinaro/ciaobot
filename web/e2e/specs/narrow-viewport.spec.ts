import { expect, test } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * jsdom has no layout engine: every `getBoundingClientRect()` is 0x0 and
 * `scrollWidth` is always 0, so none of the traps web/README.md documents — a
 * flex child with an unbreakable string widening its parent past the viewport,
 * a tap target that renders smaller than `--touch: 44px` — can be detected by
 * mounting a component. The fixture deliberately ships a project whose name is
 * one long unbreakable token for exactly this reason.
 */
const PHONE = { width: 390, height: 844 }

test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

test.describe('narrow viewport', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `narrow-${testInfo.workerIndex}`)
  })

  test('the home view does not scroll sideways at 390px', async ({ page }) => {
    await boot(page)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('an open chat does not scroll sideways at 390px', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('icon-only controls still hit the 44px touch minimum', async ({ page }) => {
    // The open chat is where the icon-only controls live (hamburger, close,
    // model picker, archive); the home view has one.
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    // `.btn-icon` and `.touch-hit` are the two utilities App.vue declares as
    // enforcing `--touch: 44px`. Measuring them in a real browser is the only
    // way to know the rule survived a component-level override.
    const boxes = await page.locator('.btn-icon:visible, .touch-hit:visible').evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect()
        return { cls: String(el.className).slice(0, 60), w: Math.round(r.width), h: Math.round(r.height) }
      }),
    )
    // Without this the test would keep passing if a refactor renamed the
    // utilities and the selector started matching nothing at all.
    expect(boxes.length, 'no touch targets were measured').toBeGreaterThanOrEqual(4)

    const small = boxes.filter((box) => box.w < 44 || box.h < 44)
    expect(small, `controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })
})
