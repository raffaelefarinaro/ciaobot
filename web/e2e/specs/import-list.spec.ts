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

/** The two import routes, answered with 60 rows and one confirmation. */
async function mockImportRoutes(page: Page) {
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
    await expect(page.getByText('claude / sonnet would receive this text')).toBeVisible()

    // Cancel is on the same block of controls, so it is reachable as well.
    await expect(page.locator('.import-actions').getByRole('button', { name: 'Cancel' })).toBeInViewport()
  })
})