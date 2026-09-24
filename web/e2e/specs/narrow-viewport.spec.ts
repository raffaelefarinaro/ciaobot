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

    const form = await page.locator('.home-intake-form').boundingBox()
    expect(form).not.toBeNull()
    const controls = page.locator('.home-intake-form input, .home-intake-form textarea, .home-intake-form select, .home-intake-form button')
    for (const control of await controls.all()) {
      const box = await control.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.x).toBeGreaterThanOrEqual(form!.x - 1)
      expect(box!.x + box!.width).toBeLessThanOrEqual(form!.x + form!.width + 1)
    }
  })

  test('an open chat does not scroll sideways at 390px', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('visible controls hit the 44px touch minimum', async ({ page }) => {
    async function assertTouchTargets() {
      const boxes = await page.locator([
        'button:visible',
        'a:visible',
        'input:visible',
        'select:visible',
        'textarea:visible',
        '[role="button"]:visible',
        '[role="link"]:visible',
      ].join(', ')).evaluateAll((els) =>
        els.map((el) => {
          const r = el.getBoundingClientRect()
          return {
            name: (el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 70),
            cls: String(el.className).slice(0, 60),
            w: Math.round(r.width),
            h: Math.round(r.height),
          }
        }),
      )
      expect(boxes.length, 'no touch targets were measured').toBeGreaterThanOrEqual(4)
      const small = boxes.filter((box) => box.w < 44 || box.h < 44)
      expect(small, `controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
    }

    await boot(page)
    await assertTouchTargets()

    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await assertTouchTargets()
  })

  test('project actions stay visible on a wide touch viewport', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 })
    await boot(page, '/project/alpha-notes', 'text=Project context')
    const action = page.locator('.project-header .project-actions-btn').first()
    await expect(action).toBeVisible()
    expect(await action.evaluate((element) => getComputedStyle(element).opacity)).toBe('1')
  })

  test('a selected memory note opens an actionable sheet on a phone', async ({ page }) => {
    await boot(page, '/memory', 'text=Review what Ciao learned')
    await page.getByRole('button', { name: 'Map', exact: true }).click()
    await page.getByRole('button', { name: 'List', exact: true }).click()
    await page.getByRole('button', { name: 'Open Launch decision' }).click()

    const detail = page.getByRole('dialog', { name: 'Details for Launch decision' })
    await expect(detail).toBeVisible()
    await expect(detail.getByRole('button', { name: 'Open Launch decision' })).toBeVisible()
    await detail.getByRole('button', { name: 'Close note detail' }).click()
    await expect(detail).toBeHidden()
  })
})
