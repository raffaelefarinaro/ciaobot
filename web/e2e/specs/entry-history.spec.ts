import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The entry-scope decision in History, in a real browser.
 *
 * The copy is the assertion. "Removed this one line and every other fact in the
 * note is untouched" is a promise about what an Undo will and will not bring
 * back, and a promise only counts if the rendered sentence says it.
 */

const HISTORY = '/memory/history'

async function withVerification(page: import('@playwright/test').Page) {
  await page.evaluate(() => fetch('/__fixture__/verification', { method: 'POST' }))
  await page.reload()
}

test.describe.configure({ mode: 'serial' })

test('an entry-scope decision shows one fact, and says it is reversible', async ({ page }) => {
  await isolate(page, 'history-entry')
  await page.setViewportSize({ width: 1280, height: 1000 })
  await boot(page, HISTORY, '.ph-list')
  await withVerification(page)

  const row = page.locator('.ph-row').filter({ hasText: 'People/Nadia.md' }).first()
  // Past tense, and the unit named: a history row records a decision that has
  // already happened, and "would rewrite the whole note" about an accept that
  // changes one bullet is the exact claim this row exists to avoid.
  await expect(row.locator('.ph-verify-summary')).toContainText('removed one fact from the note')
  await row.locator('.ph-verify-summary').click()
  const body = row.locator('.ph-verify-body')
  // Coverage is about the fact, not the file, for the same reason.
  await expect(body).toContainText('Covering all of the fact')
  // One line, not the file: the whole-note pair is one disclosure away under
  // Changes, which is where the undo lives.
  const images = row.locator('.ph-verify-text--entry')
  await expect(images).toHaveCount(1)
  await expect(images.first()).toContainText('Reports to the CTO')
  // And the two sentences that make a removal safe to agree to: the other facts
  // are untouched, and the line comes back with Undo.
  await expect(body).toContainText('every other fact in the note is untouched')
  await expect(body).toContainText('Undo below restores the file exactly as it was')

  // The undo is real rather than described: a `note_apply` receipt carries it.
  await row.locator('.ph-change-toggle').click()
  await expect(row.locator('.ph-change')).toContainText('undo this change')
})
