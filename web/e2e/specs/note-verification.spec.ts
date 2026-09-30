import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The managed note-verification pipeline, in a real browser.
 *
 * Why this cannot be a vitest one. Three of the things this child changes are
 * exactly what jsdom cannot see:
 *
 * 1. **Focus.** A review row that defers to a proposal replaces three buttons
 *    with one link. The link has to be reachable, visible and take focus —
 *    `mount()` in jsdom reports every rect as 0x0 and never runs a layout or a
 *    paint, so "is this control the right size and does focus land on it" has
 *    no answer there.
 * 2. **Layout at 390px.** The pending card adds a box, a link and two
 *    sentences to a row, and the row's actions shrink to a single control. A
 *    flex child with an unbreakable path can widen its parent past the viewport
 *    — the trap `narrow-viewport.spec.ts` exists for, and the only thing that
 *    detects it is a real `scrollWidth`.
 * 3. **Touch targets.** `--touch: 44px` is a media query over `(pointer:
 *    coarse)`, which jsdom does not evaluate, so a control that drops below the
 *    minimum in a real phone looks identical to one that does not in a unit
 *    test.
 *
 * The copy is asserted here too, because a browser is the only place the
 * rendered sentence can be read: "dismissal retains, not verifies" is a
 * promise about what a button does, and the history card is where a reader
 * decides whether to trust it.
 */

const REVIEW = '/memory/review?show=revisit'
const HISTORY = '/memory/history'

/** Opt this session into the managed-verification payloads, then reload. */
async function withVerification(page: import('@playwright/test').Page) {
  await page.evaluate(() => fetch('/__fixture__/verification', { method: 'POST' }))
  await page.reload()
}

test.describe('a pending verification proposal', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `verify-${testInfo.workerIndex}`)
  })

  test('the review row links the proposal and drops its own second answer', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)
    await expect(page.locator('.vr-pending').first()).toBeVisible()

    const first = page.locator('.vr-row').first()
    // The decision is stated as the proposal's, not this queue's.
    await expect(first.locator('.vr-pending-head')).toContainText('Verification proposal pending')
    await expect(first.locator('.vr-pending-head')).toContainText('2026-09-28')
    await expect(first.locator('.vr-pending-head')).toContainText('the note should be retired')
    // Coverage is named, because a re-stamp claims the WHOLE note and a
    // partial check is not one.
    await expect(first.locator('.vr-pending-head')).toContainText('covering the whole note')
    await expect(first.locator('.vr-pending-head')).toContainText('2 citations')
    await expect(first.locator('.vr-pending-reason')).toContainText('the team page was replaced by the org chart')
    // Dismissal is a decision ON the proposal, and the row must not read as if
    // it verified anything.
    await expect(first.locator('.vr-pending-hint')).toContainText('The decision is on the proposal, not here')

    // `unlinked` is an independent finding, so this row keeps Retire — and
    // loses `Still true`, which would stamp the note "verified today" over a
    // revision the pass has already said is wrong.
    const actions = first.locator('.vr-actions button')
    await expect(actions).toHaveText(['Retire', 'Discuss'])
  })

  test('a row held for nothing else offers no second answer at all', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    // By title, not by text: the Borealis row's excerpt names Atlas too, so a
    // text filter would match two rows and assert over the wrong one.
    const sole = page.locator('.vr-row').filter({ has: page.locator('.vr-title', { hasText: /^Atlas$/ }) })
    await expect(sole.locator('.vr-pending')).toBeVisible()
    // Partial coverage says so here, where the same sentence said "the whole
    // note" on the row above.
    await expect(sole.locator('.vr-pending-head')).toContainText('covering part of the note')
    await expect(sole.locator('.vr-actions button')).toHaveText(['Discuss'])
  })

  test('a dead proposal hands the decision back and says why', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const conflicted = page.locator('.vr-row').filter({ has: page.locator('.vr-title', { hasText: /^Borealis$/ }) })
    await expect(conflicted.locator('.vr-conflict')).toContainText('filed for an earlier version of this note')
    await expect(conflicted.locator('.vr-pending')).toHaveCount(0)
    // Its own actions are back, because nothing else can answer for it.
    await expect(conflicted.locator('.vr-actions button')).toHaveText(['Still true', 'Retire', 'Discuss'])
  })

  test('a settled verdict is reported in the row, dated separately from the note', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const settled = page.locator('.vr-row').filter({ has: page.locator('.vr-title', { hasText: /^Cassiopeia$/ }) })
    const flag = settled.locator('.vr-flag', { hasText: 'unchecked' }).first()
    await flag.click()

    const box = settled.locator('.vr-evidence--unverified')
    // The note's own `updated:` and the date the check ran are two claims, and
    // a verdict that wrote nothing leaves them a year apart.
    await expect(box).toContainText('2024-01-05')
    await expect(box).toContainText('2026-09-28')
    await expect(box).toContainText('could not be confirmed')
    await expect(box).toContainText('the tracker connector was down')
  })

  test('following the link lands on the proposal, in focus', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)

    const first = page.locator('.vr-row').first()
    const link = first.locator('.vr-pending-link')
    await link.focus()
    // A visible focus ring is the only thing that tells a keyboard user where
    // they are; `:focus-visible` is the rule that draws it.
    await expect(link).toBeVisible()
    const outline = await link.evaluate((el) => getComputedStyle(el).outlineStyle)
    expect(outline, 'the link draws no focus ring').not.toBe('none')

    await link.press('Enter')

    // Landed on the queue, filtered to it, and the row itself has focus — a
    // link that arrives at a list the row is not in is the failure mode.
    await expect(page).toHaveURL(/\/memory\/review\?show=suggested/)
    // Focus is the durable half of the arrival — the highlight is a one-shot
    // for the arrival itself, and the store clears it once the row is scrolled
    // to, so asserting on it would be asserting on a race.
    const row = page.locator('.pr-row').filter({ hasText: 'the team page was replaced by the org chart' })
    await expect(row).toBeFocused()
    // And the queue's own filters are cleared, because either one of them can
    // hide the row the link named.
    await expect(page.locator('.proposal-review .pr-chips .mr-chip[aria-pressed="true"]'))
      .toHaveText(/^All/)
  })

  test('the map tile leads with the state that explains the missing flag', async ({ page }) => {
    // The map is the surface that sends the operator to the queue, so a note the
    // pass has already looked at has to say WHICH state it is in there — not
    // only that it is no longer flagged.
    await boot(page, '/memory/map', '.mm-toolbar')
    await withVerification(page)
    await page.getByRole('button', { name: 'List', exact: true }).click()

    await page.getByRole('button', { name: 'Open Team working agreements' }).click()
    const tile = page.getByRole('dialog', { name: 'Details for Team working agreements' })
    // Being asked about is not being ignored, and the stale callout must not sit
    // beside a link to a proposal already filed about it.
    await expect(tile.locator('.mm-tile-pending')).toContainText('Being checked')
    await expect(tile.locator('.mm-tile-pending a')).toHaveText('Open the proposal')
    await expect(tile.locator('.mm-tile-stale')).toHaveCount(0)
    await tile.getByRole('button', { name: 'Close note' }).click()

    await page.getByRole('button', { name: 'Open Launch decision' }).click()
    const checked = page.getByRole('dialog', { name: 'Details for Launch decision' })
    // Asked and answered: the verdict, its coverage, and when it is due again.
    await expect(checked.locator('.mm-tile-checked')).toContainText('Checked 2026-09-28')
    await expect(checked.locator('.mm-tile-checked')).toContainText('could not be confirmed')
    await expect(checked.locator('.mm-tile-checked')).toContainText('covering part of the note')
    await expect(checked.locator('.mm-tile-checked')).toContainText('2026-10-28')
    // And no stale count for it: the flag is off, and nothing is claiming the
    // note was forgotten.
    await expect(page.locator('.mm-toolbar-stale')).toHaveText(/^1\b.*unchecked$/)
  })

  test('a settled verification is judgeable months later, with its undo', async ({ page }) => {
    await boot(page, HISTORY, '.ph-list')
    await withVerification(page)

    const accepted = page.locator('.ph-row').filter({ hasText: 'Projects/Atlas.md — replace' })
    await expect(accepted.locator('.ph-verify-summary')).toContainText('would rewrite the whole note')
    await expect(accepted.locator('.ph-change-toggle')).toHaveText('Changes')

    await accepted.locator('.ph-verify-summary').click()
    const body = accepted.locator('.ph-verify-body')
    await expect(body).toContainText('Covering all of the note')
    await expect(body).toContainText('on 1 citation')
    await expect(body).toContainText('https://tracker.example.test/atlas')
    // The exact before and after, not a summary of them.
    const images = accepted.locator('.ph-verify-text')
    await expect(images).toHaveCount(2)
    await expect(images.nth(0)).toContainText('last Friday')
    await expect(images.nth(1)).toContainText('last Thursday')

    // And the undo, which a `note_apply` receipt carries.
    await accepted.locator('.ph-change-toggle').click()
    await expect(accepted.locator('.ph-change')).toContainText('undo this change')
  })

  test('a retirement says where the note went, rather than "no snapshot"', async ({ page }) => {
    await boot(page, HISTORY, '.ph-list')
    await withVerification(page)

    const retired = page.locator('.ph-row').filter({ hasText: 'Projects/Borealis.md — retire' })
    // "No change snapshot available" beside a note that really was moved is a
    // false claim; the trash is where it went and Restore is the way back.
    await expect(retired.locator('.ph-restore')).toContainText('in Retired')
    await retired.locator('.ph-verify-summary').click()
    await expect(retired.locator('.ph-verify-summary')).toContainText('would move the note to Retired')
    // A retirement writes no text, so there is no after-image to diff.
    await expect(retired.locator('.ph-verify-text')).toHaveCount(0)
  })
})

test.describe('at 390px', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `verify-phone-${testInfo.workerIndex}`)
  })

  test('the pending card and the history disclosure fit a phone', async ({ page }) => {
    await boot(page, REVIEW, '.vault-review')
    await withVerification(page)
    await expect(page.locator('.vr-pending').first()).toBeVisible()

    // The pending card adds a box, a link and two sentences to a row whose
    // path is an unbreakable token — the combination that widens a flex parent.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow, `the document scrolls ${overflow}px past the viewport`).toBeLessThanOrEqual(0)

    const boxes = await page
      .locator('.vr-row:first-child .vr-actions button:visible, .vr-row:first-child .vr-pending-link:visible')
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
    expect(boxes.length, 'no row controls were measured').toBeGreaterThanOrEqual(2)
    const small = boxes.filter((b) => b.w < 44 || b.h < 44)
    expect(small, `row controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })

  test('the history disclosure is operable at the touch minimum', async ({ page }) => {
    await boot(page, HISTORY, '.ph-list')
    await withVerification(page)

    const summary = page.locator('.ph-verify-summary').first()
    await expect(summary).toBeVisible()
    const box = await summary.boundingBox()
    expect(box!.height, 'the disclosure is below the touch minimum').toBeGreaterThanOrEqual(44)

    await summary.click()
    await expect(page.locator('.ph-verify-body').first()).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow, `the document scrolls ${overflow}px past the viewport`).toBeLessThanOrEqual(0)
  })
})
