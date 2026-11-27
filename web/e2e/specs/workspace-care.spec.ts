import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { careReports } from '../../src/lib/careReportFixture'
import type { CareReport } from '../../src/lib/careReport'

/**
 * Workspace care on Home and its report page. What only a real layout can say:
 * the care signal shows once. With At a glance docked beside the request column
 * it is the rail's actionable line and there is no Needs you row; with the rail
 * dropped below the column (a narrow pane, or a phone) it is the Needs you row,
 * with its primary at the shared width (stretched under the text on a phone),
 * and the rail draws no care line. The report page reads without sideways
 * scroll at 390px and its controls meet the 44px floor. The care read is
 * answered here, per state.
 */
/** Pane widths the rail's drop depends on: the viewport minus the sidebar. */
const DOCKED = { width: 1440, height: 900 }
const DROPPED = { width: 1000, height: 900 }
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
    await page.setViewportSize(DROPPED)
  })

  test('a run that needs you is one Needs you row when the rail is below the column', async ({ page }) => {
    await answerCare(page, careReports.needsYou)
    await boot(page)
    await expect(page.locator('.home-review-item--care')).toHaveCount(0)
    const section = page.locator('section.needs-you')
    await expect(section.getByText('Care needs you')).toBeVisible()
    const row = section.locator('.needs-you-row', { hasText: 'Care needs you' })
    const open = row.getByRole('button', { name: 'Open report' })
    expect((await open.boundingBox())!.height).toBeGreaterThanOrEqual(44)


    // Keyboard: the primary is reachable and opens the report page.
    await open.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/memory\/care$/)
    await expect(page.getByRole('heading', { name: 'Done with 4 things for you' })).toBeVisible()
    await expect(page.locator('.page-tag')).toContainText('Workspace care')
    await page.screenshot({ path: 'test-results/workspace-care-report-desktop.png' })
  })

  test('a clean run raises no Needs you element at all', async ({ page }) => {
    await answerCare(page, careReports.complete)
    await boot(page)
    await expect(page.locator('section.needs-you')).toHaveCount(0)
    await expect(page.locator('.home-review-item--care')).toHaveCount(0)
  })

  test('with the rail docked, care is one actionable rail line and no Needs you row', async ({ page }) => {
    await page.setViewportSize(DOCKED)
    await answerCare(page, careReports.needsYou)
    await boot(page)
    const line = page.locator('.home-review-item--care')
    await expect(line).toContainText('Care needs you')
    await expect(line).toContainText('4 things need you')
    await expect(line).toHaveClass(/home-review-item--care-attention/)
    await expect(line).toHaveAttribute('aria-label', 'Care needs you. 4 things need you. Open the care report.')
    await expect(line.locator('.care-glyph--blocked')).toBeVisible()
    // Once: the words appear on the page a single time, and there is no row.
    await expect(page.locator('.needs-you-row', { hasText: 'Care needs you' })).toHaveCount(0)
    await expect(page.getByText('Care needs you')).toHaveCount(1)
    await page.screenshot({ path: 'test-results/workspace-care-rail-docked.png' })

    // Keyboard: the row is reachable and opens the report page.
    await line.focus()
    await expect(line).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/memory\/care$/)
    await expect(page.getByRole('heading', { name: 'Done with 4 things for you' })).toBeVisible()
  })

  test('a run that did not finish is the rail line alone while docked', async ({ page }) => {
    await page.setViewportSize(DOCKED)
    await answerCare(page, careReports.blocked)
    await boot(page)
    const line = page.locator('.home-review-item--care')
    await expect(line).toContainText('Care did not finish')
    await expect(line).toContainText('4 of 6 steps · 2 blocked')
    await expect(line).toHaveClass(/home-review-item--care-error/)
    await expect(line.locator('.care-glyph--blocked')).toBeVisible()
    await expect(page.getByText('Care did not finish')).toHaveCount(1)
    await expect(page.locator('section.needs-you')).toHaveCount(0)
    await page.screenshot({ path: 'test-results/workspace-care-rail-error.png' })
  })

  test('the switch is at the pane edge: 940px is dropped, 941px and 960px are docked', async ({ page }) => {
    await answerCare(page, careReports.blocked)
    await boot(page)
    const pane = page.locator('.chat-main')
    // The sidebar has a fixed width, so the pane is the window less a constant.
    const paneAt = async (window: number) => {
      await page.setViewportSize({ width: window, height: 900 })
      return pane.evaluate(el => el.clientWidth)
    }
    const offset = 1200 - (await paneAt(1200))
    const line = page.locator('.home-review-item--care')
    const row = page.locator('.needs-you-row', { hasText: 'Care did not finish' })
    for (const [width, docked] of [[940, false], [941, true], [960, true]] as const) {
      expect(await paneAt(width + offset)).toBe(width)
      await expect(line).toHaveCount(docked ? 1 : 0)
      await expect(row).toHaveCount(docked ? 0 : 1)
    }
  })

  test('resizing across the rail drop swaps the one care signal live', async ({ page }) => {
    await page.setViewportSize(DOCKED)
    await answerCare(page, careReports.blocked)
    await boot(page)
    const line = page.locator('.home-review-item--care')
    const row = page.locator('.needs-you-row', { hasText: 'Care did not finish' })
    await expect(line).toHaveCount(1)
    await expect(row).toHaveCount(0)
    await page.setViewportSize(DROPPED)
    await expect(row).toHaveCount(1)
    await expect(line).toHaveCount(0)
    await page.setViewportSize(DOCKED)
    await expect(line).toHaveCount(1)
    await expect(row).toHaveCount(0)
  })

  test('Hide for now hides the row only and the docked rail line stays', async ({ page }) => {
    await answerCare(page, careReports.blocked)
    await boot(page)
    const row = page.locator('.needs-you-row', { hasText: 'Care did not finish' })
    await row.getByRole('button', { name: 'More actions for Care did not finish' }).click()
    await page.getByRole('menuitem', { name: 'Hide for now' }).click()
    await expect(row).toHaveCount(0)
    await page.setViewportSize(DOCKED)
    await expect(page.locator('.home-review-item--care')).toContainText('2 blocked')
    await page.setViewportSize(DROPPED)
    await expect(row).toHaveCount(0)
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
    // Nothing needs the person, so the steps are open to show what stopped.
    await expect(page.getByText('Nothing needs your decision. Some steps did not finish; they are below.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Fix Hygiene with a chat' })).toBeVisible()
  })

  test('what needs you leads and the steps and notes fold below it', async ({ page }) => {
    await answerCare(page, careReports.needsYou)
    await boot(page, '/memory/care', '.mc-need')
    const needs = page.locator('.mc-need')
    await expect(needs).toHaveCount(4)
    // The buttons come from each item's path.
    await expect(needs.nth(0).getByRole('button', { name: /^Review proposal: / })).toBeVisible()
    await expect(needs.nth(1).getByRole('button', { name: /^Optimize: / })).toBeVisible()
    await expect(needs.nth(2).getByRole('button', { name: /^Open Unsure: / })).toBeVisible()
    await expect(needs.nth(3).getByRole('button', { name: /^Open report: / })).toBeVisible()
    await expect(page.getByRole('button', { name: /^Fix with a chat: / })).toHaveCount(3)
    const steps = page.locator('details.mc-disclosure').first()
    await expect(steps).not.toHaveAttribute('open', '')
    await expect(page.locator('.mc-step').first()).toBeHidden()
    const summary = steps.locator('summary')
    await expect(summary).toHaveText(/Steps\s*5: 5 done/)
    expect((await summary.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    // Below what needs you, in the page's reading order.
    expect((await summary.boundingBox())!.y).toBeGreaterThan((await needs.nth(3).boundingBox())!.y)
    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('.mc-step').first()).toBeVisible()
    // The choice holds for the session. The toggle event writes it a moment
    // after `open` flips, so wait for the write before reloading.
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('ciao.care.stepsOpen'))).toBe('open')
    await page.reload()
    await expect(page.locator('.mc-step').first()).toBeVisible()
    const notes = page.locator('details.mc-disclosure').nth(1)
    await expect(notes.locator('summary')).toHaveText(/Notes\s*4/)
    await expect(notes.locator('.mc-notes li').first()).toBeHidden()
  })
})

test.describe('Memory page chrome', () => {
  test('care has its Daily care window beside the card, and no explainer bar or back link; the site link is in the sidebar', async ({ page }, testInfo) => {
    await isolate(page, `care-chrome-${testInfo.workerIndex}`)
    await page.setViewportSize(DOCKED)
    await answerCare(page, careReports.complete)
    await boot(page, '/memory/care', '.mc-summary-title')
    await expect(page.getByRole('button', { name: 'How memory works' })).toHaveCount(0)
    await expect(page.locator('.mc-back')).toHaveCount(0)
    const rail = page.locator('section.care-rail')
    await expect(rail.getByRole('heading', { name: 'Daily care' })).toBeVisible()
    for (const label of ['Last run', 'Next run', 'Weekly checks']) await expect(rail.getByText(label, { exact: true })).toBeVisible()
    const card = (await page.locator('.mc-card').boundingBox())!
    const window = (await rail.boundingBox())!
    expect(window.x).toBeGreaterThan(card.x + card.width)
    for (const name of ['Run care again', 'Open Care-Report.md']) {
      expect((await rail.getByRole('button', { name }).boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }
    const link = page.getByRole('link', { name: /^How memory works/ })
    await expect(link).toHaveAttribute('href', 'https://www.raffaelefarinaro.com/ciaobot/memory.html')
    await expect(link).toHaveAttribute('target', '_blank')
    await expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    await page.screenshot({ path: 'test-results/care-chrome/care-desktop.png' })
    await page.goto('/memory/unsure')
    await expect(page.getByRole('heading', { name: 'How this works' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'How memory works' })).toHaveCount(0)
    await page.screenshot({ path: 'test-results/care-chrome/unsure-desktop.png' })
  })

  test('on a phone the window drops below the card', async ({ page }, testInfo) => {
    await isolate(page, `care-chrome-phone-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 390, height: 844 })
    await answerCare(page, careReports.complete)
    await boot(page, '/memory/care', '.mc-summary-title')
    const card = (await page.locator('.mc-card').boundingBox())!
    const window = (await page.locator('section.care-rail').boundingBox())!
    expect(window.y).toBeGreaterThanOrEqual(card.y + card.height)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
  })
})

test.describe('Workspace care page states, looked at', () => {
  const states = {
    'needs-you': careReports.needsYou,
    'nothing-needs-you': careReports.complete,
    running: careReports.running,
    'did-not-finish': careReports.blocked,
  } as const
  for (const [name, report] of Object.entries(states)) {
    for (const colorScheme of ['light', 'dark'] as const) {
      for (const width of [1280, 390]) {
        test(`${name}, ${colorScheme}, ${width}px`, async ({ page }, testInfo) => {
          await isolate(page, `care-look-${testInfo.workerIndex}-${name}-${colorScheme}-${width}`)
          await page.setViewportSize({ width, height: 900 })
          await page.emulateMedia({ colorScheme })
          await answerCare(page, report)
          await boot(page, '/memory/care', '.mc-summary-title')
          const { overflow, culprits } = await horizontalOverflow(page)
          expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
          await page.screenshot({ path: `test-results/care-look/${name}-${colorScheme}-${width}.png`, fullPage: true })
        })
      }
    }
  }
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

  test('the rail is below the column, so the phone has the row and no rail line', async ({ page }) => {
    await answerCare(page, careReports.blocked)
    await boot(page)
    await expect(page.locator('.needs-you-row', { hasText: 'Care did not finish' })).toHaveCount(1)
    await expect(page.locator('.home-review-item--care')).toHaveCount(0)
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
