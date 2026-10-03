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

    // The workbench command canvas must keep its controls inside the surface at
    // phone width: the project chip and the New action are the two that can
    // push past the shell.
    const form = await page.locator('.home-intake-form').boundingBox()
    expect(form).not.toBeNull()
    // The file input is hidden (the paperclip button opens it), so it has no box.
    const controls = page.locator('.home-intake-form input:not([type=file]), .home-intake-form textarea, .home-intake-form select, .home-intake-form button')
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

  test('selecting a reply does not move the transcript at 390px', async ({ page }) => {
    // Selecting a message grows it: the action footer is not in the layout a
    // moment earlier. The reader's place is kept — selection must not scroll
    // the transcript to reveal the footer. jsdom cannot see this (every rect
    // there is 0x0), which is what makes it a browser test.
    // The transcript is opt-in and per-session (see the fixture), so the chat
    // is reloaded once the fixture is holding the turns this test selects.
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', { method: 'POST' }))
    await page.reload()
    await page.waitForSelector('.message-wrap.assistant .message-row')
    await page.waitForSelector(COMPOSER)

    // Click via a dispatched event: Playwright's own click scrolls the target
    // into view first, which would move the transcript before selection even
    // runs and make the assertion meaningless.
    const scrollBefore = await page.evaluate(() => {
      const root = document.querySelector('.messages')!
      root.scrollTop = root.scrollHeight
      return root.scrollTop
    })
    await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.message-wrap.assistant .message-row')]
      rows[rows.length - 1].dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await page.waitForSelector('.message-wrap--selected .message-actions')

    const measured = await page.evaluate(() => {
      const root = document.querySelector('.messages')!
      const controls = [...document.querySelectorAll('.message-wrap--selected .message-action-btn')]
      return {
        scrollAfter: root.scrollTop,
        controls: controls.length,
        heights: controls.map((el) => Math.round(el.getBoundingClientRect().height)),
      }
    })
    expect(measured.scrollAfter, 'selecting moved the transcript').toBe(scrollBefore)
    expect(measured.controls, 'no action controls rendered').toBeGreaterThan(0)
    for (const h of measured.heights) expect(h).toBeGreaterThanOrEqual(44)
  })

  test('a selected reply does not overlap the comment card below it at 390px', async ({ page }) => {
    // The sent comment-reference card sits in the turn right under the reply it
    // annotates. Selecting that reply grows it by its action footer, and the
    // selected card's accent outline used to bleed past the transcript's turn
    // gap and draw over the reference card. Only a real layout engine can show
    // the collision (#968).
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'comment' }),
    }))
    await page.reload()
    await page.waitForSelector('.message-wrap.assistant .message-row')
    await page.waitForSelector('user-comment-reference')
    await page.waitForSelector(COMPOSER)

    await page.locator('.message-wrap.assistant .message-row').click()
    await page.waitForSelector('.message-wrap--selected .message-actions')

    const measured = await page.evaluate(() => {
      const row = document.querySelector('.message-wrap--selected .message-row')!
      // The collision is with the next TURN's box (the `.message-wrap` that
      // carries the reference card), not the inner `user-comment-reference`
      // quote card, which sits inside that turn's padding. Measuring the inner
      // card gives false confidence: the outline crosses the turn boundary
      // before it ever reaches the quote.
      const selectedWrap = document.querySelector('.message-wrap--selected')!
      const next = selectedWrap.nextElementSibling as HTMLElement
      // The 1px outline sits on the row's border box.
      return { outlineBottom: row.getBoundingClientRect().bottom + 1, nextTop: next.getBoundingClientRect().top }
    })
    expect(measured.outlineBottom - measured.nextTop, 'the selected outline crosses the next turn').toBeLessThanOrEqual(0)
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

  test('the home composer controls meet the 44px touch minimum at phone width', async ({ page }) => {
    await boot(page)

    const boxes = await page.locator('.home-intake-form button:visible, .home-intake-form textarea:visible').evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect()
        return {
          name: (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
          w: Math.round(r.width),
          h: Math.round(r.height),
        }
      }),
    )
    expect(boxes.length, 'no home composer controls were measured').toBeGreaterThanOrEqual(2)
    const small = boxes.filter((box) => box.h < 44)
    expect(small, `home composer controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })

  test('a memory-insight row fits a phone and opens the pass it stands for', async ({ page }) => {
    // A pass is a real chat, so the surface Home shows for it is the one place
    // the row's own text has to survive a phone: a long conversation title, a
    // status word, and (on one row) a pending question. jsdom has no layout
    // engine, so only a real browser can say the row neither overflows nor
    // drops below the touch minimum.
    await boot(page, undefined, '.home-insights')

    const rows = page.locator('.home-insight-row')
    await expect(rows).toHaveCount(2)
    // Newest conversation first, and named for the conversation — never for the
    // pass's own internal title.
    await expect(rows.nth(0).locator('.home-chat-title'))
      .toHaveText('alpha conversation with a running memory pass')
    await expect(rows.nth(1).locator('.home-chat-title'))
      .toHaveText('alpha conversation waiting on the pass')
    await expect(page.locator('.home-tier--working, .home-tier--unread')).toHaveCount(0)
    await expect(page.getByText('Memory pass · conversation with a running memory pass')).toHaveCount(0)

    for (const row of await rows.all()) {
      const box = await row.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.height, 'an insight row is shorter than the touch minimum').toBeGreaterThanOrEqual(44)
    }

    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)

    const open = rows.nth(0).locator('.home-chat-item')
    await expect(open).toHaveAttribute('title', 'Open the memory insight for alpha conversation with a running memory pass')
    await open.click()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-5$/)
  })

  test('a selected memory note opens an actionable sheet on a phone', async ({ page }) => {
    await boot(page, '/memory/map', '.mm-toolbar')
    await page.getByRole('button', { name: 'List', exact: true }).click()
    await page.getByRole('button', { name: 'Open Launch decision' }).click()

    const detail = page.getByRole('dialog', { name: 'Details for Launch decision' })
    await expect(detail).toBeVisible()
    await expect(detail.getByRole('button', { name: 'Close note' })).toBeVisible()
    await detail.getByRole('button', { name: 'Close note' }).click()
    await expect(detail).toBeHidden()
  })

  test('the categories list and its drawer meet the 44px touch minimum', async ({ page }) => {
    // Three controls sit in a row here — the label button, its Built-in/Custom
    // chip and the switch — and the drawer adds the whole form on top, so this
    // is where a narrow pane would quietly drop one below the touch minimum.
    await boot(page, '/memory/categories', '.cat-table-wrap')

    const small = async (selector: string) => {
      const boxes = await page.locator(selector).evaluateAll((els) =>
        els.map((el) => {
          const r = el.getBoundingClientRect()
          return {
            name: (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
            w: Math.round(r.width),
            h: Math.round(r.height),
          }
        }),
      )
      return boxes
    }

    const rows = await small('.cat-row .cat-name-btn:visible, .cat-row .cat-switch:visible')
    expect(rows.length, 'no category row controls were measured').toBeGreaterThanOrEqual(4)
    // Both directions: a 44px-tall 40px-wide switch is under the minimum, so
    // height alone would call it a pass.
    expect(rows.filter((b) => b.w < 44 || b.h < 44), `row controls below 44px: ${JSON.stringify(rows.filter((b) => b.w < 44 || b.h < 44))}`).toEqual([])

    // And the list must not push the document sideways.
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `document scrolls ${overflow}px past the viewport; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)

    // The drawer is a bottom sheet at this width, so its own controls are the
    // only thing between a tap and an edit.
    await page.getByRole('button', { name: 'Add category' }).click()
    const drawer = page.getByRole('dialog', { name: 'Add category' })
    await expect(drawer).toBeVisible()
    const fields = await small('.cat-drawer button:visible, .cat-drawer input:visible, .cat-drawer select:visible, .cat-drawer textarea:visible')
    expect(fields.length, 'no drawer controls were measured').toBeGreaterThanOrEqual(6)
    expect(fields.filter((b) => b.h < 44), `drawer controls below 44px: ${JSON.stringify(fields.filter((b) => b.h < 44))}`).toEqual([])
  })
})
