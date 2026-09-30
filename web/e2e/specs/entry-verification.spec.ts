import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * The facts inside a note, in a real browser.
 *
 * Why this cannot be a vitest one. Four of the things this child changes are
 * exactly what jsdom cannot see:
 *
 * 1. **Focus.** The entry block's proposal link is the only way to act on a
 *    fact, and it has to be reachable, visible and take focus. `mount()` in
 *    jsdom reports every rect as 0x0 and never runs a layout or a paint.
 * 2. **Layout at 390px.** The block adds a quoted bullet, a context line and
 *    two links to a row whose path is an unbreakable token — the combination
 *    that widens a flex parent, and the only thing that detects it is a real
 *    `scrollWidth`.
 * 3. **Touch targets.** `--touch: 44px` is a media query over
 *    `(pointer: coarse)`, which jsdom does not evaluate, so a control that
 *    drops below the minimum on a real phone looks identical to one that does
 *    not in a unit test.
 * 4. **Zoom.** Page zoom is indistinguishable from halving the CSS viewport, so
 *    the only honest way to check a zoomed layout is to re-measure after a
 *    resize.
 *
 * The copy is asserted here too, because a browser is the only place the
 * rendered sentence can be read: "that is the note's date" is a promise about
 * what the panel is *not* claiming, and the map is where a reader decides
 * whether to trust a node that has no amber ring on it.
 */

const REVIEW = '/memory/review?show=revisit'
const MAP = '/memory/map'

/** Opt this session into the managed-verification payloads, then reload. */
async function withVerification(page: import('@playwright/test').Page) {
  await page.evaluate(() => fetch('/__fixture__/verification', { method: 'POST' }))
  await page.reload()
}

/** The row whose only finding is an overdue fact. */
function nadiaRow(page: import('@playwright/test').Page) {
  return page.locator('.vr-row').filter({ has: page.locator('.vr-title', { hasText: /Nadia/ }) })
}

test.describe('a note holding one overdue fact', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `entries-${testInfo.workerIndex}`)
  })

  test('the row names the exact fact, its context, and its section', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const row = nadiaRow(page)
    const block = row.locator('.vr-entries')
    await expect(block).toBeVisible()
    // The counts, and the prose that is not an entry at all — the two facts a
    // badge would otherwise hide. `.first()` because the block carries a second
    // heading for decisions waiting on a fact that is not overdue.
    const head = block.locator('.vr-entries-head').first()
    await expect(head).toContainText('Facts inside this note')
    await expect(head).toContainText('3 facts in the note')
    await expect(head).toContainText('1 past due')
    await expect(head).toContainText('1 not verified')
    await expect(head).toContainText('1 block of prose not read as facts')
    await expect(head).toContainText('never counted as verified')

    // The bullet itself, the section it is under, and the line beside it.
    await expect(row.locator('.vr-entry-text').first()).toContainText('Reports to the CTO')
    await expect(row.locator('.vr-entry-section').first()).toContainText('Nadia')
    await expect(row.locator('.vr-entry-context').first()).toContainText('Based in Lisbon')

    // Last CHECKED and last VERIFIED are different claims, and this entry's
    // date is its own stamp — the panel says which one it is showing.
    await expect(row.locator('.vr-entry-why').first()).toContainText('Last checked too long ago')
    await expect(row.locator('.vr-entry-dates').first()).toContainText('2019-05-01')
    await expect(row.locator('.vr-entry-dates').first()).toContainText("on the entry's own stamp")

    // The second kind: a stamp the note's freshness cannot vouch for. Its date
    // is the file's — inherited, not its own — and the panel says so rather
    // than presenting a date nobody could have checked anything on.
    await expect(row.locator('.vr-entry-why').nth(1)).toContainText('not a usable date')
    await expect(row.locator('.vr-entry-dates').nth(1)).toContainText("that is the")
    await expect(row.locator('.vr-entry-dates').nth(1)).toContainText('this fact carries no stamp')
  })

  test('the decision is linked, not duplicated, and a dead one says so', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const row = nadiaRow(page)
    const live = row.locator('.vr-entry-decision').first()
    // Named by operation: the three genuinely do different things to the file.
    await expect(live).toContainText('Waiting in Suggested')
    await expect(live).toContainText('Rewrites this one fact')
    await expect(live).toContainText('2 citations')
    // The row's own decision is the whole file, so there is no second accept of
    // the same question here.
    await expect(row.locator('.vr-pending')).toHaveCount(0)

    // A proposal pinned to text the note no longer holds cannot be applied, and
    // saying so beats offering a button that can only fail. It is listed under
    // its own heading because its fact is not on the overdue list: a decision
    // waiting on a person is still a decision waiting on a person.
    const waiting = row.locator('.vr-entries-head--waiting')
    await expect(waiting).toContainText('Decisions waiting on you')
    await expect(waiting).toContainText('1 fact in this note that is not on the overdue list')

    const dead = row.locator('.vr-entry-decision').nth(1)
    await expect(dead.locator('.vr-entry-conflict')).toContainText('cannot be applied')
    await expect(dead).not.toContainText('Waiting in Suggested')
  })

  test('a note whose only finding is an overdue fact offers no Retire', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    // "Go and look again" is not a claim that a note is disposable, and this
    // queue's terminal action is deletion.
    const labels = await nadiaRow(page).locator('.vr-actions button').allTextContents()
    expect(labels).not.toContain('Retire')
    expect(labels).not.toContain('Complete')
  })

  test('the entry link takes focus and lands on the proposal in the queue', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const link = nadiaRow(page).locator('.vr-entry-decision .vr-pending-link').first()
    await link.focus()
    await expect(link).toBeVisible()
    // A visible focus ring is the only thing that tells a keyboard user where
    // they are; `:focus-visible` is the rule that draws it.
    const outline = await link.evaluate((el) => getComputedStyle(el).outlineStyle)
    expect(outline, 'the link draws no focus ring').not.toBe('none')

    await link.press('Enter')
    await expect(page).toHaveURL(/\/memory\/review\?show=suggested/)
    // The arrival is durable, not the highlight: the store clears the reveal
    // once the row has scrolled to, so focus is the honest signal.
    await expect(page.locator('.pr-row').filter({ hasText: 'the reorg moved her under the COO' }))
      .toBeFocused()
  })

  test('the map counts facts separately from notes, and shows them on the node', async ({ page }) => {
    await boot(page, MAP, '.mm-toolbar')
    await withVerification(page)
    await page.getByRole('button', { name: 'List', exact: true }).click()

    // Two figures, because the two lists are not subsets of each other: this
    // node's own date is current, so it is absent from the unchecked count and
    // present in the facts one.
    const stale = page.locator('.mm-toolbar-stale')
    const facts = page.locator('.mm-toolbar-entries')
    await expect(facts).toContainText('with facts unchecked')
    const staleText = await stale.textContent()
    const factsText = await facts.textContent()
    expect(staleText).not.toBe(factsText)

    await page.getByRole('button', { name: 'Open Nadia' }).click()
    const tile = page.getByRole('dialog', { name: 'Details for Nadia' })
    const block = tile.locator('.mm-tile-entries')
    await expect(block).toContainText('Facts inside this note')
    await expect(block).toContainText('1 past due')
    await expect(block).toContainText('Reports to the CTO')
    // Both kinds the counts name, so the tile's numbers and its list agree: a
    // "never checked" figure beside no such entry describes a fact the reader
    // cannot see.
    await expect(block).toContainText('Checked 2019-05-01 — unverified for 2698d against a 90d horizon')
    await expect(block).toContainText('not a usable date')
    // The whole-note flag is untouched: the map reports beside it, because the
    // nightly worklist is where an overdue bullet becomes a plan.
    await expect(tile.locator('.mm-tile-stale')).toHaveCount(0)
  })

  test('the map shows nothing at all for a node whose body could not be read', async ({ page }) => {
    await boot(page, MAP, '.mm-toolbar')
    await withVerification(page)
    await page.getByRole('button', { name: 'List', exact: true }).click()

    // A default object of zeroes would read as "read nothing, found nothing
    // wrong" — a claim about the note rather than about this client.
    await page.getByRole('button', { name: 'Open Launch decision' }).click()
    const tile = page.getByRole('dialog', { name: 'Details for Launch decision' })
    await expect(tile.locator('.mm-tile-entries')).toHaveCount(0)
  })
})

test.describe('the entry block at 390px and at 200% zoom', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `entries-phone-${testInfo.workerIndex}`)
  })

  test('the block fits a phone and its link meets the touch minimum', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const row = nadiaRow(page)
    await expect(row.locator('.vr-entries')).toBeVisible()

    // A quoted bullet is a long unbreakable token next to a path that is another
    // one, in a row that already carries a title.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow, `the document scrolls ${overflow}px past the viewport`).toBeLessThanOrEqual(0)

    const boxes = await row
      .locator('.vr-actions button:visible, .vr-entry-decision .vr-pending-link:visible')
      .evaluateAll((els) =>
        els.map((el) => {
          const r = el.getBoundingClientRect()
          return {
            name: (el.getAttribute('title') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50),
            w: Math.round(r.width),
            h: Math.round(r.height),
          }
        }),
      )
    expect(boxes.length, 'no row controls were measured').toBeGreaterThanOrEqual(1)
    const small = boxes.filter((b) => b.w < 44 || b.h < 44)
    expect(small, `row controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })
})

test.describe('the entry block at 200% zoom', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `entries-zoom-${testInfo.workerIndex}`)
  })

  test('the row reflows rather than scrolling sideways', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)
    await expect(nadiaRow(page).locator('.vr-entries')).toBeVisible()

    // Chromium's page zoom is not scriptable, but it is indistinguishable from
    // halving the CSS viewport. Re-measuring after the resize is what makes this
    // a zoom test rather than a second narrow-viewport test.
    await page.setViewportSize({ width: 640, height: 450 })
    await expect(nadiaRow(page).locator('.vr-entries')).toBeVisible()

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `zoomed layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })
})
