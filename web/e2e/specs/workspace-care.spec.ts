import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { careReports } from '../../src/lib/careReportFixture'
import type { CareReport } from '../../src/lib/careReport'

/**
 * Workspace care on Home and its report page. What only a real layout can say:
 * the Needs you row sits in the list with its primary at the shared width (and
 * stretched under the text on a phone), the At a glance line is one quiet row,
 * the report page reads without sideways scroll at 390px, and its controls meet
 * the 44px floor. The care read is answered here, per state.
 */
async function answerCare(page: Page, report: CareReport) {
  await page.route(/\/api\/care-report\?/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      ...report,
      workspace: 'alpha',
      report_path: 'alpha/Workspace/Care-Report.md',
      unsure_path: 'alpha/Workspace/Unsure.md',
    }),
  }))
}

test.describe('Workspace care', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `care-${testInfo.workerIndex}-${testInfo.title}`)
    await page.setViewportSize({ width: 1280, height: 900 })
  })

  test('a run that needs you is one Needs you row and one quiet rail line', async ({ page }) => {
    await answerCare(page, careReports.needsYou)
    await boot(page)
    const section = page.locator('section.needs-you')
    await expect(section.getByText('Care needs you')).toBeVisible()
    const row = section.locator('.needs-you-row', { hasText: 'Care needs you' })
    const open = row.getByRole('button', { name: 'Open report' })
    expect((await open.boundingBox())!.height).toBeGreaterThanOrEqual(44)

    const rail = page.locator('.home-review-item--care')
    await expect(rail).toContainText('Workspace care')
    await expect(rail).toContainText('5 of 5 steps')

    // Keyboard: the primary is reachable and opens the report page.
    await open.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/memory\/care$/)
    await expect(page.getByRole('heading', { name: 'Done with 2 things for you' })).toBeVisible()
    await expect(page.locator('.page-tag')).toContainText('Workspace care')
    await page.screenshot({ path: 'test-results/workspace-care-report-desktop.png' })
  })

  test('a clean run raises no Needs you element at all', async ({ page }) => {
    await answerCare(page, careReports.complete)
    await boot(page)
    await expect(page.locator('.home-review-item--care')).toContainText('5 of 5 steps')
    await expect(page.locator('section.needs-you')).toHaveCount(0)
  })

  test('a run that did not finish reads as an error row and lists blocked steps', async ({ page }) => {
    await answerCare(page, careReports.blocked)
    await boot(page)
    const row = page.locator('.needs-you-row', { hasText: 'Care did not finish' })
    await expect(row).toHaveClass(/needs-you-row--error/)
    await row.getByRole('button', { name: 'More actions for Care did not finish' }).click()
    await expect(page.getByRole('menuitem', { name: 'Run care again' })).toBeFocused()
    await page.keyboard.press('Escape')
    await page.goto('/memory/care')
    await expect(page.getByRole('heading', { name: 'Did not finish: 4 of 6 steps' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Fix Hygiene with a chat' })).toBeVisible()
  })
})

test.describe('Workspace care under reduced motion', () => {
  test.use({ reducedMotion: 'reduce' })

  test('the running arc holds still', async ({ page }, testInfo) => {
    await isolate(page, `care-motion-${testInfo.workerIndex}`)
    await answerCare(page, careReports.running)
    await boot(page, '/memory/care', '.mc-step')
    const arc = page.locator('.mc-summary .care-glyph-arc')
    expect(await arc.evaluate(el => getComputedStyle(el).animationName)).toBe('none')
  })
})

test.describe('Workspace care on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `care-phone-${testInfo.workerIndex}-${testInfo.title}`)
  })

  test('the row stacks its primary under the text, full width', async ({ page }) => {
    await answerCare(page, careReports.needsYou)
    await boot(page)
    const row = page.locator('.needs-you-row', { hasText: 'Care needs you' })
    const text = await row.locator('.needs-you-text').boundingBox()
    const open = await row.getByRole('button', { name: 'Open report' }).boundingBox()
    expect(open!.y).toBeGreaterThan(text!.y + text!.height - 1)
    expect(open!.width).toBeGreaterThan(text!.width * 0.6)
    expect(open!.height).toBeGreaterThanOrEqual(44)
    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('the report page does not scroll sideways and keeps its controls 44px tall', async ({ page }) => {
    await answerCare(page, careReports.blocked)
    await boot(page, '/memory/care', '.mc-step')
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
    for (const control of await page.locator('.mc button, .mc a').all()) {
      if (!(await control.isVisible())) continue
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: 'test-results/workspace-care-report-phone.png', fullPage: true })
  })
})
