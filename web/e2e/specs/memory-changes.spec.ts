import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { changesFeed } from '../../src/lib/changesFeedFixture'
import { careReports } from '../../src/lib/careReportFixture'

/**
 * Memory → Changes, answered here from the synthetic feed the stories use: the
 * page beside its This week window, the filter, Undo opening a chat (never a
 * write), the sidebar row, the phone layout with no sideways scroll and 44px
 * targets, and the care card's line that links to the page.
 */
const viewer = (path: string) => path.replace('memory-vault/storybook-alpha/', 'alpha/')

async function answerChanges(page: Page): Promise<string[]> {
  const asked: string[] = []
  const feed = {
    ...changesFeed,
    workspace: 'alpha',
    undo_log_path: 'alpha/Workspace/Memory-Consolidations.md',
    groups: changesFeed.groups.map(g => ({ ...g, entries: g.entries.map(e => ({ ...e, open_path: viewer(e.open_path) })) })),
  }
  await page.route(/\/api\/memory\/changes(\?.*)?$/, async route => {
    asked.push(route.request().method())
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(feed) })
  })
  return asked
}

test.describe('Memory › Changes', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-changes-${testInfo.workerIndex}-${testInfo.title}`)
  })

  test('lists what changed beside the week, filters, and offers Undo where the log has the original', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    const asked = await answerChanges(page)
    await boot(page, '/memory/changes', '.mch-row')

    await expect(page.locator('.page-tag')).toContainText('Changes')
    await expect(page.locator('a[href="/memory/changes"]')).toHaveAttribute('aria-current', /page|true/)
    await expect(page.locator('.mch-group')).toHaveCount(2)
    await expect(page.locator('.mch-row')).toHaveCount(8)
    await expect(page.getByRole('button', { name: /^Undo in a chat: / })).toHaveCount(3)
    // The week's window sits beside the list.
    const list = await page.locator('.mch').boundingBox()
    const rail = await page.locator('.changes-rail').boundingBox()
    expect(rail!.x).toBeGreaterThan(list!.x + list!.width - 2)
    await expect(page.locator('.changes-rail-kv').first()).toContainText('Lines replaced or removed')
    await page.screenshot({ path: 'test-results/memory-changes-desktop.png', fullPage: true })

    await page.getByRole('button', { name: 'Skills' }).click()
    await expect(page.locator('.mch-row')).toHaveCount(1)
    await expect(page.getByRole('button', { name: 'Skills' })).toHaveAttribute('aria-pressed', 'true')
    // Reading is all the page does with the engine.
    expect(asked.every(method => method === 'GET')).toBe(true)
  })

  test('reads on a phone without sideways scroll and with 44px targets', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await answerChanges(page)
    await boot(page, '/memory/changes', '.mch-row')
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
    for (const control of await page.locator('.mch-filter, .mch-undo, .mch-link').all()) {
      const box = await control.boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: 'test-results/memory-changes-phone.png', fullPage: true })
  })

  test('the care card says what the run changed and links here', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerChanges(page)
    await page.route(/\/api\/care-report\?/, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...careReports.complete, workspace: 'alpha', report_path: 'alpha/Workspace/Care-Report.md', unsure_path: 'alpha/Workspace/Unsure.md' }),
    }))
    await boot(page, '/memory/care', '[data-care-changes]')
    const line = page.locator('[data-care-changes]')
    await expect(line).toContainText('Changed 2 lines · 1 skill suggestion')
    await page.screenshot({ path: 'test-results/memory-changes-care-line.png' })
    await line.getByRole('link', { name: /See changes/ }).click()
    await expect(page).toHaveURL(/\/memory\/changes$/)
    await expect(page.locator('.mch-row').first()).toBeVisible()
  })
})
