import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * Memory → Import (#1029) is a listing and a confirmation, and the controls a
 * person has to reach — Review, Cancel, and everything the confirmation states
 * — sit *below* the rows. As a card on the Map pane it made them unreachable: a
 * 60-row listing is taller than the pane, the pane did not scroll, and the
 * buttons ended up thousands of pixels below the fold with no way to get to
 * them. It is a section with a scrolling page now.
 *
 * Only a real layout engine can say the buttons are still on screen after a long
 * listing at a phone width: jsdom's `getBoundingClientRect()` is 0x0 for every
 * element and `scrollHeight` is always 0, so `web/src/components/__tests__/
 * ImportSources.test.ts` can only pin that the markup is complete. The listing
 * is mocked here rather than added to the fixture, because 60 synthetic
 * sessions in shared fixture state would be every other spec's problem.
 */
const PHONE = { width: 390, height: 844 }

const ROWS = 60

function ref(index: number) {
  return {
    provider: 'claude_code',
    source_id: `sess-${index.toString().padStart(2, '0')}`,
    project_hint: '-synthetic-alpha',
  }
}

/** One filed batch, in the status a test wants it. */
function filedBatch(over: Record<string, unknown> = {}) {
  return {
    batch_id: 'batch-e2e',
    workspace: 'alpha',
    destination: '/synthetic/alpha/memory-vault/alpha',
    status: 'running',
    extraction_revision: 1,
    sources: [
      { provider: 'claude_code', source_id: 'sess-01', content_digest: 'abc', status: 'extracted' },
      { provider: 'claude_code', source_id: 'sess-02', content_digest: '', status: 'pending' },
    ],
    progress: {
      total_sources: 2, completed_sources: 1, proposals_filed: 3,
      skipped: 0, current_source_id: 'sess-02',
    },
    provenance: [
      {
        provider: 'claude_code', source_id: 'sess-01', anchor: 'm-1',
        destination: 'alpha', accepted: false, note: 'filed as a review proposal',
      },
    ],
    created_at: '2026-10-04T09:00:00+00:00',
    updated_at: '2026-10-04T09:01:00+00:00',
    error: '',
    ...over,
  }
}

/**
 * The three import routes, answered with 60 rows, one confirmation and the run
 * list's own read.
 *
 * The run list is mocked too (#1041, C8) because the panel now reads
 * `/api/import/batches` on mount — Ciaobot's own state, no provider history —
 * and the fixture engine holds no batches for this synthetic workspace. Without
 * this the section would sit on its "no imports filed" empty state and the spec
 * could not say anything about the run controls.
 */
async function mockImportRoutes(page: Page) {
  await page.route('**/api/import/batches?*', async (route) => {
    const workspace = new URL(route.request().url()).searchParams.get('workspace') ?? 'alpha'
    await route.fulfill({
      json: { workspace, batches: [filedBatch()], retention_days: 30 },
    })
  })
  await page.route('**/api/import/sources*', async (route) => {
    const workspace = new URL(route.request().url()).searchParams.get('workspace') ?? 'alpha'
    await route.fulfill({
      json: {
        workspace,
        sources: {
          available: Array.from({ length: ROWS }, (_, index) => ref(index)),
          excluded: [],
          unsupported: [],
          truncated: { claude_code: false, opencode: false },
        },
      },
    })
  })
  await page.route('**/api/import/preview', async (route) => {
    const body = JSON.parse(route.request().postData() ?? '{}')
    await route.fulfill({
      json: {
        preview: {
          workspace: body.workspace,
          conversations: (body.sources ?? []).map((source: { source_id: string }) => ({
            source,
            state: 'ready',
            classification: 'external',
            message_count: 12,
            estimated_chars: 4800,
            first_date: '2026-02-01',
            omitted: {},
            already_imported: false,
            reason: '',
            message: '',
          })),
          provider: 'claude',
          model: 'sonnet',
          estimated_chars: 4800,
          estimated_messages: 12,
          batch_cap: 10,
          destination: '/synthetic/alpha/memory-vault/alpha',
        },
      },
    })
  })
}

test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

test.describe('memory import listing', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `import-${testInfo.workerIndex}`)
    await mockImportRoutes(page)
  })

  test('the controls stay reachable after a 60-row listing at 390px', async ({ page }) => {
    await boot(page, '/memory/import', '.import-sources')

    // A section of its own: nothing of the map's pane is on this page, so a long
    // listing cannot squeeze the graph to nothing.
    await expect(page.locator('.mm-body')).toHaveCount(0)
    await expect(page.locator('.mm-toolbar')).toHaveCount(0)

    await page.getByRole('button', { name: 'Find conversations' }).click()
    const checkboxes = page.locator('.import-check input[type="checkbox"]')
    await expect(checkboxes).toHaveCount(ROWS)

    // The list must not push the document sideways at phone width either.
    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)

    // Tick the last row — the one furthest from the fold — and the controls have
    // to still be on screen. `toBeInViewport` is the assertion: it fails if the
    // pane did not scroll them into reach, which is exactly the defect.
    await checkboxes.nth(ROWS - 1).check()
    const review = page.getByRole('button', { name: 'Review 1 selected' })
    await expect(review).toBeVisible()
    await expect(review).toBeInViewport()

    // And clickable: the confirmation is what the consent screen exists for, and
    // it sits below the list too.
    await review.click()
    await expect(page.getByText('Before anything runs')).toBeVisible()
    // The provider and model it names are the workspace default, said as such
    // (63b61705): each conversation is read by its own provider's model.
    await expect(page.getByText("This workspace's default is")).toContainText(
      'claude / sonnet',
    )

    // Cancel is on the same block of controls, so it is reachable as well.
    await expect(page.locator('.import-actions').getByRole('button', { name: 'Cancel' })).toBeInViewport()
  })

  test('the run controls stay reachable and measured on a phone', async ({ page }) => {
    await boot(page, '/memory/import', '.import-sources')

    // The run list read Ciaobot's own state on mount: a running batch is on
    // screen without the reader pressing anything, which is what makes the
    // journey usable when they come back to it.
    await expect(page.getByText('Running')).toBeVisible()
    await expect(page.getByText('1 of 2 conversations read')).toBeVisible()
    await expect(page.getByText('Reading session sess-02')).toBeVisible()

    // 44px is the floor jsdom cannot measure, so it is measured here.
    const heights = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.import-run-actions button, .import-chip'))
      return rows.map((el) => Math.round(el.getBoundingClientRect().height))
    })
    expect(heights.length).toBeGreaterThanOrEqual(2)
    expect(Math.min(...heights)).toBeGreaterThanOrEqual(44)

    // Cancel is below a 60-row listing too, and it says both halves of what it
    // does: the proposals stay, and provider input cannot be unsent. Scoped to
    // the run's own note, because the retention paragraph below it says the
    // proposals stay queued too.
    const cancel = page.locator('.import-run-actions').getByRole('button', { name: 'Cancel' })
    await cancel.scrollIntoViewIfNeeded()
    await expect(cancel).toBeInViewport()
    await expect(page.locator('.import-run-note')).toContainText('Proposals already filed stay queued')
    await expect(page.locator('.import-run-note')).toContainText('cannot be unsent')

    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
  })
})

test.describe('memory import at 200% zoom', () => {
  test.use({ viewport: PHONE })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `import-zoom-${testInfo.workerIndex}`)
    await mockImportRoutes(page)
  })

  test('the consent and run controls survive 200% zoom', async ({ page }) => {
    await boot(page, '/memory/import', '.import-sources')
    await page.evaluate(() => { document.body.style.zoom = '2' })

    await page.getByRole('button', { name: 'Find conversations' }).click()
    await expect(page.locator('.import-check input[type="checkbox"]')).toHaveCount(ROWS)

    // At 200% the listing is the scroll, not the fold: what matters is that the
    // controls are reachable by scrolling rather than clipped out of a pane.
    await page.locator('.import-check input[type="checkbox"]').first().check()
    const review = page.getByRole('button', { name: 'Review 1 selected' })
    await review.scrollIntoViewIfNeeded()
    await expect(review).toBeInViewport()

    await review.click()
    await expect(page.getByText('Before anything runs')).toBeVisible()
    const file = page.getByRole('button', { name: /File an import of 1 conversation/ })
    await file.scrollIntoViewIfNeeded()
    await expect(file).toBeInViewport()
  })
})